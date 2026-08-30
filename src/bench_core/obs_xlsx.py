"""Phase 3: xlsx observability renderer over the shared ReplayObservability model.

One data layer (ReplayObservability), two renderers (text ReportFormatter +
this xlsx). Emits a multi-sheet workbook; openpyxl is a core dep. Semantic
metrics (throughput / retry / trajectory percentiles) are owned by the model;
this renderer only flattens raw per-sandbox series into percentile rows for
the step / lifecycle / trajectory tables (the model exposes percentile dicts,
not the raw lists + counts those tables need). A render failure (e.g. openpyxl
missing on a minimal install) is caught by the caller (run_benchmark) which
falls back to the text report + a warning; this module raises normally.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from openpyxl import Workbook
from openpyxl.styles import Font

from bench_core.utils import calc_percentiles

if TYPE_CHECKING:
    from pathlib import Path

    from bench_core.observability import ReplayObservability


def _write_table(ws, headers: list[str], rows: list[list]) -> None:
    """Write a header row (bold) + data rows to a worksheet.

    Bold the row just appended (tracked via ``ws.max_row``), not always row 1,
    so a sheet with multiple sub-tables (e.g. Admission & QPS) styles each
    header correctly.
    """
    bold = Font(bold=True)
    ws.append(headers)
    header_row = ws.max_row
    for c in ws[header_row]:
        c.font = bold
    for row in rows:
        ws.append(row)


def _pcts_row(label: str, values: list[float]) -> list:
    """One row: [label, n, min, max, avg, p50, p95, p99] for a latency list."""
    s = calc_percentiles(values)
    return [label, len(values), s["min"], s["max"], s["avg"], s["p50"], s["p95"], s["p99"]]


class XlsxReportRenderer:
    """Render a ReplayObservability model to a multi-sheet xlsx workbook."""

    def __init__(self, observability: ReplayObservability) -> None:
        self.obs = observability

    def render(self, path: str | Path) -> None:
        """Write the workbook to ``path`` (overwrites). Creates all 7 sheets."""
        wb = Workbook()
        # openpyxl seeds one default sheet; remove it after building named sheets.
        wb.remove(wb.active)
        self._sheet_overview(wb)
        self._sheet_per_step_timings(wb)
        self._sheet_lifecycle_overhead(wb)
        self._sheet_admission_qps(wb)
        self._sheet_throughput_overcommit(wb)
        self._sheet_trajectory_summary(wb)
        self._sheet_retry_impact(wb)
        wb.save(path)

    # --- sheets ---

    def _sheet_overview(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Overview")
        obs = self.obs
        cfg = obs.config
        total_success = sum(s.replay_metrics.success_count for s in obs.states.values())
        total_failed = sum(s.replay_metrics.failed_count for s in obs.states.values())
        rows = [
            ["workflow_type", cfg.workflow_type],
            ["replay_mode", cfg.replay_mode],
            ["total_count", cfg.total_count],
            ["running_concurrency", cfg.replay_running_concurrency],
            ["test_duration", cfg.test_duration],
            ["wall_sec", obs.wall_sec],
            ["total_steps", obs.total_steps],
            ["success", total_success],
            ["failed", total_failed],
            ["overcommit_ratio", obs.overcommit_ratio],
        ]
        _write_table(ws, ["metric", "value"], rows)

    def _sheet_per_step_timings(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Per-step timings")
        all_lat: list[float] = []
        for s in self.obs.states.values():
            all_lat.extend(s.replay_metrics.latencies)
        rows = [_pcts_row("latency", all_lat)]
        # per action_type
        by_action: dict[str, list[float]] = {}
        for s in self.obs.states.values():
            for act, vals in s.replay_metrics.action_type_latencies.items():
                by_action.setdefault(act, []).extend(vals)
        for act in sorted(by_action):
            rows.append(_pcts_row(act, by_action[act]))
        _write_table(ws, ["bucket", "n", "min", "max", "avg", "p50", "p95", "p99"], rows)

    def _sheet_lifecycle_overhead(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Lifecycle overhead")
        obs = self.obs
        lists = {
            "resume": [],
            "pause": [],
            "slice_total": [],
            "slot_held": [],
            "interaction": [],
        }
        for s in obs.states.values():
            m = s.replay_metrics
            lists["resume"].extend(m.resume_secs)
            lists["pause"].extend(m.pause_secs)
            lists["slice_total"].extend(m.slice_total_secs)
            lists["slot_held"].extend(m.running_slot_held_secs)
            lists["interaction"].extend(m.interaction_total_secs)
        rows = [_pcts_row(label, vals) for label, vals in lists.items()]
        _write_table(ws, ["segment", "n", "min", "max", "avg", "p50", "p95", "p99"], rows)

    def _sheet_admission_qps(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Admission & QPS")
        snap = self.obs.admission_snapshot or {}
        rs = snap.get("running_slots") or {}
        ql = snap.get("qps_limiter") or {}
        # Per-operation breakdown as the leading sub-table (so the header row
        # carries "operation" + "dispatched" for downstream consumers).
        dispatched_by_op = (ql.get("dispatched_by_operation") or {}) if ql else {}
        waiting_by_op = (ql.get("waiting_by_operation") or {}) if ql else {}
        if dispatched_by_op or waiting_by_op:
            all_ops = sorted(set(dispatched_by_op) | set(waiting_by_op))
            op_rows = [[op, dispatched_by_op.get(op, 0), waiting_by_op.get(op, 0)] for op in all_ops]
            _write_table(ws, ["operation", "dispatched", "waiting"], op_rows)
        # Scalar fields as a secondary sub-table.
        rows: list[list] = []
        if rs:
            for k in ("maximum", "active", "peak_active", "granted", "average_queue_wait_sec", "waiting"):
                rows.append([k, rs.get(k)])
        if ql:
            rows.append(["qps", ql.get("qps")])
            rows.append(["inflight_cap", ql.get("inflight_cap")])
            rows.append(["in_flight", ql.get("in_flight")])
            rows.append(["dispatched", ql.get("dispatched")])
            rows.append(["average_wait_sec", ql.get("average_wait_sec")])
            rows.append(["max_wait_sec", ql.get("max_wait_sec")])
        _write_table(ws, ["field", "value"], rows)

    def _sheet_throughput_overcommit(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Throughput & overcommit")
        obs = self.obs
        na = "n/a"
        rows = [
            ["steps_per_sec", obs.steps_per_sec if obs.steps_per_sec is not None else na],
            [
                "effective_parallelism",
                obs.effective_parallelism if obs.effective_parallelism is not None else na,
            ],
            [
                "exec_wall_utilization",
                obs.exec_wall_utilization if obs.exec_wall_utilization is not None else na,
            ],
            ["overcommit_ratio", obs.overcommit_ratio],
            ["concurrency", obs.concurrency],
            ["wall_sec", obs.wall_sec],
        ]
        _write_table(ws, ["metric", "value"], rows)

    def _sheet_trajectory_summary(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Trajectory summary")
        obs = self.obs
        # Trajectory summary is only populated in trajectory mode. Lifecycle
        # mode has no per-trajectory create/kill, so emit header only.
        if getattr(obs.config, "replay_mode", None) != "trajectory":
            _write_table(ws, ["segment", "n", "min", "max", "avg", "p50", "p95", "p99"], [])
            return
        if not any(s.replay_metrics.create_secs for s in obs.states.values()):
            # no trajectory data -> leave the header only
            _write_table(ws, ["segment", "n", "min", "max", "avg", "p50", "p95", "p99"], [])
            return
        rows = [_pcts_row("create_sec", [v for s in obs.states.values() for v in s.replay_metrics.create_secs])]
        rows.append(_pcts_row("kill_sec", [v for s in obs.states.values() for v in s.replay_metrics.kill_secs]))
        rows.append(
            _pcts_row("slot_held", [v for s in obs.states.values() for v in s.replay_metrics.running_slot_held_secs])
        )
        _write_table(ws, ["segment", "n", "min", "max", "avg", "p50", "p95", "p99"], rows)

    def _sheet_retry_impact(self, wb: Workbook) -> None:
        ws = wb.create_sheet("Retry impact")
        obs = self.obs
        rows = [
            ["retry_count", obs.retry_count],
            ["time_lost_to_retry_sec", obs.time_lost_to_retry_sec],
            ["retries_per_slice_p95", obs.retries_per_slice_p95],
        ]
        for op, n in obs.retry_count_by_op.items():
            rows.append([f"retry_queued:{op}", n])
        _write_table(ws, ["metric", "value"], rows)
