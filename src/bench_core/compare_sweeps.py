"""Cross-architecture / cross-config comparison for oversub sweeps.

Reads the oversub driver's written CSV contract (``trajectory-detail.csv``
+ ``trial-summary.csv``) from N labeled sweep output dirs declared by a
comparison manifest, joins them on ``(mode, ratio, trajectory_id)``, and
emits delta CSVs + an xlsx workbook with cross-series charts.

Sibling to ``oversub.py`` -- imports only ``bench_core.oversub`` column
constants + ``setup_logging``; no kernel data-path internals. The manifest
declares what each sweep dir represents (arch, freq/L3 caps, ...); host-level
caps are set outside the benchmark (BIOS / cpufreq / l3cat / boot), so this
tool never sniffs them -- the user declares them.

Typical use (ARM baseline vs several x86 freq/L3 configs, ratios 1:1-1:6)::

    oversub-compare --manifest config/oversub/compare-manifest.yaml

Or the 2-dir shorthand (ARM is the baseline)::

    oversub-compare --arm results/oversub/arm/ --x86 results/oversub/x86/
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import pandas as pd
import yaml
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from bench_core.oversub import TRAJECTORY_COLUMNS, TRIAL_COLUMNS
from bench_core.utils import setup_logging

logger = logging.getLogger(__name__)

# Trajectory-detail CSV metric columns (latency breakdowns in seconds). These
# are the values melted into the tidy long CSV; the medians drive the charts.
# Must match bench_core.oversub.TRAJECTORY_COLUMNS exactly.
TRAJECTORY_METRIC_COLS = [
    "elapsed_sec",
    "slice_total_sec",
    "exec_sec",
    "resume_sec",
    "pause_sec",
    "requested_delay_sec",
    "create_sec",
    "kill_sec",
    "interaction_total_sec",
    "slot_contention_wait_sec",
    "natural_delay_sec",
    "capacity_wait_sec",
    "rate_pacing_wait_sec",
    "inflight_wait_sec",
    "resume_rate_pacing_wait_sec",
    "pause_rate_pacing_wait_sec",
    "resume_inflight_wait_sec",
    "pause_inflight_wait_sec",
    "running_slot_held_sec",
]

# The component set the user wants dissected (exec / resume / pause / wait /
# create / kill). "wait" surfaces as slot_contention_wait_sec -- the headline
# queueing wait; its sub-decomposition (capacity / rate_pacing / inflight) is
# in the tidy CSV for drill-down.
COMPONENT_COLS = [
    "exec_sec",
    "resume_sec",
    "pause_sec",
    "slot_contention_wait_sec",
    "create_sec",
    "kill_sec",
]

# Trial-summary CSV metric columns surfaced in the ratio summary (per-trial
# aggregates, distinct from the per-trajectory medians). Must match
# bench_core.oversub.TRIAL_COLUMNS exactly.
TRIAL_METRIC_COLS = ["wall_sec", "tasks_per_sec", "lifecycle_overhead_pct", "avg_queue_wait_sec"]

# Heatmap color scale: sequential single hue, light -> dark (dataviz:
# sequential = one hue, never a rainbow). White -> deep blue.
_HEATMAP_START = "FFFFFF"
_HEATMAP_END = "1F4E79"


def load_manifest(path: Path | str) -> dict:
    """Load a comparison manifest YAML.

    Required keys: ``baseline_series`` (deltas computed vs it) + ``series``
    (list of ``{label, arch, dir, caps?}``). ``output_dir`` optional (default
    ``results/oversub/comparison-<ts>/``). Relative ``dir`` paths resolve
    against the manifest file's parent so the manifest is portable. Raises on
    unknown keys (a typo like ``serie`` fails loudly), an unknown
    ``baseline_series``, a series dir with no ``trajectory-detail.csv``.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"manifest must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - {"baseline_series", "output_dir", "series"}
    if unknown:
        raise ValueError(f"unknown manifest key(s): {sorted(unknown)}")
    series = raw.get("series")
    if not isinstance(series, list) or not series:
        raise ValueError("manifest needs a non-empty `series` list")
    baseline = raw.get("baseline_series")
    labels: list[str] = []
    for entry in series:
        if not isinstance(entry, dict) or "label" not in entry or "dir" not in entry:
            raise ValueError(f"each series entry needs `label` + `dir`, got {entry!r}")
        label = entry["label"]
        if label in labels:
            raise ValueError(f"duplicate series label: {label!r}")
        labels.append(label)
        d = Path(entry["dir"])
        entry["dir"] = str(d if d.is_absolute() else (path.parent / d).resolve())
        entry.setdefault("arch", "")
        entry.setdefault("caps", {})
    if not baseline:
        raise ValueError("manifest needs `baseline_series`")
    if baseline not in labels:
        raise ValueError(f"baseline_series {baseline!r} not in series labels {labels}")
    return raw


