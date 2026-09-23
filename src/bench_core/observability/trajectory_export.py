"""Per-trajectory ``replay_result.json`` export + ``trajectories/index.json`` catalog.

Two-tier (see :mod:`bench_core.observability.trajectory_summary`): the kernel
emits RAW per-run records (one per ``(trajectory_id, sandbox_index, round_id)``);
this module rolls them into a per-trajectory aggregate + a per-run drill-down.

- ``trajectories/index.json`` -- one LEAN row per trajectory (the aggregate
  headline: ``n_runs``/``n_success``/``n_failed``/``elapsed_sec`` success-median
  + a slim ``runs[]`` array). The oversub driver expands ``runs[]`` into one
  ``trajectory-detail.csv`` row per run so a reader gets per-run latency at any
  pool/N ratio and can ``groupby trajectory_id`` for the per-trajectory median.
- ``trajectories/<sanitized_tid>/replay_result.json`` -- the DEEP per-trajectory
  artifact: the full ``all_runs``/``successful_runs`` aggregate sets (p50/p95/avg
  per metric) + ``runs[].steps[]`` (per-step drill-down scoped to each run).
  ``replay_result.json`` is one-per-trajectory but a trajectory that ran on
  multiple sandboxes (pool < N) carries one ``runs[]`` entry per run.

**stderr is unavailable by design.** The lifecycle series excludes raw
stdout/stderr to stay compact and backend-agnostic; a ``step`` event carries
timings + ``exit_code`` + ``slice_failed``/``timed_out`` flags + a compact,
single-line, truncated (``ACTION_SERIES_LIMIT``) ``action`` text but NO error
text and NO full action body. The full action lives in the trajectory
``*.replay.json`` source -- the series ``action`` is enough to identify which
command ran without joining back. The only failure signal the series carries
beyond per-step is the trajectory-level ``error_type`` + ``error`` (a short
string, ``[:120]``) on ``trajectory_create(success=False)`` /
``trajectory_kill(success=False)`` events. So ``create_error``/``kill_error``
is what a user sees for a run that failed at create/kill, and a *successful*
run whose individual steps failed (``exit_code != 0``) shows only
``return_code``/``timed_out``/``slice_failed`` per step -- no error text. The
``stderr: null`` in each step is therefore by-design, not a bug.

**Naming collision.** The reference replay-aenv-main per-step ``paused_sec``
is the think-time gap *between* slices; bench-core's series field
``pause_sec`` is the pause-API duration. Both appear here: ``paused_sec``
(reconstructed think gap, for reference parity) and ``pause_sec`` (API
duration, a bench-core extra).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from bench_core.observability.lifecycle_series import load_events
from bench_core.observability.trajectory_summary import (
    SEG_KEYS,
    aggregate_trajectories,
    trajectory_summaries,
)
from bench_core.utils import _atomic_write_text, calc_percentiles

_UNSAFE_TID = re.compile(r"[^A-Za-z0-9._-]")

# SEG_KEY -> short key used in the flat ``time_breakdown_sec`` dict (kept
# aligned with oversub._BREAKDOWN_KEY_TO_COL so the CSV reader maps each seg).
_BREAKDOWN_SHORT = {
    "slice_total_sec": "slice_total",
    "exec_sec": "exec",
    "resume_sec": "resume",
    "pause_sec": "pause",
    "interaction_total_sec": "interaction_total",
    "slot_contention_wait_sec": "slot_contention_wait",
    "natural_delay_sec": "natural_delay",
    "capacity_wait_sec": "capacity_wait",
    "rate_pacing_wait_sec": "rate_pacing_wait",
    "inflight_wait_sec": "inflight_wait",
    "resume_rate_pacing_wait_sec": "resume_rate_pacing_wait",
    "pause_rate_pacing_wait_sec": "pause_rate_pacing_wait",
    "resume_inflight_wait_sec": "resume_inflight_wait",
    "pause_inflight_wait_sec": "pause_inflight_wait",
    "running_slot_held_sec": "running_slot_held",
}


def _sort_key(x) -> tuple:
    """Sort None last without comparing None to int (avoids TypeError)."""
    return (x is None, x)


def export_trajectories(
    series_path: Path | str,
    output_dir: Path | str,
    *,
    filename_prefix: str | None = None,
) -> int:
    """Load the lifecycle series and write one ``replay_result.json`` per trajectory.

    Also writes ``<output_dir>/trajectories/index.json`` -- a browsable
    top-level catalog (one aggregate row per trajectory, plus a slim ``runs[]``
    array the oversub driver expands into per-run CSV rows) so a fleet of
    dozens/hundreds of trajectories is navigable without walking folders.

    Returns the number of trajectories written. No-op (``0``) if the series
    file is missing or carries no trajectory events. ``filename_prefix`` is
    accepted for forward-compat but the per-trajectory path is
    ``<output_dir>/trajectories/<sanitized_tid>/replay_result.json`` (the run
    already has a unique ``output_dir``).
    """
    events = load_events(Path(series_path))
    per_run = trajectory_summaries(events)
    if not per_run:
        return 0
    aggregate = aggregate_trajectories(per_run)

    base = Path(output_dir) / "trajectories"
    base.mkdir(parents=True, exist_ok=True)

    # Per-run step drill-down, keyed by (tid, sandbox, round) so each run record
    # carries its own steps[] in step_index order. (trajectory_summaries already
    # computed per-run sums/elapsed; this is the per-step drill-down.)
    steps_by_run: dict[tuple, list[dict]] = {}
    for ev in events:
        if ev.get("event") == "step":
            key = (ev.get("trajectory_id") or "", ev.get("sandbox_index"), ev.get("round_id"))
            steps_by_run.setdefault(key, []).append(ev)

    runs_by_tid: dict[str, list[dict]] = {}
    for r in per_run:
        runs_by_tid.setdefault(r["trajectory_id"], []).append(r)

    index_rows: list[dict] = []
    for agg in aggregate:
        tid = agg["trajectory_id"]
        run_sums = sorted(
            runs_by_tid.get(tid, []),
            key=lambda r: (_sort_key(r["sandbox_index"]), _sort_key(r["round_id"])),
        )
        run_records = []
        for r in run_sums:
            steps = sorted(
                steps_by_run.get((r["trajectory_id"], r["sandbox_index"], r["round_id"]), []),
                key=lambda e: e.get("step_index", 0),
            )
            run_records.append(_build_run_record(r, steps))
        record = _build_tid_record(agg, run_records)
        sanitized = _sanitize_tid(tid)
        sub = base / sanitized
        sub.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(sub / "replay_result.json", json.dumps(record, indent=2) + "\n")
        index_rows.append(_index_row(agg, run_sums, sanitized))

    index = {"n_trajectories": len(index_rows), "trajectories": index_rows}
    _atomic_write_text(base / "index.json", json.dumps(index, indent=2) + "\n")
    return len(index_rows)


def _sanitize_tid(tid: str) -> str:
    """Map an arbitrary trajectory_id to a safe, collision-free directory name.

    Replace path/unsafe chars with ``_``; strip leading/trailing ``.`` (so
    ``..`` / ``.`` can't traverse); fall back to ``unknown`` if empty; cap to
    40 chars; always append ``_<sha1(tid)[:8]>`` -- the hash is over the
    ORIGINAL tid, so two distinct tids that sanitize to the same base (e.g.
    ``a/b`` and ``a:b``) still get distinct directories, and no sanitization
    can collide or escape the ``trajectories/`` root.
    """
    raw = tid or ""
    name = _UNSAFE_TID.sub("_", raw).strip(".")
    if not name:
        name = "unknown"
    name = name[:40]
    suffix = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{name}_{suffix}"


def _flat_breakdown(sums: dict, requested_delay: float, create: float, kill: float) -> dict:
    """Flat short-key breakdown dict from a single run's sums (for index runs[])."""
    bd = {_short: round(float(sums[seg]), 6) for seg, _short in _BREAKDOWN_SHORT.items()}
    bd["requested_delay"] = round(float(requested_delay), 6)
    bd["create"] = round(float(create), 6)
    bd["kill"] = round(float(kill), 6)
    return bd


def _flat_breakdown_p50(set_dict: dict) -> dict:
    """Flat short-key breakdown of p50 values from an aggregate set (for index top level)."""
    tbs = set_dict["time_breakdown_sec"]
    bd = {_short: tbs[seg]["p50"] for seg, _short in _BREAKDOWN_SHORT.items()}
    bd["requested_delay"] = set_dict["requested_delay_sec"]["p50"]
    bd["create"] = set_dict["create_sec"]["p50"]
    bd["kill"] = set_dict["kill_sec"]["p50"]
    return bd


def _build_run_record(s: dict, steps: list[dict]) -> dict:
    """Assemble the deep per-run record (one entry in replay_result.json ``runs[]``).

    ``elapsed_sec``/``requested_delay_sec`` come straight from the per-run
    summary (trajectory_summaries already computed them from the step stamps) --
    no re-derivation here. This fn adds the per-run overhead + per-segment
    distribution + per-step enrichment that need the step list.
    """
    sums = s["sums"]
    n = s["n_steps"]

    # Inter-step think gaps (the reference's paused_sec). step i>0 ->
    # max(0, resume_start[i] - pause_end[i-1]); step 0 -> 0.0. Sentinel guard:
    # a failed step's stamps are 0.0 (not None); drop <=0 sentinels or the
    # cross-step gap becomes ~1.8e9s.
    paused_secs: list[float] = []
    for i, ev in enumerate(steps):
        gap = 0.0
        if i > 0:
            prev = steps[i - 1]
            pe = prev.get("pause_end")
            rs = ev.get("resume_start")
            if pe is not None and rs is not None and float(pe) > 0 and float(rs) > 0:
                gap = max(0.0, float(rs) - float(pe))
        paused_secs.append(gap)

    # Overhead decomposition (reference's pause_resume_overhead), from this run's sums.
    resume_pause_total = sums["resume_sec"] + sums["pause_sec"]
    per_cycle = resume_pause_total / n if n else 0.0
    slice_total = sums["slice_total_sec"]
    pct = (resume_pause_total / slice_total * 100) if slice_total else 0.0
    overhead = {
        "pause_resume_total_sec": round(resume_pause_total, 6),
        "per_cycle_sec": round(per_cycle, 6),
        "pct_of_slice_total": round(pct, 6),
    }

    # Per-segment distribution. EXCLUDE failed steps (slice_failed=True, all-zero
    # durations) -- zeros would skew p50/avg toward 0; n_failed is reported
    # separately so the failure count isn't lost. Sums above still include
    # failed steps at 0.0 (honest total-cost attribution).
    aggregates: dict[str, dict] = {}
    for k in SEG_KEYS:
        vals: list[float] = []
        for ev in steps:
            if ev.get("slice_failed"):
                continue
            v = ev.get(k)
            vals.append(float(v) if v is not None else 0.0)
        agg = calc_percentiles(vals)
        agg["n"] = len(vals)
        aggregates[k] = agg

    enriched = [_enrich_step(i, ev, gap) for i, (ev, gap) in enumerate(zip(steps, paused_secs))]
    success = s["success_rate"] is not None and s["success_rate"] >= 1.0

    return {
        "sandbox_index": s["sandbox_index"],
        "round_id": s["round_id"],
        "n_steps": n,
        "n_failed": s["n_failed"],
        "n_timeout": s["n_timeout"],
        "success_rate": s["success_rate"],
        "success": success,
        "elapsed_sec": s["elapsed_sec"],
        "requested_delay_sec": s["requested_delay_sec"],
        "create_sec": round(s["create_sec"], 6),
        "kill_sec": round(s["kill_sec"], 6),
        "create_error_type": s["create_error_type"],
        "kill_error_type": s["kill_error_type"],
        "create_error": s["create_error"],
        "kill_error": s["kill_error"],
        "sums": {k: round(sums[k], 6) for k in SEG_KEYS},
        "overhead": overhead,
        "aggregates": aggregates,
        "steps": enriched,
    }


def _build_tid_record(agg: dict, run_records: list[dict]) -> dict:
    """Assemble the per-trajectory replay_result.json: aggregate + runs[].steps[]."""
    # Per-trajectory overhead derived from the headline set's p50 resume/pause/
    # slice/n_steps (successful_runs when present, else all_runs).
    use_set = agg["successful_runs"] or agg["all_runs"]
    tbs = use_set["time_breakdown_sec"]
    resume_p50 = tbs["resume_sec"]["p50"]
    pause_p50 = tbs["pause_sec"]["p50"]
    slice_p50 = tbs["slice_total_sec"]["p50"]
    n_steps_p50 = use_set["n_steps"]["p50"]
    rpt = resume_p50 + pause_p50
    overhead = {
        "pause_resume_total_sec": round(rpt, 6),
        "per_cycle_sec": round(rpt / n_steps_p50, 6) if n_steps_p50 else 0.0,
        "pct_of_slice_total": round(rpt / slice_p50 * 100, 6) if slice_p50 else 0.0,
    }
    return {
        "trajectory_id": agg["trajectory_id"],
        "n_runs": agg["n_runs"],
        "n_success": agg["n_success"],
        "n_failed": agg["n_failed"],
        "success_rate": agg["success_rate"],
        "elapsed_sec": agg["elapsed_sec"],
        "all_failed": agg["all_failed"],
        "n_steps_max": agg["n_steps_max"],
        "all_runs": agg["all_runs"],
        "successful_runs": agg["successful_runs"],
        "overhead": overhead,
        "runs": run_records,
    }


def _enrich_step(index: int, ev: dict, paused_sec: float) -> dict:
    """Map a series step event to the replay_result.json per-step shape.

    Reference fields (index/action_type/pause_sec/paused_sec/resume_sec/
    elapsed_sec/return_code/timed_out/slice_failed/stderr) + the bench-core
    sub-segments. ``elapsed_sec`` mirrors the reference (= exec duration);
    ``stderr`` is always null (see module docstring).
    """

    def _f(key: str) -> float:
        v = ev.get(key)
        return round(float(v), 6) if v is not None else 0.0

    return {
        "index": ev.get("step_index", index),
        "action_type": ev.get("action_type"),
        "action": ev.get("action"),
        "pause_sec": _f("pause_sec"),
        "paused_sec": round(paused_sec, 6),
        "resume_sec": _f("resume_sec"),
        "elapsed_sec": _f("exec_sec"),
        "return_code": ev.get("exit_code"),
        "timed_out": bool(ev.get("timed_out")),
        "slice_failed": bool(ev.get("slice_failed")),
        "stderr": None,
        "slice_total_sec": _f("slice_total_sec"),
        "interaction_total_sec": _f("interaction_total_sec"),
        "slot_contention_wait_sec": _f("slot_contention_wait_sec"),
        # Wait-decoupling split (the four independent components + per-phase
        # inflight). slot_contention_wait_sec stays as the natural_delay +
        # capacity_wait composite; rate_pacing/inflight are the QPS limiter's
        # rate-shaping / fuse-block contributions (resume rate-pacing is
        # pre-lease, so NOT in resume_sec; pause rate-pacing is in-lease).
        "natural_delay_sec": _f("natural_delay_sec"),
        "capacity_wait_sec": _f("capacity_wait_sec"),
        "rate_pacing_wait_sec": _f("rate_pacing_wait_sec"),
        "inflight_wait_sec": _f("inflight_wait_sec"),
        "resume_rate_pacing_wait_sec": _f("resume_rate_pacing_wait_sec"),
        "resume_api_sec": _f("resume_api_sec"),
        "resume_ready_wait_sec": _f("resume_ready_wait_sec"),
        "resume_inflight_wait_sec": _f("resume_inflight_wait_sec"),
        "pause_rate_pacing_wait_sec": _f("pause_rate_pacing_wait_sec"),
        "pause_api_sec": _f("pause_api_sec"),
        "pause_inflight_wait_sec": _f("pause_inflight_wait_sec"),
        "running_slot_held_sec": _f("running_slot_held_sec"),
    }


def _index_row(agg: dict, run_summaries: list[dict], sanitized: str) -> dict:
    """Lean per-trajectory row for trajectories/index.json (no steps[]/aggregates).

    Top level = the per-trajectory aggregate headline (``n_runs``/``n_success``/
    ``n_failed``/``elapsed_sec`` success-median + ``time_breakdown_sec`` of p50s
    for browsability / old readers). ``runs[]`` is the slim per-run array the
    oversub driver expands into one ``trajectory-detail.csv`` row per run so a
    reader gets per-run latency at any pool/N ratio.
    """
    use_set = agg["successful_runs"] or agg["all_runs"]
    runs_slim = [
        {
            "sandbox_index": r["sandbox_index"],
            "round_id": r["round_id"],
            "elapsed_sec": r["elapsed_sec"],
            "n_steps": r["n_steps"],
            "n_failed": r["n_failed"],
            "n_timeout": r["n_timeout"],
            "success_rate": r["success_rate"],
            "success": r["success_rate"] is not None and r["success_rate"] >= 1.0,
            "create_error_type": r["create_error_type"],
            "kill_error_type": r["kill_error_type"],
            "time_breakdown_sec": _flat_breakdown(r["sums"], r["requested_delay_sec"], r["create_sec"], r["kill_sec"]),
        }
        for r in run_summaries
    ]
    # First-seen error types across runs (a run that failed at create surfaces
    # its error_type so the worst case is visible at the per-trajectory level).
    create_err_type = next((r["create_error_type"] for r in run_summaries if r["create_error_type"]), None)
    kill_err_type = next((r["kill_error_type"] for r in run_summaries if r["kill_error_type"]), None)
    return {
        "trajectory_id": agg["trajectory_id"],
        "n_runs": agg["n_runs"],
        "n_success": agg["n_success"],
        "n_failed": agg["n_failed"],
        "success_rate": agg["success_rate"],
        "elapsed_sec": agg["elapsed_sec"],
        "all_failed": agg["all_failed"],
        "n_steps_max": agg["n_steps_max"],
        "time_breakdown_sec": _flat_breakdown_p50(use_set),
        "runs": runs_slim,
        "create_error_type": create_err_type,
        "kill_error_type": kill_err_type,
        "file": f"{sanitized}/replay_result.json",
    }
