"""Phase 3: ReplayObservability data model (pure, no I/O)."""
from __future__ import annotations

import pytest

from bench_core.config import KernelConfig
from bench_core.observability import ReplayObservability
from bench_core.schemas import BenchSandbox, ReplayMetrics
from env_provider import SandboxInstance


def _state_with_metrics() -> BenchSandbox:
    """One sandbox with 3 seeded slices: resume=0.05, pause=0.05, slice_total=1.0,
    running_slot_held=0.8."""
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
        )
    state.replay_metrics = m
    return state


def test_throughput_metrics():
    state = _state_with_metrics()
    cfg = KernelConfig(workflow_type="replay", total_count=2, replay_running_concurrency=1)
    obs = ReplayObservability(cfg, {0: state}, wall_sec=10.0)
    assert obs.total_steps == 3
    assert obs.steps_per_sec == pytest.approx(0.3)  # 3 / 10
    assert obs.effective_parallelism == pytest.approx(0.24)  # (3 * 0.8) / 10
    assert obs.exec_wall_utilization == pytest.approx(0.27)  # (3 * (1.0-0.05-0.05)) / (10*1)
    assert obs.overcommit_ratio == 2.0  # 2 / 1


def test_zero_wall_skips_throughput():
    state = _state_with_metrics()
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    obs = ReplayObservability(cfg, {0: state}, wall_sec=0.0)
    assert obs.steps_per_sec is None
    assert obs.effective_parallelism is None
    assert obs.exec_wall_utilization is None
    # overcommit_ratio does NOT depend on wall_sec
    assert obs.overcommit_ratio == 1.0  # total_count=1, concurrency defaults to total_count


def test_wall_sec_defaults_to_test_duration():
    """When wall_sec not passed, fall back to config.test_duration."""
    state = _state_with_metrics()
    cfg = KernelConfig(workflow_type="replay", total_count=1, test_duration=5)
    obs = ReplayObservability(cfg, {0: state})
    assert obs.wall_sec == 5.0
    assert obs.steps_per_sec == pytest.approx(0.6)  # 3 / 5


def test_empty_states_safe():
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    obs = ReplayObservability(cfg, {}, wall_sec=10.0)
    assert obs.total_steps == 0
    assert obs.steps_per_sec == 0.0
    assert obs.overcommit_ratio == 1.0


def test_retry_impact_properties():
    state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
    m = ReplayMetrics()
    # seed one normal slice so the lists populate
    m.add(
        latency=0.1,
        success=True,
        action_type="shell",
        resume_sec=0.05,
        pause_sec=0.05,
        slice_total_sec=1.0,
    )
    # seed retry events via the Task-1 accumulators
    m.record_retry_event("retry_queued", operation="resume", time_lost_sec=0.05)
    m.record_retry_event("retry_queued", operation="resume", time_lost_sec=0.03)
    m.record_retry_event("retry_queued", operation="pause", time_lost_sec=0.02)
    m.append_retries_per_slice(2)  # this slice had 2 retries
    m.append_retries_per_slice(0)
    state.replay_metrics = m
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    obs = ReplayObservability(cfg, {0: state})
    assert obs.retry_count == 3
    assert obs.retry_count_by_op == {"resume": 2, "pause": 1}
    assert obs.time_lost_to_retry_sec == pytest.approx(0.10)
    assert obs.retries_per_slice_p95 == pytest.approx(2.0)  # [2,0] -> p95=2


def test_retry_impact_zero_when_no_events():
    state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
    state.replay_metrics = ReplayMetrics()
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    obs = ReplayObservability(cfg, {0: state})
    assert obs.retry_count == 0
    assert obs.retry_count_by_op == {}
    assert obs.time_lost_to_retry_sec == 0.0
    assert obs.retries_per_slice_p95 == 0.0  # empty list -> calc_percentiles returns 0.0


def test_trajectory_summary_stats():
    state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
    m = ReplayMetrics()
    # seed trajectory slices with create_sec/kill_sec
    for cs, ks in ((1.0, 0.5), (2.0, 0.6), (3.0, 0.7)):
        m.add(
            latency=0.1,
            success=True,
            action_type="shell",
            slice_total_sec=1.0,
            create_sec=cs,
            kill_sec=ks,
        )
    state.replay_metrics = m
    cfg = KernelConfig(workflow_type="replay", replay_mode="trajectory", total_count=1)
    obs = ReplayObservability(cfg, {0: state})
    cs_stats = obs.create_sec_stats
    assert cs_stats["min"] == 1.0 and cs_stats["max"] == 3.0
    assert cs_stats["p50"] == 2.0
    ks_stats = obs.kill_sec_stats
    assert ks_stats["min"] == 0.5 and ks_stats["max"] == 0.7


def test_trajectory_stats_empty_when_no_create_secs():
    state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
    state.replay_metrics = ReplayMetrics()  # no slices -> empty create_secs
    cfg = KernelConfig(workflow_type="replay", replay_mode="trajectory", total_count=1)
    obs = ReplayObservability(cfg, {0: state})
    assert obs.create_sec_stats == {"min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    assert obs.kill_sec_stats["p50"] == 0.0