def _read_series_tables(manifest: dict) -> dict[str, dict[str, pd.DataFrame]]:
    """Read each series' trajectory-detail.csv + trial-summary.csv.

    Returns ``{label: {"traj": df, "trial": df}}``. Raises on a missing CSV
    (the driver always writes both for a completed sweep). Numeric coercion is
    pandas' default (bad cells -> NaN, not a crash).
    """
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for entry in manifest["series"]:
        label = entry["label"]
        d = Path(entry["dir"])
        traj_p = d / "trajectory-detail.csv"
        trial_p = d / "trial-summary.csv"
        if not traj_p.exists():
            raise FileNotFoundError(f"series {label!r}: missing {traj_p}")
        if not trial_p.exists():
            raise FileNotFoundError(f"series {label!r}: missing {trial_p}")
        traj = pd.read_csv(traj_p)
        trial = pd.read_csv(trial_p)
        # Validate the contract: the columns we melt / aggregate must be present.
        missing = [
            c for c in ["mode", "ratio", "repeat", "trajectory_id", *TRAJECTORY_METRIC_COLS] if c not in traj.columns
        ]
        if missing:
            raise ValueError(f"series {label!r} trajectory-detail.csv missing columns: {missing}")
        out[label] = {"traj": traj, "trial": trial}
    return out


def build_tidy(manifest: dict, tables: dict[str, dict[str, pd.DataFrame]]) -> pd.DataFrame:
    """Concat+melt all series into a long tidy frame.

    Columns: ``series, arch, <cap keys...>, mode, ratio, repeat,
    trajectory_id, metric, value``. Cap keys are exploded to columns (union
    across series; NaN where a series lacks a key) so downstream pivoting works.
    """
    frames: list[pd.DataFrame] = []
    for entry in manifest["series"]:
        label = entry["label"]
        df = tables[label]["traj"].copy()
        df.insert(0, "series", label)
        df.insert(1, "arch", entry.get("arch", ""))
        for k, v in (entry.get("caps") or {}).items():
            df[k] = v  # scalar broadcast; concat aligns across series
        id_vars = ["series", "arch", "mode", "ratio", "repeat", "trajectory_id", *(entry.get("caps") or {}).keys()]
        tidy = df.melt(id_vars=id_vars, value_vars=TRAJECTORY_METRIC_COLS, var_name="metric", value_name="value")
        frames.append(tidy)
    return pd.concat(frames, ignore_index=True)


