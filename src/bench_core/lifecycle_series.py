"""LifecycleSeriesWriter -- thread-safe, line-buffered JSONL time-series writer.

Lives on its own (not in stats_collector.py) to keep the stats collector
focused. The writer does only I/O: one file handle, a Lock around
json.dumps + write + flush, each record ``\\n``-terminated and flushed
immediately so a mid-run crash leaves a complete, parseable file. Record
*assembly* (the per-step dict shape) stays in the replay runner so the
format is unit-testable without a real file.

Constructed by run_benchmark only when ``replay_mode == "lifecycle"``
(exec-only emits no file -- its lifecycle fields are all-zero, nothing to
curve, and its per-step exec timing already lives in the text report).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class LifecycleSeriesWriter:
    """Thread-safe, line-buffered streaming writer for one JSONL series file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self._path, "a", encoding="utf-8")

    def write(self, record: dict) -> None:
        """Append one JSON record as a ``\\n``-terminated, flushed line.

        Thread-safe. After :meth:`close`, silently drops (the file is closed;
        a late write from a daemon thread must not crash the stop phase).
        """
        with self._lock:
            if self._fh.closed:
                return
            line = json.dumps(record)
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        """Close the file handle. Idempotent."""
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

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
