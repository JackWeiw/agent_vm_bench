"""Shared per-trajectory aggregation over a loaded event list.

Pure transform (no I/O) -- both the obs_xlsx Trajectory summary sheet (DRY:
replaces the inline sum loop) and the per-trajectory replay_result.json
exporter call this. Union-keyed over ``step`` + ``trajectory_create`` +
``trajectory_kill`` + ``trajectory_failed`` events so a trajectory that failed
at create (zero step events) still appears -- otherwise the worst trajectories
vanish from the comparison.

Record *assembly* (the per-step dict shape) stays in the replay runner; this
module only reads the loaded dicts. Sums are raw floats -- callers round at the
render boundary (the existing avg_slice==0.7 assertion depends on no
pre-rounding here).
"""
from __future__ import annotations

# The nine per-step duration fields summed per trajectory. slice_total_sec is
# the invariant total (resume+exec+pause); interaction_total_sec adds delay +
# capacity_wait (>= slice). The three wait sums (slot / resume_queue /
# pause_queue) isolate non-productive time: admission contention + QPS-limiter
# queueing. Exported so obs_xlsx imports it back instead of keeping a local copy
# (DRY).
SEG_KEYS = (
    "slice_total_sec",
    "exec_sec",
    "resume_sec",
    "pause_sec",
    "interaction_total_sec",
    "slot_contention_wait_sec",
    "resume_queue_wait_sec",
    "pause_queue_wait_sec",
    "running_slot_held_sec",
)


def trajectory_summaries(events: list[dict]) -> list[dict]:
    """Group step + trajectory_create/kill/failed events by ``trajectory_id``.

    Returns one dict per trajectory, sorted by trajectory_id. Each dict:
      ``{trajectory_id, sandbox_index, round_id, n_steps, n_failed, n_timeout,
      success_rate, sums, avg_slice, create_sec, kill_sec,
      create_error_type, kill_error_type, create_error, kill_error}``.

    - **Sums include failed steps at 0.0** (honest cost attribution; a failed
      slice did no work but still counts as an attempted step so avg_slice
      reflects per-attempt cost). ``n_failed``/``n_timeout`` count failures
      separately.
    - ``success_rate`` is ``None`` when ``n_steps==0`` (no steps attempted,
      distinct from ``0.0`` = all steps failed); guards ZeroDivisionError.
    - ``create_sec``/``kill_sec`` SUM across all create/kill events for that tid
      (round-robin/multi-round recurrence -> total lifecycle cost, not a
      per-instance average) -- matches the step-sum semantics.
    - ``create_error``/``kill_error`` carry the short error STRING from a
      ``trajectory_create(success=False)``/``trajectory_kill(success=False)``
      event (the only failure signal the series carries; per-step stderr is
      unavailable by design).
    """
    tids: dict[str, dict] = {}

    def _slot(tid: str) -> dict:
        return tids.setdefault(
            tid,
            {
                "trajectory_id": tid,
                "sandbox_index": None,
                "round_id": None,
                "n_steps": 0,
                "n_failed": 0,
                "n_timeout": 0,
                "sums": {k: 0.0 for k in SEG_KEYS},
                "create_sec": 0.0,
                "kill_sec": 0.0,
                "create_error_type": None,
                "kill_error_type": None,
                "create_error": None,
                "kill_error": None,
            },
        )

    for ev in events:
        evtype = ev.get("event")
        if evtype == "step":
            tid = ev.get("trajectory_id") or ""
            acc = _slot(tid)
            acc["n_steps"] += 1
            if ev.get("slice_failed"):
                acc["n_failed"] += 1
            if ev.get("timed_out"):
                acc["n_timeout"] += 1
            if acc["sandbox_index"] is None and ev.get("sandbox_index") is not None:
                acc["sandbox_index"] = ev.get("sandbox_index")
            if acc["round_id"] is None and ev.get("round_id") is not None:
                acc["round_id"] = ev.get("round_id")
            sums = acc["sums"]
            for k in SEG_KEYS:
                v = ev.get(k)
                if v is not None:
                    sums[k] += float(v)
        elif evtype in ("trajectory_create", "trajectory_kill"):
            tid = ev.get("trajectory_id")
            if not tid:
                continue
            acc = _slot(tid)
            if acc["sandbox_index"] is None and ev.get("sandbox_index") is not None:
                acc["sandbox_index"] = ev.get("sandbox_index")
            if evtype == "trajectory_create":
                v = ev.get("create_sec")
                if v is not None:
                    acc["create_sec"] += float(v)
                if ev.get("success") is False:
                    acc["create_error_type"] = ev.get("error_type")
                    acc["create_error"] = ev.get("error")
            else:  # trajectory_kill
                v = ev.get("kill_sec")
                if v is not None:
                    acc["kill_sec"] += float(v)
                if ev.get("success") is False:
                    acc["kill_error_type"] = ev.get("error_type")
                    acc["kill_error"] = ev.get("error")
        elif evtype == "trajectory_failed":
            tid = ev.get("trajectory_id")
            if not tid:
                continue
            acc = _slot(tid)
            if acc["sandbox_index"] is None and ev.get("sandbox_index") is not None:
                acc["sandbox_index"] = ev.get("sandbox_index")
            v = ev.get("create_sec")
            if v is not None:
                acc["create_sec"] += float(v)
            v = ev.get("kill_sec")
            if v is not None:
                acc["kill_sec"] += float(v)

    out = []
    for tid in sorted(tids):
        acc = tids[tid]
        n = acc["n_steps"]
        slice_sum = acc["sums"]["slice_total_sec"]
        acc["avg_slice"] = slice_sum / n if n else 0.0
        n_success = n - acc["n_failed"]
        acc["success_rate"] = round(n_success / n, 6) if n else None
        out.append(acc)
    return out
