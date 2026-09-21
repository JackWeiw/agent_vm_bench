#!/usr/bin/env python3
"""Recompute oversub trajectory aggregates from lifecycle_series.jsonl.

Runs done before PR #197 (two-tier per-run aggregation) have inflated
trajectory summaries: when pool < N (e.g. 55 trajectories, 384 sandboxes),
the round-robin wraps and each trajectory is run by ~N/pool sandboxes, but
the old `trajectory_summaries` keyed by trajectory_id alone -- collapsing all
~7 runs' step events into ONE record with durations SUMMED (~7x inflated) and
`elapsed_sec` a wall-span across the concurrent runs.

The full raw event set is in `lifecycle_series.jsonl`, so the aggregates are
re-derivable. This script re-runs the now-merged two-tier aggregation (median
across runs, not sum) over each trial's jsonl and rewrites:

  - ``trajectories/index.json`` + ``trajectories/<tid>/replay_result.json``
    (via `export_trajectories`)
  - ``trajectory-detail.csv`` + ``ratio-summary.csv`` + ``trial-summary.csv``
    + ``benchmark-report.json`` (via `write_outputs`)

The old ``trajectory-detail.csv`` is backed up to ``.pre197`` before overwrite
so a before/after diff is possible. Idempotent: re-running just overwrites.

Usage:
    python scripts/recompute_trajectory_aggregates.py <oversub_output_root>

where <oversub_output_root> is the dir containing ``benchmark-report.json``
(e.g. ``results/oversub/oversub-N384-...``).

Scope: lifecycle / exec_only runs whose jsonl carries `round_id` on step
events (true since the early replay stack -- `6421f32`). Trajectory-mode runs
whose pre-#197 jsonl lacks `round_id` on `trajectory_create/kill` events are
partially repaired (steps re-aggregate correctly; create/kill cost lands in a
`round_id=None` slot -- acceptable, since trajectory mode's headline metric is
the per-step latency, not the create/kill cost).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def _find_jsonl(stamp_dir: Path) -> Path | None:
    hits = sorted(stamp_dir.glob("*_lifecycle_series.jsonl"))
    return hits[-1] if hits else None


def main(output_root: str | Path) -> int:
    output_root = Path(output_root)
    report_path = output_root / "benchmark-report.json"
    if not report_path.exists():
        print(f"error: {report_path} not found (pass the oversub run's output_root)", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    trials = report.get("trials", [])
    configuration = report.get("configuration")
    if not trials:
        print("error: benchmark-report.json has no trials", file=sys.stderr)
        return 2

    # Lazy imports -- keep --help / arg-error fast and offline-friendly.
    from bench_core.observability.trajectory_export import export_trajectories
    from bench_core.oversub import write_outputs

    fixed = 0
    skipped = 0
    for t in trials:
        rsp = t.get("run_summary_path")
        mode = t.get("mode", "?")
        ratio = t.get("ratio", "?")
        repeat = t.get("repeat", "?")
        label = f"{mode}/ratio-{ratio}/repeat-{repeat}"
        if not rsp:
            print(f"  skip  {label}: no run_summary_path")
            skipped += 1
            continue
        stamp_dir = Path(rsp).parent
        series = _find_jsonl(stamp_dir)
        if series is None:
            print(f"  skip  {label}: no *_lifecycle_series.jsonl in {stamp_dir}")
            skipped += 1
            continue
        n = export_trajectories(series, stamp_dir)
        print(f"  ok    {label}: {n} trajectories re-aggregated from {series.name}")
        fixed += 1

    # Back up the old trajectory-detail.csv before write_outputs overwrites it.
    detail_csv = output_root / "trajectory-detail.csv"
    if detail_csv.exists():
        bak = output_root / "trajectory-detail.csv.pre197"
        shutil.copy2(detail_csv, bak)
        print(f"backed up old {detail_csv.name} -> {bak.name}")

    write_outputs(trials, output_root=output_root, configuration=configuration)
    print(
        f"\nre-aggregated {fixed} trial(s) ({skipped} skipped); "
        f"rewrote trajectory-detail.csv + ratio-summary.csv + trial-summary.csv + benchmark-report.json"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/recompute_trajectory_aggregates.py <oversub_output_root>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
