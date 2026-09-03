"""Lifecycle reconstruct tests (P2.5 Task 1-2 -- load_events + reconstruct_concurrency)."""
from __future__ import annotations

import json
from pathlib import Path

from bench_core.observability.lifecycle_series import load_events


# ---------------------------------------------------------------------------
# Task 1: load_events
# ---------------------------------------------------------------------------


def test_load_events_reads_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "series.jsonl"
    p.write_text(
        json.dumps({"event": "step", "step_index": 0}) + "\n" + json.dumps({"event": "step", "step_index": 1}) + "\n",
        encoding="utf-8",
    )
    events = load_events(p)
    assert len(events) == 2
    assert events[0]["step_index"] == 0
    assert events[1]["step_index"] == 1


def test_load_events_skips_malformed_line(tmp_path: Path) -> None:
    p = tmp_path / "series.jsonl"
    p.write_text('{"event": "step"}\nnot json\n{"event": "step", "i": 1}\n', encoding="utf-8")
    events = load_events(p)
    assert len(events) == 2  # malformed middle line skipped


def test_load_events_missing_file(tmp_path: Path) -> None:
    assert load_events(tmp_path / "nope.jsonl") == []


# ---------------------------------------------------------------------------
# Task 2: reconstruct_concurrency
# ---------------------------------------------------------------------------

from bench_core.observability.lifecycle_reconstruct import reconstruct_concurrency


def _step(idx: int, rs: float, re: float, xs: float, xe: float, ps: float, pe: float) -> dict:
    """Build a step event with the 6 timestamp fields reconstruct reads."""
    return {
        "event": "step",
        "sandbox_index": 0,
        "step_index": idx,
        "resume_start": rs,
        "resume_end": re,
        "exec_start": xs,
        "exec_end": xe,
        "pause_start": ps,
        "pause_end": pe,
    }


def test_reconstruct_concurrency_single_sandbox_one_second() -> None:
    # one sandbox: resume 0.0-0.5, exec 0.5-1.5, pause 1.5-2.0
    events = [_step(0, 0.0, 0.5, 0.5, 1.5, 1.5, 2.0)]
    bins = reconstruct_concurrency(events)
    # second 0 (0.0-1.0): resuming [0,0.5)=0.5s vs exec [0.5,1.0)=0.5s
    # tie -> max() returns first maximal index: PHASES[2]=resuming beats PHASES[3]=exec
    assert bins[0]["second"] == 0
    assert bins[0]["resuming"] == 1
    # second 1 (1.0-2.0): exec [1.0,1.5)=0.5s vs pausing [1.5,2.0)=0.5s
    # tie -> PHASES[0]=pausing beats PHASES[3]=exec (earliest index wins)
    assert bins[1]["pausing"] == 1
    assert bins[1]["active"] == 1


def test_reconstruct_concurrency_filters_zeroed_timestamps() -> None:
    # failed step: all timestamps 0 -> filtered, no bins
    events = [_step(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)]
    assert reconstruct_concurrency(events) == []


def test_reconstruct_concurrency_active_excludes_paused() -> None:
    # sandbox A: only pausing 0-1 (pause_start=0, pause_end=1)
    # sandbox B: only exec 0-1 (resume_end=0, exec_end=1)
    events = [
        _step(0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),  # pausing [0,1)
        {
            "event": "step",
            "sandbox_index": 1,
            "step_index": 0,
            "resume_start": 0.0,
            "resume_end": 0.0,
            "exec_start": 0.0,
            "exec_end": 1.0,
            "pause_start": 1.0,
            "pause_end": 1.0,
        },  # exec [0,1)
    ]
    bins = reconstruct_concurrency(events)
    assert bins[0]["pausing"] == 1  # sandbox A: pausing (not "paused")
    assert bins[0]["exec"] == 1  # sandbox B: exec
    # active = pausing + resuming + exec = 1 + 0 + 1 = 2
    assert bins[0]["active"] == 2


def test_reconstruct_concurrency_includes_trailing_empty_bins() -> None:
    # max end = 2.0 -> seconds 0..3 (t1 = int(2.0)+1 = 3, n_sec = 3-0+1 = 4)
    events = [_step(0, 0.0, 0.5, 0.5, 1.0, 1.0, 2.0)]
    bins = reconstruct_concurrency(events)
    assert len(bins) == 4
    assert bins[2]["active"] == 0  # trailing empty
    assert bins[3]["active"] == 0


