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
