"""Tests for collect_sample lifecycle correctness in vm_monitor/base.py.

Covers two raw-dumper integration bugs:
1. One sample timestamp must be shared across host_*_raw.csv and vm_cpu_raw.csv
   rows so an agent can join them by timestamp (was: enqueue_proc and
   enqueue_vm called datetime.now() separately, skewed across get_vms_realtime).
2. _close_proc_raw must run even when collect_sample raises a non-KbInterrupt
   exception (was: a bare try/except KeyboardInterrupt skipped cleanup).

Both patch /proc reads + the clock + signal/sleep so they run on any host.
"""
from __future__ import annotations

import datetime as _dt

import pytest

import vm_monitor.base as base
from vm_monitor.base import VMMonitorBase, _ProcRawWriter


class _NoProcMonitor(VMMonitorBase):
    """Concrete VMMonitorBase whose collect_sample touches no real /proc or /sys."""

    def _read_meminfo(self):  # noqa: N802 - seam
        return {"MemTotal": 1}

    def _read_vmstat(self):  # noqa: N802 - seam
        return {"pswpin": 0}

    def _read_proc_stat(self):  # noqa: N802 - seam
        return {
            "cpu": [1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            "cpus": {},
            "procs_running": 0,
            "procs_blocked": 0,
            "softirq": [],
        }

    # Collector no-ops so collect_sample never touches /sys or psutil.
    def collect_hugepage_stats(self):  # noqa: N802 - seam
        pass

    def collect_numa_cpu(self):  # noqa: N802 - seam
        pass

    def collect_host_stats(self):  # noqa: N802 - seam
        pass

    def collect_swap_stats(self, meminfo=None, vmstat=None):  # noqa: N802 - seam
        pass

    def collect_host_mem_detail(self, meminfo=None):  # noqa: N802 - seam
        pass

    def collect_host_pressure(self, meminfo=None, vmstat=None, stat=None):  # noqa: N802 - seam
        pass

    def get_numa_nodes_memory(self):  # noqa: N802 - seam
        pass

    def collect_vm_total_memory(self, vms):  # noqa: N802 - seam
        pass

    def collect_vm_total_pss(self, vms):  # noqa: N802 - seam
        pass

    # ABC seams:
    def get_vms_realtime(self):  # noqa: N802 - ABC seam
        return []

    def get_process_names(self):  # noqa: N802 - ABC seam
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):  # noqa: N802 - ABC seam
        return "vm0"

    def get_monitor_title(self):
        return "NoProcMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "no_proc_monitor"


def test_collect_sample_one_timestamp_for_proc_and_vm_rows(monkeypatch, tmp_path):
    """All raw CSV rows for one sample cycle share a single timestamp; an agent
    joins host_*_raw.csv to vm_cpu_raw.csv by it. Bug: enqueue_proc and
    enqueue_vm call datetime.now() separately -> skewed across get_vms_realtime."""
    mon = _NoProcMonitor()
    mon.log_dir = str(tmp_path)
    monkeypatch.setattr(
        mon,
        "get_vms_realtime",
        lambda: [
            {"name": "vm0", "pid": 1, "utime": 5, "stime": 2, "cpu_percent": 0.0, "memory_mb": 0.0, "status": "running"}
        ],
    )

    # Controlled clock: each datetime.now() returns the next tick. base binds
    # `datetime` as the immutable class, so patch the module-level name instead
    # of the class attr. A second now() call inside collect_sample yields a
    # different ts string -> the join key diverges. Hold the last tick on
    # exhaustion so an unexpected extra now() (e.g. a new collector) can't
    # raise StopIteration through pytest's generator hooks (PEP 479).
    class _FakeDateTime:
        def __init__(self, ticks):
            self._ticks = list(ticks)
            self._i = 0

        def now(self, tz=None):
            v = self._ticks[min(self._i, len(self._ticks) - 1)]
            self._i += 1
            return _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")

    monkeypatch.setattr(
        base, "datetime", _FakeDateTime(["2026-01-01 00:00:00", "2026-01-01 00:00:01", "2026-01-01 00:00:02"])
    )
    captured = {}
    monkeypatch.setattr(_ProcRawWriter, "enqueue_proc", lambda self, ts, *a: captured.__setitem__("proc", ts))
    monkeypatch.setattr(_ProcRawWriter, "enqueue_vm", lambda self, ts, *a: captured.__setitem__("vm", ts))
    try:
        mon.collect_sample()
    finally:
        mon._close_proc_raw()
    assert "proc" in captured and "vm" in captured
    assert captured["proc"] == captured["vm"], f"proc/vm raw rows must share one sample ts; got {captured}"


def test_proc_raw_closed_when_collect_sample_raises(monkeypatch, tmp_path):
    """A non-KeyboardInterrupt exception from collect_sample must still run
    _close_proc_raw (try/finally) so the raw CSVs are flushed. Bug: the bare
    except KeyboardInterrupt let other exceptions skip cleanup, leaving the
    drain thread + open handles dangling."""
    mon = _NoProcMonitor()
    mon.log_dir = str(tmp_path)

    def boom(meminfo=None, vmstat=None):
        raise OSError("simulated /proc read failure")

    # collect_sample creates _proc_raw before reaching collect_swap_stats, so
    # booby-trapping swap fires AFTER the dumper exists (the cleanup-relevant
    # state).
    monkeypatch.setattr(mon, "collect_swap_stats", boom)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    monkeypatch.setattr(base.signal, "signal", lambda *a, **k: None)
    with pytest.raises(OSError):
        mon.start_monitoring(duration_seconds=1, interval_seconds=1)
    assert mon._proc_raw is None, "raw dumper must be closed even on exception"


def test_collect_ublk_daemon_enqueues_raw_jiffies_to_dumper(monkeypatch, tmp_path):
    """collect_ublk_daemon must feed raw utime+stime jiffies to the dumper
    (ublk_cpu_raw.csv), not just derive 'cores' into ublk_daemon_history. The
    derived rate alone is lossy -- an agent recomputes CPU% from cumulative
    counters via delta, so the raw jiffies must reach the CSV."""
    mon = _NoProcMonitor()
    mon.log_dir = str(tmp_path)
    monkeypatch.setattr(mon, "_find_pid_by_comm", lambda comm: 4242)
    monkeypatch.setattr(mon, "_read_pid_jiffies", lambda pid: (100, 10))
    mon._proc_raw = _ProcRawWriter(str(tmp_path))
    enqueued = []
    monkeypatch.setattr(
        _ProcRawWriter,
        "enqueue_ublk",
        lambda self, ts, pid, ut, st: enqueued.append((pid, ut, st)),
    )
    try:
        mon.collect_ublk_daemon()
    finally:
        mon._close_proc_raw()
    assert enqueued, "collect_ublk_daemon must enqueue raw jiffies to the dumper"
    assert enqueued[0] == (4242, 100, 10)
