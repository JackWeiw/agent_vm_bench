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
