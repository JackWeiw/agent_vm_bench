"""Value tests for the Disk_IO_Timeline and Host_Mem_Timeline Excel sheets.

Mirrors the DummyMonitor pattern in test_exporters.py: populate the new
monitor histories, export with an empty log dir (so only monitor-derived
sheets appear), and assert columns, row counts, and per-sample values.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from vm_monitor.base import VMMonitorBase
from vm_monitor.exporters import PANDAS_AVAILABLE, export_to_excel

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None


class DummyMonitor(VMMonitorBase):
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


def _populate(monitor):
    """Populate disk / host-mem-detail / ublk / pressure histories (2 samples, 2 disks)."""
    monitor.target_disks = ["sda", "sdb"]

    def dr(r, w, util, ifl, q, ra, wa):
        return {
            "r_mb_s": r,
            "w_mb_s": w,
            "util_pct": util,
            "inflight": ifl,
            "r_mb": r,
            "w_mb": w,
            "avg_queue_depth": q,
            "read_await_ms": ra,
            "write_await_ms": wa,
        }

    monitor.disk_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "disks": {
                "sda": dr(10.0, 20.0, 5.0, 1, 0.5, 2.0, 3.0),
                "sdb": dr(5.0, 15.0, 3.0, 2, 1.0, 4.0, 5.0),
            },
        },
        {
            "ts": "2026-08-14 10:00:05",
            "disks": {
                "sda": dr(12.0, 25.0, 6.0, 1, 0.8, 2.5, 3.5),
                "sdb": dr(6.0, 18.0, 4.0, 3, 1.2, 4.5, 5.5),
            },
        },
    ]
    monitor.peak_disk_write_mb_s = 25.0
    monitor.host_mem_detail_history = [
        {"ts": "2026-08-14 10:00:00", "cached_mb": 1000.0, "buffers_mb": 50.0, "dirty_mb": 10.0, "writeback_mb": 5.0},
        {"ts": "2026-08-14 10:00:05", "cached_mb": 1100.0, "buffers_mb": 55.0, "dirty_mb": 20.0, "writeback_mb": 8.0},
    ]
    monitor.peak_dirty_mb = 20.0
    monitor.peak_writeback_mb = 8.0
    monitor.ublk_history = [
        {"ts": "2026-08-14 10:00:00", "ublk_devices": 3},
        {"ts": "2026-08-14 10:00:05", "ublk_devices": 4},
    ]
    monitor.peak_ublk_devices = 4


@unittest.skipUnless(PANDAS_AVAILABLE and pd is not None, "pandas/openpyxl required")
class TestDiskIoAndHostMemSheets(unittest.TestCase):
    def setUp(self):
        self.monitor = DummyMonitor()
        _populate(self.monitor)
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_disk_")
        self.output_file = os.path.join(self.log_dir, "analysis_report.xlsx")

    def tearDown(self):
        for f in os.listdir(self.log_dir):
            try:
                os.unlink(os.path.join(self.log_dir, f))
            except PermissionError:
                pass
        os.rmdir(self.log_dir)

    def _export(self):
        with patch("vm_monitor.exporters.parse_all_logs", return_value={}):
            return export_to_excel(self.monitor, self.log_dir, numa_nodes=[0], output_file=self.output_file)

    def test_disk_io_sheet_columns_and_values(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="Disk_IO_Timeline")
        self.assertEqual(
            list(df.columns),
            [
                "Timestamp",
                "sda Read (MB/s)",
                "sda Write (MB/s)",
                "sda Util (%)",
                "sda Inflight",
                "sda Queue Depth",
                "sda Read Await (ms)",
                "sda Write Await (ms)",
                "sdb Read (MB/s)",
                "sdb Write (MB/s)",
                "sdb Util (%)",
                "sdb Inflight",
                "sdb Queue Depth",
                "sdb Read Await (ms)",
                "sdb Write Await (ms)",
                "ublk Devices",
            ],
        )
        self.assertEqual(len(df), 2)
        row0 = df.iloc[0]
        self.assertAlmostEqual(row0["sda Write (MB/s)"], 20.0)
        self.assertAlmostEqual(row0["sdb Read (MB/s)"], 5.0)
        self.assertEqual(row0["sda Inflight"], 1)
        self.assertAlmostEqual(row0["sda Queue Depth"], 0.5)
        self.assertAlmostEqual(row0["sda Read Await (ms)"], 2.0)
        self.assertEqual(row0["ublk Devices"], 3)
        row1 = df.iloc[1]
        self.assertAlmostEqual(row1["sda Write (MB/s)"], 25.0)
        self.assertAlmostEqual(row1["sdb Queue Depth"], 1.2)
        self.assertEqual(row1["ublk Devices"], 4)

    def test_host_pressure_sheet_omitted_when_empty(self):
        # _populate does not populate host_pressure_history -> sheet is skipped.
        self._export()
        with pd.ExcelFile(self.output_file) as xl:
            names = set(xl.sheet_names)
        self.assertNotIn("Host_Pressure_Timeline", names)

    def test_host_mem_sheet_columns_and_values(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="Host_Mem_Timeline")
        self.assertEqual(
            list(df.columns),
            ["Timestamp", "Cached (MB)", "Buffers (MB)", "Dirty (MB)", "Writeback (MB)"],
        )
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.iloc[0]["Dirty (MB)"], 10.0)
        self.assertAlmostEqual(df.iloc[0]["Cached (MB)"], 1000.0)
        self.assertAlmostEqual(df.iloc[1]["Dirty (MB)"], 20.0)
        self.assertAlmostEqual(df.iloc[1]["Writeback (MB)"], 8.0)

    def test_summary_carries_disk_mem_peaks(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="Summary")
        peaks = dict(zip(df["Metric"], df["Value"]))
        self.assertAlmostEqual(peaks["Disk Write Peak"], 25.0)
        self.assertAlmostEqual(peaks["Dirty Peak"], 20.0)
        self.assertAlmostEqual(peaks["Writeback Peak"], 8.0)
        self.assertEqual(peaks["ublk Devices Peak"], 4)
        # Dirty Avg = mean(10, 20) = 15
        self.assertAlmostEqual(peaks["Dirty Avg"], 15.0)

    def test_empty_monitor_omits_sheets(self):
        m = DummyMonitor()
        with patch("vm_monitor.exporters.parse_all_logs", return_value={}):
            export_to_excel(m, self.log_dir, numa_nodes=None, output_file=self.output_file)
        with pd.ExcelFile(self.output_file) as xl:
            names = set(xl.sheet_names)
        self.assertNotIn("Disk_IO_Timeline", names)
        self.assertNotIn("Host_Mem_Timeline", names)
        self.assertNotIn("Host_Pressure_Timeline", names)


if __name__ == "__main__":
    unittest.main()