# ---------------------------------------------------------------------------
# Cross-step "paused" (no-CPU idle) reconstruction.
#
# `paused` is the idle gap between a pause boundary (the initial pause or step
# N's pause_end) and the next step's resume_start. It is NOT expressible from
# one step's timestamp pairs (resume_start < pause_end inside a step), so the
# old within-step segment derivation dropped it -> paused was always 0.
# ---------------------------------------------------------------------------


def _initial_pause(sbx: int, ps: float, pe: float) -> dict:
    """A lifecycle initial_pause event with the two pause timestamps."""
    return {
        "event": "initial_pause",
        "sandbox_index": sbx,
        "pause_start": ps,
        "pause_end": pe,
        "initial_pause_sec": pe - ps,
    }


def _traj_event(name: str, sbx: int, tid: str, ts: float) -> dict:
    """A trajectory lifecycle boundary event (create/kill) carrying sandbox + tid."""
    return {"event": name, "sandbox_index": sbx, "trajectory_id": tid, "timestamp": ts}


def _step_tid(
    idx: int, tid: str, rs: float, re_: float, xs: float, xe: float, ps: float, pe: float, sbx: int = 0
) -> dict:
    """A step event tagged with its trajectory_id (trajectory-mode runs)."""
    s = _step(idx, rs, re_, xs, xe, ps, pe)
    s["sandbox_index"] = sbx
    s["trajectory_id"] = tid
    return s


def test_reconstruct_concurrency_cross_step_paused_gap() -> None:
    # One sandbox, two steps with a 1.8s no-CPU idle gap between step0's
    # pause_end (1.2) and step1's resume_start (3.0). lifecycle signal present
    # via the initial_pause event -> the gap must surface as "paused".
    events = [
        _initial_pause(0, 0.0, 0.2),
        _step(0, 0.5, 0.7, 0.7, 1.0, 1.0, 1.2),  # step0
        _step(1, 3.0, 3.2, 3.2, 3.5, 3.5, 3.7),  # step1; idle 1.2..3.0
    ]
    bins = reconstruct_concurrency(events)
    # second 2 (2.0-3.0) sits fully inside the paused idle gap.
    assert any(b["paused"] > 0 for b in bins), bins
    assert bins[2]["paused"] == 1, bins[2]


def test_reconstruct_concurrency_initial_pause_leads_paused() -> None:
    # The initial pause's API call (0.0..0.5) is "pausing"; the idle from its
    # pause_end (0.5) to step0.resume_start (2.0) is "paused" (no CPU).
    events = [
        _initial_pause(0, 0.0, 0.5),
        _step(0, 2.0, 2.5, 2.5, 3.0, 3.0, 3.5),
    ]
    bins = reconstruct_concurrency(events)
    assert bins[0]["pausing"] == 1, bins[0]  # initial pause API call
    assert bins[1]["paused"] == 1, bins[1]  # 1.0..2.0 fully inside 0.5..2.0


def test_reconstruct_concurrency_exec_only_no_false_paused() -> None:
    # exec_only: no initial_pause, no trajectory create/kill -> no lifecycle
    # signal. Resume/pause are no-ops (instant), so the think-time idle between
    # steps must NOT be relabelled "paused" (the sandbox never snapshotted).
    events = [
        _step(0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0),  # exec 0..1; resume/pause instant
        _step(1, 3.0, 3.0, 3.0, 4.0, 4.0, 4.0),  # exec 3..4; 2s think-time gap
    ]
    bins = reconstruct_concurrency(events)
    assert all(b["paused"] == 0 for b in bins), bins
    assert any(b["exec"] > 0 for b in bins), bins  # exec phases still register


