"""Per-trajectory e2e-latency-vs-oversub-ratio analysis for a single sweep.

Reads the oversub driver's ``trajectory-detail.csv`` (per-run rows: one row
per ``(trajectory, sandbox, round)`` per trial) from a sweep output dir,
collapses to per-``(mode, ratio, trajectory_id)`` medians, and plots each
trajectory's end-to-end latency (and its degradation vs that trajectory's own
``baseline_ratio`` median) across ratios.

Sibling to ``oversub.py`` / ``compare_sweeps.py`` -- imports only
``bench_core.oversub`` column constants + ``setup_logging``; no kernel
data-path internals. ``trajectory-detail.csv`` is the required contract;
``ratio-summary.csv`` is optional (feeds the fleet-median reference line in
the Top-N chart; absent -> that line is dropped with a WARNING).

Why heatmaps not 55-line charts: a trajectory count in the tens-to-hundreds
is exactly the "spaghetti of multi-line" the repo avoids (see
``compare_sweeps._write_component_heatmaps``). The all-trajectories view is a
``ColorScaleRule`` heatmap (white -> deep blue, sequential single hue); the
readable line view is a Top-N worst-degrading subset. A ``--heatmap-top-n``
knob caps heatmap rows to the worst N when the trajectory count grows.

Typical use::

    traj-degradation --sweep-dir results/oversub/oversub-N384-2026.../
    traj-degradation --sweep-dir .../ --mode lifecycle --top-n 10 --heatmap-top-n 50
"""
from __future__ import annotations

import argparse
import csv
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from bench_core.oversub import TRAJECTORY_COLUMNS
from bench_core.utils import setup_logging

logger = logging.getLogger(__name__)

# Required columns from trajectory-detail.csv (the contract lives in
# bench_core.oversub.TRAJECTORY_COLUMNS; we only enforce the subset we read).
_REQUIRED_COLS = ["mode", "ratio", "trajectory_id", "elapsed_sec"]
# Component breakdown surfaced in the Breakdown heatmap (absolute-seconds
# median, NOT degradation % -- a 0.1s->0.5s pause would otherwise inflate to
# 500%). Each gets its own ColorScale with independent min/max because the
# magnitudes differ (exec ~ seconds, pause ~ milliseconds).
_COMPONENT_COLS = ["exec_sec", "resume_sec", "pause_sec", "slot_contention_wait_sec"]

# Heatmap color scale: sequential single hue, light -> dark (dataviz:
# sequential = one hue, never a rainbow). White -> deep blue -- matches
# compare_sweeps._HEATMAP_START/_END.
_HEATMAP_START = "FFFFFF"
_HEATMAP_END = "1F4E79"
_HEADER_FILL = PatternFill(fill_type="solid", fgColor="DDDDDD")
_HEADER_FONT = Font(bold=True)

# Fleet-median reference line caveat (per-trajectory baseline != fleet
# baseline -- the two degradation definitions are NOT directly comparable).
_FLEET_CAVEAT = (
    "Fleet median uses a fleet-wide k=1 baseline (from ratio-summary.csv); "
    "trajectory lines use each trajectory's own k=1 baseline. The two "
    "degradation definitions differ -- this line is a macro shape reference "
    "only; do NOT compare % values directly."
)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="traj-degradation",
        description="Plot per-trajectory e2e latency vs oversub ratio for one sweep.",
    )
    p.add_argument("--sweep-dir", required=True, help="oversub-bench sweep output dir (holds trajectory-detail.csv)")
    p.add_argument("--mode", default=None, help="filter to one replay mode; default = all modes in the sweep")
    p.add_argument(
        "--top-n", type=int, default=10, help="worst-degrading trajectories in the Top-N line chart (default 10)"
    )
    p.add_argument(
        "--heatmap-top-n",
        type=int,
        default=0,
        help="cap heatmap rows to the worst N trajectories (default 0 = show all)",
    )
    p.add_argument(
        "--baseline-ratio", type=int, default=1, help="ratio whose median is the per-trajectory baseline (default 1)"
    )
    p.add_argument("--output-dir", default=None, help="default <sweep-dir>/traj-degradation-<ts>/")
    return p


