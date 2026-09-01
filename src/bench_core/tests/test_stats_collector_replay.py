"""Phase 3: enriched admission block + [Throughput & Overcommit] rendering."""
from __future__ import annotations

from bench_core.config import KernelConfig
from bench_core.schemas import BenchSandbox, ReplayMetrics
from bench_core.stats_collector import ReportFormatter
from env_provider import SandboxInstance


def _state_with_slices() -> BenchSandbox:
    state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
    m = ReplayMetrics()
    for _ in range(3):
        m.add(
            latency=0.1,
            success=True,
            action_type="shell",
            resume_sec=0.05,
            pause_sec=0.05,
            slice_total_sec=1.0,
            running_slot_held_sec=0.8,
            interaction_total_sec=1.0,
            slot_contention_wait_sec=0.02,
        )
    state.replay_metrics = m
    return state


_FULL_ADMISSION = {
    "running": 1,
    "total": 2,
    "qps": 100.0,
    "peak_active": 1,
    "avg_queue_wait_sec": 0.01,
    "qps_dispatched": 12,
    "running_slots": {
        "maximum": 1,
        "active": 0,
        "peak_active": 1,
        "granted": 3,
        "average_queue_wait_sec": 0.01,
        "waiting": 0,
    },
    "qps_limiter": {
        "qps": 100.0,
        "inflight_cap": 4,
        "in_flight": 0,
        "dispatched": 12,
        "average_wait_sec": 0.001,
        "max_wait_sec": 0.01,
        "dispatched_by_operation": {"resume": 3, "pause": 3, "cleanup": 0, "create": 3, "command": 3},
        "waiting": 0,
        "waiting_by_operation": {"resume": 0, "pause": 0, "cleanup": 0, "create": 0, "command": 0},
    },
}


def _format(state, *, admission_snapshot=None, wall_sec=None) -> str:
    cfg = KernelConfig(
        workflow_type="replay", replay_mode="lifecycle", total_count=2, replay_running_concurrency=1, test_duration=1
    )
    f = ReportFormatter(cfg, {0: state}, "fake", admission_snapshot=admission_snapshot, wall_sec=wall_sec)
    return "\n".join(f.format_replay_stats_section())


class TestAdmissionBlockRender:
    def test_full_admission_block_renders_all_lines(self):
        joined = _format(_state_with_slices(), admission_snapshot=_FULL_ADMISSION, wall_sec=10.0)
        assert "Admission:" in joined
        assert "Running slots:" in joined
        assert "maximum=1" in joined and "granted=3" in joined and "waiting=0" in joined
        assert "QPS limiter:" in joined
        assert "inflight=0/4" in joined and "dispatched=12" in joined
        assert "Dispatched by operation:" in joined
        assert "resume=3" in joined and "command=3" in joined
        # All-zero waiting -> the line is suppressed (pure noise, no op queued).
        assert "Waiting by operation:" not in joined

    def test_waiting_by_operation_renders_when_nonzero(self):
        snap = {
            **_FULL_ADMISSION,
            "qps_limiter": {
                **_FULL_ADMISSION["qps_limiter"],
                "waiting_by_operation": {"resume": 0, "pause": 1, "cleanup": 0, "create": 0, "command": 0},
            },
        }
        joined = _format(_state_with_slices(), admission_snapshot=snap, wall_sec=10.0)
        assert "Waiting by operation:" in joined
        assert "pause=1" in joined

    def test_qps_off_renders_only_running_slots(self):
        snap = {**_FULL_ADMISSION, "qps": "off"}
        snap.pop("qps_limiter")
        joined = _format(_state_with_slices(), admission_snapshot=snap, wall_sec=10.0)
        assert "Running slots:" in joined
        assert "QPS limiter:" not in joined
        assert "Dispatched by operation:" not in joined


class TestThroughputSection:
    def _report(self, *, wall_sec):
        state = _state_with_slices()
        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="lifecycle",
            total_count=2,
            replay_running_concurrency=1,
            test_duration=1,
        )
        f = ReportFormatter(cfg, {0: state}, "fake", wall_sec=wall_sec)
        return "\n".join(f.format_throughput_section())

    def test_throughput_metrics_rendered(self):
        joined = self._report(wall_sec=10.0)
        assert "[Throughput & Overcommit]" in joined
        assert "steps_per_sec:" in joined
        assert "effective_parallelism:" in joined
        assert "exec_wall_utilization:" in joined
        assert "overcommit_ratio:" in joined
        # 3 steps / 10s = 0.3
        assert "0.30" in joined

    def test_zero_wall_renders_na(self):
        joined = self._report(wall_sec=0.0)
        assert "[Throughput & Overcommit]" in joined
        assert "n/a" in joined
        # overcommit_ratio is NOT wall-gated -> still a number
        assert "overcommit_ratio:" in joined
        assert "2.0" in joined  # 2/1


class TestRetryImpactBlock:
    def test_retry_impact_renders_when_events_present(self):
        state = _state_with_slices()
        m = state.replay_metrics
        m.record_retry_event("retry_queued", operation="resume", time_lost_sec=0.05)
        m.append_retries_per_slice(1)
        joined = _format(state, admission_snapshot=_FULL_ADMISSION, wall_sec=10.0)
        assert "Retry impact:" in joined
        assert "retries: 1" in joined or "retries=1" in joined
        assert "time lost" in joined
        assert "retries/slice P95" in joined  # percentile line only when retry_count > 0

    def test_retry_impact_zero_renders_no_percentile(self):
        state = _state_with_slices()  # no retry events seeded
        joined = _format(state, admission_snapshot=_FULL_ADMISSION, wall_sec=10.0)
        assert "Retry impact:" in joined
        assert "retries: 0" in joined or "retries=0" in joined
        assert "time lost" in joined
        # P95 line is gated on retry_count > 0 -> absent here
        assert "retries/slice P95" not in joined


class TestTrajectorySummarySection:
    def _report(self, *, replay_mode, with_create=True):
        state = _state_with_slices()
        m = state.replay_metrics
        if with_create:
            # re-seed slices that carry create_sec/kill_sec (state already has 3 plain slices;
            # append trajectory slices)
            for cs, ks in ((1.0, 0.5), (2.0, 0.6), (3.0, 0.7)):
                m.add(
                    latency=0.1,
                    success=True,
                    action_type="shell",
                    slice_total_sec=1.0,
                    create_sec=cs,
                    kill_sec=ks,
                )
        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode=replay_mode,
            total_count=1,
            replay_running_concurrency=1,
            test_duration=1,
        )
        f = ReportFormatter(cfg, {0: state}, "fake", wall_sec=10.0)
        return "\n".join(f.format_trajectory_summary_section())

    def test_trajectory_mode_renders_summary(self):
        joined = self._report(replay_mode="trajectory")
        assert "[Trajectory Summary]" in joined
        assert "Create sec:" in joined
        assert "Kill sec:" in joined
        assert "P50=" in joined and "P95=" in joined and "P99=" in joined

    def test_lifecycle_mode_skips_summary(self):
        joined = self._report(replay_mode="lifecycle")
        assert joined == ""  # section absent outside trajectory mode

    def test_trajectory_mode_no_create_secs_skips_summary(self):
        joined = self._report(replay_mode="trajectory", with_create=False)
        assert joined == ""