def test_reconstruct_concurrency_trajectory_inter_trajectory_not_paused() -> None:
    # trajectory mode: sandbox 0 runs trajectory A then B, killed in between.
    # Within-trajectory step gaps ARE paused; the kill->create dead window is
    # NOT paused (the sandbox was destroyed, not snapshotted).
    events = [
        _traj_event("trajectory_create", 0, "A", 0.0),
        _step_tid(0, "A", 0.5, 0.6, 0.6, 1.0, 1.0, 1.1),  # A step0
        _step_tid(1, "A", 2.0, 2.1, 2.1, 2.5, 2.5, 2.6),  # A step1; within-gap 1.1..2.0
        _traj_event("trajectory_kill", 0, "A", 2.7),
        _traj_event("trajectory_create", 0, "B", 5.0),
        _step_tid(2, "B", 5.5, 5.6, 5.6, 6.0, 6.0, 6.1),  # B step0
        _step_tid(3, "B", 7.0, 7.1, 7.1, 7.5, 7.5, 7.6),  # B step1; within-gap 6.1..7.0
        _traj_event("trajectory_kill", 0, "B", 7.7),
    ]
    bins = reconstruct_concurrency(events)
    by_sec = {b["second"]: b for b in bins}
    # within-trajectory paused gaps surface (A: ~1.1..2.0, B: ~6.1..7.0)
    assert by_sec[1]["paused"] == 1, by_sec.get(1)
    assert by_sec[6]["paused"] == 1, by_sec.get(6)
    # the inter-trajectory dead window (kill 2.7 -> create 5.0) is not paused
    assert by_sec[3]["paused"] == 0, by_sec.get(3)
    assert by_sec[4]["paused"] == 0, by_sec.get(4)


# ---------------------------------------------------------------------------
# Task 3: gantt_segments
# ---------------------------------------------------------------------------

from bench_core.observability.lifecycle_reconstruct import gantt_segments


def test_gantt_segments_groups_by_sandbox_sorted_by_start() -> None:
    # sandbox 1 starts earlier than sandbox 0 -> sorted first
    events = [
        _step(0, 5.0, 5.5, 5.5, 6.5, 6.5, 7.0),  # sbx 0
        {
            "event": "step",
            "sandbox_index": 1,
            "step_index": 0,
            "resume_start": 1.0,
            "resume_end": 1.5,
            "exec_start": 1.5,
            "exec_end": 2.0,
            "pause_start": 0.0,
            "pause_end": 1.0,
        },
    ]
    rows = gantt_segments(events)
    assert rows[0][0] == "sbx1"  # earlier start first
    assert rows[1][0] == "sbx0"
    # each row's segments are (start, end, phase)
    phases = {ph for _, _, ph in rows[0][1]}
    assert "pausing" in phases and "exec" in phases


def test_gantt_segments_include_cross_step_paused_gap() -> None:
    # The leading no-CPU idle (initial_pause.pause_end 0.5 -> step0.resume_start
    # 2.0) must render as a "paused" bar, and the initial pause API call as
    # "pausing" -- both were dropped by the old within-step derivation.
    events = [
        _initial_pause(0, 0.0, 0.5),
        _step(0, 2.0, 2.5, 2.5, 3.0, 3.0, 3.5),
    ]
    rows = gantt_segments(events)
    assert rows[0][0] == "sbx0"
    phases = {ph for _, _, ph in rows[0][1]}
    assert "paused" in phases, phases
    assert "pausing" in phases, phases  # initial pause API call


# ---------------------------------------------------------------------------
# Task 4: snapshot_rows
# ---------------------------------------------------------------------------

from bench_core.observability.lifecycle_reconstruct import snapshot_rows


def test_snapshot_rows_filters_snapshot_events_and_converts_mb() -> None:
    events = [
        {
            "event": "snapshot_size",
            "sandbox_index": 0,
            "sandbox_id": "abc",
            "pause_seq": 1,
            "logical_bytes": 2 * 1024 * 1024,
            "disk_bytes": 1024 * 1024,
            "inherited_bytes": 512 * 1024,
            "cumulative_bytes": 3 * 1024 * 1024,
            "generations": 2,
            "files": 10,
        },
        {"event": "step", "sandbox_index": 0},  # ignored
    ]
    rows = snapshot_rows(events)
    assert len(rows) == 1
    r = rows[0]
    assert r["pause_seq"] == 1
    assert r["logical_mb"] == 2.0
    assert r["disk_mb"] == 1.0
    assert r["inherited_mb"] == 0.5
    assert r["cumulative_mb"] == 3.0
