"""Thread-based admission controllers for replay lifecycle overcommit (P2.6).

Two independent controllers govern running-sandbox concurrency and control-plane
dispatch rate. The replay runner constructs them only in ``lifecycle`` mode;
exec-only has no lifecycle calls and needs no admission.

``RunningSlotScheduler`` — FIFO admission for complete resume→(probe)→exec→pause
slices. A slot is reserved before resume and held until pause is confirmed, so
client-side admission never exceeds ``maximum`` even while lifecycle calls are in
flight.

``QpsRateLimiter`` — smooth FIFO dispatch with a bounded in-flight fuse for
non-create lifecycle calls (pause/resume/cleanup). One global queue; operation
type is metrics-only (no priority differentiation). The ``slot()`` enter order is
load-bearing:

1. QPS time-wait FIRST (sleep for the smooth dispatch delay)
2. inflight semaphore acquire AFTER the wait
3. ``__exit__`` (finally): release the inflight semaphore even if the body raised

**Forbidden**: acquire inflight THEN sleep — threads would hold inflight slots
while parked, exhausting the fuse and deadlocking on slow-backend accumulation.

Both classes use plain ``threading`` (``Condition``/``Semaphore``/``Lock``,
``time.perf_counter``/``time.monotonic``). No asyncio — the kernel is thread-based
and the sibling toolkit's async controllers cannot be imported here.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunningLease:
    """A granted running-slot lease. Lightweight (``__slots__`` via dataclass).

    Attributes:
        lease_id: Monotonically increasing lease identifier.
        task_id: The task that requested the slot.
        acquired_at: Monotonic timestamp when the slot was granted.
        queue_wait_sec: Time spent waiting in the queue before grant.
        natural_delay_sec: The ready_at pre-delay sleep (0 when no ready_at supplied).
        _released: Internal flag; exposed via the ``released`` property.
    """

    lease_id: int
    task_id: str
    acquired_at: float
    queue_wait_sec: float
    natural_delay_sec: float = 0.0
    _released: bool = False

    @property
    def released(self) -> bool:
        """True if :meth:`RunningSlotScheduler.release` was already called."""
        return self._released


class RunningSlotScheduler:
    """FIFO admission for running-sandbox slices (resume→probe→exec→pause).

    A reservation is made before resume and held until pause is confirmed, so
    client-side admission never exceeds ``maximum`` even while lifecycle calls
    are in flight. The lease MUST be releasable from a ``finally`` block even
    when the slice body raised — i.e. release is the caller's responsibility,
    called in ``finally``.

    Args:
        maximum: Maximum concurrent running slots. Must be >= 1.

    Raises:
        ValueError: ``maximum < 1``.
    """

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError(f"maximum must be >= 1, got {maximum}")
        self._maximum = maximum
        self._active: set[int] = set()  # lease_ids currently held
        self._peak_active = 0
        self._granted = 0
        self._total_queue_wait = 0.0
        # FIFO queue: list of (task_id, queued_at_monotonic, event, seq)
        # event is set when the lease is granted.
        self._queue: list[tuple[str, float, threading.Event, int]] = []
        self._seq = 0  # monotonically increasing sequence number
        self._cond = threading.Condition(threading.Lock())

    def acquire(self, task_id: str, *, ready_at: float | None = None) -> RunningLease:
        """Block until a running slot is free (FIFO order preserved).

        If ``ready_at`` is given (monotonic timestamp in the future), the caller
        sleeps until that time **before** entering the FIFO queue. The pre-delay
        is returned as ``natural_delay_sec`` on the lease so that
        ``queue_wait_sec`` measures only capacity contention (not the natural
        scheduling delay).

        Returns a :class:`RunningLease` carrying ``queue_wait_sec`` (capacity
        wait) and ``natural_delay_sec`` (ready_at pre-delay, 0 when absent).
        The caller is responsible for releasing the lease in a ``finally`` block.
        """
        natural_delay = 0.0
        if ready_at is not None:
            delay = ready_at - time.monotonic()
            if delay > 0:
                natural_delay = delay
                time.sleep(delay)
        queued_at = time.monotonic()
        event = threading.Event()
        with self._cond:
            seq = self._seq
            self._seq += 1
            self._queue.append((task_id, queued_at, event, seq))
            # Wait until I am head of queue AND a slot is available.
            while True:
                if self._queue and self._queue[0][3] == seq and len(self._active) < self._maximum:
                    break
                self._cond.wait()
            # Pop myself from the head
            self._queue.pop(0)
            # Grant the lease
            acquired_at = time.monotonic()
            queue_wait = acquired_at - queued_at
            lease_id = self._granted
            self._granted += 1
            self._active.add(lease_id)
            self._total_queue_wait += queue_wait
            if len(self._active) > self._peak_active:
                self._peak_active = len(self._active)
            return RunningLease(
                lease_id=lease_id,
                task_id=task_id,
                acquired_at=acquired_at,
                queue_wait_sec=queue_wait,
                natural_delay_sec=natural_delay,
            )

    def release(self, lease: RunningLease) -> None:
        """Return the slot. **Double-release raises ``RuntimeError``.**"""
        with self._cond:
            if lease._released:
                raise RuntimeError(f"Lease {lease.lease_id} already released")
            lease._released = True
            self._active.discard(lease.lease_id)
            self._cond.notify_all()

    def snapshot(self) -> dict[str, Any]:
        """Return ``{"maximum", "active", "peak_active", "granted", "average_queue_wait_sec", "waiting"}``."""
        with self._cond:
            return {
                "maximum": self._maximum,
                "active": len(self._active),
                "peak_active": self._peak_active,
                "granted": self._granted,
                "average_queue_wait_sec": self._total_queue_wait / self._granted if self._granted else 0.0,
                "waiting": len(self._queue),
            }


class QpsRateLimiter:
    """Smooth FIFO dispatch with a bounded in-flight fuse.

    One global queue for all non-create lifecycle calls (pause/resume/cleanup).
    Operation type is metrics-only (no priority differentiation).

    The ``slot()`` context manager enforces the load-bearing enter order:

    1. QPS time-wait FIRST (sleep for the smooth dispatch delay, computed from
       ``next_dispatch_at + 1/qps``).
    2. inflight semaphore acquire AFTER the wait.
    3. ``__exit__`` (finally): release the inflight semaphore even if the body
       raised.

    Smooth **no-catch-up**: if the scheduler falls behind (dispatch_at is in the
    past), slide the deadline forward from the actual dispatch time rather than
    bursting. Concretely: ``next_dispatch_at = max(now, dispatch_at) + interval``
    — never let multiple ops dispatch in the same instant to "catch up".

    Args:
        qps: Target queries-per-second dispatch rate. Must be > 0.
        inflight_cap: Maximum concurrent in-flight operations. Must be >= 1.

    Raises:
        ValueError: ``qps <= 0`` or ``inflight_cap < 1``.
    """

    def __init__(self, qps: float, inflight_cap: int) -> None:
        if qps <= 0:
            raise ValueError(f"qps must be > 0, got {qps}")
        if inflight_cap < 1:
            raise ValueError(f"inflight_cap must be >= 1, got {inflight_cap}")
        self._qps = qps
        self._inflight_cap = inflight_cap
        self._interval = 1.0 / qps
        self._inflight = threading.Semaphore(inflight_cap)
        self._next_dispatch_at = time.monotonic()
        self._dispatch_lock = threading.Lock()
        # Metrics
        self._dispatched = 0
        self._total_wait = 0.0
        self._max_wait = 0.0
        self._dispatched_by_op: dict[str, int] = {"resume": 0, "pause": 0, "cleanup": 0}
        self._waiting = 0  # threads parked in the QPS time-wait
        self._waiting_by_op: dict[str, int] = {}

    def slot(self, operation: str) -> _QpsSlot:
        """Return a context manager that enforces QPS rate + inflight cap.

        Args:
            operation: Operation type for metrics (``"resume"``, ``"pause"``,
                ``"cleanup"``, or a custom string).
        """
        return _QpsSlot(self, operation)

    def snapshot(self) -> dict[str, Any]:
        """Return dispatch metrics.

        Returns:
            ``{"qps", "inflight_cap", "in_flight", "dispatched",
            "average_wait_sec", "max_wait_sec", "dispatched_by_operation",
            "waiting", "waiting_by_operation"}``.
        """
        # in_flight = inflight_cap - current semaphore value
        with self._dispatch_lock:
            return {
                "qps": self._qps,
                "inflight_cap": self._inflight_cap,
                "in_flight": self._inflight_cap - self._inflight._value,
                "dispatched": self._dispatched,
                "average_wait_sec": self._total_wait / self._dispatched if self._dispatched else 0.0,
                "max_wait_sec": self._max_wait,
                "dispatched_by_operation": dict(self._dispatched_by_op),
                "waiting": self._waiting,
                "waiting_by_operation": dict(self._waiting_by_op),
            }


class _QpsSlot:
    """Context manager for :meth:`QpsRateLimiter.slot`.

    Enter order (load-bearing):

    1. Compute dispatch time under ``_dispatch_lock``; sleep for the rate delay
       **before** acquiring inflight (so we don't hold a permit while parked).
    2. Acquire inflight semaphore.
    3. Record metrics.

    Exit (finally): release inflight even if the body raised.
    """

    def __init__(self, limiter: QpsRateLimiter, operation: str) -> None:
        self._lim = limiter
        self._op = operation
        self._wait_started_at = 0.0

    def __enter__(self) -> None:
        self._wait_started_at = time.monotonic()
        # Step 1: QPS time-wait FIRST (no inflight permit held yet).
        with self._lim._dispatch_lock:
            now = time.monotonic()
            dispatch_at = self._lim._next_dispatch_at
            if dispatch_at > now:
                delay = dispatch_at - now
                # Slide forward from actual dispatch time (no catch-up burst).
                self._lim._next_dispatch_at = dispatch_at + self._lim._interval
            else:
                # dispatch_at is in the past; slide forward from now.
                delay = 0.0
                self._lim._next_dispatch_at = now + self._lim._interval
            if delay > 0:
                self._lim._waiting += 1
                self._lim._waiting_by_op[self._op] = self._lim._waiting_by_op.get(self._op, 0) + 1
        if delay > 0:
            try:
                time.sleep(delay)
            finally:
                with self._lim._dispatch_lock:
                    self._lim._waiting -= 1
                    self._lim._waiting_by_op[self._op] = max(0, self._lim._waiting_by_op.get(self._op, 0) - 1)
        # Step 2: inflight semaphore acquire AFTER the wait.
        self._lim._inflight.acquire()
        # Step 3: record metrics.
        wait_sec = time.monotonic() - self._wait_started_at
        with self._lim._dispatch_lock:
            self._lim._dispatched += 1
            self._lim._total_wait += wait_sec
            if wait_sec > self._lim._max_wait:
                self._lim._max_wait = wait_sec
            self._lim._dispatched_by_op[self._op] = self._lim._dispatched_by_op.get(self._op, 0) + 1

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Finally: release inflight even if the body raised.
        self._lim._inflight.release()


@dataclass(slots=True)
class Admission:
    """Holder for the two admission controllers.

    Constructed by ``run_benchmark`` only when ``replay_mode == "lifecycle"``.
    The two controllers are **independently constructed**: ``qps`` is ``None``
    when ``control_plane_qps`` is unset, even if ``running_concurrency`` is set.
    They are NOT a forced pair.

    Attributes:
        slots: FIFO running-slot scheduler (always present).
        qps: Smooth QPS rate limiter with inflight fuse (optional).
    """

    slots: RunningSlotScheduler
    qps: QpsRateLimiter | None = None
