"""Tests for the 1-second disk sub-sampling in vm_monitor/base.py.

collect_disk_stats divides by the ACTUAL monotonic elapsed time between reads
(not the nominal sampling interval), and _disk_subsample_sleep ticks disk + ublk
together every 1s so disk bandwidth stays a true per-second rate at any
interval. These tests patch /sys reads + time.monotonic/time.sleep so they run
on any host (no real block devices needed).
"""
from __future__ import annotations

import io

import pytest

import vm_monitor.base as base
from vm_monitor.base import VMMonitorBase


class _StubMonitor(VMMonitorBase):
    """Concrete VMMonitorBase that touches no real /proc or /sys at runtime."""

    def get_process_names(self):  # noqa: N802 - ABC seam
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):  # noqa: N802 - ABC seam
        return "vm0"

    def get_vms_realtime(self):  # noqa: N802 - ABC seam
        return []

    def get_monitor_title(self):
        return "StubMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "stub_monitor"


def _stat_line(*, sectors_read=0, sectors_written=0, inflight=0, ms_io=0) -> str:
    """Build a minimal 11-field /sys/block/<dev>/stat line.

    Fields: reads_completed, reads_merged, sectors_read, read_ms,
    writes_completed, writes_merged, sectors_written, write_ms, inflight,
    ms_io, weighted_ms.
    """
    return f"0 0 {sectors_read} 0 0 0 {sectors_written} 0 {inflight} {ms_io} 0"


def test_collect_disk_stats_divides_by_actual_elapsed_not_interval(monkeypatch):
    """r_mb_s uses the real monotonic delta between reads, so it is correct
    even when self.interval differs from the actual sampling cadence."""
    mon = _StubMonitor()
    mon.target_disks = ["sda"]
    mon.interval = 5  # deliberately != the 2s elapsed below

    # First call: baseline (prev=None -> zero rates). Second: 2048 sectors
    # (1 MiB) over a 2s window -> 0.5 MB/s.
    snaps = iter([_stat_line(sectors_read=0), _stat_line(sectors_read=2048)])

    def fake_open(path, *a, **k):
        assert path == "/sys/block/sda/stat"
        return io.StringIO(next(snaps))

    monkeypatch.setattr("builtins.open", fake_open)

    # monotonic: 1000.0 on the baseline read, 1002.0 on the second read
    # (2s elapsed). If the code used self.interval=5 instead, r_mb_s would be
    # 1.0/5 = 0.2, not 0.5.
    ticks = iter([1000.0, 1002.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(ticks))

    mon.collect_disk_stats()  # baseline (zero rates, prev=None)
    mon.collect_disk_stats()  # 1 MiB over 2s

    row = mon.disk_history[-1]["disks"]["sda"]
    assert row["r_mb"] == 1.0  # 2048 sectors * 512 B = 1 MiB
    assert row["r_mb_s"] == 0.5  # 1 MiB / 2s actual elapsed (NOT 0.2 from interval=5)


def test_collect_disk_stats_baseline_row_has_zero_rates(monkeypatch):
    """First read (prev=None) yields a zero-rate baseline row + sets the
    monotonic baseline; the second read computes a real delta from it."""
    mon = _StubMonitor()
    mon.target_disks = ["sda"]

    snaps = iter([_stat_line(sectors_read=0), _stat_line(sectors_read=4096)])
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: io.StringIO(next(snaps)))
    ticks = iter([10.0, 11.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(ticks))

    mon.collect_disk_stats()
    assert mon.disk_history[-1]["disks"]["sda"]["r_mb_s"] == 0.0  # baseline

    mon.collect_disk_stats()
    # 4096 sectors = 2 MiB over 1s elapsed -> 2.0 MB/s
    assert mon.disk_history[-1]["disks"]["sda"]["r_mb_s"] == 2.0


def test_disk_subsample_sleep_ticks_disk_and_ublk_together(monkeypatch):
    """_disk_subsample_sleep calls collect_disk_stats + collect_ublk_count
    once per 1s tick, keeping the two histories index-aligned. Stops early
    when self.running is cleared."""
    mon = _StubMonitor()
    mon.running = True

    calls = {"disk": 0, "ublk": 0}

    def fake_disk():
        calls["disk"] += 1

    def fake_ublk():
        calls["ublk"] += 1

    monkeypatch.setattr(mon, "collect_disk_stats", fake_disk)
    monkeypatch.setattr(mon, "collect_ublk_count", fake_ublk)
    # no-op sleep so the loop runs as fast as monotonic advances
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    # monotonic: end=0+3=3, then 1,2,3,4 -> ticks at remaining 2,1,0(break)
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(ticks, 999.0))

    mon._disk_subsample_sleep(3.0)
    # at least one tick fired, and disk/ublk are 1:1 aligned per tick
    assert calls["disk"] >= 1
    assert calls["ublk"] == calls["disk"]


def test_disk_subsample_sleep_noop_when_not_running():
    """When monitoring is stopped, the sub-sampler returns immediately."""
    mon = _StubMonitor()
    mon.running = False
    calls = {"disk": 0}
    mon.collect_disk_stats = lambda: calls.__setitem__("disk", calls["disk"] + 1)  # type: ignore[assignment]
    mon._disk_subsample_sleep(3.0)
    assert calls["disk"] == 0


def test_disk_subsample_sleep_noop_when_seconds_le_zero(monkeypatch):
    mon = _StubMonitor()
    mon.running = True
    fired = {"n": 0}
    monkeypatch.setattr(mon, "collect_disk_stats", lambda: fired.__setitem__("n", fired["n"] + 1))
    monkeypatch.setattr(mon, "collect_ublk_count", lambda: None)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    monkeypatch.setattr(base.time, "monotonic", lambda: 0.0)
    mon._disk_subsample_sleep(0)
    mon._disk_subsample_sleep(-1)
    assert fired["n"] == 0
