"""Admission controller tests: RunningSlotScheduler + QpsRateLimiter + Admission holder.

P2.6 overcommit admission. Both classes are thread-based; tests drive them from
multiple worker threads behind barriers to exercise real concurrency.
"""
from __future__ import annotations

import threading
import time

import pytest

from bench_core.admission import Admission, QpsRateLimiter, RunningLease, RunningSlotScheduler


def test_acquire_ready_at_parks_before_enqueue():
    """ready_at in the future: the sleep happens before FIFO wait, so
    queue_wait_sec measures only capacity contention (not the natural delay)."""
    sched = RunningSlotScheduler(maximum=1)
    # Occupy the only slot so a 2nd acquireor waits.
    held = sched.acquire("first")
    ready_at = time.monotonic() + 0.3
    lease2 = [None]

    def _acq():
        lease2[0] = sched.acquire("second", ready_at=ready_at)

    t = threading.Thread(target=_acq)
    t.start()
    # Wait for ready_at sleep to finish, then release the held slot.
    time.sleep(0.4)
    sched.release(held)
    t.join()
    assert lease2[0] is not None
    # natural_delay ~= 0.3s (the ready_at sleep); capacity_wait small (held released
    # immediately, but the waiter parked ~0.3s before enqueue).
    assert lease2[0].natural_delay_sec >= 0.2
    # queue_wait_sec (capacity) should be small -- the slot freed right away.
    assert lease2[0].queue_wait_sec < 0.2


def test_acquire_no_ready_at_has_zero_natural_delay():
    sched = RunningSlotScheduler(maximum=2)
    lease = sched.acquire("t")
    assert lease.natural_delay_sec == 0.0
    sched.release(lease)


def test_snapshot_waiting_counts_parked_threads():
    sched = RunningSlotScheduler(maximum=1)
    held = sched.acquire("first")
    started = threading.Event()
    done = threading.Event()

    def _acq():
        started.set()
        sched.acquire("second")
        done.set()

    t = threading.Thread(target=_acq, daemon=True)
    t.start()
    assert started.wait(2)
    time.sleep(0.1)  # let it park in the FIFO wait
    snap = sched.snapshot()
    assert snap["waiting"] >= 1
    sched.release(held)
    assert done.wait(2)


def test_qps_snapshot_waiting_by_operation():
    lim = QpsRateLimiter(qps=1.0, inflight_cap=1)
    # Occupy inflight so a 2nd slot() parks in the QPS sleep.
    entered = threading.Event()
    release_ev = threading.Event()

    def _hold():
        with lim.slot("command"):
            entered.set()
            release_ev.wait(2)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert entered.wait(2)

    parked = threading.Event()

    def _wait():
        with lim.slot("command"):
            parked.set()

    w = threading.Thread(target=_wait, daemon=True)
    w.start()
    time.sleep(0.15)  # let it enter the QPS time-wait
    snap = lim.snapshot()
    assert snap["waiting"] >= 1
    assert snap["waiting_by_operation"].get("command", 0) >= 1
    release_ev.set()
    assert parked.wait(2)


# -------------------------------------------------------------- RunningSlotScheduler


class TestRunningSlotSchedulerValidation:
    def test_maximum_below_one_raises(self):
        with pytest.raises(ValueError):
            RunningSlotScheduler(maximum=0)
        with pytest.raises(ValueError):
            RunningSlotScheduler(maximum=-1)


class TestRunningSlotSchedulerCap:
    def test_cap_enforced_four_threads_max_two(self):
        sched = RunningSlotScheduler(maximum=2)
        barrier = threading.Barrier(4)
        peak_observed = [0]
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=2)
            lease = sched.acquire(f"t{threading.get_ident()}")
            try:
                with lock:
                    snap = sched.snapshot()
                    if snap["active"] > peak_observed[0]:
                        peak_observed[0] = snap["active"]
                assert snap["active"] <= 2
                time.sleep(0.05)
            finally:
                sched.release(lease)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        snap = sched.snapshot()
        assert snap["peak_active"] == 2
        assert snap["granted"] == 4


