"""Unit tests for the oversub benchmark driver's pure helpers.

The driver's pure helpers are module-level functions in bench_core.oversub,
so pytest imports them directly (no conftest path hack). The subprocess +
main() path is covered by the --dry-run integration test (Task 7).
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
import os
import subprocess
import sys

from bench_core.oversub import (
    build_trial_config,
    default_running_concurrency,
    parse_ratios,
)


def test_parse_ratios_basic():
    assert parse_ratios("1,2,3") == [1, 2, 3]


def test_parse_ratios_ignores_spaces_and_blanks():
    assert parse_ratios(" 1 , 2 ,3,") == [1, 2, 3]


def test_parse_ratios_rejects_non_positive():
    import pytest

    with pytest.raises(ValueError):
        parse_ratios("0,2")
    with pytest.raises(ValueError):
        parse_ratios("")


def _base_yaml() -> dict:
    return {
        "workflow_type": "replay",
        "sandbox": {"total_count": 384},
        "replay": {"running_concurrency": 384, "mode": "lifecycle"},
        "create_batch": {"size": 96, "interval": 1},
        "test": {"duration": 3600, "round_size": 384, "round_count": 1, "benchmark_mode": "round_robin"},
        "report": {"output_dir": "results/replay", "filename_prefix": "replay_bench", "format": "both"},
        "aenv": {"template": "t", "env": {"E2B_API_URL": "http://x"}},
    }


def test_default_running_concurrency_from_replay():
    assert default_running_concurrency(_base_yaml()) == 384


def test_default_running_concurrency_falls_back_to_total_count():
    base = _base_yaml()
    del base["replay"]["running_concurrency"]
    base["sandbox"]["total_count"] = 200
    assert default_running_concurrency(base) == 200


def test_build_trial_config_overrides_and_preserves_base():
    base = _base_yaml()
    cfg = build_trial_config(
        base,
        mode="exec_only",
        ratio=2,
        n=384,
        test_duration=600,
        trial_dir="out/t1",
        prefix="replay_bench",
    )
    # Overrides applied.
    assert cfg["sandbox"]["total_count"] == 768  # k*N = 2*384
    assert cfg["replay"]["running_concurrency"] == 384  # N stays fixed
    assert cfg["replay"]["mode"] == "exec_only"
    assert cfg["test"]["round_size"] == 768  # scale with total_count
    assert cfg["test"]["round_count"] == 0  # sustained until duration
    assert cfg["test"]["duration"] == 600
    assert cfg["report"]["output_dir"] == "out/t1"
    assert cfg["report"]["filename_prefix"] == "replay_bench"
    # Base fields preserved (backend block, create_batch pass through).
    assert cfg["aenv"]["template"] == "t"
    assert cfg["create_batch"] == {"size": 96, "interval": 1}
    # The base dict is not mutated (deep copy).
    assert base["sandbox"]["total_count"] == 384
    assert base["replay"]["mode"] == "lifecycle"


def test_build_trial_config_round_yaml_round_trips():
    """Generated trial.yaml must load back to the same overrides (real subprocess input)."""
    base = _base_yaml()
    cfg = build_trial_config(
        base,
        mode="lifecycle",
        ratio=3,
        n=384,
        test_duration=120,
        trial_dir="out/t2",
        prefix="rb",
    )
    text = yaml.safe_dump(cfg, sort_keys=False)
    reloaded = yaml.safe_load(text)
    assert reloaded["sandbox"]["total_count"] == 1152
    assert reloaded["test"]["round_size"] == 1152
    assert reloaded["test"]["round_count"] == 0
    assert reloaded["replay"]["running_concurrency"] == 384


def test_default_running_concurrency_rejects_bad():
    import pytest

    # absent -> 0 -> ValueError
    with pytest.raises(ValueError):
        default_running_concurrency({})
    # present-but-null total_count -> coerced to 0 -> ValueError (Fix 1)
    with pytest.raises(ValueError):
        default_running_concurrency({"sandbox": {"total_count": None}})
    # explicit 0 -> ValueError
    with pytest.raises(ValueError):
        default_running_concurrency({"sandbox": {"total_count": 0}})


from bench_core.oversub import compute_valid, parse_run_summary


def _summary(
    total=768,
    succeeded=768,
    failed=0,
    wall_sec=940.0,
    peak_active=380,
    maximum=384,
    mode="lifecycle",
    test_duration=1000,
):
    return {
        "replay_mode": mode,
        "test_duration": test_duration,
        "wall_sec": wall_sec,
        "throughput": {"total": total, "succeeded": succeeded, "failed": failed},
        "admission": {"maximum": maximum, "peak_active": peak_active} if peak_active is not None else None,
    }


def test_parse_run_summary_round_trips(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_summary()), encoding="utf-8")
    assert parse_run_summary(p)["throughput"]["total"] == 768


def test_compute_valid_happy_path():
    s = _summary()
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is True


def test_compute_valid_nonzero_return_code():
    s = _summary()
    assert compute_valid(s, return_code=2, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_no_work_done():
    s = _summary(total=0, succeeded=0)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_exited_early_wall_below_threshold():
    # wall_sec 100 < 0.9*1000 -> did not sustain the window -> invalid.
    s = _summary(wall_sec=100.0)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_peak_active_exceeds_n():
    s = _summary(peak_active=400)  # 400 > N=384 -> over-admitted -> invalid.
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_exec_only_drops_peak_active_clause():
    # exec_only: admission is null -> peak_active clause skipped; only wall+total+rc gate.
    s = _summary(peak_active=None, mode="exec_only", wall_sec=950.0, total=10)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is True


def test_compute_valid_failure_tolerance_allows_wrap_noise():
    # 10 failures / 768 total = ~1.3%; tolerance 0.05 -> valid; tolerance 0.0 -> invalid.
    s = _summary(total=768, succeeded=758, failed=10, wall_sec=950.0)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.05) is True
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


from bench_core.oversub import aggregate_ratio_summary, trial_row, write_outputs


def _trial_row(mode, ratio, repeat, *, wall=100.0, tps=10.0, peak=380, valid=True, failed=0, total=768):
    s = _summary(total=total, succeeded=total - failed, failed=failed, wall_sec=wall, peak_active=peak, mode=mode)
    s["throughput"]["tasks_per_sec"] = tps  # thread tps so throughput-degradation medians are non-zero
    return trial_row(
        mode=mode,
        ratio=ratio,
        repeat=repeat,
        running_concurrency=384,
        target_count=ratio * 384,
        summary=s,
        return_code=0,
        valid=valid,
        reused=False,
        trial_dir="t",
        run_summary_path="s.json",
    )


def test_trial_row_fields_match_csv_schema():
    r = _trial_row("lifecycle", 2, 1)
    assert r["mode"] == "lifecycle"
    assert r["ratio"] == 2
    assert r["oversubscription_ratio"] == 2
    assert r["target_count"] == 768
    assert r["running_concurrency"] == 384
    assert r["failure_rate"] == 0.0
    assert r["peak_active"] == 380
    assert r["valid"] is True
    assert r["test_duration"] == 1000


def test_aggregate_ratio_summary_medians_and_degradation():
    trials = [
        _trial_row("lifecycle", 1, 1, wall=100, tps=10),
        _trial_row("lifecycle", 1, 2, wall=120, tps=9),
        _trial_row("lifecycle", 2, 1, wall=200, tps=5),
        _trial_row("lifecycle", 2, 2, wall=180, tps=6),
    ]
    rows = aggregate_ratio_summary(trials)
    by = {(r["mode"], r["ratio"]): r for r in rows}
    r1 = by[("lifecycle", 1)]
    r2 = by[("lifecycle", 2)]
    assert r1["attempted"] == 2 and r1["successful"] == 2
    assert r1["median_wall_sec"] == 110.0  # median(100,120)
    # k=2 baseline is k=1: degradation vs median_wall 110 -> (190-110)/110*100
    assert abs(r2["median_wall_sec"] - 190.0) < 0.01
    assert abs(r2["time_degradation_vs_1_1_pct"] - (190 - 110) / 110 * 100) < 0.01
    # throughput gain: median(5,6)=5.5 vs 9.5 -> (5.5-9.5)/9.5*100
    assert abs(r2["throughput_gain_vs_1_1_pct"] - (5.5 - 9.5) / 9.5 * 100) < 0.01


def test_aggregate_ratio_summary_degradation_needs_k1_baseline():
    """No k=1 in a mode -> degradation columns absent (0.0), not a crash."""
    trials = [_trial_row("exec_only", 2, 1, wall=200, tps=5, peak=None)]
    rows = aggregate_ratio_summary(trials)
    assert len(rows) == 1
    # No k=1 baseline -> degradation defaults to 0.0 (no baseline to compare).
    assert rows[0]["time_degradation_vs_1_1_pct"] == 0.0


def test_write_outputs_emits_all_four_files(tmp_path):
    trials = [_trial_row("lifecycle", 1, 1), _trial_row("lifecycle", 2, 1, wall=200, tps=5)]
    # Seed a fake trajectories/index.json in one trial's (fake) run_summary path dir.
    write_outputs(trials, output_root=tmp_path)
    assert (tmp_path / "trial-summary.csv").exists()
    assert (tmp_path / "ratio-summary.csv").exists()
    assert (tmp_path / "trajectory-detail.csv").exists()
    assert (tmp_path / "benchmark-report.json").exists()
    # trial-summary has one row per trial.
    tsv = (tmp_path / "trial-summary.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(tsv) == 1 + 2  # header + 2 trials
    # benchmark-report.json is valid JSON with the trials array.
    import json as _j

    rep = _j.loads((tmp_path / "benchmark-report.json").read_text(encoding="utf-8"))
    assert "configuration" in rep and "trials" in rep and "ratio_summary" in rep


def test_write_outputs_trajectory_detail_has_rows(tmp_path):
    # Seed a trial dir with trajectories/index.json next to the run_summary path.
    trial_stamp = tmp_path / "t" / "rb_20260905-140000"
    traj_dir = trial_stamp / "trajectories"
    traj_dir.mkdir(parents=True)
    idx = {
        "n_trajectories": 1,
        "trajectories": [
            {
                "trajectory_id": "t0",
                "sandbox_index": 0,
                "n_steps": 3,
                "n_failed": 0,
                "n_timeout": 0,
                "success_rate": 1.0,
                "elapsed_sec": 1.5,
                "create_error_type": None,
                "kill_error_type": None,
                "file": "t0/replay_result.json",
            }
        ],
    }
    (traj_dir / "index.json").write_text(json.dumps(idx), encoding="utf-8")
    # Build a trial row whose run_summary_path points at the run_summary inside that stamp dir.
    s = _summary(total=4, succeeded=4, failed=0, wall_sec=100.0, peak_active=380, mode="lifecycle", test_duration=1000)
    s["throughput"]["tasks_per_sec"] = 10.0
    row = trial_row(
        mode="lifecycle",
        ratio=2,
        repeat=1,
        running_concurrency=384,
        target_count=768,
        summary=s,
        return_code=0,
        valid=True,
        reused=False,
        trial_dir=str(trial_stamp),
        run_summary_path=str(trial_stamp / "rb_run_summary.json"),
    )
    write_outputs([row], output_root=tmp_path)
    detail = (tmp_path / "trajectory-detail.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(detail) == 2  # header + 1 trajectory row
    assert "t0" in detail[1]
    # benchmark-report.json trajectory_details also populated
    import json as _j

    rep = _j.loads((tmp_path / "benchmark-report.json").read_text(encoding="utf-8"))
    assert len(rep["trajectory_details"]) == 1
    assert rep["trajectory_details"][0]["trajectory_id"] == "t0"


from bench_core.oversub import main


def _write_stub_bench_core(stub_path: Path, prefix: str):
    """Write a stub script that mimics bench-core: stamps a subdir, writes a
    canned run_summary.json + trajectories/index.json, exits 0. The driver
    invokes it via --bench-core-bin."""
    stub_path.write_text(
        f"""import json, os, sys, time
