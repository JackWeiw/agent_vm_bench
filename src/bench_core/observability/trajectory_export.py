"""Per-trajectory ``replay_result.json`` export + ``trajectories/index.json`` catalog.

**stderr is unavailable by design.** The lifecycle series excludes raw
stdout/stderr to stay compact and backend-agnostic; a ``step`` event carries
timings + ``exit_code`` + ``slice_failed``/``timed_out`` flags but NO error
text. The only failure signal the series carries is the trajectory-level
``error_type`` + ``error`` (a short string, ``[:120]``) on
``trajectory_create(success=False)`` / ``trajectory_kill(success=False)``
events. So ``create_error``/``kill_error`` is what a user sees for a
trajectory that failed at create/kill, and a *successful* trajectory whose
individual steps failed (``exit_code != 0``) shows only
``return_code``/``timed_out``/``slice_failed`` per step -- no error text. The
``stderr: null`` in each step is therefore by-design, not a bug. (Long-term,
out of scope here: an optional runner config to emit a truncated step-error
summary into the series for failed slices.)

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
from bench_core.observability.trajectory_summary import SEG_KEYS, trajectory_summaries
from bench_core.utils import calc_percentiles

# Fields excluded from the per-step distribution (aggregates). The 9 SEG_KEYS
# are the durations worth distributing; flag/identity fields are not.
_UNSAFE_TID = re.compile(r"[^A-Za-z0-9._-]")


def export_trajectories(
    series_path: Path | str,
    output_dir: Path | str,
    *,
    filename_prefix: str | None = None,
) -> int:
    """Load the lifecycle series and write one ``replay_result.json`` per trajectory.

    Also writes ``<output_dir>/trajectories/index.json`` -- a browsable
    top-level catalog (one short-summary row per trajectory) so a fleet of
    dozens/hundreds of trajectories is navigable without walking folders.

    Returns the number of trajectories written. No-op (``0``) if the series
    file is missing or carries no trajectory events. ``filename_prefix`` is
    accepted for forward-compat but the per-trajectory path is
    ``<output_dir>/trajectories/<sanitized_tid>/replay_result.json`` (the run
    already has a unique ``output_dir``).
    """
    events = load_events(Path(series_path))
    summaries = trajectory_summaries(events)
    if not summaries:
        return 0

    base = Path(output_dir) / "trajectories"
    base.mkdir(parents=True, exist_ok=True)

    # Index step events by trajectory_id so each record can carry its steps[]
    # in step_index order. (trajectory_summaries already summed them; this is
    # the per-step drill-down.)
    steps_by_tid: dict[str, list[dict]] = {}
    for ev in events:
        if ev.get("event") == "step":
            tid = ev.get("trajectory_id") or ""
            steps_by_tid.setdefault(tid, []).append(ev)

    index_rows: list[dict] = []
    for s in summaries:
        tid = s["trajectory_id"]
        steps = sorted(steps_by_tid.get(tid, []), key=lambda e: e.get("step_index", 0))
        record = _build_record(s, steps)
        sanitized = _sanitize_tid(tid)
        sub = base / sanitized
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "replay_result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        index_rows.append(_index_row(record, sanitized))

    index = {"n_trajectories": len(index_rows), "trajectories": index_rows}
    (base / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
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


def _build_record(s: dict, steps: list[dict]) -> dict:
    """Assemble the full replay_result.json record from a summary + its steps."""
    sums = s["sums"]
    n = s["n_steps"]

    # Inter-step think gaps (the reference's paused_sec). step i>0 ->
    # max(0, resume_start[i] - pause_end[i-1]); step 0 -> 0.0.
    paused_secs: list[float] = []
    for i, ev in enumerate(steps):
        gap = 0.0
        if i > 0:
            prev = steps[i - 1]
            pe = prev.get("pause_end")
            rs = ev.get("resume_start")
            if pe is not None and rs is not None:
                gap = max(0.0, float(rs) - float(pe))
        paused_secs.append(gap)
    requested_delay = sum(paused_secs)

    # elapsed: prefer wall-clock span from first resume_start to last pause_end;
    # add create+kill lifecycle cost. Fall back to slice_total + idle when
    # timestamps are absent (exec-only / synthetic series).
    resume_starts = [float(e["resume_start"]) for e in steps if e.get("resume_start") is not None]
    pause_ends = [float(e["pause_end"]) for e in steps if e.get("pause_end") is not None]
    create_kill = s["create_sec"] + s["kill_sec"]
    if resume_starts and pause_ends:
        elapsed = max(pause_ends) - min(resume_starts) + create_kill
    else:
        elapsed = sums["slice_total_sec"] + requested_delay + create_kill

    # Overhead decomposition (reference's pause_resume_overhead).
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

    return {
        "trajectory_id": s["trajectory_id"],
        "sandbox_index": s["sandbox_index"],
        "round_id": s["round_id"],
        "n_steps": n,
        "n_failed": s["n_failed"],
        "n_timeout": s["n_timeout"],
        "success_rate": s["success_rate"],
        "elapsed_sec": round(elapsed, 6),
        "requested_delay_sec": round(requested_delay, 6),
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
        "resume_queue_wait_sec": _f("resume_queue_wait_sec"),
        "resume_api_sec": _f("resume_api_sec"),
        "resume_ready_wait_sec": _f("resume_ready_wait_sec"),
        "pause_queue_wait_sec": _f("pause_queue_wait_sec"),
        "pause_api_sec": _f("pause_api_sec"),
        "running_slot_held_sec": _f("running_slot_held_sec"),
    }


def _index_row(rec: dict, sanitized: str) -> dict:
    """Short summary row for trajectories/index.json (no steps[]/aggregates).

    ``time_breakdown_sec`` carries the trajectory-level sums that decompose
    ``elapsed_sec`` into exec / lifecycle-API (resume+pause) / inter-step think
    delay / create+kill cost / queueing waits. Values are the sums already
    rounded in ``_build_record`` (``rec["sums"]`` for the SEG_KEYS +
    ``requested_delay_sec`` / ``create_sec`` / ``kill_sec`` at the record top
    level). Surfacing them in the index -- not just in each replay_result.json --
    lets downstream consumers (the oversub driver's trajectory-detail.csv)
    attribute per-trajectory wall time from one file read per trial instead of
    walking every replay_result.json.
    """
    sums = rec["sums"]
    return {
        "trajectory_id": rec["trajectory_id"],
        "sandbox_index": rec["sandbox_index"],
        "n_steps": rec["n_steps"],
        "n_failed": rec["n_failed"],
        "n_timeout": rec["n_timeout"],
        "success_rate": rec["success_rate"],
        "elapsed_sec": rec["elapsed_sec"],
        "time_breakdown_sec": {
            "slice_total": sums["slice_total_sec"],
            "exec": sums["exec_sec"],
            "resume": sums["resume_sec"],
            "pause": sums["pause_sec"],
            "requested_delay": rec["requested_delay_sec"],
            "create": rec["create_sec"],
            "kill": rec["kill_sec"],
            "interaction_total": sums["interaction_total_sec"],
            "slot_contention_wait": sums["slot_contention_wait_sec"],
            "resume_queue_wait": sums["resume_queue_wait_sec"],
            "pause_queue_wait": sums["pause_queue_wait_sec"],
            "running_slot_held": sums["running_slot_held_sec"],
        },
        "create_error_type": rec["create_error_type"],
        "kill_error_type": rec["kill_error_type"],
        "file": f"{sanitized}/replay_result.json",
    }