class TestRunningSlotSchedulerFIFO:
    def test_fifo_order(self):
        sched = RunningSlotScheduler(maximum=1)
        # each entry: (label, started_event, acquired_event, done_event)
        results: list[str] = []
        results_lock = threading.Lock()
        release_events: list[threading.Event] = []

        def worker(label: str, acquired: threading.Event, done: threading.Event) -> None:
            lease = sched.acquire(label)
            with results_lock:
                results.append(label)
            acquired.set()
            # block until the test tells us to release
            done.wait(timeout=5)
            sched.release(lease)

        # t0 starts first and takes the single slot
        acq0, done0 = threading.Event(), threading.Event()
        t0 = threading.Thread(target=worker, args=("t0", acq0, done0))
        t0.start()
        assert acq0.wait(timeout=2), "t0 did not acquire"
        release_events.append(done0)

        # t1 enqueues behind t0
        acq1, done1 = threading.Event(), threading.Event()
        t1 = threading.Thread(target=worker, args=("t1", acq1, done1))
        t1.start()
        time.sleep(0.03)

        # t2 enqueues behind t1
        acq2, done2 = threading.Event(), threading.Event()
        t2 = threading.Thread(target=worker, args=("t2", acq2, done2))
        t2.start()
        time.sleep(0.03)

        # t1 and t2 must still be queued
        assert not acq1.is_set()
        assert not acq2.is_set()

        # release t0 -> t1 should acquire next
        done0.set()
        assert acq1.wait(timeout=2), "t1 did not acquire after t0 released"
        assert not acq2.is_set()

        # release t1 -> t2 should acquire next
        done1.set()
        assert acq2.wait(timeout=2), "t2 did not acquire after t1 released"

        # release t2
        done2.set()
        for t in (t0, t1, t2):
            t.join(timeout=2)

        assert results == ["t0", "t1", "t2"]


class TestRunningSlotSchedulerDoubleRelease:
    def test_double_release_raises(self):
        sched = RunningSlotScheduler(maximum=2)
        lease = sched.acquire("x")
        sched.release(lease)
        with pytest.raises(RuntimeError):
            sched.release(lease)


class TestRunningSlotSchedulerReleaseOnException:
    def test_lease_released_on_body_exception(self):
        """Acquire, raise inside a try/finally: release pattern -- the next
        acquire must not block (slot returned to baseline).
        """
        sched = RunningSlotScheduler(maximum=1)
        exceptions: list[BaseException] = []

        def failing_worker() -> None:
            lease = sched.acquire("fail")
            try:
                raise ValueError("boom")
            except ValueError as e:
                exceptions.append(e)
            finally:
                sched.release(lease)

        t = threading.Thread(target=failing_worker)
        t.start()
        t.join(timeout=2)
        assert sched.snapshot()["active"] == 0
        assert len(exceptions) == 1

        # the next acquire must succeed immediately (slot is free)
        lease = sched.acquire("next")
        assert isinstance(lease, RunningLease)
        sched.release(lease)


class TestRunningSlotSchedulerSnapshot:
    def test_snapshot_keys_and_rest_state(self):
        sched = RunningSlotScheduler(maximum=3)
        snap = sched.snapshot()
        assert set(snap.keys()) == {
            "maximum",
            "active",
            "peak_active",
            "granted",
            "average_queue_wait_sec",
            "waiting",
        }
        assert snap["maximum"] == 3
        assert snap["active"] == 0
        assert snap["peak_active"] == 0
        assert snap["granted"] == 0
        assert snap["average_queue_wait_sec"] == 0.0


# -------------------------------------------------------------- QpsRateLimiter


class TestQpsRateLimiterValidation:
    def test_qps_below_positive_raises(self):
        with pytest.raises(ValueError):
            QpsRateLimiter(qps=0, inflight_cap=4)
        with pytest.raises(ValueError):
            QpsRateLimiter(qps=-1.0, inflight_cap=4)

    def test_inflight_cap_below_positive_raises(self):
        with pytest.raises(ValueError):
            QpsRateLimiter(qps=10, inflight_cap=0)
        with pytest.raises(ValueError):
            QpsRateLimiter(qps=10, inflight_cap=-2)


class TestQpsRateLimiterDispatchedByOperation:
    def test_dispatched_by_operation_counts(self):
        lim = QpsRateLimiter(qps=1000, inflight_cap=4)
        with lim.slot("resume"):
            pass
        with lim.slot("resume"):
            pass
        with lim.slot("pause"):
            pass
        snap = lim.snapshot()
        assert snap["dispatched_by_operation"]["resume"] == 2
        assert snap["dispatched_by_operation"]["pause"] == 1
        assert snap["dispatched_by_operation"]["cleanup"] == 0
        assert snap["dispatched"] == 3

    def test_unknown_operation_added_on_the_fly(self):
        lim = QpsRateLimiter(qps=1000, inflight_cap=4)
        with lim.slot("custom_op"):
            pass
        snap = lim.snapshot()
        assert snap["dispatched_by_operation"]["custom_op"] == 1
        # the three built-in keys are still present
        assert "resume" in snap["dispatched_by_operation"]
        assert "pause" in snap["dispatched_by_operation"]
        assert "cleanup" in snap["dispatched_by_operation"]


