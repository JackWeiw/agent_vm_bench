"""Thread-based admission controllers for replay lifecycle overcommit (P2.6).

Two **independent** controllers govern running-sandbox concurrency and
control-plane dispatch. They are NOT a forced pair: either, both, or neither
may be constructed (each knob is independently bypassed when ``None``).

``RunningSlotScheduler`` — FIFO admission for complete resume->(probe)->exec->
pause slices. A slot is reserved before resume and held until pause is
confirmed, so client-side admission never exceeds ``maximum`` even while
lifecycle calls are in flight. The lease carries ``natural_delay_sec`` (the
ready_at think-time pre-delay) separate from ``queue_wait_sec`` (the FIFO
capacity-contention wait) so the two never conflate.

``QpsRateLimiter`` — two **independently-enabled** functions exposed as a single
class for shared metrics; they solve two different bottlenecks and must not be
conflated:

* ``time_wait(operation)`` — **rate pacing** (request-interval shaping). Sleeps
  for the smooth 1/qps dispatch delay. Solves "how closely together may requests
  be *dispatched*?" A RATE control, not a concurrency control.
* ``inflight(operation)`` — **concurrency fuse** (in-flight cap). A bounded
  semaphore around the API call. Solves "how many requests may be *in flight*
  at once?" A CONCURRENCY control, not a rate control.

Each function no-ops when its own knob is ``None`` (``qps=None`` -> ``time_wait``
returns 0; ``inflight_cap=None`` -> ``inflight`` is a no-op context with wait 0).
``slot()`` is the combined time_wait+inflight context for one-shot operations
(create/cleanup); retried lifecycle calls use ``time_wait`` **once** (pre-lease
for resume) + ``inflight`` per attempt, so a transient retry does NOT re-pay the
rate-pacing delay.

**Forbidden**: acquire inflight THEN sleep — threads would hold inflight permits
while parked, exhausting the fuse and deadlocking on slow-backend accumulation.
``time_wait`` always runs before ``inflight`` within a combined ``slot()``.

The ``clock`` / ``sleep_fn`` constructor seams are for deterministic unit tests
of the no-catch-up dispatch curve (a real clock can only assert inequalities,
never the exact 1/qps spacing). Production leaves them at the ``time.*``
defaults.

Both classes use plain ``threading`` (``Condition``/``Semaphore``/``Lock``,
``time.perf_counter``/``time.monotonic``). No asyncio — the kernel is
thread-based and the sibling toolkit's async controllers cannot be imported
here.
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

# The five built-in control-plane operation types (column-stable in every
# snapshot). Custom operation strings are still accepted on the fly -- the
# dicts below are seeded with these and unknown keys are added when first
# seen -- so this set defines the stable reporting columns, not a closed enum.
_OPERATION_TYPES: tuple[str, ...] = ("resume", "pause", "cleanup", "create", "command")

# Polling interval (seconds) used by every stop-responsive wait in this module.
# Chosen so a benchmark shutdown is observed within ~250 ms without hammering
# condition locks / semaphores on a hot fleet. Tune here, not at call sites.
_STOP_POLL_SEC: float = 0.25


class ShutdownInterrupted(BaseException):
    """Raised from admission waits when the benchmark stop_event is set.

    A ``BaseException`` (not ``Exception``) deliberately: shutdown is not a
    retryable failure, so it must bypass the lifecycle retry loop's
    ``except Exception`` and the per-step ``except Exception`` handlers (a
    shutdown must not be recorded as ``slice_failed``). It propagates up to the
    runner's ``run()`` loop, which catches it and exits the thread cleanly. The
    slice/trajectory ``finally`` blocks still run, so running-slot leases release.
    """


def _stop_aware_sleep(delay: float, stop_event: threading.Event | None) -> None:
    """Sleep ``delay`` seconds; raise ``ShutdownInterrupted`` if stop sets.

    Without a stop_event, falls back to a plain ``time.sleep`` (preserves the
    single-thread / unit-test path where no shutdown signal exists).
    """
    if delay <= 0:
        return
    if stop_event is None:
        time.sleep(delay)
        return
    deadline = time.monotonic() + delay
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            return
        if stop_event.is_set():
            raise ShutdownInterrupted("admission time-wait interrupted by stop_event")
        stop_event.wait(min(remaining, _STOP_POLL_SEC))


def _stop_aware_acquire(semaphore: threading.Semaphore, stop_event: threading.Event | None) -> bool:
    """Acquire ``semaphore``; raise ``ShutdownInterrupted`` if stop sets.

    Returns True on acquire. Without a stop_event, blocks normally.
    """
    if stop_event is None:
        semaphore.acquire()
        return True
    while True:
        if semaphore.acquire(timeout=_STOP_POLL_SEC):
            return True
        if stop_event.is_set():
            raise ShutdownInterrupted("inflight acquire interrupted by stop_event")


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

    def __init__(self, maximum: int, *, stop_event: threading.Event | None = None) -> None:
        if maximum < 1:
            raise ValueError(f"maximum must be >= 1, got {maximum}")
        self._maximum = maximum
        self._stop_event = stop_event
        self._active: set[int] = set()  # lease_ids currently held
        self._peak_active = 0
        self._granted = 0
        self._total_queue_wait = 0.0
        # P1-3: count requests still sleeping until their ready_at. They are
        # pending (not yet granted) but absent from _queue, so without this
        # counter snapshot.waiting would undercount during launch pacing /
        # inter-step delays.
        self._delayed_count = 0
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
                # P1-3: register as pending BEFORE sleeping so snapshot.waiting
                # counts delayed-but-not-yet-eligible requests.
                with self._cond:
                    self._delayed_count += 1
                try:
                    _stop_aware_sleep(delay, self._stop_event)
                finally:
                    with self._cond:
                        self._delayed_count -= 1
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
                # P0-2: stop-responsive FIFO wait. Bounded timeout so a shutdown
                # is observed even without a notify (e.g. a dead backend that
                # never releases).
                if self._stop_event is not None and self._stop_event.is_set():
                    try:
                        self._queue.remove((task_id, queued_at, event, seq))
                    except ValueError:
                        pass
                    self._cond.notify_all()
                    raise ShutdownInterrupted("running-slot FIFO wait interrupted by stop_event")
                self._cond.wait(timeout=_STOP_POLL_SEC)
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
        """Return ``{"maximum", "active", "peak_active", "granted", "average_queue_wait_sec", "waiting"}``.

        ``waiting`` counts both capacity-queued requests and delayed-but-not-yet-
        eligible requests (still sleeping until their ``ready_at``).
        """
        with self._cond:
            return {
                "maximum": self._maximum,
                "active": len(self._active),
                "peak_active": self._peak_active,
                "granted": self._granted,
                "average_queue_wait_sec": self._total_queue_wait / self._granted if self._granted else 0.0,
                "waiting": len(self._queue) + self._delayed_count,
            }


class QpsRateLimiter:
    """Rate pacing + concurrency fuse (two independently-enabled functions).

    The two functions solve **different** bottlenecks and are independently
    bypassed when their knob is ``None``:

    * :meth:`time_wait` — **rate pacing**. Smooth 1/qps dispatch delay with
      no-catch-up (``next_dispatch_at = max(now, dispatch_at) + interval``;
      never bursts to catch up). A RATE control. ``qps=None`` bypasses it
      (returns 0.0, no sleep, no metrics).
    * :meth:`inflight` — **concurrency fuse**. A bounded semaphore around the
      API call. A CONCURRENCY control. ``inflight_cap=None`` bypasses it
      (no-op context, wait 0.0).
    * :meth:`slot` — the combined time_wait+inflight context for one-shot
      operations (create/cleanup). Retried lifecycle calls instead call
      ``time_wait`` **once** (pre-lease for resume) + ``inflight`` per attempt
      so a transient retry does NOT re-pay the rate-pacing delay.

    API-contract boundary (locked): ``time_wait`` owns arrival pacing and
    touches NOTHING about concurrency; ``inflight`` owns the in-flight cap and
    touches NOTHING about spacing. Within a combined ``slot()``, ``time_wait``
    always runs first (forbidden: inflight-then-sleep would park threads on
    held permits and deadlock the fuse on slow backends).

    Args:
        qps: Target dispatch rate. ``None`` bypasses rate pacing. Must be > 0
            when set.
        inflight_cap: Max concurrent in-flight ops. ``None`` bypasses the fuse.
            Must be >= 1 when set.
        stop_event: When set, admission waits raise :class:`ShutdownInterrupted`.
        clock: Injectable monotonic clock (default ``time.monotonic``) for
            deterministic no-catch-up unit tests. Production uses the default.
        sleep_fn: Injectable sleep (default ``time.sleep``) used by
            ``time_wait`` only when ``stop_event`` is None. Tests pass a no-op
            and advance the injected clock manually.

    Raises:
        ValueError: ``qps`` set and ``<= 0``; ``inflight_cap`` set and ``< 1``.
    """

    def __init__(
        self,
        qps: float | None,
        inflight_cap: int | None,
        *,
        stop_event: threading.Event | None = None,
        clock=time.monotonic,
        sleep_fn=time.sleep,
    ) -> None:
        if qps is not None and qps <= 0:
            raise ValueError(f"qps must be > 0 or None, got {qps}")
        if inflight_cap is not None and inflight_cap < 1:
            raise ValueError(f"inflight_cap must be >= 1 or None, got {inflight_cap}")
        self._qps = qps
        self._inflight_cap = inflight_cap
        self._interval = (1.0 / qps) if qps is not None else 0.0
        self._inflight = threading.Semaphore(inflight_cap) if inflight_cap is not None else None
        self._next_dispatch_at = clock()
        self._dispatch_lock = threading.Lock()
        self._stop_event = stop_event
        self._clock = clock
        self._sleep_fn = sleep_fn
        # Rate-pacing metrics (time_wait callers).
        self._dispatched = 0
        self._total_wait = 0.0
        self._max_wait = 0.0
        self._dispatched_by_op: dict[str, int] = {op: 0 for op in _OPERATION_TYPES}
        self._waiting = 0  # threads parked in the rate-pacing sleep
        self._waiting_by_op: dict[str, int] = {op: 0 for op in _OPERATION_TYPES}
        # Fuse metrics (inflight callers).
        self._inflight_dispatched = 0
        self._total_inflight_wait = 0.0

    def time_wait(self, operation: str) -> float:
        """Sleep the 1/qps rate-pacing delay; return the wait (0.0 if qps=None).

        No inflight permit is acquired -- call :meth:`inflight` separately for
        the fuse. Smooth no-catch-up: if the scheduler fell behind
        (``dispatch_at`` in the past), slide the deadline forward from the
        actual dispatch time (``max(now, dispatch_at) + interval``) rather than
        bursting. The ``clock``/``sleep_fn`` seams make this deterministic in
        tests: inject a fake clock + no-op sleep, advance the clock between
        calls, and assert the exact returned curve.
        """
        if self._qps is None:
            return 0.0
        now = self._clock()
        with self._dispatch_lock:
            dispatch_at = self._next_dispatch_at
            if dispatch_at > now:
                delay = dispatch_at - now
                # Slide forward from the scheduled dispatch time (no catch-up).
                self._next_dispatch_at = dispatch_at + self._interval
            else:
                # dispatch_at is in the past; slide forward from now.
                delay = 0.0
                self._next_dispatch_at = now + self._interval
            if delay > 0:
                self._waiting += 1
                self._waiting_by_op[operation] = self._waiting_by_op.get(operation, 0) + 1
        if delay > 0:
            try:
                if self._stop_event is not None:
                    _stop_aware_sleep(delay, self._stop_event)
                else:
                    self._sleep_fn(delay)
            finally:
                with self._dispatch_lock:
                    self._waiting -= 1
                    self._waiting_by_op[operation] = max(0, self._waiting_by_op.get(operation, 0) - 1)
        with self._dispatch_lock:
            self._dispatched += 1
            self._total_wait += delay
            if delay > self._max_wait:
                self._max_wait = delay
            self._dispatched_by_op[operation] = self._dispatched_by_op.get(operation, 0) + 1
        return delay

    def inflight(self, operation: str) -> _InflightCtx:
        """Return the concurrency-fuse context manager.

        Acquires the inflight semaphore on enter (blocking at the cap), releases
        on exit (even if the body raised). No rate pacing -- call
        :meth:`time_wait` separately. No-op (``wait_sec`` 0.0) when
        ``inflight_cap`` is None. The resulting context exposes ``wait_sec``
        (the semaphore-block time, 0.0 when uncontended or bypassed) so the
        runner can attribute fuse contention separately from rate pacing.
        """
        return _InflightCtx(self, operation)

    def slot(self, operation: str, *, hold_inflight: bool = True) -> _QpsSlot:
        """Combined rate-pacing + fuse context for one-shot operations.

        Calls :meth:`time_wait` then :meth:`inflight` (when ``hold_inflight``).
        Used for create/cleanup (one call, no retry). For retried lifecycle
        calls (resume/pause), call ``time_wait`` once + ``inflight`` per attempt
        instead so a transient retry does not re-pay the rate-pacing delay.

        Args:
            operation: Operation type for metrics.
            hold_inflight: When False, apply rate pacing only (no fuse permit).
                Used for the ``"command"`` bucket: ``provider.exec`` is a
                monolithic blocking RPC with no stream handle, so holding a fuse
                permit for the whole body would serialize long commands behind
                the cap.
        """
        return _QpsSlot(self, operation, hold_inflight=hold_inflight)

    def snapshot(self) -> dict[str, Any]:
        """Return dispatch + fuse metrics.

        Rate-pacing group: ``qps``, ``dispatched``, ``average_wait_sec``,
        ``max_wait_sec``, ``dispatched_by_operation``, ``waiting``,
        ``waiting_by_operation``. Fuse group: ``inflight_cap``, ``in_flight``,
        ``inflight_dispatched``, ``average_inflight_wait_sec``. Each group is
        zeros when its knob is ``None`` (that function was bypassed).
        """
        with self._dispatch_lock:
            return {
                "qps": self._qps,
                "inflight_cap": self._inflight_cap,
                "in_flight": (self._inflight_cap - self._inflight._value) if self._inflight_cap is not None else 0,
                "dispatched": self._dispatched,
                "average_wait_sec": self._total_wait / self._dispatched if self._dispatched else 0.0,
                "max_wait_sec": self._max_wait,
                "dispatched_by_operation": dict(self._dispatched_by_op),
                "waiting": self._waiting,
                "waiting_by_operation": dict(self._waiting_by_op),
                "inflight_dispatched": self._inflight_dispatched,
                "average_inflight_wait_sec": (
                    self._total_inflight_wait / self._inflight_dispatched if self._inflight_dispatched else 0.0
                ),
            }


class _InflightCtx:
    """Context manager for :meth:`QpsRateLimiter.inflight` (fuse only).

    Enter: block on the inflight semaphore (unless ``inflight_cap`` is None, in
    which case this is a no-op and ``wait_sec`` is 0.0). Exit: release the
    permit only if one was acquired. The block time is exposed as
    ``wait_sec`` so the runner can attribute fuse contention separately from
    rate pacing.
    """

    def __init__(self, limiter: QpsRateLimiter, operation: str) -> None:
        self._lim = limiter
        self._op = operation
        self.wait_sec = 0.0
        self._acquired = False

    def __enter__(self) -> _InflightCtx:
        if self._lim._inflight_cap is None:
            return self
        t0 = self._lim._clock()
        _stop_aware_acquire(self._lim._inflight, self._lim._stop_event)
        self._acquired = True
        self.wait_sec = self._lim._clock() - t0
        with self._lim._dispatch_lock:
            self._lim._inflight_dispatched += 1
            self._lim._total_inflight_wait += self.wait_sec
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._acquired:
            self._lim._inflight.release()


class _QpsSlot:
    """Combined rate-pacing + fuse context for :meth:`QpsRateLimiter.slot`.

    Delegates to :meth:`time_wait` (rate pacing) then :meth:`inflight` (fuse)
    so the two functions share one metrics path. Exposes ``rate_wait_sec``
    and ``inflight_wait_sec`` for callers that need the split (one-shot
    create/cleanup). The load-bearing order -- time_wait FIRST, inflight AFTER
    -- is inherited from the method order; inflight-then-sleep would park
    threads on held permits and deadlock the fuse on slow backends.
    """

    def __init__(self, limiter: QpsRateLimiter, operation: str, *, hold_inflight: bool = True) -> None:
        self._lim = limiter
        self._op = operation
        self._hold_inflight = hold_inflight
        self.rate_wait_sec = 0.0
        self.inflight_wait_sec = 0.0
        self._inflight_ctx: _InflightCtx | None = None

    def __enter__(self) -> _QpsSlot:
        # Step 1: rate pacing FIRST (no inflight permit held while parked).
        self.rate_wait_sec = self._lim.time_wait(self._op)
        # Step 2: inflight fuse AFTER the wait (skipped for the command bucket,
        # whose body may outlive a reasonable inflight permit).
        if self._hold_inflight:
            self._inflight_ctx = self._lim.inflight(self._op)
            self._inflight_ctx.__enter__()
            self.inflight_wait_sec = self._inflight_ctx.wait_sec
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Finally: release the inflight permit only if one was acquired.
        if self._inflight_ctx is not None:
            self._inflight_ctx.__exit__(exc_type, exc_val, exc_tb)


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


class LaunchPacer:
    """G5: shared no-catch-up launch pacing for a trajectory runner fleet.

    One instance is shared across all worker threads in trajectory mode so the
    ``next_launch_at`` deadline is visible to every worker. A per-runner field
    would let each worker read its own ``0.0`` and burst-create in the same
    instant -- the shared lock alone serializes nothing cross-runner because it
    guards state only one worker can see.

    The launch interval comes from the runner's config
    (``replay_launch_interval_sec``); the pacer only owns the shared
    lock + deadline cell. ``claim_turn`` is the sole mutation: under the lock it
    computes ``wait_until = max(now, next_at)`` and advances
    ``next_at = wait_until + interval`` (no catch-up burst), then returns the
    deadline. The caller sleeps *outside* the lock so parked workers don't
    block the queue.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0

    def claim_turn(self, interval: float) -> float:
        """Claim a launch turn; return the monotonic deadline to wait until.

        No-op-equivalent when ``interval <= 0`` (returns ``time.monotonic()``,
        i.e. no wait); callers gate on the interval before calling.
        """
        with self._lock:
            now = time.monotonic()
            wait_until = max(now, self._next_at)
            self._next_at = wait_until + interval
        return wait_until