def build_ratio_summary(
    tidy: pd.DataFrame,
    tables: dict[str, dict[str, pd.DataFrame]],
    manifest: dict,
) -> pd.DataFrame:
    """Per ``(mode, ratio, series, metric)``: median + delta vs the baseline
    series + pct delta + within-series degradation vs that series' own k=1.

    Long form (one row per metric) so the xlsx can pivot freely. Metrics come
    from the trajectory tidy (per-trajectory medians) + the trial-summary
    tables (per-trial medians: wall_sec / tasks_per_sec / lifecycle_overhead /
    avg_queue_wait). A series without a k=1 trial yields blank degradation
    (no baseline to normalize to), not a crash.
    """
    baseline = manifest["baseline_series"]
    # Trajectory-derived medians: groupby series/mode/ratio/metric over all
    # repeats + trajectories (the median is robust to a flaky repeat).
    traj_med = (
        tidy.groupby(["series", "mode", "ratio", "metric"], sort=False)["value"].median().reset_index(name="median_sec")
    )
    # Trial-derived medians: wall_sec etc. are per-trial, not per-trajectory.
    trial_frames: list[pd.DataFrame] = []
    for entry in manifest["series"]:
        label = entry["label"]
        t = tables[label]["trial"]
        avail = [c for c in TRIAL_METRIC_COLS if c in t.columns]
        if not avail:
            continue
        melted = t.melt(
            id_vars=[c for c in ["mode", "ratio", "repeat"] if c in t.columns],
            value_vars=avail,
            var_name="metric",
            value_name="value",
        )
        melted.insert(0, "series", label)
        med = (
            melted.groupby(["series", "mode", "ratio", "metric"], sort=False)["value"]
            .median()
            .reset_index(name="median_sec")
        )
        trial_frames.append(med)
    if trial_frames:
        trial_med = pd.concat(trial_frames, ignore_index=True)
        med = pd.concat([traj_med, trial_med], ignore_index=True)
    else:
        med = traj_med

    # Baseline-series medians at each (mode, ratio, metric) for cross-series delta.
    base_med = med[med["series"] == baseline][["mode", "ratio", "metric", "median_sec"]].rename(
        columns={"median_sec": "baseline_median_sec"}
    )
    med = med.merge(base_med, on=["mode", "ratio", "metric"], how="left")
    med["delta_vs_baseline_sec"] = med["median_sec"] - med["baseline_median_sec"]
    med["pct_delta_vs_baseline"] = (
        (med["median_sec"] - med["baseline_median_sec"]) / med["baseline_median_sec"] * 100
    ).round(3)

    # Within-series degradation vs that series' own k=1.
    k1 = med[med["ratio"] == 1][["series", "mode", "metric", "median_sec"]].rename(
        columns={"median_sec": "k1_median_sec"}
    )
    med = med.merge(k1, on=["series", "mode", "metric"], how="left")
    med["degradation_vs_ratio1_pct"] = (
        ((med["median_sec"] - med["k1_median_sec"]) / med["k1_median_sec"] * 100)
        .where(med["k1_median_sec"].notna() & (med["k1_median_sec"] != 0))
        .round(3)
    )
    # ratio=1 row: degradation is 0 by definition (vs itself).
    med.loc[med["ratio"] == 1, "degradation_vs_ratio1_pct"] = 0.0

    cols = [
        "mode",
        "ratio",
        "series",
        "metric",
        "median_sec",
        "delta_vs_baseline_sec",
        "pct_delta_vs_baseline",
        "degradation_vs_ratio1_pct",
    ]
    return med[cols].sort_values(["mode", "metric", "series", "ratio"]).reset_index(drop=True)


