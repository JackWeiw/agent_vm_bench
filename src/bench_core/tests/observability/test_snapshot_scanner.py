"""SnapshotSizeScanner tests -- the off-hot-path snapshot-size collector.

Mirrors ``test_lifecycle_series.py``: the scanner is a background-drain daemon
thread + SimpleQueue, so hundreds of concurrent replay threads enqueue scan
requests via a non-blocking ``put`` (no scan, no I/O wait on the step path).
The drain thread owns the scan + the series emit.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from bench_core.observability.snapshot_scanner import SnapshotSizeScanner


class _FakeSeries:
    """Captures writes in lieu of a real LifecycleSeriesWriter (fast, no I/O)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        with self._lock:
            self.events.append(record)


class _FakeProvider:
    """Duck-typed SnapshotSizeCapable provider; sleeps to expose hot-path blocking."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    def snapshot_sizes(self, inst) -> dict | None:  # noqa: ARG002
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return {
            "logical_bytes": 1,
            "disk_bytes": 1,
            "inherited_bytes": 0,
            "cumulative_bytes": 1,
            "generations": 1,
            "files": 1,
        }


def _inst(i: int):
    return type("I", (), {"id": f"sbx{i}", "index": i})()


def test_request_does_not_block_on_slow_scan(tmp_path: Path) -> None:
    # Regression guard for the O(steps^2) scan that sat ON the step path: a slow
    # scan (heavy aenv snapshot I/O) serialized every step. Off the hot path,
    # N threads each ``request`` once and return in milliseconds even though the
    # drain thread still pays the slow scan off-path.
    series = _FakeSeries()
    provider = _FakeProvider(delay=0.02)  # 20ms/scan
    scanner = SnapshotSizeScanner(provider, series)

    n_threads = 64
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()  # release all at once = max contention on the put path
        scanner.request(_inst(i), i)

    start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    scanner.close()

    # If request blocked on the scan: 64 x 20ms = 1.28s serialized. Off-path,
    # request is just an enqueue -> well under the budget.
    assert elapsed < 0.3, f"request path blocked {elapsed:.2f}s (scan on hot path)"
    # The drain thread paid the scans off-path; close() flushed them all.
    assert len(series.events) == n_threads


def test_close_drains_all_requests_in_order(tmp_path: Path) -> None:
    series = _FakeSeries()
    scanner = SnapshotSizeScanner(_FakeProvider(), series)
    for i in range(200):
        scanner.request(_inst(i), i)
    scanner.close()

    assert len(series.events) == 200
    # pause_seq is assigned on the hot path (monotonic per request), so drained
    # events preserve request order.
    seqs = [e["pause_seq"] for e in series.events]
    assert seqs == list(range(200))


def test_close_is_idempotent(tmp_path: Path) -> None:
    scanner = SnapshotSizeScanner(_FakeProvider(), _FakeSeries())
    scanner.request(_inst(0), 0)
    scanner.close()
    scanner.close()  # must not raise


def test_request_after_close_is_safe(tmp_path: Path) -> None:
    series = _FakeSeries()
    scanner = SnapshotSizeScanner(_FakeProvider(), series)
    scanner.request(_inst(0), 0)
    scanner.close()
    scanner.request(_inst(1), 1)  # no-op, no raise
    assert len(series.events) == 1  # only the pre-close request landed


def test_concurrent_requests_no_loss(tmp_path: Path) -> None:
    series = _FakeSeries()
    scanner = SnapshotSizeScanner(_FakeProvider(), series)

    n_threads, n_records = 32, 10
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(n_records):
            scanner.request(_inst(tid * 1000 + i), tid * 1000 + i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    scanner.close()

    assert len(series.events) == n_threads * n_records  # no loss under contention
    seqs = {e["pause_seq"] for e in series.events}
    assert len(seqs) == n_threads * n_records  # no duplicates


def test_skips_none_result_silently(tmp_path: Path) -> None:
    # A sandbox whose snapshot dir is absent returns None -> no event, no crash.
    class NoneProvider:
        def snapshot_sizes(self, inst):  # noqa: ARG002
            return None

    series = _FakeSeries()
    scanner = SnapshotSizeScanner(NoneProvider(), series)
    scanner.request(_inst(0), 0)
    scanner.close()
    assert series.events == []
