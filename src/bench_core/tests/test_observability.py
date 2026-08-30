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
