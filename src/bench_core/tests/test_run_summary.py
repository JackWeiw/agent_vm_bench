"""Unit tests for the run_summary.json writer (replay-only, raw facts).

The writer is a pure renderer of inputs the kernel already holds
(ReplayObservability + admission_snapshot + ReplayMetrics). These tests
construct a StatsCollector with hand-built sandbox states so no provider /
SDK is needed.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bench_core.config import KernelConfig
from bench_core.observability.run_summary import write_run_summary
from bench_core.observability.stats_collector import StatsCollector
from bench_core.schemas import BenchSandbox


def _replay_config(tmp_path: Path, *, mode: str = "exec_only", total: int = 4, n: int | None = None) -> KernelConfig:
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=total,
        benchmark_mode="fixed",
        test_duration=1,
        replay_mode=mode,
        output_dir=str(tmp_path),
        filename_prefix="rs",
    )
    if n is not None:
        cfg.replay_running_concurrency = n
    return cfg


def _stats_with_replay_metrics(config: KernelConfig, *, total: int, succeeded: int) -> StatsCollector:
    """Build a StatsCollector whose one sandbox carries replay metrics.

    Drives exactly ``total`` ``add()`` calls so ``total_tasks == total`` (step
    count); the first ``succeeded`` steps succeed, the rest fail. The last
    step marks ``trajectory_complete`` so the one trajectory is counted as
    completed (``trajectory_completions == 1``). With one sandbox,
    trajectory-level throughput is total=1, succeeded=1, failed=0. Requires
    ``id`` (no default), hence ``id="s0"``.
    """
    state = BenchSandbox(id="s0", index=0, workflow_type="replay")
    for i in range(total):
        state.replay_metrics.add(latency=0.1, success=(i < succeeded), trajectory_complete=(i == total - 1))
    return StatsCollector(config, {0: state}, provider_label="fake")


def test_run_summary_has_required_fields(tmp_path):
    config = _replay_config(tmp_path, total=4)
    sc = _stats_with_replay_metrics(config, total=4, succeeded=4)
    sc.start_time = 1700000000.0  # deterministic epoch
    path = write_run_summary(
        config,
        sc,
        series_path=None,
        obs_xlsx_path=None,
        report_path="r.txt",
        error=None,
    )
    assert path is not None and path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["workflow_type"] == "replay"
    assert data["replay_mode"] == "exec_only"
    assert data["provider"] == "fake"
    assert data["total_count"] == 4
    # Throughput is trajectory-level (spec §4): 1 sandbox -> 1 trajectory
    # attempted, which completed all 4 steps -> succeeded 1, failed 0.
    # total_steps stays step-level (4) -- the two axes are distinct.
    assert data["throughput"]["total"] == 1
    assert data["throughput"]["succeeded"] == 1
    assert data["throughput"]["failed"] == 0
    assert data["throughput"]["total_steps"] == 4
    assert "paths" in data and data["paths"]["report"] == "r.txt"


def test_run_summary_skips_non_replay(tmp_path):
    config = KernelConfig(workflow_type="browser", total_count=2, output_dir=str(tmp_path), filename_prefix="x")
    sc = StatsCollector(config, {}, provider_label="fake")
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    assert path is None
    assert not (tmp_path / "x_run_summary.json").exists()


def test_run_summary_has_no_valid_field(tmp_path):
    """Raw facts only — experiment validity is the driver's job, not the kernel's."""
    config = _replay_config(tmp_path, total=2)
    sc = _stats_with_replay_metrics(config, total=2, succeeded=2)
    sc.start_time = 1700000000.0
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "valid" not in data


def test_run_summary_epoch_and_iso_timestamps(tmp_path):
    config = _replay_config(tmp_path, total=2)
    sc = _stats_with_replay_metrics(config, total=2, succeeded=2)
    sc.start_time = 1700000000.5
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["started_epoch"], float)
    assert isinstance(data["completed_epoch"], float)
    assert data["started_epoch"] == 1700000000.5
    # ISO strings parse back to the same epoch.
    started_dt = datetime.fromisoformat(data["started_at"])
    assert started_dt.timestamp() == 1700000000.5
    assert data["completed_at"]  # non-empty ISO string


def test_run_summary_admission_null_for_exec_only(tmp_path):
    """exec_only has no admission controller -> admission block is null."""
    config = _replay_config(tmp_path, mode="exec_only", total=2)
    sc = _stats_with_replay_metrics(config, total=2, succeeded=2)
    sc.admission_snapshot = None  # exec_only: no controller built
    sc.start_time = 1700000000.0
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["admission"] is None
    assert data["lifecycle_overhead"] is None  # exec_only has no lifecycle lists


def test_run_summary_admission_populated_for_lifecycle(tmp_path):
    config = _replay_config(tmp_path, mode="lifecycle", total=4, n=4)
    sc = _stats_with_replay_metrics(config, total=4, succeeded=4)
    sc.admission_snapshot = {
        "running": 4,
        "total": 4,
        "qps": 1000.0,
        "peak_active": 3,
        "avg_queue_wait_sec": 0.4,
        "running_slots": {
            "maximum": 4,
            "active": 2,
            "peak_active": 3,
            "granted": 4,
            "waiting": 0,
            "average_queue_wait_sec": 0.4,
        },
        "qps_dispatched": 8,
        "qps_limiter": {"dispatched": 8},
    }
    sc.start_time = 1700000000.0
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    adm = data["admission"]
    assert adm["maximum"] == 4
    assert adm["peak_active"] == 3
    assert adm["granted"] == 4
    assert adm["avg_queue_wait_sec"] == 0.4
    assert adm["control_qps"] == 1000.0
    assert adm["control_dispatched"] == 8


def test_run_summary_steps_per_sec_consumes_replay_observability(tmp_path):
    """The writer must consume ReplayObservability, not recompute an independent path."""
    from bench_core.observability.replay_obs import ReplayObservability

    config = _replay_config(tmp_path, total=2)
    sc = _stats_with_replay_metrics(config, total=2, succeeded=2)
    sc.start_time = 1700000000.0
    write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    # Same inputs -> ReplayObservability.steps_per_sec must equal the filed value.
    wall = sc._resolved_wall_sec()
    obs = ReplayObservability(config, sc.sandbox_states, admission_snapshot=sc.admission_snapshot, wall_sec=wall)
    data = json.loads((tmp_path / "rs_run_summary.json").read_text(encoding="utf-8"))
    assert data["throughput"]["steps_per_sec"] == round(obs.steps_per_sec, 3)
    assert data["throughput"]["total_steps"] == obs.total_steps


def test_run_summary_wall_sec_none_nils_rates(tmp_path):
    """No measured wall -> both rate fields nil (consistent single-wall policy)."""
    config = _replay_config(tmp_path, total=2)
    sc = _stats_with_replay_metrics(config, total=2, succeeded=2)
    # start_time left at default 0.0 -> _resolved_wall_sec() returns None
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["wall_sec"] is None
    assert data["throughput"]["steps_per_sec"] is None
    assert data["throughput"]["tasks_per_sec"] is None


def test_run_summary_lifecycle_overhead_with_real_slices(tmp_path):
    """Exercise the lifecycle_overhead pct arithmetic + the MIN_SLICE_SEC guard."""
    config = _replay_config(tmp_path, mode="lifecycle", total=1, n=1)
    state = BenchSandbox(id="s0", index=0, workflow_type="replay")
    state.replay_metrics.add(latency=0.1, success=True, resume_sec=0.02, pause_sec=0.01, slice_total_sec=1.0)
    # Pathological sub-MIN_SLICE slice: appended (0.0001 > 0) then dropped by the guard.
    state.replay_metrics.add(latency=0.0, success=True, resume_sec=0.001, pause_sec=0.001, slice_total_sec=0.0001)
    sc = StatsCollector(config, {0: state}, provider_label="fake")
    sc.admission_snapshot = None
    sc.start_time = 1700000000.0
    path = write_run_summary(config, sc, series_path=None, obs_xlsx_path=None, report_path=None)
    data = json.loads(path.read_text(encoding="utf-8"))
    lo = data["lifecycle_overhead"]
    assert lo is not None
    assert lo["resume_sec_sum"] == 0.02
    assert lo["pause_sec_sum"] == 0.01
    assert lo["pct_of_slice_total"] == 3.0  # (0.02+0.01)/1.0*100; sub-MIN slice excluded


# ---- end-to-end: run_benchmark actually emits run_summary.json ----


def _seed_traj_pool(tmp_path):
    """Two minimal trajectories under tmp_path/traj (FakeProvider exec_only)."""
    import json as _json

    traj = tmp_path / "traj"
    traj.mkdir()
    (traj / "a.replay.json").write_text(
        _json.dumps({"instance_id": "a", "trajectory": [{"action": "echo a", "delay_time": 0}]}),
        encoding="utf-8",
    )
    (traj / "b.replay.json").write_text(
        _json.dumps({"instance_id": "b", "trajectory": [{"action": "echo b", "delay_time": 0}]}),
        encoding="utf-8",
    )


def test_run_benchmark_emits_run_summary_exec_only(tmp_path):
    from bench_core.bench import run_benchmark
    from bench_core.payload.replay_payload import reset_pool_cache
    from env_provider.fake import FakeProvider

    _seed_traj_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=2,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_mode="exec_only",
        replay_delay_scale=0.0,
        output_dir=str(tmp_path),
        filename_prefix="e2e",
    )
    run_benchmark(cfg, FakeProvider(count=2))
    hits = list(tmp_path.glob("e2e_run_summary.json"))
    assert len(hits) == 1, f"expected one run_summary.json, got {hits}"
    data = json.loads(hits[0].read_text(encoding="utf-8"))
    assert data["workflow_type"] == "replay"
    assert data["replay_mode"] == "exec_only"
    assert data["admission"] is None  # exec_only builds no admission controller
    assert data["test_duration"] == cfg.test_duration


def test_run_benchmark_emits_run_summary_lifecycle(tmp_path):
    from bench_core.bench import run_benchmark
    from bench_core.payload.replay_payload import reset_pool_cache
    from env_provider.tests.lifecycle_fake import FakeLifecycleProvider

    _seed_traj_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        replay_running_concurrency=2,  # k=2 oversubscription -> admission built
        benchmark_mode="round_robin",
        round_size=4,
        round_count=1,
        round_interval=0,
        test_duration=2,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_mode="lifecycle",
        replay_delay_scale=0.0,
        replay_control_plane_qps=1000.0,
        output_dir=str(tmp_path),
        filename_prefix="lc",
    )
    run_benchmark(cfg, FakeLifecycleProvider(count=4))
    hits = list(tmp_path.glob("lc_run_summary.json"))
    assert len(hits) == 1
    data = json.loads(hits[0].read_text(encoding="utf-8"))
    assert data["replay_mode"] == "lifecycle"
    assert data["running_concurrency"] == 2
    assert data["overcommit_ratio"] == 2.0
    assert data["admission"] is not None  # lifecycle k=2 + qps -> admission built
    assert data["admission"]["maximum"] == 2
    assert data["lifecycle_overhead"] is not None
    assert data["test_duration"] == cfg.test_duration