class TestQpsRateLimiterInflightCap:
    def test_inflight_cap_bounds_concurrency(self):
        lim = QpsRateLimiter(qps=1000, inflight_cap=2)
        barrier = threading.Barrier(5)
        peak_observed = [0]
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=2)
            with lim.slot("resume"):
                with lock:
                    snap = lim.snapshot()
                    if snap["in_flight"] > peak_observed[0]:
                        peak_observed[0] = snap["in_flight"]
                assert snap["in_flight"] <= 2
                time.sleep(0.05)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        snap = lim.snapshot()
        assert snap["dispatched"] == 5
        assert peak_observed[0] <= 2


class TestQpsRateLimiterOrdering:
    def test_inflight_not_held_during_rate_wait(self, monkeypatch):
        """ORDERING INVARIANT: during the QPS time-wait sleep, NO inflight
        permit is held. The rate-wait MUST precede the inflight acquire; the
        inverse ordering would hold permits during the sleep and deadlock the
        fuse on slow-backend accumulation.
        """
        lim = QpsRateLimiter(qps=5, inflight_cap=3)  # interval=0.2s
        observations = [0]
        spy_error: list[BaseException] = []

        original_sleep = time.sleep

        def sleep_spy(duration: float) -> None:
            # The 1st call may have dispatch_at in the past -> no sleep.
            # The 2nd+ call actually sleeps (interval > 0).
            if duration > 0.01:
                observations[0] += 1
                try:
                    # All inflight_cap permits must be available during the sleep:
                    # no thread can be holding one while we park for rate dispatch.
                    assert lim._inflight._value == 3, (
                        f"inflight not full during rate-wait sleep: " f"_value={lim._inflight._value}, cap=3"
                    )
                except AssertionError as e:
                    spy_error.append(e)
            original_sleep(duration)

        monkeypatch.setattr("bench_core.admission.time.sleep", sleep_spy)

        try:
            with lim.slot("resume"):
                time.sleep(0.01)
            with lim.slot("resume"):
                time.sleep(0.01)
            with lim.slot("pause"):
                time.sleep(0.01)
        finally:
            monkeypatch.undo()

        assert observations[0] >= 1, "spy did not observe any rate-wait sleep"
        if spy_error:
            raise spy_error[0]


class TestQpsRateLimiterReleaseOnException:
    def test_inflight_released_on_body_exception(self):
        lim = QpsRateLimiter(qps=1000, inflight_cap=2)
        with pytest.raises(ValueError), lim.slot("resume"):
            raise ValueError("boom")
        # Both inflight permits must be available again
        assert lim._inflight._value == 2


class TestQpsRateLimiterSnapshot:
    def test_snapshot_keys(self):
        lim = QpsRateLimiter(qps=10, inflight_cap=3)
        snap = lim.snapshot()
        assert set(snap.keys()) == {
            "qps",
            "inflight_cap",
            "in_flight",
            "dispatched",
            "average_wait_sec",
            "max_wait_sec",
            "dispatched_by_operation",
            "waiting",
            "waiting_by_operation",
        }
        assert snap["qps"] == 10
        assert snap["inflight_cap"] == 3
        assert snap["in_flight"] == 0
        assert snap["dispatched"] == 0

    def test_snapshot_dispatched_by_operation_initial_keys(self):
        lim = QpsRateLimiter(qps=10, inflight_cap=3)
        snap = lim.snapshot()
        assert set(snap["dispatched_by_operation"].keys()) >= {"resume", "pause", "cleanup"}


# -------------------------------------------------------------- Admission holder


class TestAdmissionHolder:
    def test_qps_optional_when_only_slots_set(self):
        sched = RunningSlotScheduler(maximum=2)
        adm = Admission(slots=sched, qps=None)
        assert adm.slots is sched
        assert adm.qps is None

    def test_qps_present_when_provided(self):
        sched = RunningSlotScheduler(maximum=2)
        lim = QpsRateLimiter(qps=100, inflight_cap=4)
        adm = Admission(slots=sched, qps=lim)
        assert adm.qps is lim


# -------------------------------------------------------------- RunningLease


class TestRunningLease:
    def test_fields_populated(self):
        sched = RunningSlotScheduler(maximum=1)
        lease = sched.acquire("task-42")
        try:
            assert lease.task_id == "task-42"
            assert isinstance(lease.lease_id, int)
            assert lease.acquired_at > 0
            assert lease.queue_wait_sec >= 0.0
            assert lease.released is False
        finally:
            sched.release(lease)
        assert lease.released is True
