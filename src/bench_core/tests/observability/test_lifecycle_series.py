"""LifecycleSeriesWriter tests (P2.5 Task 2)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from bench_core.observability.lifecycle_series import LifecycleSeriesWriter


class TestLifecycleSeriesWriter:
    def test_write_appends_jsonl_line(self, tmp_path: Path):
        path = tmp_path / "series.jsonl"
        w = LifecycleSeriesWriter(path)
        w.write({"event": "step", "step_index": 0, "resume_sec": 0.1})
        w.close()

        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "step"

    def test_write_is_thread_safe_no_torn_lines(self, tmp_path: Path):
        path = tmp_path / "series.jsonl"
        w = LifecycleSeriesWriter(path)

        def worker(i: int) -> None:
            w.write({"event": "step", "step_index": i})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.close()

        lines = path.read_text().splitlines()
        assert len(lines) == 100
        # every line must be valid JSON (no interleaving / torn writes)
        idxs = sorted(json.loads(line)["step_index"] for line in lines)
        assert idxs == list(range(100))

    def test_flush_per_line_survives_crash(self, tmp_path: Path):
        path = tmp_path / "series.jsonl"
        w = LifecycleSeriesWriter(path)
        w.write({"event": "step", "step_index": 0})
        # simulate a mid-run crash: do NOT call close()
        # line-buffered flush already wrote + flushed the line.
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["step_index"] == 0

    def test_close_is_idempotent(self, tmp_path: Path):
        path = tmp_path / "series.jsonl"
        w = LifecycleSeriesWriter(path)
        w.write({"event": "initial_pause"})
        w.close()
        w.close()  # must not raise

    def test_creates_parent_dir(self, tmp_path: Path):
        path = tmp_path / "nested" / "deep" / "series.jsonl"
        w = LifecycleSeriesWriter(path)
        w.write({"event": "step"})
        w.close()
        assert path.exists()

    def test_write_after_close_is_safe(self, tmp_path: Path):
        # close() then write() must not crash; silently drop (file closed).
        path = tmp_path / "series.jsonl"
        w = LifecycleSeriesWriter(path)
        w.write({"event": "step", "step_index": 0})
        w.close()
        w.write({"event": "step", "step_index": 1})  # no-op, no raise
        lines = path.read_text().splitlines()
        assert len(lines) == 1
