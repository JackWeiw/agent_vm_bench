"""Control-plane sandbox-state sampling for the stress window.

host_resources.xlsx's ``Concurrent VMs`` gauge counts live VMM *processes*: a
paused sandbox has none, and a sandbox still booting has none yet, so the
process gauge undercounts the fleet the provider believes it holds -- exactly
the wrong shape for oversubscription (pause-and-reuse) runs. This sampler asks
the provider for its sandbox states once per monitor interval and appends a
row to ``raw_data/api_state_raw.csv``; ``vm_monitor.raw_report`` as-of joins it
onto the host samples.

Best-effort by contract (same rule as vm_monitor): a provider without a list
API, or any poll error, never fails the bench.
"""
from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CSV_FIELDS = (
    "timestamp",
    "sandboxes_running",
    "sandboxes_paused",
    "sandboxes_other",
    "sandboxes_total",
)


class ApiStateSampler:
    """Poll ``provider.list_sandbox_states()`` and write ``api_state_raw.csv``.

    One row per sample; a provider returning ``None`` (no list API) ends the
    thread on the first poll, leaving no file. The CSV header is written from
    the first row so an empty run leaves nothing for raw_report to merge.
    """

    def __init__(self, provider, csv_path: Path, interval: float):
        self._provider = provider
        self._csv_path = Path(csv_path)
        self._interval = max(0.5, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fh = None
        self._writer: csv.DictWriter | None = None
        self._errors = 0

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="api-state-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self._interval * 2))
            self._thread = None
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            self._writer = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._sample():
                return  # provider has no list API -> nothing to sample
            self._stop.wait(self._interval)

    # ---------------------------------------------------------------- sample
    def _sample(self) -> bool:
        """Write one row; return False when the provider has no list API."""
        try:
            states = self._provider.list_sandbox_states()
            if states is None:
                logger.info("API state series skipped: provider %s has no sandbox-list API", self._provider.name)
                return False
            running = int(states.get("running", 0))
            paused = int(states.get("paused", 0))
            total = sum(int(v) for v in states.values())
        except Exception as e:  # noqa: BLE001 - monitoring must never kill the bench
            self._errors += 1
            if self._errors == 1 or self._errors % 100 == 0:
                logger.warning("sandbox state poll failed (%d so far): %s", self._errors, e)
            return True
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sandboxes_running": running,
            "sandboxes_paused": paused,
            "sandboxes_other": total - running - paused,
            "sandboxes_total": total,
        }
        try:
            if self._writer is None:
                self._csv_path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._csv_path, "w", newline="", buffering=1)
                self._writer = csv.DictWriter(self._fh, fieldnames=_CSV_FIELDS)
                self._writer.writeheader()
            self._writer.writerow(row)
        except OSError as e:
            logger.warning("API state series: cannot write %s: %s", self._csv_path, e)
            return False
        return True
