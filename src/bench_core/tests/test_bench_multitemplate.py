"""Tests for bench.py multi-template template-map wiring (Task 6).

Verifies that ``run_benchmark`` computes a round-robin ``templates`` map from
the trajectory pool and threads it into ``provider.create_all(templates=...)``
when a replay template manifest is configured.
"""
from __future__ import annotations

import json
from pathlib import Path

from bench_core.bench import _replay_template_map, run_benchmark
from bench_core.config import KernelConfig
from bench_core.payload.replay_payload import reset_pool_cache
from env_provider.fake import FakeProvider


def _seed_pool(tmp_path: Path) -> Path:
    """Write two minimal trajectories + a manifest; return the manifest path."""
    traj = tmp_path / "traj"
    traj.mkdir()
    (traj / "a.replay.json").write_text(
        json.dumps({"instance_id": "a", "trajectory": [{"action": "echo a", "delay_time": 0}]}),
        encoding="utf-8",
    )
    (traj / "b.replay.json").write_text(
        json.dumps({"instance_id": "b", "trajectory": [{"action": "echo b", "delay_time": 0}]}),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"a.replay.json": "swb-a", "b.replay.json": "swb-b"}),
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# _replay_template_map (focused unit test — no spine needed)
# ---------------------------------------------------------------------------


def test_replay_template_map_round_robin(tmp_path):
    """Round-robin the pool's templates over total_count slots."""
    manifest = _seed_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_template_manifest=str(manifest),
        replay_mode="exec_only",
        output_dir=str(tmp_path),
        filename_prefix="mt",
    )
    result = _replay_template_map(cfg)
    assert result == {0: "swb-a", 1: "swb-b", 2: "swb-a", 3: "swb-b"}


def test_replay_template_map_none_when_no_manifest(tmp_path):
    """No manifest -> None (legacy single-template path)."""
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_mode="exec_only",
        output_dir=str(tmp_path),
        filename_prefix="mt",
    )
    assert _replay_template_map(cfg) is None


def test_replay_template_map_none_for_non_replay(tmp_path):
    """Non-replay workflow -> None (guard; bench.py skips the call anyway)."""
    cfg = KernelConfig(
        workflow_type="browser",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        output_dir=str(tmp_path),
        filename_prefix="mt",
    )
    assert _replay_template_map(cfg) is None


# ---------------------------------------------------------------------------
# End-to-end: run_benchmark threads templates= into create_all
# ---------------------------------------------------------------------------


def test_run_benchmark_passes_template_map_to_create_all(tmp_path):
    manifest = _seed_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_template_manifest=str(manifest),
        replay_mode="exec_only",
        replay_delay_scale=0.0,
        output_dir=str(tmp_path),
        filename_prefix="mt",
    )
    provider = FakeProvider(count=4)
    run_benchmark(cfg, provider)
    # Sandbox 0..3 templates: round-robin over pool [swb-a, swb-b] -> a,b,a,b
    seen = [provider._instances[i].template for i in range(4)]
    assert seen == ["swb-a", "swb-b", "swb-a", "swb-b"]
