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
        content = (
            "SwapTotal:        2048000 kB\n"
            "SwapFree:         1948000 kB\n"
        )
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
        content = (
            "nr_free_pages 256000\n"
            "pswpin 543210\n"
            "pswpout 432100\n"
            "pgpgin 12345\n"
        )
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
