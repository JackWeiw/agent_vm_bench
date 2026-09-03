"""Pure transforms over loaded lifecycle-series events.

No I/O -- takes a ``list[dict]`` (from :func:`bench_core.observability.lifecycle_series.load_events`)
and produces the shapes the xlsx renderer draws: per-second concurrency-state bins,
per-sandbox Gantt phase segments, and per-pause snapshot-size rows. Keeping this
out of ``replay_obs.py`` (which is contractually I/O-free) is why the reverted
``180e46c`` placement was wrong.
"""
from __future__ import annotations

# Phase order matters: ties in dominant-state selection resolve to the earlier
# phase (mirrors the reference's max() over phases.index ordering).
PHASES = ("pausing", "paused", "resuming", "exec")

# Within-step segments -- positive-duration slices derivable from one step
# event's own timestamp pairs. ``paused`` is deliberately ABSENT: a step's
# resume_start precedes its pause_end (you resume, then later pause), so the
# no-CPU idle "paused" gap is a *cross-step* interval (step N's pause_end ->
# step N+1's resume_start, or the initial pause_end -> step 0's resume_start).
# It is reconstructed per-sandbox in :func:`_sandbox_segments`, never from one
# step's timestamps -- the old within-step pairing read pause_end/resume_start
# off the same event and was always backwards, so ``paused`` was always 0.
_STEP_SEGMENTS = (
    ("resume_start", "resume_end", "resuming"),
    ("resume_end", "exec_end", "exec"),
    ("pause_start", "pause_end", "pausing"),
)

# Lifecycle-mode event types that prove the run did real pause/resume work
# (lifecycle emits ``initial_pause``; trajectory emits ``trajectory_create`` /
# ``trajectory_kill``). exec_only emits none of these -- resume/pause are
# no-ops -- so its think-time idle must not be relabelled ``paused`` (the
# sandbox never snapshotted). Used to gate cross-step ``paused`` reconstruction.
_LIFECYCLE_EVENTS = frozenset({"initial_pause", "trajectory_create", "trajectory_kill"})


def _segment(a, b, phase: str) -> tuple[float, float, str] | None:
    """A ``(start, end, phase)`` tuple only when the interval has positive
    width; ``None`` for missing timestamps or non-positive / empty intervals
    (covers all-zero failed-step timestamps and degenerate no-op calls)."""
    if a is None or b is None:
        return None
    if b <= 0 or b <= a:
        return None
    return (a, b, phase)


def _within_step_segments(s: dict) -> list[tuple[float, float, str]]:
    """The three within-step phases (resuming / exec / pausing) for one step
    event, dropping degenerate slices."""
    segs: list[tuple[float, float, str]] = []
    for sk, ek, ph in _STEP_SEGMENTS:
        seg = _segment(s.get(sk), s.get(ek), ph)
        if seg is not None:
            segs.append(seg)
    return segs


def _step_spans(steps: list[dict], has_kills: bool) -> list[list[dict]]:
    """Split a sandbox's steps (sorted by resume_start) into continuous alive
    spans.

    lifecycle (``has_kills`` False): the sandbox never dies -- one span covers
    the whole run, so the cross-round idle between trajectories also counts as
    ``paused``. trajectory (``has_kills`` True): a kill ends each trajectory, so
    spans split at ``trajectory_id`` changes; the kill->create dead window is
    not ``paused`` (the sandbox was destroyed, not snapshotted) and is never
    paired across.
    """
    if not has_kills:
        return [steps]
    spans: list[list[dict]] = []
    cur: list[dict] = []
    cur_id = None
    for s in steps:
        tid = s.get("trajectory_id")
        if tid != cur_id:
            if cur:
                spans.append(cur)
            cur, cur_id = [], tid
        cur.append(s)
    if cur:
        spans.append(cur)
    return spans


def _sandbox_segments(
    steps: list[dict],
    initial_pause: dict | None,
    *,
    emit_paused_gaps: bool,
) -> list[tuple[float, float, str]]:
    """Ordered ``(start, end, phase)`` timeline for one continuous alive span.

    Composes the within-step phases (resuming / exec / pausing) with the
    cross-step ``paused`` idle gaps that single-step timestamp pairs cannot
    express: the no-CPU wait from a pause boundary (the initial pause or step
    N's ``pause_end``) to the next step's ``resume_start``.

    ``steps`` are one alive span's step events sorted by ``resume_start``.
    ``initial_pause`` is the sandbox's ``initial_pause`` event, attached only to
    the first span (lifecycle: the only span; trajectory: ``None`` -- no
    initial pause). ``emit_paused_gaps`` gates the cross-step idle
    reconstruction off for exec_only (no real pauses -> no paused state).
    """
    segs: list[tuple[float, float, str]] = []
    prev_pause_end = None
    if initial_pause is not None:
        # The one-time initial pause: the pause API call (CPU-active -> pausing),
        # then idle up to the first step's resume (-> paused).
        p = _segment(initial_pause.get("pause_start"), initial_pause.get("pause_end"), "pausing")
        if p is not None:
            segs.append(p)
        prev_pause_end = initial_pause.get("pause_end")
    for step in steps:
        if emit_paused_gaps:
            gap = _segment(prev_pause_end, step.get("resume_start"), "paused")
            if gap is not None:
                segs.append(gap)
        segs.extend(_within_step_segments(step))
        prev_pause_end = step.get("pause_end")
    return segs