from pathlib import Path
import yaml
cfg = yaml.safe_load(open(sys.argv[sys.argv.index('--config')+1]))
pfx = cfg['report']['filename_prefix']
out = Path(cfg['report']['output_dir'])
stamp = time.strftime('%Y%m%d-%H%M%S')
run_dir = out / f'{{pfx}}_{{stamp}}'
run_dir.mkdir(parents=True, exist_ok=True)
summary = {{
  'schema_version': 1, 'workflow_type': 'replay',
  'replay_mode': cfg['replay']['mode'], 'provider': 'stub',
  'started_at': '2026-09-05T14:00:00+08:00',
  'completed_at': '2026-09-05T14:18:00+08:00',
  'started_epoch': 1000.0, 'completed_epoch': 2080.0, 'wall_sec': 1080.0,
  'test_duration': cfg['test']['duration'],
  'total_count': cfg['sandbox']['total_count'],
  'running_concurrency': cfg['replay']['running_concurrency'],
  'overcommit_ratio': cfg['sandbox']['total_count'] / cfg['replay']['running_concurrency'],
  'throughput': {{'total': cfg['sandbox']['total_count'],
                 'succeeded': cfg['sandbox']['total_count'],
                 'failed': 0, 'total_steps': 10,
                 'steps_per_sec': 0.01, 'tasks_per_sec': 0.7}},
  'admission': None, 'lifecycle_overhead': None,
  'paths': {{'report': None, 'obs_xlsx': None, 'lifecycle_series': None,
            'trajectory_index': None, 'vm_monitor_dir': None}},
  'error': None}}