def _load_trajectory_detail(sweep_dir: Path) -> pd.DataFrame:
    """Read trajectory-detail.csv; validate required columns."""
    path = sweep_dir / "trajectory-detail.csv"
    if not path.exists():
        raise FileNotFoundError(f"trajectory-detail.csv not found in sweep dir: {path}")
    df = pd.read_csv(path)
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"trajectory-detail.csv missing required columns: {missing}")
    # Coerce numeric cols to float (blank -> NaN); keep breakdown cols that exist.
    for c in ["elapsed_sec", *_COMPONENT_COLS]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _load_fleet_degradation(sweep_dir: Path, mode: str | None) -> dict[str, dict[int, float]]:
    """Read ratio-summary.csv -> {mode: {ratio: time_degradation_vs_1_1_pct}}.

    Optional: returns {} if the file is absent (caller drops the fleet line).
    """
    path = sweep_dir / "ratio-summary.csv"
    if not path.exists():
        logger.warning("ratio-summary.csv absent in %s -- Top-N fleet-median reference line will be omitted", sweep_dir)
        return {}
    df = pd.read_csv(path)
    if "time_degradation_vs_1_1_pct" not in df.columns:
        logger.warning("ratio-summary.csv has no time_degradation_vs_1_1_pct column -- fleet-median line omitted")
        return {}
    out: dict[str, dict[int, float]] = {}
    for mode_val, grp in df.groupby("mode"):
        if mode is not None and mode_val != mode:
            continue
        out[mode_val] = {int(r): float(v) for r, v in zip(grp["ratio"], grp["time_degradation_vs_1_1_pct"])}
    return out


def _aggregate(df: pd.DataFrame, baseline_ratio: int) -> pd.DataFrame:
    """Collapse per-run rows to one row per (mode, ratio, trajectory_id).

    median of elapsed_sec + each component col. Adds ``degradation_pct``:
    per-trajectory baseline = that trajectory's median at ``baseline_ratio``
    (per mode); degradation = (median(k) - baseline) / baseline * 100.
    Baseline missing/0 -> degradation_pct is NaN for ALL ratios of that
    trajectory (no denominator -> cannot compute; NOT 0%).
    """
    agg_cols = ["elapsed_sec", *[c for c in _COMPONENT_COLS if c in df.columns]]
    g = df.groupby(["mode", "ratio", "trajectory_id"], sort=False)[agg_cols].median().reset_index()

    # Per-trajectory baseline (median at baseline_ratio, per mode). Merge on
    # (mode, trajectory_id) so baseline propagates to all ratios of that traj.
    base = g[g["ratio"] == baseline_ratio][["mode", "trajectory_id", "elapsed_sec"]].rename(
        columns={"elapsed_sec": "baseline_sec"}
    )
    g = g.merge(base, on=["mode", "trajectory_id"], how="left")
    g["baseline_sec"] = pd.to_numeric(g["baseline_sec"], errors="coerce")
    # degradation = (median(k) - baseline) / baseline * 100. Baseline
    # missing/0 -> inf/NaN -> coerced to NA for ALL ratios of that trajectory
    # (no denominator -> cannot compute; NOT 0%). Absolute cols stay populated.
    with np.errstate(divide="ignore", invalid="ignore"):
        g["degradation_pct"] = (g["elapsed_sec"] - g["baseline_sec"]) / g["baseline_sec"] * 100
    g["degradation_pct"] = g["degradation_pct"].replace([np.inf, -np.inf], pd.NA)
    g.loc[g["baseline_sec"].isna() | (g["baseline_sec"] == 0), "degradation_pct"] = pd.NA
    g["degradation_pct"] = pd.to_numeric(g["degradation_pct"], errors="coerce")
    return g