def _segments_by_sandbox(events: list[dict]) -> dict[int, list[tuple[float, float, str]]]:
    """Per-sandbox ordered phase timelines (all alive spans concatenated).

    Shared by :func:`reconstruct_concurrency` (per-second binning) and
    :func:`gantt_segments` (per-sandbox rendering) so both draw from one source
    of truth. Steps with no ``sandbox_index`` are skipped (cannot be attributed
    to a Gantt row).
    """
    initial_pauses: dict[int, dict] = {}
    steps_by_sandbox: dict[int, list[dict]] = {}
    emit_paused = False
    for e in events:
        ev = e.get("event")
        if ev == "initial_pause":
            sbx = e.get("sandbox_index")
            if sbx is not None:
                initial_pauses.setdefault(sbx, e)  # first wins; idempotent guard
        elif ev == "step":
            sbx = e.get("sandbox_index")
            if sbx is None:
                continue
            steps_by_sandbox.setdefault(sbx, []).append(e)
        if ev in _LIFECYCLE_EVENTS:
            emit_paused = True  # lifecycle/trajectory signal present somewhere

    has_kills = any(e.get("event") in ("trajectory_create", "trajectory_kill") for e in events)

    out: dict[int, list[tuple[float, float, str]]] = {}
    for sbx, sb_steps in steps_by_sandbox.items():
        sb_steps.sort(key=lambda s: s.get("resume_start") or 0.0)
        init = initial_pauses.get(sbx)
        all_segs: list[tuple[float, float, str]] = []
        for si, span in enumerate(_step_spans(sb_steps, has_kills)):
            # initial_pause leads only the first span (lifecycle: the only one;
            # trajectory: init is None regardless).
            span_init = init if si == 0 else None
            all_segs.extend(_sandbox_segments(span, span_init, emit_paused_gaps=emit_paused))
        if all_segs:
            out[sbx] = all_segs
    return out


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
    by_sandbox = _segments_by_sandbox(events)
    all_segs = [seg for segs in by_sandbox.values() for seg in segs]
    if not all_segs:
        return []
    t0 = int(min(a for a, _, _ in all_segs))
    t1 = int(max(b for _, b, _ in all_segs)) + 1
    n_sec = t1 - t0 + 1
    pi = {ph: i for i, ph in enumerate(PHASES)}

    # per-sandbox dominant phase index per second
    per_task: list[list[int]] = []
    for _, segs in by_sandbox.items():
        dur = [[0.0] * len(PHASES) for _ in range(n_sec)]
        for a, b, ph in segs:
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


def gantt_segments(events: list[dict]) -> list[tuple[str, list[tuple[float, float, str]]]]:
    """Per-sandbox ``(label, [(start, end, phase), ...])`` for the Gantt chart.

    Sandboxes are sorted by earliest event time. ``label`` is ``sbx<index>``.
    """
    by_sandbox = _segments_by_sandbox(events)
    rows = [(f"sbx{idx}", segs) for idx, segs in by_sandbox.items() if segs]
    rows.sort(key=lambda r: min(a for a, _, _ in r[1]))
    return rows


_MIB = 1024 * 1024


def snapshot_rows(events: list[dict]) -> list[dict]:
    """Per-pause snapshot-size rows from ``snapshot_size`` series events.

    Converts bytes -> MiB. Only ``event == "snapshot_size"`` rows are kept.
    """
    rows: list[dict] = []
    for e in events:
        if e.get("event") != "snapshot_size":
            continue
        rows.append(
            {
                "pause_seq": e.get("pause_seq"),
                "sandbox_index": e.get("sandbox_index"),
                "sandbox_id": e.get("sandbox_id"),
                "logical_mb": round((e.get("logical_bytes") or 0) / _MIB, 3),
                "disk_mb": round((e.get("disk_bytes") or 0) / _MIB, 3),
                "inherited_mb": round((e.get("inherited_bytes") or 0) / _MIB, 3),
                "cumulative_mb": round((e.get("cumulative_bytes") or 0) / _MIB, 3),
                "generations": e.get("generations"),
                "files": e.get("files"),
            }
        )
    return rows