def build_trajectory_delta(
    tables: dict[str, dict[str, pd.DataFrame]],
    manifest: dict,
) -> pd.DataFrame:
    """Best-effort per-trajectory join on ``(mode, ratio, trajectory_id)``.

    One row per ``(mode, ratio, trajectory_id)`` present in the baseline series,
    with the baseline ``elapsed_sec`` + each other series' ``elapsed_sec`` +
    delta vs baseline. A trajectory absent on a side -> blank delta (the join
    is left on the baseline series). Orphans are counted by the caller; this
    frame surfaces the e2e metric only (components are covered by heatmaps).
    """
    baseline = manifest["baseline_series"]
    base = tables[baseline]["traj"][["mode", "ratio", "trajectory_id", "elapsed_sec"]].copy()
    base = base.rename(columns={"elapsed_sec": "elapsed_sec_baseline"})
    for entry in manifest["series"]:
        label = entry["label"]
        if label == baseline:
            continue
        other = tables[label]["traj"][["mode", "ratio", "trajectory_id", "elapsed_sec"]].rename(
            columns={"elapsed_sec": f"elapsed_sec__{label}"}
        )
        base = base.merge(other, on=["mode", "ratio", "trajectory_id"], how="left")
        base[f"delta_sec__{label}"] = base[f"elapsed_sec__{label}"] - base["elapsed_sec_baseline"]
        base[f"pct_delta__{label}"] = (
            (base[f"elapsed_sec__{label}"] - base["elapsed_sec_baseline"]) / base["elapsed_sec_baseline"] * 100
        ).round(3)
    return base.sort_values(["mode", "ratio", "trajectory_id"]).reset_index(drop=True)


def check_n_parity(tables: dict[str, dict[str, pd.DataFrame]]) -> tuple[bool, list[str]]:
    """Verify ``running_concurrency`` is identical across series.

    Different N means ratio 1:k maps to different absolute sandbox counts ->
    the cross-series comparison is apples-to-oranges. Returns (ok, warnings).
    """
    seen: dict[str, int] = {}
    for label, t in tables.items():
        trial = t["trial"]
        if "running_concurrency" not in trial.columns or trial.empty:
            seen[label] = None
            continue
        vals = pd.to_numeric(trial["running_concurrency"], errors="coerce").dropna().unique()
        seen[label] = int(vals[0]) if len(vals) == 1 else f"mixed:{list(vals)}"
    ns = [v for v in seen.values() if isinstance(v, int)]
    ok = len(set(ns)) <= 1
    warns = [f"{label}: N={seen[label]}" for label, v in seen.items() if v is not None]
    if not ok:
        warns.append(
            "running_concurrency differs across series -- ratios are not comparable; "
            "pass --allow-mismatched-n to proceed anyway"
        )
    return ok, warns


def _add_line_chart(
    ws,
    title: str,
    y_title: str,
    cat_col: int,
    data_cols: list[int],
    n_rows: int,
    anchor: str,
    *,
    header_row: int = 1,
) -> None:
    """One LineChart, one line per data column; categories from cat_col.

    Mirrors ``obs_xlsx._add_line_chart`` so the compare workbook shares the
    repo's chart idiom (default series colors + a legend; identity is never
    color-alone). ``n_rows`` is DATA rows (excludes the header at header_row).
    """
    if n_rows <= 0:
        return
    ch = LineChart()
    ch.title = title
    ch.y_axis.title = y_title
    ch.x_axis.title = str(ws.cell(header_row, cat_col).value)
    ch.height = 9
    ch.width = 18
    for col in data_cols:
        ref = Reference(ws, min_col=col, min_row=header_row, max_row=header_row + n_rows)
        ch.add_data(ref, titles_from_data=True)
    cats = Reference(ws, min_col=cat_col, min_row=header_row + 1, max_row=header_row + n_rows)
    ch.set_categories(cats)
    ws.add_chart(ch, anchor)


