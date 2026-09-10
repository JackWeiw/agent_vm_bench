"""LifecycleSeriesWriter -- background-draining, lock-free-on-the-hot-path
JSONL time-series writer.

Lives on its own (not in stats_collector.py) to keep the stats collector
focused. The writer offloads all I/O to a single background drain thread:
sandbox threads enqueue records via a non-blocking ``put`` (no cross-thread
lock, no ``json.dumps``, no ``flush`` on the hot path), so hundreds of
concurrent replay threads never serialize on a shared file lock or a
per-line ``flush``. The drain thread owns the file handle, runs
``json.dumps`` + ``write`` + ``flush`` at its own pace, and coalesces bursts
from many threads into single ``write`` calls.

Durability contract: every queued record is flushed by the time
:meth:`close` returns (the clean stop path drains the queue to empty). A
mid-run crash may lose only the un-drained tail -- the file is always
*complete and parseable* (each line is a whole ``json.dumps`` string written
in one ``write``). This replaces the old per-line ``flush()``-under-a-global-
lock contract, which serialized every writer on one lock + flush and inflated
lifecycle-replay wall-clock ~4x under high sandbox concurrency (the lock was
held through ``flush`` for every one of the ~2-3 writes per step across all
384 threads, costing ~25s/step of invisible inter-step wait).

Constructed by run_benchmark only when ``replay_mode == "lifecycle"``
(exec-only emits no file -- its lifecycle fields are all-zero, nothing to
curve, and its per-step exec timing already lives in the text report).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Pushed by close() to wake a blocked drain thread and signal shutdown.
_CLOSE_SENTINEL = object()


class LifecycleSeriesWriter:
    """Background-draining JSONL series writer.

    Producers call :meth:`write` (a non-blocking enqueue); a single daemon
    drain thread owns the file handle and does ``json.dumps`` + ``write`` +
    ``flush``. No cross-thread lock or per-line ``flush`` touches the producer
    hot path, so high-concurrency replay is not serialized on I/O.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a", encoding="utf-8")
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._closed = threading.Event()
        self._drain_thread = threading.Thread(target=self._drain, name="lifecycle-series-drain", daemon=True)
        self._drain_thread.start()

    def write(self, record: dict) -> None:
        """Enqueue one record. Non-blocking; off the sandbox hot path.

        After :meth:`close`, silently drops (the drain thread has exited;
        a late write from a daemon thread must never crash).
        """
        if self._closed.is_set():
            return
        self._q.put(record)

    def _drain(self) -> None:
        """Own the file handle: ``json.dumps`` + ``write`` + ``flush``, off
        every producer thread. Coalesces a burst into a single ``write``.
        """
        fh = self._fh
        q = self._q
        closed = False
        while not closed:
            rec = q.get()  # block until a record or the close sentinel
            if rec is _CLOSE_SENTINEL:
                break
            # Opportunistic batch: drain everything currently queued into one
            # write + flush, coalescing a burst from many sandbox threads.
            lines = [json.dumps(rec)]
            while True:
                try:
                    nxt = q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _CLOSE_SENTINEL:
                    closed = True
                    break
                lines.append(json.dumps(nxt))
            try:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
            except Exception:  # noqa: BLE001 - best-effort I/O, never raise
                logger.warning("lifecycle-series write failed", exc_info=True)
        try:
            fh.flush()
            fh.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass

    def close(self) -> None:
        """Signal the drain thread to flush + close; block until drained."""
        if not self._closed.is_set():
            self._closed.set()
            self._q.put(_CLOSE_SENTINEL)
            self._drain_thread.join()

    def __enter__(self) -> LifecycleSeriesWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def load_events(path: Path) -> list[dict]:
    """Read a lifecycle-series JSONL file into a list of event dicts.

    Malformed lines are skipped with a warning (the series is append-only +
    line-buffered; a torn final line is tolerable). Missing file -> [].
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed series line in %s", p)
    return out
