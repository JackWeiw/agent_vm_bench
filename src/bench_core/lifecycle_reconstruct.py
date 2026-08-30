"""Pure transforms over loaded lifecycle-series events.

No I/O -- takes a ``list[dict]`` (from :func:`bench_core.lifecycle_series.load_events`)
and produces the shapes the xlsx renderer draws: per-second concurrency-state bins,
per-sandbox Gantt phase segments, and per-pause snapshot-size rows. Keeping this
out of ``observability.py`` (which is contractually I/O-free) is why the reverted
``180e46c`` placement was wrong.
"""
from __future__ import annotations

# Phase order matters: ties in dominant-state selection resolve to the earlier
# phase (mirrors the reference's max() over phases.index ordering).
PHASES = ("pausing", "paused", "resuming", "exec")

# (start_key, end_key, phase) -- the four segments derived from a step's
# timestamp pairs. pause_start->pause_end = pausing (the pause API call);
# pause_end->resume_start = paused (no CPU); resume_start->resume_end =
# resuming; resume_end->exec_end = exec.
_STEP_SEGMENTS = (
    ("pause_start", "pause_end", "pausing"),
    ("pause_end", "resume_start", "paused"),
    ("resume_start", "resume_end", "resuming"),
    ("resume_end", "exec_end", "exec"),
)


def _step_segments(s: dict) -> list[tuple[float, float, str]]:
    """Derive (start, end, phase) segments from one step event's timestamps.

    Drops any segment whose endpoints are missing, whose end is non-positive
    (all-zero failed step), or where end <= start (degenerate/empty).
    """
    segs: list[tuple[float, float, str]] = []
    for sk, ek, ph in _STEP_SEGMENTS:
        a = s.get(sk)
        b = s.get(ek)
        if a is None or b is None:
            continue
        if b <= 0 or b <= a:
            continue
        segs.append((a, b, ph))
    return segs


def reconstruct_concurrency(events: list[dict]) -> list[dict]:
    """Per-second dominant sandbox state counts.

    For each sandbox, for each second, accumulate each phase's fractional
    occupancy via left-closed right-open interval intersection
    ``max(0, min(b, sec+1) - max(a, sec))``; the longest-occupancy phase is
    that second's dominant state (so per-state counts sum to the sandbox
    count). Returns one row per second:
    ``{second, pausing, paused, resuming, exec, active}`` where
    ``active = pausing + resuming + exec``.
    """
    segs_by_task: list[list[tuple[float, float, str]]] = []
    all_segs: list[tuple[float, float, str]] = []
    for e in events:
        if e.get("event") != "step":
            continue
        segs = _step_segments(e)
        if segs:
            segs_by_task.append(segs)
            all_segs.extend(segs)
    if not all_segs:
        return []
    t0 = int(min(a for a, _, _ in all_segs))
    t1 = int(max(b for _, b, _ in all_segs)) + 1
    n_sec = t1 - t0 + 1
    pi = {ph: i for i, ph in enumerate(PHASES)}

    # per-task dominant phase index per second
    per_task: list[list[int]] = []
    for task_segs in segs_by_task:
        dur = [[0.0] * len(PHASES) for _ in range(n_sec)]
        for a, b, ph in task_segs:
            lo, hi = int(a), int(b)
            for sec in range(max(lo, t0), min(hi, t1) + 1):
                dur[sec - t0][pi[ph]] += max(0.0, min(b, sec + 1) - max(a, sec))
        per_task.append([max(range(len(PHASES)), key=lambda p: d[p]) if any(d) else -1 for d in dur])

    rows: list[dict] = []
    for i in range(n_sec):
        cnt = {ph: 0 for ph in PHASES}
        for tp in per_task:
            if 0 <= tp[i] < len(PHASES):
                cnt[PHASES[tp[i]]] += 1
        active = cnt["pausing"] + cnt["resuming"] + cnt["exec"]
        rows.append(
            {
                "second": i,
                "pausing": cnt["pausing"],
                "paused": cnt["paused"],
                "resuming": cnt["resuming"],
                "exec": cnt["exec"],
                "active": active,
            }
        )
    return rows