(run_dir / f'{{pfx}}_run_summary.json').write_text(json.dumps(summary, indent=2)+'\\n')
(run_dir / 'trajectories').mkdir(parents=True, exist_ok=True)
idx = {{'n_trajectories': 1, 'trajectories': [
  {{'trajectory_id':'t0','sandbox_index':0,'n_steps':1,'n_failed':0,'n_timeout':0,
   'success_rate':1.0,'elapsed_sec':1.0,'create_error_type':None,
   'kill_error_type':None,'file':'t0/replay_result.json'}}]}}
(run_dir / 'trajectories' / 'index.json').write_text(json.dumps(idx, indent=2)+'\\n')
""",
        encoding="utf-8",
    )


def test_dry_run_writes_trial_yamls_and_empty_outputs(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "workflow_type": "replay",
                "sandbox": {"total_count": 4},
                "replay": {"running_concurrency": 4, "mode": "lifecycle"},
                "test": {"duration": 60, "round_size": 4, "round_count": 1, "benchmark_mode": "round_robin"},
                "report": {"output_dir": "results/replay", "filename_prefix": "rb", "format": "both"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    out_root = tmp_path / "sweep"
    rc = main(
        [
            "--config",
            str(base),
            "--provider",
            "aenv",
            "--ratios",
            "1,2",
            "--modes",
            "lifecycle",
            "--repeats",
            "1",
            "--test-duration",
            "60",
            "--dry-run",
            "--output-root",
            str(out_root),
        ]
    )
    assert rc == 0
    # Two trial.yaml files generated (ratio 1 + 2).
    yamls = sorted(out_root.glob("lifecycle/ratio-*/repeat-00/trial.yaml"))
    assert len(yamls) == 2
    # trial.yaml for ratio=2 carries total_count=8, round_size=8, round_count=0.
    cfg2 = yaml.safe_load(yamls[1].read_text(encoding="utf-8"))
    assert cfg2["sandbox"]["total_count"] == 8
    assert cfg2["test"]["round_size"] == 8
    assert cfg2["test"]["round_count"] == 0
    assert (out_root / "benchmark-report.json").exists()


def test_run_with_stub_aggregates_trials(tmp_path):
    """End-to-end driver run using a stub bench-core (no real kernel/SDK)."""
    stub = tmp_path / "stub_bench_core.py"
    _write_stub_bench_core(stub, "rb")

    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "workflow_type": "replay",
                "sandbox": {"total_count": 4},
                "replay": {"running_concurrency": 4, "mode": "lifecycle"},
                "test": {"duration": 60, "round_size": 4, "round_count": 1, "benchmark_mode": "round_robin"},
                "report": {"output_dir": "results/replay", "filename_prefix": "rb", "format": "both"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    out_root = tmp_path / "sweep"
    rc = main(
        [
            "--config",
            str(base),
            "--provider",
            "aenv",
            "--ratios",
            "1,2",
            "--modes",
            "lifecycle",
            "--repeats",
            "1",
            "--test-duration",
            "60",
            "--output-root",
            str(out_root),
            "--bench-core-bin",
            sys.executable,
            str(stub),
            "--no-vm-monitor",
            "--cooldown-sec",
            "0",
            "--cleanup-between-trials",
            "off",
        ]
    )
    assert rc == 0
    tsv = (out_root / "trial-summary.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(tsv) == 1 + 2  # header + 2 trials
    # Both trials valid (stub returns wall 1080 >= 0.9*60, total>0, no admission).
    body = "\n".join(tsv[1:])
    assert "True" in body
    # trajectory-detail has 2 rows (one per stub trial).
    trj = (out_root / "trajectory-detail.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(trj) == 1 + 2
