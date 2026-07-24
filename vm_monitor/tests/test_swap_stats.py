"""Unit tests for swap monitoring enhancement in vm_monitor/base.py"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from vm_monitor.base import VMMonitorBase


# Concrete subclass for testing (VMMonitorBase is abstract)
class DummyMonitor(VMMonitorBase):
    def get_vms_realtime(self):
        return []

    def get_process_names(self):
        return ["test_process"]

    def extract_vm_id(self, line):
        return "vm0"

    def get_monitor_title(self):
        return "DummyMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "dummy_monitor"


class TestReadMeminfo(unittest.TestCase):
    """Tests for _read_meminfo() parsing /proc/meminfo"""

    def _write_mock_meminfo(self, content):
        """Write mock /proc/meminfo content to a temp file"""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_parse_swap_cached(self):
        """Should parse SwapCached from meminfo as MB"""
        content = (
            "MemTotal:       16384000 kB\n"
            "MemFree:         1024000 kB\n"
            "SwapTotal:        2048000 kB\n"
            "SwapFree:         1948000 kB\n"
            "SwapCached:         46144 kB\n"
        )
        path = self._write_mock_meminfo(content)
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", return_value=open(path)):
            result = monitor._read_meminfo()
        self.assertIn("SwapCached", result)
        self.assertAlmostEqual(result["SwapCached"], 46144 / 1024, places=2)  # ~44.47 MB
        os.unlink(path)

    def test_parse_swap_total_free(self):
        """Should parse SwapTotal and SwapFree as MB"""
        content = "SwapTotal:        2048000 kB\n" "SwapFree:         1948000 kB\n"
        path = self._write_mock_meminfo(content)
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", return_value=open(path)):
            result = monitor._read_meminfo()
        self.assertIn("SwapTotal", result)
        self.assertAlmostEqual(result["SwapTotal"], 2048000 / 1024, places=2)  # ~2000 MB
        os.unlink(path)

    def test_missing_proc_file_returns_empty(self):
        """Should return empty dict when /proc/meminfo is unavailable"""
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", side_effect=FileNotFoundError):
            result = monitor._read_meminfo()
        self.assertEqual(result, {})

    def test_all_fields_converted_to_mb(self):
        """All parsed values should be in MB (kB / 1024)"""
        content = "SomeField:       2048 kB\n"
        path = self._write_mock_meminfo(content)
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", return_value=open(path)):
            result = monitor._read_meminfo()
        self.assertEqual(result["SomeField"], 2.0)  # 2048 kB = 2 MB
        os.unlink(path)


class TestReadVmstat(unittest.TestCase):
    """Tests for _read_vmstat() parsing /proc/vmstat"""

    def _write_mock_vmstat(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".vmstat", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_parse_pswpin_pswpout(self):
        """Should parse pswpin and pswpout as integers"""
        content = "nr_free_pages 256000\n" "pswpin 543210\n" "pswpout 432100\n" "pgpgin 12345\n"
        path = self._write_mock_vmstat(content)
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", return_value=open(path)):
            result = monitor._read_vmstat()
        self.assertEqual(result["pswpin"], 543210)
        self.assertEqual(result["pswpout"], 432100)

    def test_missing_proc_file_returns_empty(self):
        """Should return empty dict when /proc/vmstat is unavailable"""
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", side_effect=FileNotFoundError):
            result = monitor._read_vmstat()
        self.assertEqual(result, {})

    def test_only_key_fields_parsed(self):
        """Should parse all fields as key=int pairs"""
        content = "pswpin 100\npswpout 200\n"
        path = self._write_mock_vmstat(content)
        monitor = DummyMonitor()
        with patch("vm_monitor.base.open", return_value=open(path)):
            result = monitor._read_vmstat()
        self.assertEqual(result, {"pswpin": 100, "pswpout": 200})
        os.unlink(path)


class TestCollectSwapStats(unittest.TestCase):
    """Tests for collect_swap_stats() with nested dict and rate calculation"""

    def setUp(self):
        self.monitor = DummyMonitor()
        self.monitor.interval = 3  # default sampling interval

    def test_first_sample_has_zero_rate(self):
        """First sample should have rate=0 (no prior baseline)"""
        mock_swap = MagicMock(total=2048 * 1024 * 1024, used=100 * 1024 * 1024, free=1948 * 1024 * 1024, percent=4.9)
        meminfo_content = (
            "SwapTotal:        2048000 kB\n" "SwapFree:         1948000 kB\n" "SwapCached:         46144 kB\n"
        )
        vmstat_content = "pswpin 543210\npswpout 432100\n"

        meminfo_path = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
        meminfo_path.write(meminfo_content)
        meminfo_path.close()

        vmstat_path = tempfile.NamedTemporaryFile(mode="w", suffix=".vmstat", delete=False)
        vmstat_path.write(vmstat_content)
        vmstat_path.close()

        with patch("vm_monitor.base.psutil.swap_memory", return_value=mock_swap), patch(
            "vm_monitor.base.open"
        ) as mock_open:

            def open_side_effect(path, *args, **kwargs):
                if "meminfo" in path:
                    return open(meminfo_path.name)
                elif "vmstat" in path:
                    return open(vmstat_path.name)
                return open(path, *args, **kwargs)

            mock_open.side_effect = open_side_effect
            self.monitor.collect_swap_stats()

        snapshot = self.monitor.swap_history[0]
        # Verify nested structure
        self.assertIn("ts", snapshot)
        self.assertIn("capacity", snapshot)
        self.assertIn("cache", snapshot)
        self.assertIn("activity", snapshot)

        # First sample: rate should be 0
        self.assertEqual(snapshot["activity"]["swap_in_rate"], 0)
        self.assertEqual(snapshot["activity"]["swap_out_rate"], 0)
        self.assertEqual(snapshot["activity"]["pswpin_delta"], 0)
        self.assertEqual(snapshot["activity"]["pswpout_delta"], 0)

        # Cumulative should have raw values
        self.assertEqual(snapshot["activity"]["pswpin_cumulative"], 543210)
        self.assertEqual(snapshot["activity"]["pswpout_cumulative"], 432100)

        # Capacity values
        self.assertAlmostEqual(snapshot["capacity"]["used_mb"], 100, places=2)
        self.assertAlmostEqual(snapshot["capacity"]["total_mb"], 2048, places=2)

        # Cache values
        self.assertIn("cached_mb", snapshot["cache"])
        self.assertIn("cached_ratio_pct", snapshot["cache"])

        os.unlink(meminfo_path.name)
        os.unlink(vmstat_path.name)

    def test_second_sample_computes_rate(self):
        """Second sample should compute delta and rate from difference"""
        mock_swap = MagicMock(total=2048 * 1024 * 1024, used=100 * 1024 * 1024, free=1948 * 1024 * 1024, percent=4.9)

        vmstat1 = "pswpin 543210\npswpout 432100\n"
        vmstat2 = "pswpin 543330\npswpout 432180\n"
        meminfo_content = "SwapCached:         46144 kB\nSwapTotal:        2048000 kB\nSwapFree:         1948000 kB\n"

        meminfo_path = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
        meminfo_path.write(meminfo_content)
        meminfo_path.close()

        vmstat_path1 = tempfile.NamedTemporaryFile(mode="w", suffix=".vmstat", delete=False)
        vmstat_path1.write(vmstat1)
        vmstat_path1.close()

        vmstat_path2 = tempfile.NamedTemporaryFile(mode="w", suffix=".vmstat", delete=False)
        vmstat_path2.write(vmstat2)
        vmstat_path2.close()

        vmstat_paths = [vmstat_path1.name, vmstat_path2.name]
        call_count = [0]

        with patch("vm_monitor.base.psutil.swap_memory", return_value=mock_swap), patch(
            "vm_monitor.base.open"
        ) as mock_open:

            def open_side_effect(path, *args, **kwargs):
                if "vmstat" in path:
                    idx = call_count[0]
                    call_count[0] += 1
                    return open(vmstat_paths[min(idx, len(vmstat_paths) - 1)])
                elif "meminfo" in path:
                    return open(meminfo_path.name)
                return open(path, *args, **kwargs)

            mock_open.side_effect = open_side_effect

            self.monitor.collect_swap_stats()
            self.monitor.collect_swap_stats()

        snapshot2 = self.monitor.swap_history[1]
        self.assertEqual(snapshot2["activity"]["pswpin_delta"], 120)
        self.assertEqual(snapshot2["activity"]["pswpout_delta"], 80)
        self.assertAlmostEqual(snapshot2["activity"]["swap_in_rate"], 120 / 3, places=2)
        self.assertAlmostEqual(snapshot2["activity"]["swap_out_rate"], 80 / 3, places=2)

        os.unlink(meminfo_path.name)
        os.unlink(vmstat_path1.name)
        os.unlink(vmstat_path2.name)

    def test_peak_swap_cached_tracking(self):
        """peak_swap_cached_mb should track high-water mark"""
        self.monitor.peak_swap_cached_mb = 0.0

        mock_swap1 = MagicMock(total=2048 * 1024 * 1024, used=100 * 1024 * 1024, free=1948 * 1024 * 1024, percent=4.9)
        meminfo1 = "SwapCached:         46144 kB\nSwapTotal:        2048000 kB\nSwapFree:         1948000 kB\n"
        vmstat1 = "pswpin 100\npswpout 50\n"

        meminfo2 = "SwapCached:         82944 kB\nSwapTotal:        2048000 kB\nSwapFree:         1948000 kB\n"
        vmstat2 = "pswpin 200\npswpout 100\n"

        meminfo_path1 = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
        meminfo_path1.write(meminfo1)
        meminfo_path1.close()
        meminfo_path2 = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
        meminfo_path2.write(meminfo2)
        meminfo_path2.close()

        vmstat_path1 = tempfile.NamedTemporaryFile(mode="w", suffix=".vmstat", delete=False)
        vmstat_path1.write(vmstat1)
        vmstat_path1.close()
        vmstat_path2 = tempfile.NamedTemporaryFile(mode="w", suffix=".vmstat", delete=False)
        vmstat_path2.write(vmstat2)
        vmstat_path2.close()

        meminfo_paths = [meminfo_path1.name, meminfo_path2.name]
        vmstat_paths = [vmstat_path1.name, vmstat_path2.name]
        call_count = [0]
        meminfo_count = [0]

        with patch("vm_monitor.base.psutil.swap_memory", return_value=mock_swap1), patch(
            "vm_monitor.base.open"
        ) as mock_open:

            def open_side_effect(path, *args, **kwargs):
                if "meminfo" in path:
                    idx = meminfo_count[0]
                    meminfo_count[0] += 1
                    return open(meminfo_paths[min(idx, len(meminfo_paths) - 1)])
                elif "vmstat" in path:
                    idx = call_count[0]
                    call_count[0] += 1
                    return open(vmstat_paths[min(idx, len(vmstat_paths) - 1)])
                return open(path, *args, **kwargs)

            mock_open.side_effect = open_side_effect

            self.monitor.collect_swap_stats()
            self.monitor.collect_swap_stats()

        self.assertAlmostEqual(self.monitor.peak_swap_cached_mb, 82944 / 1024, places=2)

        os.unlink(meminfo_path1.name)
        os.unlink(meminfo_path2.name)
        os.unlink(vmstat_path1.name)
        os.unlink(vmstat_path2.name)
