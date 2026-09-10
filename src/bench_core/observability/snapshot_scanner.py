"""SnapshotSizeScanner -- off-hot-path snapshot-size collector.

Mirrors :class:`bench_core.observability.lifecycle_series.LifecycleSeriesWriter`:
a daemon drain thread + :class:`queue.SimpleQueue`. Each replay step's sandbox
thread calls :meth:`request` (a non-blocking enqueue -- no scan, no I/O wait
on the step path) instead of scanning synchronously after ``pause``. The drain
thread owns the scan (``provider.snapshot_sizes``, which is incremental-cached
in :mod:`env_provider.aenv._snapshot`) and the series emit, so a slow scan
under heavy aenv snapshot I/O never stalls a step.

Why off the hot path: the per-step scan is cheap (incremental, one new
generation ~0.5ms) but it is *on* the step path, and under the 384-way disk
contention of a lifecycle replay a sandbox's own ``lstat`` storm can spike,
delaying its next resume. Moving collection to a background thread decouples
scan latency from step latency -- steps proceed at full speed regardless of
disk pressure, and the snapshot-size series still gets a timestamped point
per step (the drain thread keeps up easily: ~0.5ms/scan vs ~22 req/s).

Durability: every queued request is scanned + emitted by the time
:meth:`close` returns. A mid-run crash loses only the un-drained tail.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

# Pushed by close() to wake a blocked drain thread and signal shutdown.
_CLOSE_SENTINEL = object()


class SnapshotSizeScanner:
    """Background-draining snapshot-size collector.

    Producers (replay sandbox threads) call :meth:`request` (a non-blocking
    enqueue); a single daemon drain thread does ``provider.snapshot_sizes`` +
    ``series.write``. No scan or I/O touches the producer hot path.
    """

    def __init__(self, provider, series) -> None:
        self._provider = provider
        self._series = series
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._closed = threading.Event()
        self._drain_thread = threading.Thread(target=self._drain, name="snapshot-size-scanner", daemon=True)
        self._drain_thread.start()

    def request(self, state, pause_seq: int) -> None:
        """Enqueue one scan request. Non-blocking; off the sandbox hot path.

        After :meth:`close`, silently drops (the drain thread has exited).
        """
        if self._closed.is_set():
            return
        self._q.put((state, pause_seq))

    def _drain(self) -> None:
        provider = self._provider
        series = self._series
        while True:
            rec = self._q.get()  # block until a request or the close sentinel
            if rec is _CLOSE_SENTINEL:
                break
            state, pause_seq = rec
            try:
                snap = provider.snapshot_sizes(state)
            except Exception as e:  # noqa: BLE001 - best-effort, never fail a slice
                logger.warning(f"[Sandbox{getattr(state, 'index', -1)}] snapshot_sizes failed: {str(e)[:80]}")
                continue
            if snap is None:
                continue
            try:
                series.write(
                    {
                        "event": "snapshot_size",
                        "sandbox_index": state.index,
                        "sandbox_id": getattr(state, "id", None),
                        "timestamp": time.time(),
                        "pause_seq": pause_seq,
                        **snap,
                    }
                )
            except Exception as e:  # noqa: BLE001 - best-effort emit
                logger.warning(f"[Sandbox{getattr(state, 'index', -1)}] snapshot_size emit failed: {str(e)[:80]}")

    def close(self) -> None:
        """Signal the drain thread to finish; block until drained."""
        if not self._closed.is_set():
            self._closed.set()
            self._q.put(_CLOSE_SENTINEL)
            self._drain_thread.join()

    def __enter__(self) -> SnapshotSizeScanner:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
