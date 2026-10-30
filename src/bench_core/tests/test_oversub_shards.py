"""Unit + stub-e2e tests for process-sharded oversub trials (oversub_shards).

Mechanism units (planning, per-shard config, merge algebra, group supervision)
are pure/stdlib-only; the e2e test drives ``oversub.main`` with a stub
bench-core (the established ``--bench-core-bin`` pattern) so no kernel, SDK,
or host is needed.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from bench_core.oversub import main
from bench_core.oversub_shards import (
    GroupResult,
    ShardArtifacts,
    ShardGroupRunner,
    build_shard_command,
    build_shard_config,
    coerce_shards,
    merge_run_summaries,
    merge_trajectory_indexes,
    plan_shards,
    resolve_shard_count,
    shard_sizes,
    write_merged_artifacts,
    write_shard_configs,
)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_coerce_shards_forms():
    assert coerce_shards("auto") == "auto"
    assert coerce_shards("AUTO") == "auto"
    assert coerce_shards("4") == 4
    assert coerce_shards(4) == 4
    with pytest.raises(ValueError):
        coerce_shards(0)
    with pytest.raises(ValueError):
        coerce_shards("lots")
    with pytest.raises(ValueError):
        coerce_shards(True)


def test_resolve_shard_count_auto_follows_ratio_for_shardable_modes():
    assert resolve_shard_count("auto", mode="exec_ephemeral", ratio=10, n=384) == 10
    assert resolve_shard_count("auto", mode="exec_only", ratio=3, n=4) == 3


def test_resolve_shard_count_global_admission_modes_never_shard():
    # lifecycle/trajectory enforce a GLOBAL running_concurrency; per-shard
    # admission would over-admit to P*N, so they collapse to single-process.
    assert resolve_shard_count("auto", mode="lifecycle", ratio=8, n=384) == 1
    assert resolve_shard_count("auto", mode="trajectory", ratio=8, n=384) == 1
    assert resolve_shard_count(4, mode="lifecycle", ratio=8, n=384) == 1  # explicit downgraded


def test_resolve_shard_count_explicit_and_capped():
    assert resolve_shard_count(2, mode="exec_ephemeral", ratio=4, n=8) == 2
    assert resolve_shard_count(1, mode="exec_ephemeral", ratio=4, n=8) == 1
    # capped at the fleet size: every shard needs >= 1 slot
    assert resolve_shard_count(100, mode="exec_ephemeral", ratio=2, n=4) == 8


def test_shard_sizes_balanced_remainder_first():
    assert shard_sizes(8, 2) == [4, 4]
    assert shard_sizes(10, 3) == [4, 3, 3]
    assert shard_sizes(7, 7) == [1] * 7


def test_plan_shards_none_for_single_process_and_offsets_cumulative(tmp_path):
    assert plan_shards(trial_dir=tmp_path, mode="lifecycle", ratio=4, n=8, setting="auto") is None
    assert plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=1, n=8, setting="auto") is None
    assert plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting=1) is None

    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=5, setting="auto")
    assert plan.count == 2
    assert [s.size for s in plan.specs] == [5, 5]
    assert [s.slot_offset for s in plan.specs] == [0, 5]
    assert plan.specs[0].directory == tmp_path / "shard-00"
    assert plan.specs[1].log_path == tmp_path / "shard-01" / "driver.log"


# ---------------------------------------------------------------------------
# Per-shard config + command
# ---------------------------------------------------------------------------


def _trial_cfg() -> dict:
    return {
        "sandbox": {"total_count": 8},
        "replay": {"running_concurrency": 4, "trajectory_limit": 4, "mode": "exec_ephemeral", "multiplier": 2},
        "test": {"round_size": 8, "duration": 60},
        "report": {"output_dir": "trial", "filename_prefix": "rb"},
        "docker": {"image": "img", "container_prefix": "oc-bench"},
    }


def test_build_shard_config_derives_self_contained_shard(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    cfg = build_shard_config(_trial_cfg(), plan.specs[1], count=plan.count)
    assert cfg["sandbox"]["total_count"] == 4  # shard share, not k*N
    assert cfg["test"]["round_size"] == 4
    assert cfg["report"]["output_dir"] == str(tmp_path / "shard-01")
    # global-by-design knobs stay untouched
    assert cfg["replay"]["trajectory_limit"] == 4
    assert cfg["replay"]["running_concurrency"] == 4
    assert cfg["replay"]["multiplier"] == 2
    # docker names get a shard suffix (keeps startswith(base) for cleanup reach)
    assert cfg["docker"]["container_prefix"] == "oc-bench-s1"
    assert cfg["docker"]["container_prefix"].startswith("oc-bench")


def test_build_shard_config_scales_launch_interval_to_preserve_global_rate(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=3, n=4, setting="auto")
    cfg_in = _trial_cfg()
    cfg_in["replay"]["launch_interval_sec"] = 0.5
    cfg = build_shard_config(cfg_in, plan.specs[0], count=plan.count)
    assert cfg["replay"]["launch_interval_sec"] == pytest.approx(1.5)  # P * interval


def test_write_shard_configs_materializes_one_yaml_per_shard(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    paths = write_shard_configs(_trial_cfg(), plan)
    assert paths == [tmp_path / "shard-00" / "shard.yaml", tmp_path / "shard-01" / "shard.yaml"]
    for p in paths:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert loaded["sandbox"]["total_count"] == 4


def test_build_shard_command_vm_monitor_owned_by_shard_zero(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    cmd0 = build_shard_command(["bench-core"], plan.specs[0], provider="aenv", no_vm_monitor=False)
    cmd1 = build_shard_command(["bench-core"], plan.specs[1], provider="aenv", no_vm_monitor=False)
    assert "--no-vm-monitor" not in cmd0
    assert "--no-vm-monitor" in cmd1
    assert cmd0[:5] == ["bench-core", "--config", str(plan.specs[0].config_path), "--provider", "aenv"]
    # global opt-out applies to every shard
    cmd0n = build_shard_command(["bench-core"], plan.specs[0], provider="aenv", no_vm_monitor=True)
    assert "--no-vm-monitor" in cmd0n


# ---------------------------------------------------------------------------
# Merge algebra
# ---------------------------------------------------------------------------


def _shard_summary(**over) -> dict:
    s = {
        "schema_version": 2,
        "workflow_type": "replay",
        "replay_mode": "exec_ephemeral",
        "provider": "aenv",
        "started_at": "2026-10-30T10:00:00+08:00",
        "completed_at": "2026-10-30T10:18:00+08:00",
        "started_epoch": 1000.0,
        "completed_epoch": 2080.0,
        "test_duration": 3600,
        "wall_sec": 1080.0,
        "total_count": 4,
        "running_concurrency": 4,
        "overcommit_ratio": 1.0,
        "throughput": {
            "total": 8,
            "succeeded": 8,
            "failed": 0,
            "total_steps": 40,
            "steps_per_sec": 0.037,
            "tasks_per_sec": 0.0074,
        },
        "admission": None,
        "lifecycle_overhead": None,
        "paths": {"trajectory_index": "/x/trajectories/index.json", "vm_monitor_dir": None},
        "error": None,
    }
    s.update(over)
    return s


def _plan2(tmp_path):
    return plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")


def test_merge_run_summaries_counts_sum_rates_recompute_span_envelope(tmp_path):
    plan = _plan2(tmp_path)
    s0 = _shard_summary()
    s1 = _shard_summary(started_epoch=1005.0, started_at="2026-10-30T10:00:05+08:00", wall_sec=1075.0)
    artifacts = ShardArtifacts(summaries=[s0, s1], summary_paths=["/a.json", "/b.json"])
    result = GroupResult(returncodes=[0, 0], interrupted=False, timed_out=False)
    merged = merge_run_summaries([s0, s1], plan=plan, artifacts=artifacts, result=result)

    assert merged["throughput"]["total"] == 16
    assert merged["throughput"]["succeeded"] == 16
    assert merged["throughput"]["total_steps"] == 80
    assert merged["wall_sec"] == 1080.0  # max across concurrent shards
    # rates recomputed over the merged wall, NOT summed per-shard rates
    assert merged["throughput"]["tasks_per_sec"] == round(16 / 1080.0, 3)
    assert merged["throughput"]["steps_per_sec"] == round(80 / 1080.0, 3)
    assert merged["started_epoch"] == 1000.0 and merged["started_at"] == s0["started_at"]
    assert merged["completed_epoch"] == 2080.0
    assert merged["shard_start_skew_sec"] == 5.0
    assert merged["total_count"] == 8  # k*N echo restored
    assert merged["overcommit_ratio"] == 2.0  # 8 slots / N=4
    assert merged["admission"] is None and merged["lifecycle_overhead"] is None
    assert merged["error"] is None
    assert merged["shard_count"] == 2 and merged["shard_return_codes"] == [0, 0]
    assert merged["shard_run_summaries"] == ["/a.json", "/b.json"]


def test_merge_run_summaries_missing_and_failed_shards_surface_in_error(tmp_path):
    plan = _plan2(tmp_path)
    s0 = _shard_summary(error="create pool exhausted")
    artifacts = ShardArtifacts(summaries=[s0, None], summary_paths=["/a.json", None])
    result = GroupResult(returncodes=[0, 1], interrupted=False, timed_out=False)
    merged = merge_run_summaries([s0, None], plan=plan, artifacts=artifacts, result=result)
    assert merged["error"]
    assert "shard 1: no run_summary.json" in merged["error"]
    assert "shard 1: exit code 1" in merged["error"]
    assert "create pool exhausted" in merged["error"]
    assert merged["throughput"]["succeeded"] == 8  # present shard only; never backfilled
    assert result.trial_return_code() == 1


def test_merge_run_summaries_nothing_present_yields_minimal_invalid_shape(tmp_path):
    plan = _plan2(tmp_path)
    artifacts = ShardArtifacts(summaries=[None, None], summary_paths=[None, None])
    result = GroupResult(returncodes=[-9, -9], interrupted=False, timed_out=False)
    merged = merge_run_summaries([None, None], plan=plan, artifacts=artifacts, result=result)
    assert merged["throughput"] == {} and merged["error"]


def test_group_result_interrupt_maps_to_130():
    assert GroupResult([0, 0], interrupted=True, timed_out=False).trial_return_code() == 130
    assert GroupResult([0, 0], interrupted=False, timed_out=False).trial_return_code() == 0


def _catalog(sandbox_index: int, *, runs_shape: bool = True) -> dict:
    run = {
        "sandbox_index": sandbox_index,
        "round_id": 0,
        "n_steps": 2,
        "n_failed": 0,
        "n_timeout": 0,
        "success_rate": 1.0,
        "elapsed_sec": 3.0,
        "time_breakdown_sec": {"exec": 1.0},
        "create_error_type": None,
        "kill_error_type": None,
    }
    entry = {"trajectory_id": "t0", "action_series": "echo hi", "file": "t0/replay_result.json"}
    if runs_shape:
        entry.update({"n_runs": 1, "n_success": 1, "n_failed": 0, "elapsed_sec": 3.0, "runs": [run]})
    else:
        entry.update(run)  # legacy flat shape: the entry IS the run
    return {"n_trajectories": 1, "trajectories": [entry]}


def test_merge_trajectory_indexes_groups_by_tid_and_offsets_sandbox_index():
    merged = merge_trajectory_indexes([(0, _catalog(0)), (4, _catalog(0))])
    assert merged["n_trajectories"] == 1
    entry = merged["trajectories"][0]
    assert entry["trajectory_id"] == "t0"
    assert entry["n_runs"] == 2 and entry["n_success"] == 2 and entry["n_failed"] == 0
    assert [r["sandbox_index"] for r in entry["runs"]] == [0, 4]  # run keys stay unique
    assert entry["action_series"] == "echo hi"  # scalar inherited once
    assert entry.get("sandbox_index") is None  # run-scoped keys never leak to entry scalars


def test_merge_trajectory_indexes_promotes_legacy_flat_entries():
    merged = merge_trajectory_indexes([(0, _catalog(1, runs_shape=False))])
    entry = merged["trajectories"][0]
    assert entry["n_runs"] == 1
    assert entry["runs"][0]["sandbox_index"] == 1
    assert entry["runs"][0]["n_steps"] == 2


def test_write_merged_artifacts_fixed_path_and_index_patching(tmp_path):
    summary = {"throughput": {"total": 1}, "paths": {"trajectory_index": None}}
    index = {"n_trajectories": 1, "trajectories": []}
    path = write_merged_artifacts(tmp_path, "rb", summary, index)
    assert path == tmp_path / "merged" / "rb_run_summary.json"
    assert json.loads(path.read_text(encoding="utf-8"))["paths"]["trajectory_index"].endswith(
        str(Path("merged") / "trajectories" / "index.json")
    )
    assert (tmp_path / "merged" / "trajectories" / "index.json").exists()


# ---------------------------------------------------------------------------
# Group supervision (real subprocesses, stdlib-only payloads)
# ---------------------------------------------------------------------------


def test_shard_group_runner_collects_all_return_codes(tmp_path):
    cmds = [
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        [sys.executable, "-c", "import sys; sys.exit(3)"],
    ]
    result = ShardGroupRunner(cmds, [tmp_path / "a.log", tmp_path / "b.log"], 60).run()
    assert result.returncodes == [0, 3]
    assert not result.interrupted and not result.timed_out
    assert result.trial_return_code() == 3


def test_shard_group_runner_deadline_terminates_whole_group(tmp_path):
    # One fast shard, one sleeper: the shared deadline must tear down BOTH
    # (an orphaned shard would keep stressing the host).
    cmds = [
        [sys.executable, "-c", "pass"],
        [sys.executable, "-c", "import time; time.sleep(60)"],
    ]
    started = time.monotonic()
    result = ShardGroupRunner(cmds, [tmp_path / "a.log", tmp_path / "b.log"], 2).run()
    assert time.monotonic() - started < 40  # killed, not waited out
    assert result.timed_out
    assert all(rc != 0 for rc in result.returncodes[1:])


# ---------------------------------------------------------------------------
# Stub end-to-end: main() -> sharded trial -> merged contract artifacts
# ---------------------------------------------------------------------------

_SHARD_STUB = """import json, sys, time
from pathlib import Path
import yaml
cfg = yaml.safe_load(open(sys.argv[sys.argv.index("--config") + 1]))
pfx = cfg["report"]["filename_prefix"]
out = Path(cfg["report"]["output_dir"])
out.mkdir(parents=True, exist_ok=True)
(out / "argv.json").write_text(json.dumps(sys.argv[1:]))
run_dir = out / f"{pfx}_{time.strftime('%Y%m%d-%H%M%S')}"
(run_dir / "trajectories").mkdir(parents=True, exist_ok=True)
total = cfg["sandbox"]["total_count"]
m = (cfg.get("replay") or {}).get("multiplier") or 1
summary = {
  "schema_version": 2, "workflow_type": "replay",
  "replay_mode": cfg["replay"]["mode"], "provider": "stub",
  "started_at": "2026-10-30T10:00:00+08:00", "completed_at": "2026-10-30T10:18:00+08:00",
  "started_epoch": 1000.0, "completed_epoch": 2080.0,
  "test_duration": cfg["test"]["duration"], "wall_sec": 1080.0,
  "total_count": total, "running_concurrency": cfg["replay"]["running_concurrency"],
  "overcommit_ratio": 1.0,
  "throughput": {"total": total * m, "succeeded": total * m, "failed": 0,
                 "total_steps": 10 * m, "steps_per_sec": 0.01, "tasks_per_sec": 0.7},
  "admission": None, "lifecycle_overhead": None,
  "paths": {"report": None, "obs_xlsx": None, "lifecycle_series": None,
            "trajectory_index": None, "vm_monitor_dir": None},
  "error": None,
}
(run_dir / f"{pfx}_run_summary.json").write_text(json.dumps(summary, indent=2) + "\\n")
run = {"sandbox_index": 0, "round_id": 0, "n_steps": 1, "n_failed": 0, "n_timeout": 0,
       "success_rate": 1.0, "elapsed_sec": 1.0,
       "time_breakdown_sec": {"slice_total": 0.8, "exec": 0.5, "create": 0.05, "kill": 0.05,
                              "stress": 0.9},
       "create_error_type": None, "kill_error_type": None}