def _max_ratio(g_mode: pd.DataFrame) -> int:
    rs = g_mode["ratio"].unique()
    return int(max(rs)) if len(rs) else 0


def _sorted_trajectories(g_mode: pd.DataFrame, max_ratio: int) -> list[str]:
    """Trajectory ids sorted by degradation at max_ratio (desc); NaN last.
    Tie-break: absolute elapsed_sec at max_ratio desc (slow + degrading first)."""
    mr = g_mode[g_mode["ratio"] == max_ratio].sort_values(
        ["degradation_pct", "elapsed_sec"], ascending=False, na_position="last"
    )
    return mr["trajectory_id"].tolist()


def _apply_heatmap_top_n(tids: list[str], heatmap_top_n: int) -> list[str]:
    return tids[:heatmap_top_n] if heatmap_top_n > 0 else tids


def _write_heatmap_sheet(
    wb: Workbook,
    sheet_name: str,
    g_mode: pd.DataFrame,
    tids: list[str],
    value_col: str,
    *,
    title: str,
    independent_scale: bool = False,
) -> None:
    """rows=trajectory (in tids order), cols=ratio, cell=value_col.

    independent_scale: when True (Breakdown components, whose magnitudes
    differ), each component block gets its own ColorScale min/max. Here each
    sheet is one value_col so the flag just documents intent; multi-component
    stacking is done by calling this once per component into sections of one
    sheet (see _write_breakdown_sheet).
    """
    ws = wb.create_sheet(sheet_name)
    ratios = sorted(int(r) for r in g_mode["ratio"].unique())
    ws.append(["trajectory_id", *[f"ratio {r}" for r in ratios]])
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
    # Pivot value_col into the grid.
    grid = g_mode.pivot(index="trajectory_id", columns="ratio", values=value_col)
    for tid in tids:
        if tid not in grid.index:
            continue
        row = grid.loc[tid]
        ws.append([tid, *[None if pd.isna(row[r]) else round(float(row[r]), 4) for r in ratios]])
    n_rows = len(tids)
    if n_rows > 0 and len(ratios) > 0:
        first_data_col = 2  # B
        last_data_col = 1 + len(ratios)
        rng = f"{get_column_letter(first_data_col)}2:{get_column_letter(last_data_col)}{n_rows + 1}"
        ws.conditional_formatting.add(
            rng,
            ColorScaleRule(start_type="min", start_color=_HEATMAP_START, end_type="max", end_color=_HEATMAP_END),
        )
    ws.column_dimensions["A"].width = 28
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    # Title banner above the table would shift rows; keep sheet title in tab +
    # a comment on A1 instead so the grid starts at row 1 (chart refs stay simple).
    ws["A1"].comment = Comment(title + " -- rows sorted worst-first; blank = baseline missing.", "traj-degradation")


