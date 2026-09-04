"""Tests for /proc read dedup + actual-elapsed rate divisors in vm_monitor/base.py.

collect_sample reads /proc/meminfo and /proc/vmstat ONCE per cycle and passes
the dicts to collect_swap_stats / collect_host_mem_detail / collect_host_pressure
(was: 3 meminfo + 2 vmstat parses per cycle). swap in/out and page-cache
pressure rates now divide by the ACTUAL monotonic elapsed window between samples
(not the nominal self.interval), so the MiB/s label holds even when the
sampling cadence drifts. These tests patch /proc reads + time.monotonic so they
run on any host.
"""
from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from vm_monitor.base import _PAGE_SIZE, VMMonitorBase


class _DummyMonitor(VMMonitorBase):
    """Concrete VMMonitorBase that touches no real /proc or /sys at runtime."""

    def get_vms_realtime(self):
        return []

    def get_process_names(self):
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):
        return "vm0"

    def get_monitor_title(self):
        return "DummyMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "dummy_monitor"


class TestProcDedup(unittest.TestCase):
    """The three /proc/meminfo + /proc/vmstat consumers must honor the shared
    dicts passed by collect_sample instead of re-reading /proc."""

    def test_collectors_use_shared_dicts_not_reread(self):
        mon = _DummyMonitor()
        mon.interval = 1

        def boom_meminfo():
            raise AssertionError("collect_sample's shared meminfo dict was not used; /proc/meminfo re-read")

        def boom_vmstat():
            raise AssertionError("collect_sample's shared vmstat dict was not used; /proc/vmstat re-read")

        mi = {
            "SwapCached": 0,
            "Cached": 0,
            "SReclaimable": 0,
            "Buffers": 0,
            "Shmem": 0,
            "AnonPages": 0,
            "Dirty": 0,
            "Writeback": 0,
        }
        vs = {
            "pswpin": 0,
            "pswpout": 0,
            "pgscan_kswapd": 0,
            "pgscan_direct": 0,
            "pgscan_direct_throttle": 0,
            "pgsteal_kswapd": 0,
            "pgsteal_direct": 0,
            "workingset_refault_file": 0,
        }

        with patch.object(mon, "_read_meminfo", boom_meminfo), patch.object(mon, "_read_vmstat", boom_vmstat):
            mock_swap = MagicMock(total=0, used=0, free=0, percent=0.0)
            with patch("vm_monitor.base.psutil.swap_memory", return_value=mock_swap):
                mon.collect_swap_stats(meminfo=mi, vmstat=vs)
            mon.collect_host_mem_detail(meminfo=mi)
            # collect_host_pressure also reads /proc/stat (patched) + dirty
            # sysctls (lazy -- pre-mark read so it skips).
            mon._dirty_limits_read = True
            stat_io = io.StringIO("cpu  100 0 0 0 0 0 0 0 0 0\nprocs_running 1\nprocs_blocked 0\n")
            with patch("vm_monitor.base.open", return_value=stat_io):
                mon.collect_host_pressure(meminfo=mi, vmstat=vs)

    def test_collect_sample_reads_meminfo_vmstat_once_each(self):
        """collect_sample reads /proc/meminfo + /proc/vmstat exactly once per
        cycle and threads the dicts through the three consumers (no re-read)."""
        mon = _DummyMonitor()
        mon.interval = 1

        mi_calls = [0]
        vs_calls = [0]

        def fake_mi():
            mi_calls[0] += 1
            return {
                "SwapCached": 0,
                "Cached": 0,
                "SReclaimable": 0,
                "Buffers": 0,
                "Shmem": 0,
                "AnonPages": 0,
                "Dirty": 0,
                "Writeback": 0,
            }

        def fake_vs():
            vs_calls[0] += 1
            return {
                "pswpin": 0,
                "pswpout": 0,
                "pgscan_kswapd": 0,
                "pgscan_direct": 0,
                "pgscan_direct_throttle": 0,
                "pgsteal_kswapd": 0,
                "pgsteal_direct": 0,
                "workingset_refault_file": 0,
            }

        mon._dirty_limits_read = True  # skip /proc/sys/vm read in collect_host_pressure
        stat_io = io.StringIO("cpu  100 0 0 0 0 0 0 0 0 0\nprocs_running 1\nprocs_blocked 0\n")

        with patch.object(mon, "_read_meminfo", fake_mi), patch.object(mon, "_read_vmstat", fake_vs), patch.object(
            mon, "collect_hugepage_stats", lambda: None
        ), patch.object(mon, "collect_numa_cpu", lambda: None), patch.object(
            mon, "collect_host_stats", lambda: None
        ), patch.object(mon, "get_numa_nodes_memory", lambda: None), patch.object(
            mon, "get_vms_realtime", lambda: []
        ), patch(
            "vm_monitor.base.psutil.swap_memory", return_value=MagicMock(total=0, used=0, free=0, percent=0.0)
        ), patch("vm_monitor.base.open", return_value=stat_io), patch("vm_monitor.base.time.monotonic", lambda: 100.0):
            mon.collect_sample()

        self.assertEqual(mi_calls[0], 1, "meminfo read once per cycle (was 3 before dedup)")
        self.assertEqual(vs_calls[0], 1, "vmstat read once per cycle (was 2 before dedup)")


