from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bench_core.observability.lifecycle_series import LifecycleSeriesWriter, load_events
from bench_core.task_runner.replay import ReplayBaseRunner
from env_provider import SnapshotSizeCapable


def _make_runner(series: LifecycleSeriesWriter, provider, replay_mode: str) -> ReplayBaseRunner:
    state = MagicMock()
    state.index = 0
    state.id = "abc"
    cfg = MagicMock()
    cfg.replay_mode = replay_mode
    runner = ReplayBaseRunner.__new__(ReplayBaseRunner)
    runner.state = state
    runner.config = cfg
    runner.provider = provider
    runner.series = series
    runner._pause_seq = 0
    return runner


def test_emit_snapshot_size_emits_when_capable(tmp_path: Path) -> None:
    series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
    # MagicMock(spec=SnapshotSizeCapable) passes isinstance check
    provider = MagicMock(spec=SnapshotSizeCapable)
    provider.snapshot_sizes.return_value = {
        "logical_bytes": 100,
        "disk_bytes": 50,
        "inherited_bytes": 10,
        "cumulative_bytes": 200,
        "generations": 1,
        "files": 2,
    }
    runner = _make_runner(series, provider, replay_mode="lifecycle")
    try:
        runner._emit_snapshot_size()
    finally:
        series.close()
    events = load_events(tmp_path / "s.jsonl")
    snaps = [e for e in events if e.get("event") == "snapshot_size"]
    assert len(snaps) == 1
    assert snaps[0]["logical_bytes"] == 100
    assert snaps[0]["pause_seq"] == 1
    assert snaps[0]["sandbox_id"] == "abc"


def test_emit_snapshot_size_skips_exec_only(tmp_path: Path) -> None:
    series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
    provider = MagicMock(spec=SnapshotSizeCapable)
    provider.snapshot_sizes.return_value = {"logical_bytes": 1}
    runner = _make_runner(series, provider, replay_mode="exec_only")
    try:
        runner._emit_snapshot_size()
    finally:
        series.close()
    events = load_events(tmp_path / "s.jsonl")
    assert [e for e in events if e.get("event") == "snapshot_size"] == []


def test_emit_snapshot_size_skips_when_none(tmp_path: Path) -> None:
    series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
    provider = MagicMock(spec=SnapshotSizeCapable)
    provider.snapshot_sizes.return_value = None
    runner = _make_runner(series, provider, replay_mode="lifecycle")
    try:
        runner._emit_snapshot_size()
    finally:
        series.close()
    events = load_events(tmp_path / "s.jsonl")
    assert [e for e in events if e.get("event") == "snapshot_size"] == []


def test_emit_snapshot_size_increments_pause_seq(tmp_path: Path) -> None:
    series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
    provider = MagicMock(spec=SnapshotSizeCapable)
    provider.snapshot_sizes.return_value = {
        "logical_bytes": 1,
        "disk_bytes": 1,
        "inherited_bytes": 0,
        "cumulative_bytes": 1,
        "generations": 1,
        "files": 1,
    }
    runner = _make_runner(series, provider, replay_mode="lifecycle")
    try:
        runner._emit_snapshot_size()
        runner._emit_snapshot_size()
    finally:
        series.close()
    events = load_events(tmp_path / "s.jsonl")
    snaps = [e for e in events if e.get("event") == "snapshot_size"]
    assert [s["pause_seq"] for s in snaps] == [1, 2]


def test_emit_snapshot_size_skips_non_capable_provider(tmp_path: Path) -> None:
    series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
    # Plain MagicMock does NOT pass isinstance(..., SnapshotSizeCapable)
    provider = MagicMock()
    runner = _make_runner(series, provider, replay_mode="lifecycle")
    try:
        runner._emit_snapshot_size()
    finally:
        series.close()
    events = load_events(tmp_path / "s.jsonl")
    assert [e for e in events if e.get("event") == "snapshot_size"] == []


def test_emit_snapshot_size_handles_exception(tmp_path: Path) -> None:
    series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
    provider = MagicMock(spec=SnapshotSizeCapable)
    provider.snapshot_sizes.side_effect = RuntimeError("snapshot dir missing")
    runner = _make_runner(series, provider, replay_mode="lifecycle")
    try:
        # Should not raise
        runner._emit_snapshot_size()
    finally:
        series.close()
    events = load_events(tmp_path / "s.jsonl")
    assert [e for e in events if e.get("event") == "snapshot_size"] == []