def _write_breakdown_sheet(
    wb: Workbook,
    sheet_name: str,
    g_mode: pd.DataFrame,
    tids: list[str],
) -> None:
    """4 component blocks stacked vertically, each its own ColorScale (independent
    min/max -- exec ~s, pause ~ms differ in magnitude)."""
    ws = wb.create_sheet(sheet_name)
    ratios = sorted(int(r) for r in g_mode["ratio"].unique())
    row_cursor = 1
    for comp in _COMPONENT_COLS:
        if comp not in g_mode.columns:
            continue
        # Block header
        ws.cell(row=row_cursor, column=1, value=f"{comp} (median, seconds)").font = Font(bold=True, size=12)
        row_cursor += 1
        ws.cell(row=row_cursor, column=1, value="trajectory_id")
        for j, r in enumerate(ratios):
            ws.cell(row=row_cursor, column=2 + j, value=f"ratio {r}")
        for cell in ws[row_cursor]:
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
        header_row = row_cursor
        row_cursor += 1
        grid = g_mode.pivot(index="trajectory_id", columns="ratio", values=comp)
        first_data_row = row_cursor
        for tid in tids:
            if tid not in grid.index:
                continue
            row = grid.loc[tid]
            ws.cell(row=row_cursor, column=1, value=tid)
            for j, r in enumerate(ratios):
                v = row[r]
                ws.cell(row=row_cursor, column=2 + j, value=None if pd.isna(v) else round(float(v), 4))
            row_cursor += 1
        n_block_rows = row_cursor - first_data_row
        if n_block_rows > 0 and len(ratios) > 0:
            rng = f"{get_column_letter(2)}{first_data_row}:{get_column_letter(1 + len(ratios))}{row_cursor - 1}"
            ws.conditional_formatting.add(
                rng,
                ColorScaleRule(start_type="min", start_color=_HEATMAP_START, end_type="max", end_color=_HEATMAP_END),
            )
        row_cursor += 1  # blank spacer between blocks
    ws.column_dimensions["A"].width = 28
    ws.freeze_panes = "B2"


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
    """One LineChart, one line per data column; mirrors compare_sweeps._add_line_chart."""
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


def _write_topn_sheet(
    wb: Workbook,
    sheet_name: str,
    g_mode: pd.DataFrame,
    tids_sorted: list[str],
    top_n: int,
    fleet: dict[int, float],
) -> None:
    """Top-N worst trajectories: ratio | tid_1 | ... | tid_N | fleet_median."""
    ws = wb.create_sheet(sheet_name)
    ratios = sorted(int(r) for r in g_mode["ratio"].unique())
    top_tids = tids_sorted[:top_n]
    # Degradation grid for top trajectories.
    grid = g_mode.pivot(index="trajectory_id", columns="ratio", values="degradation_pct")
    has_fleet = bool(fleet)
    header = ["ratio", *top_tids]
    if has_fleet:
        header.append("fleet_median")
    ws.append(header)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
    for r in ratios:
        row = [r]
        for tid in top_tids:
            v = grid.loc[tid, r] if tid in grid.index else None
            row.append(None if pd.isna(v) else round(float(v), 2))
        if has_fleet:
            row.append(round(float(fleet.get(r, 0.0)), 2))
        ws.append(row)
    n_rows = len(ratios)
    data_cols = list(range(2, 2 + len(top_tids)))
    if has_fleet and data_cols:
        fleet_col = 2 + len(top_tids)
        ws.cell(row=1, column=fleet_col).comment = Comment(_FLEET_CAVEAT, "traj-degradation")
        _add_line_chart(
            ws,
            "Top-N trajectory degradation (per-trajectory k=1 baseline) -- fleet median: shape reference only",
            "degradation %",
            1,
            [*data_cols, fleet_col],
            n_rows,
            f"{get_column_letter(2 + len(header) + 1)}2",
        )
    elif data_cols:
        _add_line_chart(
            ws,
            "Top-N trajectory degradation (per-trajectory k=1 baseline)",
            "degradation %",
            1,
            data_cols,
            n_rows,
            f"{get_column_letter(2 + len(header) + 1)}2",
        )
    ws.column_dimensions["A"].width = 10
    for c in range(2, 2 + len(header)):
        ws.column_dimensions[get_column_letter(c)].width = 16
    ws.freeze_panes = "B2"


