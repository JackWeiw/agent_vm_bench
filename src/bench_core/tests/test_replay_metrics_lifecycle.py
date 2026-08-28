"""ReplayMetrics lifecycle duration-list tests (P2.5 Task 1)."""
from __future__ import annotations

import threading

from bench_core.schemas import ReplayMetrics


class TestReplayMetricsLifecycleDurations:
    def test_add_stores_resume_pause_slice_total(self):
        m = ReplayMetrics()
        m.add(latency=1.0, success=True, action_type="shell", resume_sec=0.1, pause_sec=0.2, slice_total_sec=1.3)
        assert m.resume_secs == [0.1]
        assert m.pause_secs == [0.2]
        assert m.slice_total_secs == [1.3]

    def test_zero_slice_total_excludes_from_all_three_lists(self):
        m = ReplayMetrics()
        # synthesized failure path: slice_total_sec == 0.0
        m.add(latency=0.0, success=False, action_type="shell", resume_sec=0.0, pause_sec=0.0, slice_total_sec=0.0)
        assert m.resume_secs == []
        assert m.pause_secs == []
        assert m.slice_total_secs == []

    def test_lists_stay_aligned_under_concurrent_adds(self):
        m = ReplayMetrics()

        def worker(_i: int) -> None:
            m.add(latency=0.5, success=True, action_type="shell", resume_sec=0.05, pause_sec=0.05, slice_total_sec=0.6)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        n = len(m.resume_secs)
        assert n == 50
        assert len(m.pause_secs) == n
        assert len(m.slice_total_secs) == n

    def test_properties_return_copies(self):
        m = ReplayMetrics()
        m.add(latency=1.0, success=True, action_type="shell", resume_sec=0.1, pause_sec=0.1, slice_total_sec=1.2)
        got = m.resume_secs
        got.append(999.0)
        # mutating the returned list must not affect the internal list
        assert m.resume_secs == [0.1]

    def test_defaults_when_kwargs_omitted(self):
        m = ReplayMetrics()
        m.add(latency=1.0, success=True, action_type="shell")
        # no lifecycle kwargs -> all default to 0.0 -> slice_total_sec == 0 excludes from all three lists
        assert m.resume_secs == []
        assert m.pause_secs == []
        assert m.slice_total_secs == []

    def test_initial_pause_sec_unchanged(self):
        m = ReplayMetrics()
        m.initial_pause_sec = 0.42
        m.add(latency=1.0, success=True, action_type="shell", resume_sec=0.1, pause_sec=0.1, slice_total_sec=1.2)
        assert m.initial_pause_sec == 0.42


class TestReplayMetricsP26Segments:
    """P2.6: four segment-duration lists (resume_api, resume_ready_wait,
    slot_contention_wait, pause_api) appended atomically with the P2.5 three."""

    def test_add_stores_four_segments(self):
        m = ReplayMetrics()
        m.add(
            latency=1.0,
            success=True,
            action_type="shell",
            resume_sec=0.5,
            pause_sec=0.3,
            slice_total_sec=1.8,
            resume_api_sec=0.2,
            resume_ready_wait_sec=0.15,
            slot_contention_wait_sec=0.1,
            pause_api_sec=0.25,
            resume_queue_wait_sec=0.05,
        )
        assert m.resume_api_secs == [0.2]
        assert m.resume_ready_wait_secs == [0.15]
        assert m.slot_contention_wait_secs == [0.1]
        assert m.pause_api_secs == [0.25]
        assert m.resume_queue_wait_secs == [0.05]

    def test_zero_slice_excludes_all_eight(self):
        m = ReplayMetrics()
        m.add(
            latency=0.0,
            success=False,
            action_type="shell",
            resume_sec=0.0,
            pause_sec=0.0,
            slice_total_sec=0.0,
            resume_api_sec=0.0,
            resume_ready_wait_sec=0.0,
            slot_contention_wait_sec=0.0,
            pause_api_sec=0.0,
            resume_queue_wait_sec=0.0,
        )
        assert m.resume_secs == []
        assert m.pause_secs == []
        assert m.slice_total_secs == []
        assert m.resume_api_secs == []
        assert m.resume_ready_wait_secs == []
        assert m.slot_contention_wait_secs == []
        assert m.pause_api_secs == []
        assert m.resume_queue_wait_secs == []

    def test_eight_lists_aligned_under_concurrency(self):
        m = ReplayMetrics()

        def worker(_i: int) -> None:
            m.add(
                latency=0.5,
                success=True,
                action_type="shell",
                resume_sec=0.05,
                pause_sec=0.05,
                slice_total_sec=0.6,
                resume_api_sec=0.02,
                resume_ready_wait_sec=0.01,
                slot_contention_wait_sec=0.005,
                pause_api_sec=0.03,
                resume_queue_wait_sec=0.004,
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        n = len(m.resume_secs)
        assert n == 50
        assert len(m.pause_secs) == n
        assert len(m.slice_total_secs) == n
        assert len(m.resume_api_secs) == n
        assert len(m.resume_ready_wait_secs) == n
        assert len(m.slot_contention_wait_secs) == n
        assert len(m.pause_api_secs) == n
        assert len(m.resume_queue_wait_secs) == n

    def test_segment_properties_return_copies(self):
        m = ReplayMetrics()
        m.add(
            latency=1.0,
            success=True,
            action_type="shell",
            resume_sec=0.5,
            pause_sec=0.3,
            slice_total_sec=1.8,
            resume_api_sec=0.2,
            resume_ready_wait_sec=0.15,
            slot_contention_wait_sec=0.1,
            pause_api_sec=0.25,
            resume_queue_wait_sec=0.05,
        )
        # Mutate every returned list; internal state must be unaffected.
        m.resume_api_secs.append(999.0)
        m.resume_ready_wait_secs.append(999.0)
        m.slot_contention_wait_secs.append(999.0)
        m.pause_api_secs.append(999.0)
        m.resume_queue_wait_secs.append(999.0)
        assert m.resume_api_secs == [0.2]
        assert m.resume_ready_wait_secs == [0.15]
        assert m.slot_contention_wait_secs == [0.1]
        assert m.pause_api_secs == [0.25]
        assert m.resume_queue_wait_secs == [0.05]

    def test_add_without_segment_kwargs_defaults_zero(self):
        m = ReplayMetrics()
        # Omit the four P2.6 kwargs entirely; they default to 0.0 and are
        # appended so lengths stay aligned with the P2.5 three.
        m.add(
            latency=1.0,
            success=True,
            action_type="shell",
            resume_sec=0.5,
            pause_sec=0.3,
            slice_total_sec=1.8,
        )
        assert m.resume_api_secs == [0.0]
        assert m.resume_ready_wait_secs == [0.0]
        assert m.slot_contention_wait_secs == [0.0]
        assert m.pause_api_secs == [0.0]
        assert m.resume_queue_wait_secs == [0.0]

    def test_add_stores_resume_queue_wait(self):
        m = ReplayMetrics()
        m.add(
            latency=1.0,
            success=True,
            action_type="shell",
            resume_sec=0.5,
            pause_sec=0.3,
            slice_total_sec=1.8,
            resume_queue_wait_sec=0.05,
        )
        assert m.resume_queue_wait_secs == [0.05]
