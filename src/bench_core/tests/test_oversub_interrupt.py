"""Driver-side interrupt / partial-flush capture for ``oversub-bench``.

Companion to ``test_bench_interrupt.py`` (kernel side). Covers the driver's
half of the no-stats-on-interrupt contract: when a trial is interrupted
mid-fleet, the partial ``run_summary.json`` the kernel's ``finally`` flushed
must surface in ``trial-summary.csv`` as an invalid row (rc=130) instead of
being orphaned, and a user-initiated interrupt must halt the sweep.

Two interrupt flavors, distinguished by whether the DRIVER saw a
``KeyboardInterrupt`` (not by the subprocess exit code, which is 130 in both):

  * trial-exit-130: the kernel exited 130 (SIGTERM-cooperative) but the driver
    was not itself interrupted -> the row is captured invalid and the sweep
    CONTINUES (no ``_interrupted`` sentinel).
  * user-interrupt: the driver's ``_run_subprocess`` raised ``KeyboardInterrupt``
    (user Ctrl-C'd the driver) -> ``_run_trial`` sets ``_interrupted`` and
    ``main`` halts the sweep (returns 130, runs no further trials).
"""
from __future__ import annotations

import csv
import sys
import yaml

from bench_core.oversub import main


def _write_stub_bench_core_partial(stub_path, *, exit_code):
    """Stub bench-core: writes a PARTIAL run_summary (succeeded = total//2) +
    trajectories/index.json, then exits ``exit_code``.

    Models a trial interrupted mid-fleet: the kernel's SIGTERM-cooperative
    ``finally`` flushed partial artifacts (ran half the fleet) before exiting.
    """
    stub_path.write_text(
        f"""import json, sys, time
from pathlib import Path
import yaml
cfg = yaml.safe_load(open(sys.argv[sys.argv.index('--config')+1]))
pfx = cfg['report']['filename_prefix']
out = Path(cfg['report']['output_dir'])
stamp = time.strftime('%Y%m%d-%H%M%S')
run_dir = out / f'{{pfx}}_{{stamp}}'
run_dir.mkdir(parents=True, exist_ok=True)
total = cfg['sandbox']['total_count']
succeeded = total // 2  # partial: only half the fleet completed
summary = {{
  'schema_version': 1, 'workflow_type': 'replay',
  'replay_mode': cfg['replay']['mode'], 'provider': 'stub',
  'started_at': '2026-09-05T14:00:00+08:00',
  'completed_at': '2026-09-05T14:18:00+08:00',
  'started_epoch': 1000.0, 'completed_epoch': 2080.0, 'wall_sec': 1080.0,
  'test_duration': cfg['test']['duration'],
  'total_count': total,
  'running_concurrency': cfg['replay']['running_concurrency'],
  'overcommit_ratio': total / cfg['replay']['running_concurrency'],
  'throughput': {{'total': total, 'succeeded': succeeded,
                 'failed': total - succeeded, 'total_steps': 10,
                 'steps_per_sec': 0.01, 'tasks_per_sec': 0.7}},
  'admission': None, 'lifecycle_overhead': None,
  'paths': {{'report': None, 'obs_xlsx': None, 'lifecycle_series': None,
            'trajectory_index': None, 'vm_monitor_dir': None}},
  'error': None}}
(run_dir / f'{{pfx}}_run_summary.json').write_text(json.dumps(summary, indent=2)+'\\n')
(run_dir / 'trajectories').mkdir(parents=True, exist_ok=True)
idx = {{'n_trajectories': 1, 'trajectories': [
  {{'trajectory_id':'t0','sandbox_index':0,'n_steps':1,'n_failed':0,'n_timeout':0,
   'success_rate':1.0,'elapsed_sec':1.0,
   'time_breakdown_sec':{{'slice_total':0.8,'exec':0.5,'resume':0.1,'pause':0.2,
     'requested_delay':0.1,'create':0.05,'kill':0.05,'interaction_total':0.9,
     'slot_contention_wait':0.0,'resume_rate_pacing_wait':0.0,'pause_rate_pacing_wait':0.0,
     'running_slot_held':0.8}},
   'create_error_type':None,'kill_error_type':None,'file':'t0/replay_result.json'}}]}}
(run_dir / 'trajectories' / 'index.json').write_text(json.dumps(idx, indent=2)+'\\n')
sys.exit({exit_code})
""",
        encoding="utf-8",
    )


def _partial_base_yaml(tmp_path):
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
    return base


def _common_argv(base, out_root, stub):
    return [
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


def test_interrupted_trial_exit_130_captured_as_invalid_sweep_continues(tmp_path):
    """Trial subprocess exits 130 (kernel SIGTERM-cooperative exit) but the
    driver was NOT itself interrupted: the partial row is captured
    (rc=130, valid=False) and the sweep CONTINUES (no ``_interrupted``
    sentinel, so main does not halt)."""
    stub = tmp_path / "stub_partial_130.py"
    _write_stub_bench_core_partial(stub, exit_code=130)
    out_root = tmp_path / "sweep"
    rc = main(_common_argv(_partial_base_yaml(tmp_path), out_root, stub))
    assert rc == 0  # no user-interrupt -> sweep completes all trials
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2  # both ratios ran (sweep did not halt)
    assert all(r["return_code"] == "130" for r in rows)
    assert all(r["valid"] == "False" for r in rows)  # rc != 0 -> invalid
    assert "_interrupted" not in rows[0]  # internal sentinel, not a CSV column


def test_user_interrupt_halts_sweep_and_captures_partial_row(tmp_path, monkeypatch):
    """User Ctrl-C of the DRIVER mid-trial: ``_run_subprocess`` raises
    KeyboardInterrupt (after the kernel flushed a partial summary during the
    terminate-grace), ``_run_trial`` captures it as rc=130 + ``_interrupted``
    and ``main`` halts the sweep (returns 130, runs no further trials)."""
    stub = tmp_path / "stub_partial_interrupt.py"
    _write_stub_bench_core_partial(stub, exit_code=0)  # stub flushes + exits 0; wrapper raises after
    out_root = tmp_path / "sweep"

    # Run the stub to completion (it writes the partial summary), then raise
    # KeyboardInterrupt to model the user ctrl-c'ing the driver during the
    # post-trial grace -- the kernel's SIGTERM-cooperative handler has already
    # flushed partial artifacts, so _run_trial must capture them as a row.
    import bench_core.oversub as oversub_mod

    real_run_subprocess = oversub_mod._run_subprocess

    def _interrupt_after_flush(cmd, log_path, timeout_sec):
        real_run_subprocess(cmd, log_path, 0)  # stub exits fast after flushing
        raise KeyboardInterrupt

    monkeypatch.setattr(oversub_mod, "_run_subprocess", _interrupt_after_flush)

    rc = main(_common_argv(_partial_base_yaml(tmp_path), out_root, stub))
    assert rc == 130  # user-interrupt halts the sweep
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1  # halted after the first (interrupted) trial
    assert rows[0]["return_code"] == "130"
    assert rows[0]["valid"] == "False"
    assert rows[0]["run_summary_path"]  # the partial summary was captured


def test_grace_constants_match_kernel_partial_flush_budget():
    """The terminate-grace must exceed the kernel's partial-flush wall time so a
    SIGTERM-triggered flush completes before the hard kill. Locked at 30s/5s;
    future CLI migration should not silently regress this."""
    from bench_core.oversub import _POSTKILL_GRACE_SEC, _TERMINATE_GRACE_SEC

    assert _TERMINATE_GRACE_SEC == 30
    assert _POSTKILL_GRACE_SEC == 5