idx = {"n_trajectories": 1, "trajectories": [
  {"trajectory_id": "t0", "n_runs": 1, "n_success": 1, "n_failed": 0, "elapsed_sec": 1.0,
   "action_series": "echo hi", "file": "t0/replay_result.json", "runs": [run]}]}
(run_dir / "trajectories" / "index.json").write_text(json.dumps(idx, indent=2) + "\\n")
"""


def _base_yaml(tmp_path) -> Path:
    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "workflow_type": "replay",
                "sandbox": {"total_count": 4},
                "replay": {"running_concurrency": 4, "mode": "exec_ephemeral"},
                "test": {"duration": 60, "round_size": 4, "round_count": 1, "benchmark_mode": "fixed"},
                "report": {"output_dir": "results/replay", "filename_prefix": "rb", "format": "both"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return base


def _common_argv(tmp_path, base: Path, out_root: Path, stub: Path) -> list[str]:
    return [
        "--config",
        str(base),
        "--provider",
        "aenv",
        "--output-root",
        str(out_root),
        "--bench-core-bin",
        sys.executable,
        str(stub),
        "--cooldown-sec",
        "0",
        "--cleanup-between-trials",
        "off",
    ]


def test_e2e_sharded_trial_merges_into_contract_artifacts(tmp_path):
    """ratio 2 x M 2 -> P=2 shards x 4 slots; merged summary passes M*k*N validity."""
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + [
            "--ratios",
            "2",
            "--modes",
            "exec_ephemeral",
            "--multiplier",
            "2",
            "--shards",
            "auto",
            "--test-duration",
            "60",
        ]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"

    # Per-shard layout + vm_monitor ownership (shard 0 monitors, shard 1 doesn't).
    argv0 = json.loads((trial_dir / "shard-00" / "argv.json").read_text(encoding="utf-8"))
    argv1 = json.loads((trial_dir / "shard-01" / "argv.json").read_text(encoding="utf-8"))
    assert "--no-vm-monitor" not in argv0
    assert "--no-vm-monitor" in argv1

    # Merged summary: the schema-2 contract, with shard provenance attached.
    merged_path = trial_dir / "merged" / "rb_run_summary.json"
    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    assert merged["shard_count"] == 2
    assert merged["throughput"]["succeeded"] == 16  # M*k*N = 2*2*4
    assert merged["throughput"]["total"] == 16
    assert merged["total_count"] == 8  # k*N echo

    # Driver outputs see ONE trial row, valid, keyed to the merged artifacts.
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["valid"] == "True" and rows[0]["succeeded"] == "16" and rows[0]["target_count"] == "8"
    assert rows[0]["run_summary_path"].endswith(str(merged_path))

    # trajectory-detail: per-run rows with re-keyed sandbox_index (0 and 4).
    with open(out_root / "trajectory-detail.csv", newline="", encoding="utf-8") as f:
        trj = list(csv.DictReader(f))
    assert len(trj) == 2
    assert sorted(int(r["sandbox_index"]) for r in trj) == [0, 4]
    assert all(r["trajectory_id"] == "t0" and r["exec_sec"] == "0.5" for r in trj)


def test_e2e_explicit_shards_override_and_cap(tmp_path):
    """--shards 4 on ratio 2 (8 slots) -> 4 shards x 2 slots."""
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "2", "--modes", "exec_ephemeral", "--shards", "4", "--test-duration", "60"]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"
    shard_cfgs = sorted(trial_dir.glob("shard-*/shard.yaml"))
    assert len(shard_cfgs) == 4
    for p in shard_cfgs:
        assert yaml.safe_load(p.read_text(encoding="utf-8"))["sandbox"]["total_count"] == 2
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # non-multiplier validity: merged total = 8 attempts >= 0.9 * N(=4)
    assert rows[0]["valid"] == "True" and rows[0]["total"] == "8"


def test_e2e_crashed_shard_invalidates_trial_without_backfill(tmp_path):
    """A shard that exits nonzero with no summary -> trial invalid, error recorded."""
    stub = tmp_path / "stub.py"
    # Shard 1 crashes before writing anything (rc from env), shard 0 succeeds.
    stub.write_text(
        "import os, sys\n"
        "if os.environ.get('SHARD_CRASH') and 'shard-01' in sys.argv[sys.argv.index('--config') + 1]:\n"
        "    sys.exit(2)\n" + _SHARD_STUB,
        encoding="utf-8",
    )
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    old = os.environ.get("SHARD_CRASH")
    os.environ["SHARD_CRASH"] = "1"
    try:
        rc = main(
            _common_argv(tmp_path, base, out_root, stub)
            + ["--ratios", "2", "--modes", "exec_ephemeral", "--shards", "auto", "--test-duration", "60"]
        )
    finally:
        if old is None:
            del os.environ["SHARD_CRASH"]
        else:
            os.environ["SHARD_CRASH"] = old
    assert rc == 0  # the sweep continues; the TRIAL is what fails
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["valid"] == "False"
    assert rows[0]["return_code"] == "2"
    assert "shard 1" in rows[0]["error"]
    # the surviving shard's 4 attempts are merged as-is -- never padded to 8
    assert rows[0]["succeeded"] == "4"


def test_e2e_dry_run_writes_shard_yamls(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "3", "--modes", "exec_ephemeral", "--shards", "auto", "--dry-run", "--test-duration", "60"]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-03" / "repeat-00"
    assert (trial_dir / "trial.yaml").exists()  # logical provenance kept
    yamls = sorted(trial_dir.glob("shard-*/shard.yaml"))
    assert len(yamls) == 3
    for p in yamls:
        assert yaml.safe_load(p.read_text(encoding="utf-8"))["sandbox"]["total_count"] == 4  # 3*4/3


def test_e2e_reuse_skips_completed_sharded_trial(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    argv = _common_argv(tmp_path, base, out_root, stub) + [
        "--ratios",
        "2",
        "--modes",
        "exec_ephemeral",
        "--multiplier",
        "2",
        "--shards",
        "auto",
        "--test-duration",
        "60",
    ]
    assert main(argv) == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"
    marker = trial_dir / "shard-00" / "argv.json"
    first_run = marker.read_text(encoding="utf-8")
    marker.write_text("SENTINEL", encoding="utf-8")
    assert main(argv + ["--reuse"]) == 0
    # reuse hit the merged summary: the stub never re-ran (sentinel intact)
    assert marker.read_text(encoding="utf-8") == "SENTINEL"
    assert first_run != "SENTINEL"
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[-1]["reused"] == "True" and rows[-1]["valid"] == "True"
