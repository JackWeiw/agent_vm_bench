"""LifecycleSeriesWriter tests (P2.5 Task 2)."""
from __future__ import annotations

import json
import threading
import time
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

    def test_close_drains_all_records_in_order(self, tmp_path: Path):
        # Contract: write() enqueues to a background drain; close() flushes
        # every queued record so the file is complete at the end of a run.
        # A single producer's records stay in FIFO order (the drain preserves
        # queue order). Replaces the old per-line-flush "durable before
        # write() returns" contract, which serialized every writer on one
        # lock + flush (see test_concurrent_writers_do_not_block_on_flush).
        path = tmp_path / "series.jsonl"
        w = LifecycleSeriesWriter(path)
        for i in range(500):
            w.write({"event": "step", "step_index": i})
        w.close()

        lines = path.read_text().splitlines()
        assert len(lines) == 500
        idxs = [json.loads(line)["step_index"] for line in lines]
        assert idxs == list(range(500))  # no loss, in order

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

    def test_concurrent_writers_do_not_block_on_flush(self, tmp_path: Path, monkeypatch):
        # Regression guard for the lock-contention pathology that inflated
        # 1:1 replay wall-clock ~4x: the old writer wrapped json.dumps +
        # write + flush in ONE global lock shared by every replay thread,
        # so a slow flush (heavy disk / fsync under 384-way snapshot I/O)
        # serialized all writers -- each thread waited on every other
        # thread's flush. The fix offloads json.dumps+write+flush to a
        # single background drain thread; write() on the hot path is just a
        # non-blocking enqueue, so concurrent writers never block on each
        # other's flush.
        w = LifecycleSeriesWriter(tmp_path / "series.jsonl")
        # Inject a slow flush to expose the serialization (a fast flush
        # hides the contention on SSD/CI). 2ms/flush x 3200 writes under a
        # global lock ~= 6.4s serialized; off-loaded, the hot path returns
        # in milliseconds.
        real_flush = w._fh.flush

        def slow_flush() -> None:
            real_flush()
            time.sleep(0.002)

        monkeypatch.setattr(w._fh, "flush", slow_flush)

        n_threads, n_records = 64, 50  # 3200 writes
        barrier = threading.Barrier(n_threads)

        def worker(tid: int) -> None:
            barrier.wait()  # release all threads at once = max contention
            for i in range(n_records):
                w.write({"event": "step", "t": tid, "i": i})

        start = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - start
        w.close()

        # Old impl: ~6.4s (3200 flushes serialized under the global lock).
        # New impl: hot path is enqueue-only, so well under the budget even
        # though the background drain still pays the 2ms/flush off-path.
        assert elapsed < 2.0, f"concurrent writes took {elapsed:.2f}s (flush lock-contention regression)"

        # Correctness holds under contention: no loss, no torn lines.
        lines = (tmp_path / "series.jsonl").read_text().splitlines()
        assert len(lines) == n_threads * n_records
        for line in lines:
            json.loads(line)  # every line valid JSON (no interleaving)