def write_xlsx(
    out_path: Path,
    agg: pd.DataFrame,
    *,
    top_n: int,
    heatmap_top_n: int,
    baseline_ratio: int,
    fleet_by_mode: dict[str, dict[int, float]],
    mode_filter: str | None,
) -> None:
    wb = Workbook()
    wb.remove(wb.active)  # drop the default sheet; we create named ones
    modes = sorted(str(m) for m in agg["mode"].unique()) if mode_filter is None else [mode_filter]
    multi = len(modes) > 1
    for mode in modes:
        g_mode = agg[agg["mode"] == mode]
        if g_mode.empty:
            continue
        max_ratio = _max_ratio(g_mode)
        tids_sorted = _sorted_trajectories(g_mode, max_ratio)
        heat_tids = _apply_heatmap_top_n(tids_sorted, heatmap_top_n)
        suffix = f" ({mode})" if multi else ""
        fleet = fleet_by_mode.get(mode, {})
        _write_heatmap_sheet(
            wb,
            f"Degradation{suffix}",
            g_mode,
            heat_tids,
            "degradation_pct",
            title=f"Per-trajectory degradation % vs k={baseline_ratio} baseline (mode {mode})",
        )
        _write_heatmap_sheet(
            wb,
            f"Absolute e2e{suffix}",
            g_mode,
            heat_tids,
            "elapsed_sec",
            title=f"Per-trajectory median e2e seconds (mode {mode})",
        )
        _write_breakdown_sheet(wb, f"Breakdown{suffix}", g_mode, heat_tids)
        _write_topn_sheet(wb, f"Top-N{suffix}", g_mode, tids_sorted, top_n, fleet)
    if not wb.sheetnames:
        wb.create_sheet("empty")  # never save a workbook with zero sheets
    wb.save(out_path)


def write_csv(out_path: Path, agg: pd.DataFrame) -> None:
    """Long table: mode, trajectory_id, ratio, median_elapsed_sec, degradation_pct, + component medians."""
    cols = ["mode", "trajectory_id", "ratio", "elapsed_sec", "degradation_pct", *_COMPONENT_COLS]
    cols = [c for c in cols if c in agg.columns]
    out = agg[cols].rename(columns={c: c.replace("_sec", "_median_sec") for c in cols if c.endswith("_sec")})
    out = out.rename(columns={"elapsed_median_sec": "median_elapsed_sec"})
    out.to_csv(out_path, index=False)


def run_traj_degradation(
    sweep_dir: Path | str,
    out_dir: Path | str,
    *,
    mode: str | None = None,
    top_n: int = 10,
    heatmap_top_n: int = 0,
    baseline_ratio: int = 1,
) -> int:
    sweep_dir = Path(sweep_dir)
    out_dir = Path(out_dir)
    if not sweep_dir.exists():
        logger.error("sweep dir does not exist: %s", sweep_dir)
        return 2
    try:
        df = _load_trajectory_detail(sweep_dir)
    except (FileNotFoundError, ValueError) as e:
        logger.error("%s", e)
        return 2
    if mode is not None:
        df = df[df["mode"] == mode]
        if df.empty:
            logger.error("no trajectory-detail rows for mode %r in %s", mode, sweep_dir)
            return 2
    agg = _aggregate(df, baseline_ratio=baseline_ratio)
    fleet_by_mode = _load_fleet_degradation(sweep_dir, mode)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "traj-degradation.csv", agg)
    write_xlsx(
        out_dir / "traj-degradation.xlsx",
        agg,
        top_n=top_n,
        heatmap_top_n=heatmap_top_n,
        baseline_ratio=baseline_ratio,
        fleet_by_mode=fleet_by_mode,
        mode_filter=mode,
    )
    n_traj = agg["trajectory_id"].nunique()
    logger.info(
        "wrote traj-degradation.xlsx + .csv to %s (%d trajectories, %d modes)",
        out_dir,
        n_traj,
        agg["mode"].nunique(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_arg_parser().parse_args(argv)
    out_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(args.sweep_dir) / f"traj-degradation-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    return run_traj_degradation(
        args.sweep_dir,
        out_dir,
        mode=args.mode,
        top_n=args.top_n,
        heatmap_top_n=args.heatmap_top_n,
        baseline_ratio=args.baseline_ratio,
    )


if __name__ == "__main__":
    raise SystemExit(main())
