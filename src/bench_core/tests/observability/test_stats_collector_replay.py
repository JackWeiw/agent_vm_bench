"""Phase 3: enriched admission block + [Throughput & Overcommit] rendering."""
from __future__ import annotations

import logging
import time

from bench_core.config import KernelConfig
from bench_core.observability.stats_collector import ReportFormatter, StatsCollector
from bench_core.schemas import BenchSandbox, ReplayMetrics
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
    "inflight_cap": 4,
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
        "inflight_dispatched": 8,
        "average_inflight_wait_sec": 0.0005,
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
        # Rate pacing + inflight fuse are now separate sub-lines (decoupled):
        # rate pacing shows dispatch/wait; inflight fuse shows cap/in_flight.
        assert "Rate pacing:" in joined
        assert "dispatched=12" in joined
        assert "Dispatched by operation:" in joined
        assert "resume=3" in joined and "command=3" in joined
        assert "Inflight fuse:" in joined
        assert "cap=4" in joined
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
        snap = {**_FULL_ADMISSION, "qps": "off", "inflight_cap": "off"}
        snap.pop("qps_limiter")
        joined = _format(_state_with_slices(), admission_snapshot=snap, wall_sec=10.0)
        assert "Running slots:" in joined
        assert "Rate pacing:" not in joined
        assert "Inflight fuse:" not in joined
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


class TestOnePassTargetReport:
    """The report's 'One-pass Target' must be the fleet size (one
    trajectory/sandbox/round), not the meaningless ``pool x fleet`` product.
    """

    def _report(self, *, total_count, round_count=1):
        state = _state_with_slices()
        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="lifecycle",
            total_count=total_count,
            replay_running_concurrency=1,
            test_duration=1,
            round_count=round_count,
        )
        f = ReportFormatter(cfg, {0: state}, "fake")
        return "\n".join(f.format_replay_stats_section())

    def test_target_is_fleet_size_not_pool_times_fleet(self):
        joined = self._report(total_count=4)
        line = [ln for ln in joined.splitlines() if "One-pass Target:" in ln]
        assert line, "One-pass Target line missing"
        assert "4 (1 trajectory/sandbox per round" in line[0]
        assert "x fleet" not in line[0]  # old pool*fleet product must be gone

    def test_pool_note_shown_when_pool_resolvable(self, monkeypatch):
        import bench_core.observability.stats_collector as mod

        # pool(401) > fleet(384): the user's 1:1 aenv run. Target stays 384
        # (one trajectory/sandbox), pool is context only -- not a multiplier.
        monkeypatch.setattr(mod, "replay_pool_size", lambda cfg: 401)
        joined = self._report(total_count=384)
        line = [ln for ln in joined.splitlines() if "One-pass Target:" in ln][0]
        assert "384 (1 trajectory/sandbox per round; pool 401 distinct)" in line


class TestReplaySnapshotTrajDenominator:
    """Live snapshot ``traj=done/total`` denominator = round_count * total_count
    (cumulative ceiling), 0 (bare count) when sustained."""

    def _snapshot(self, *, total_count, round_count, completions=2):
        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="lifecycle",
            total_count=total_count,
            replay_running_concurrency=total_count,
            test_duration=60,
            round_count=round_count,
        )
        state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
        for _ in range(completions):
            state.replay_metrics.add(0.1, True, trajectory_complete=True)
        sc = StatsCollector(cfg, {0: state})
        sc.start_time = time.time()  # set without spawning the collect thread
        sc._take_snapshot()
        return sc.snapshots[-1]

    def test_bounded_single_round_denominator_is_fleet(self):
        snap = self._snapshot(total_count=4, round_count=1)
        assert snap.replay_traj_done == 2
        assert snap.replay_total_trajs == 4  # 1 round * 4 fleet, NOT pool*fleet

    def test_multi_round_denominator_scales(self):
        snap = self._snapshot(total_count=4, round_count=3)
        assert snap.replay_total_trajs == 12  # 3 rounds * 4 fleet

    def test_sustained_denominator_is_zero(self):
        # round_count=None (default) -> sustained-until-duration, no fixed ceiling
        snap = self._snapshot(total_count=4, round_count=None)
        assert snap.replay_total_trajs == 0


class TestReplaySnapshotTrajPrint:
    """The printed ``traj=`` line shows a ratio when bounded, a bare count when sustained."""

    def _take_and_msgs(self, *, total_count, round_count, caplog):
        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="lifecycle",
            total_count=total_count,
            replay_running_concurrency=total_count,
            test_duration=60,
            round_count=round_count,
        )
        state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
        state.replay_metrics.add(0.1, True, trajectory_complete=True)
        sc = StatsCollector(cfg, {0: state})
        sc.start_time = time.time()
        with caplog.at_level(logging.INFO):
            sc._take_snapshot()
        return [r.message for r in caplog.records if "traj=" in r.message]

    def test_bounded_prints_ratio(self, caplog):
        msgs = self._take_and_msgs(total_count=4, round_count=1, caplog=caplog)
        assert msgs, "no traj= line captured"
        assert "traj=1/4" in msgs[0]

    def test_sustained_prints_bare_count(self, caplog):
        msgs = self._take_and_msgs(total_count=4, round_count=None, caplog=caplog)
        assert msgs, "no traj= line captured"
        assert "traj=1/" not in msgs[0]  # no denominator when sustained
        assert "traj=1 " in msgs[0]  # bare count then column gap