def _write_overview(
    wb: Workbook,
    manifest: dict,
    n_ok: bool,
    n_warns: list[str],
    ratio_summary: pd.DataFrame,
    series_stats: list[dict],
) -> None:
    ws = wb.active
    ws.title = "Overview"
    ws.append(["Cross-architecture oversub comparison"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Baseline series:", manifest["baseline_series"]])
    ws.append(["Generated:", time.strftime("%Y-%m-%dT%H:%M:%S")])
    ws.append([])
    cap_keys = sorted({k for s in manifest["series"] for k in (s.get("caps") or {})})
    ws.append(["Series", "Arch", "Dir", *cap_keys])
    for s in manifest["series"]:
        caps = s.get("caps") or {}
        ws.append([s["label"], s.get("arch", ""), s["dir"], *(str(caps.get(k, "")) for k in cap_keys)])
    ws.append([])
    ws.append(["Trial validity (invalid trials stay in the median; the median is robust):"])
    ws["A" + str(ws.max_row)].font = Font(bold=True)
    ws.append(["Series", "Trials", "Valid", "Invalid"])
    for st in series_stats:
        ws.append([st["label"], st["trials"], st["valid"], st["invalid"]])
    ws.append([])
    ws.append(["N-parity check:", "OK" if n_ok else "MISMATCH"])
    for w in n_warns:
        ws.append(["", w])
    ws.append([])
    # Headline: median e2e latency per (series, ratio).
    e2e = ratio_summary[ratio_summary["metric"] == "elapsed_sec"]
    if not e2e.empty:
        pivot = e2e.pivot_table(index="ratio", columns="series", values="median_sec", sort=False)
        ws.append(["Median end-to-end latency (sec)"])
        ws["A" + str(ws.max_row)].font = Font(bold=True)
        ws.append(["ratio", *pivot.columns])
        for ratio, row in pivot.iterrows():
            ws.append([int(ratio), *(float(v) if pd.notna(v) else None for v in row)])
    ws.column_dimensions["A"].width = 28
    for col in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18


def _write_per_ratio(wb: Workbook, ratio_summary: pd.DataFrame, manifest: dict) -> None:
    """Per-ratio sheet: two chart-data blocks + LineCharts.

    Block 1 = absolute median e2e latency (who's faster). Block 2 = within-series
    degradation % vs k=1 (whose oversub behavior degrades worse). Two charts,
    NOT a dual-axis (dataviz anti-pattern) -- each has one y-axis.
    """
    ws = wb.create_sheet("Per-ratio")
    e2e = ratio_summary[ratio_summary["metric"] == "elapsed_sec"]
    if e2e.empty:
        ws.append(["no elapsed_sec data"])
        return
    series_labels = [s["label"] for s in manifest["series"]]

    # Block 1: absolute median e2e.
    ws.append(["Median end-to-end latency (sec) by ratio"])
    ws["A1"].font = Font(bold=True)
    ws.append(["ratio", *series_labels])
    pivot_abs = e2e.pivot_table(index="ratio", columns="series", values="median_sec", sort=False)
    for ratio, row in pivot_abs.iterrows():
        ws.append([int(ratio), *(float(v) if pd.notna(v) else None for v in row.reindex(series_labels))])
    n_rows = len(pivot_abs)
    # data_cols: one per series (cols 2..1+len), header at row 2.
    _add_line_chart(
        ws,
        "End-to-end latency vs oversub ratio",
        "median elapsed_sec",
        cat_col=1,
        data_cols=list(range(2, 2 + len(series_labels))),
        n_rows=n_rows,
        anchor=f"{get_column_letter(2 + len(series_labels) + 1)}2",
        header_row=2,
    )

    # Block 2: degradation % vs k=1 (within-series normalization).
    start = ws.max_row + 3
    ws.cell(start, 1, "Degradation vs ratio-1 (%)").font = Font(bold=True)
    ws.cell(start + 1, 1, "ratio")
    for j, lab in enumerate(series_labels, start=2):
        ws.cell(start + 1, j, lab)
    pivot_deg = e2e.pivot_table(index="ratio", columns="series", values="degradation_vs_ratio1_pct", sort=False)
    for i, (ratio, row) in enumerate(pivot_deg.iterrows(), start=1):
        ws.cell(start + 1 + i, 1, int(ratio))
        for j, lab in enumerate(series_labels, start=2):
            v = row.get(lab)
            ws.cell(start + 1 + i, j, float(v) if pd.notna(v) else None)
    _add_line_chart(
        ws,
        "Degradation vs ratio-1 (%)",
        "pct degradation",
        cat_col=1,
        data_cols=list(range(2, 2 + len(series_labels))),
        n_rows=len(pivot_deg),
        anchor=f"{get_column_letter(2 + len(series_labels) + 1)}{start}",
        header_row=start + 1,
    )
    ws.column_dimensions["A"].width = 12
    for col in range(2, 2 + len(series_labels)):
        ws.column_dimensions[get_column_letter(col)].width = 18


def _write_component_heatmaps(wb: Workbook, ratio_summary: pd.DataFrame, manifest: dict) -> None:
    """One grid per component (exec/resume/pause/wait/create/kill): rows=
    series, cols=ratio, ColorScale conditional formatting on the median value.

    Heatmaps scale to many configs without the spaghetti of multi-line, and
    make "pause blows up at ratio>=4 on x86-l3half" jump out. Sequential single
    hue (white->deep blue), one direction -- never a rainbow (dataviz).
    """
    ws = wb.create_sheet("Component heatmaps")
    series_labels = [s["label"] for s in manifest["series"]]
    ratios = sorted(ratio_summary["ratio"].unique())
    row = 1
    for comp in COMPONENT_COLS:
        sub = ratio_summary[ratio_summary["metric"] == comp]
        if sub.empty:
            continue
        ws.cell(row, 1, f"{comp} (median sec)").font = Font(bold=True)
        row += 1
        ws.cell(row, 1, "series")
        for j, r in enumerate(ratios, start=2):
            ws.cell(row, j, int(r))
        row += 1
        pivot = sub.pivot_table(index="series", columns="ratio", values="median_sec", sort=False)
        block_first_data_row = row
        for lab in series_labels:
            ws.cell(row, 1, lab)
            for j, r in enumerate(ratios, start=2):
                v = pivot.loc[lab, r] if lab in pivot.index and r in pivot.columns else None
                ws.cell(row, j, float(v) if pd.notna(v) else None)
            row += 1
        # ColorScale over the data grid (series rows x ratio cols).
        n_data_rows = len(series_labels)
        if n_data_rows and ratios:
            rng = (
                f"B{block_first_data_row}:{get_column_letter(1 + len(ratios))}{block_first_data_row + n_data_rows - 1}"
            )
            ws.conditional_formatting.add(
                rng,
                ColorScaleRule(
                    start_type="min",
                    start_color=_HEATMAP_START,
                    end_type="max",
                    end_color=_HEATMAP_END,
                ),
            )
        row += 1  # blank spacer between components
    ws.column_dimensions["A"].width = 22
    for col in range(2, 2 + len(ratios)):
        ws.column_dimensions[get_column_letter(col)].width = 12


def _write_trajectory_delta(wb: Workbook, traj_delta: pd.DataFrame) -> None:
    ws = wb.create_sheet("Per-trajectory delta")
    if traj_delta.empty:
        ws.append(["no per-trajectory data"])
        return
    # Write header + rows; pandas to_excel would add a sheet but we are in a
    # shared Workbook -- write cell-by-cell to keep styling control.
    headers = list(traj_delta.columns)
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        ws.cell(1, c).font = Font(bold=True)
        ws.cell(1, c).fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")
    for _, r in traj_delta.iterrows():
        ws.append([r[c] if pd.notna(r[c]) else None for c in headers])
    ws.column_dimensions["A"].width = 14
    ws.freeze_panes = "D2"


def write_xlsx(
    out_path: Path,
    ratio_summary: pd.DataFrame,
    traj_delta: pd.DataFrame,
    manifest: dict,
    n_ok: bool,
    n_warns: list[str],
    series_stats: list[dict],
) -> None:
    """Assemble the 4-sheet comparison workbook."""
    wb = Workbook()
    _write_overview(wb, manifest, n_ok, n_warns, ratio_summary, series_stats)
    _write_per_ratio(wb, ratio_summary, manifest)
    _write_component_heatmaps(wb, ratio_summary, manifest)
    _write_trajectory_delta(wb, traj_delta)
    wb.save(out_path)


def write_csvs(out_dir: Path, tidy: pd.DataFrame, ratio_summary: pd.DataFrame, traj_delta: pd.DataFrame) -> None:
    """Write the tidy long + ratio-summary + per-trajectory CSVs."""
    tidy.to_csv(out_dir / "comparison-tidy.csv", index=False)
    ratio_summary.to_csv(out_dir / "comparison-ratio-summary.csv", index=False)
    traj_delta.to_csv(out_dir / "comparison-trajectory-delta.csv", index=False)


def run_comparison(manifest: dict, out_dir: Path, *, allow_mismatched_n: bool = False) -> int:
    """Drive the comparison end-to-end (testable; no argparse)."""
    tables = _read_series_tables(manifest)
    n_ok, n_warns = check_n_parity(tables)
    for w in n_warns:
        logger.info("N-parity: %s", w)
    if not n_ok and not allow_mismatched_n:
        logger.error("refusing: %s", n_warns[-1])
        return 2

    # Per-series trial validity counts. An invalid trial is KEPT in the median
    # (the median is robust to one flaky repeat) but surfaced so a corrupt ratio
    # is visible in Overview -- the user decides whether to drop + re-run.
    series_stats: list[dict] = []
    for s in manifest["series"]:
        t = tables[s["label"]]["trial"]
        if "valid" in t.columns and not t.empty:
            valid_count = int(t["valid"].astype(str).str.lower().eq("true").sum())
        else:
            valid_count = 0
        series_stats.append(
            {"label": s["label"], "trials": len(t), "valid": valid_count, "invalid": len(t) - valid_count}
        )

    tidy = build_tidy(manifest, tables)
    ratio_summary = build_ratio_summary(tidy, tables, manifest)
    traj_delta = build_trajectory_delta(tables, manifest)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csvs(out_dir, tidy, ratio_summary, traj_delta)
    write_xlsx(out_dir / "comparison.xlsx", ratio_summary, traj_delta, manifest, n_ok, n_warns, series_stats)
    logger.info(
        "comparison written to %s (series=%d, ratios=%s, baseline=%s)",
        out_dir,
        len(manifest["series"]),
        sorted(tidy["ratio"].unique()),
        manifest["baseline_series"],
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oversub-compare",
        description="Compare oversub sweep results across architectures / configs.",
    )
    p.add_argument("--manifest", help="comparison manifest YAML (series dirs + baseline_series)")
    p.add_argument("--arm", help="shorthand: ARM baseline sweep dir (with --x86)")
    p.add_argument("--x86", help="shorthand: x86 sweep dir (with --arm)")
    p.add_argument("--output-dir", default=None, help="default results/oversub/comparison-<ts>/")
    p.add_argument("--allow-mismatched-n", action="store_true", help="proceed even if N differs across series")
    return p


def _shorthand_manifest(arm_dir: str, x86_dir: str) -> dict:
    """Synthesize a 2-series manifest from --arm/--x86 (ARM is the baseline)."""
    return {
        "baseline_series": "arm",
        "series": [
            {"label": "arm", "arch": "arm", "dir": arm_dir, "caps": {}},
            {"label": "x86", "arch": "x86", "dir": x86_dir, "caps": {}},
        ],
    }


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_arg_parser().parse_args(argv)
    if args.manifest:
        manifest = load_manifest(args.manifest)
    elif args.arm and args.x86:
        manifest = _shorthand_manifest(args.arm, args.x86)
    else:
        logger.error("must provide --manifest, or both --arm and --x86")
        return 2
    out_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(f"results/oversub/comparison-{time.strftime('%Y%m%d-%H%M%S')}")
    )
    return run_comparison(
        manifest,
        out_dir,
        allow_mismatched_n=args.allow_mismatched_n,
    )


if __name__ == "__main__":
    raise SystemExit(main())
