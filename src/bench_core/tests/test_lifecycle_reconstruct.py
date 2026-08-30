"""load_events reader tests (P2.5 Task 1 -- lifecycle reconstruct)."""
from __future__ import annotations

import json
from pathlib import Path

from bench_core.lifecycle_series import load_events


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