class TestSwapRateActualElapsed(unittest.TestCase):
    """swap in/out rate divides by ACTUAL monotonic elapsed, not self.interval."""

    def test_uses_actual_elapsed_not_interval(self):
        mon = _DummyMonitor()
        mon.interval = 5  # nominal; actual elapsed below is 2s
        mock_swap = MagicMock(total=2 * 1024**3, used=0, free=2 * 1024**3, percent=0.0)
        mi = {"SwapCached": 0}
        vs1 = {"pswpin": 0, "pswpout": 0}
        vs2 = {"pswpin": 512, "pswpout": 0}  # 512 pages swapped in

        ticks = iter([100.0, 102.0])  # 2s elapsed between samples
        with patch("vm_monitor.base.psutil.swap_memory", return_value=mock_swap), patch(
            "vm_monitor.base.time.monotonic", lambda: next(ticks)
        ):
            mon.collect_swap_stats(meminfo=mi, vmstat=vs1)  # baseline (rate=0)
            mon.collect_swap_stats(meminfo=mi, vmstat=vs2)  # delta

        row = mon.swap_history[-1]["activity"]
        mib_per_page = _PAGE_SIZE / 2**20
        # 512 pages * page_size = 2.0 MiB total over 2s actual elapsed = 1.0 MiB/s.
        # If the code divided by self.interval=5 instead, it would be 0.4 MiB/s.
        self.assertEqual(row["swap_in_rate"], round(512 * mib_per_page / 2, 2))
        self.assertNotAlmostEqual(row["swap_in_rate"], round(512 * mib_per_page / 5, 2), places=2)

    def test_first_sample_has_zero_rate(self):
        mon = _DummyMonitor()
        mon.interval = 5
        mock_swap = MagicMock(total=2 * 1024**3, used=0, free=2 * 1024**3, percent=0.0)
        with patch("vm_monitor.base.psutil.swap_memory", return_value=mock_swap), patch(
            "vm_monitor.base.time.monotonic", lambda: 100.0
        ):
            mon.collect_swap_stats(meminfo={"SwapCached": 0}, vmstat={"pswpin": 100, "pswpout": 50})
        self.assertEqual(mon.swap_history[-1]["activity"]["swap_in_rate"], 0)


class TestPressureRateActualElapsed(unittest.TestCase):
    """page-scan / reclaim / refault rates divide by ACTUAL monotonic elapsed."""

    def test_uses_actual_elapsed_not_interval(self):
        mon = _DummyMonitor()
        mon.interval = 5  # nominal; actual elapsed below is 2s
        mon._dirty_limits_read = True  # skip /proc/sys/vm read
        mi = {"AnonPages": 0, "Cached": 0, "SReclaimable": 0, "Buffers": 0, "Shmem": 0}
        vs1 = {
            "pgscan_kswapd": 0,
            "pgscan_direct": 0,
            "pgscan_direct_throttle": 0,
            "pgsteal_kswapd": 0,
            "pgsteal_direct": 0,
            "workingset_refault_file": 0,
        }
        vs2 = {
            "pgscan_kswapd": 512,
            "pgscan_direct": 0,
            "pgscan_direct_throttle": 0,
            "pgsteal_kswapd": 0,
            "pgsteal_direct": 0,
            "workingset_refault_file": 0,
        }

        ticks = iter([100.0, 102.0])  # 2s elapsed
        stat_io = io.StringIO("cpu  100 0 0 0 0 0 0 0 0 0\nprocs_running 1\nprocs_blocked 0\n")
        with patch("vm_monitor.base.open", return_value=stat_io), patch(
            "vm_monitor.base.time.monotonic", lambda: next(ticks)
        ):
            mon.collect_host_pressure(meminfo=mi, vmstat=vs1)  # baseline
            mon.collect_host_pressure(meminfo=mi, vmstat=vs2)  # delta

        row = mon.host_pressure_history[-1]
        mib_per_page = _PAGE_SIZE / 2**20
        # 512 pages scanned over 2s actual elapsed.
        # If divided by self.interval=5, it would be 5x lower.
        self.assertEqual(row["page_scan_mib_s"], round(512 * mib_per_page / 2, 3))
        self.assertNotAlmostEqual(row["page_scan_mib_s"], round(512 * mib_per_page / 5, 3), places=3)


if __name__ == "__main__":
    unittest.main()
