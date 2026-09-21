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
    (via `export_trajectories`) -- the per-trajectory catalog with a slim
    ``runs[]`` array (~N/pool entries) per trajectory.
  - ``trajectory-detail.csv`` + ``ratio-summary.csv`` + ``trial-summary.csv``
    + ``benchmark-report.json`` (via `write_outputs`) -- only when the input
    is the oversub run root (contains ``benchmark-report.json``).

The old ``trajectory-detail.csv`` is backed up to ``.pre197`` before overwrite.
Idempotent: re-running just overwrites.

Usage -- the script accepts ANY of these as the argument (it recursively finds
``*_lifecycle_series.jsonl`` underneath):

    python scripts/recompute_trajectory_aggregates.py results/oversub/oversub-N384-...
    python scripts/recompute_trajectory_aggregates.py .../repeat-00/                       # one trial
    python scripts/recompute_trajectory_aggregates.py .../repeat-00/replay_bench_20260920-222102/   # one stamp

When given the oversub run root, it ALSO regenerates trajectory-detail.csv
(one row per run ~= 384) from the new index.json. When given a finer path it
only regenerates that trial's trajectories/ (re-run at the root for the CSV).

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


def main(arg: str | Path) -> int:
    root = Path(arg)
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    from bench_core.observability.trajectory_export import export_trajectories

    # Recursively find every lifecycle_series.jsonl under the input path. This
    # accepts the oversub root, a trial dir (.../repeat-00/), or a single stamp
    # dir (.../replay_bench_<ts>/) -- the jsonl lives in the stamp dir either way.
    jsonls = sorted(root.rglob("*_lifecycle_series.jsonl"))
    if not jsonls:
        print(
            f"error: no *_lifecycle_series.jsonl found under {root}\n"
            f"       pass the oversub run root (e.g. results/oversub/oversub-N384-...)\n"
            f"       or a trial/stamp dir that contains one.",
            file=sys.stderr,
        )
        return 2

    fixed = 0
    for series in jsonls:
        stamp_dir = series.parent
        try:
            n = export_trajectories(series, stamp_dir)
        except Exception as e:
            print(f"  FAIL  {series}: {e}", file=sys.stderr)
            continue
        if n:
            print(f"  ok    {n:3d} trajectories  <-  {series.relative_to(root) if root.is_dir() else series}")
            fixed += 1
        else:
            print(f"  empty 0 trajectories  <-  {series} (no trajectory events in series)")

    if fixed == 0:
        print("\nno trajectory aggregates were written (see errors above).", file=sys.stderr)
        return 1

    # If the input is the oversub run root (has benchmark-report.json), also
    # regenerate trajectory-detail.csv from the freshly-written index.json.
    report_path = root / "benchmark-report.json"
    if report_path.exists():
        from bench_core.oversub import write_outputs

        report = json.loads(report_path.read_text(encoding="utf-8"))
        trials = report.get("trials", [])
        configuration = report.get("configuration")
        if trials:
            detail_csv = root / "trajectory-detail.csv"
            if detail_csv.exists():
                bak = root / "trajectory-detail.csv.pre197"
                shutil.copy2(detail_csv, bak)
                print(f"backed up old {detail_csv.name} -> {bak.name}")
            write_outputs(trials, output_root=root, configuration=configuration)
            print(
                f"\nre-aggregated {fixed} jsonl(s); rewrote trajectory-detail.csv "
                f"(one row per run) + ratio-summary.csv + trial-summary.csv + benchmark-report.json"
            )
            return 0
        print("\nbenchmark-report.json has no trials; skipped trajectory-detail.csv regeneration.")
    else:
        print(
            f"\nre-aggregated {fixed} jsonl(s). To also regenerate trajectory-detail.csv, "
            f"re-run with the oversub run root (the dir containing benchmark-report.json)."
        )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "usage: python scripts/recompute_trajectory_aggregates.py <oversub_root | trial_dir | stamp_dir>",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
