"""Unit tests for vm_monitor/exporters.py Excel sheet structure.

Covers the consolidation of the former NUMA_CPU, NUMA_Memory, and
Hugepage_Per_NUMA sheets into a single NUMA_Overview sheet.
"""

import os
import tempfile
import unittest

from vm_monitor.base import VMMonitorBase
from vm_monitor.exporters import PANDAS_AVAILABLE, export_to_excel

try:
    import pandas as pd
except ImportError:  # pragma: no cover - pandas is a core dep but guard anyway
    pd = None


class DummyMonitor(VMMonitorBase):
    """Concrete subclass for testing (VMMonitorBase is abstract)."""

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


def _populate_numa_data(monitor):
    """Populate the three per-NUMA histories with two nodes (0 and 5)."""
    # NUMA CPU: node0 [10,20,30] avg=20 peak=30; node5 [40,50] avg=45 peak=50
    monitor.numa_cpu_history[0] = [10.0, 20.0, 30.0]
    monitor.numa_cpu_peak[0] = 30.0
    monitor.numa_cpu_history[5] = [40.0, 50.0]
    monitor.numa_cpu_peak[5] = 50.0

    # NUMA memory: two samples per node (both alias key styles present)
    # node0: used 1000/1200 usage 50/60 ; node5: used 2000/2400 usage 80/90
    monitor.numa_memory_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "nodes": [
                {"node": 0, "used": 1000.0, "usage": 50.0, "used_mb": 1000.0, "usage_pct": 50.0},
                {"node": 5, "used": 2000.0, "usage": 80.0, "used_mb": 2000.0, "usage_pct": 80.0},
            ],
        },
        {
            "ts": "2026-08-14 10:00:05",
            "nodes": [
                {"node": 0, "used": 1200.0, "usage": 60.0, "used_mb": 1200.0, "usage_pct": 60.0},
                {"node": 5, "used": 2400.0, "usage": 90.0, "used_mb": 2400.0, "usage_pct": 90.0},
            ],
        },
    ]

    # Hugepage per NUMA: identical across two samples
    # node0: 4096/1024/25% ; node5: 4096/2048/50%
    monitor.hugepage_per_numa_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "nodes": {
                0: {"total_mb": 4096.0, "used_mb": 1024.0, "usage_pct": 25.0},
                5: {"total_mb": 4096.0, "used_mb": 2048.0, "usage_pct": 50.0},
            },
        },
        {
            "ts": "2026-08-14 10:00:05",
            "nodes": {
                0: {"total_mb": 4096.0, "used_mb": 1024.0, "usage_pct": 25.0},
                5: {"total_mb": 4096.0, "used_mb": 2048.0, "usage_pct": 50.0},
            },
        },
    ]


@unittest.skipUnless(PANDAS_AVAILABLE and pd is not None, "pandas/openpyxl required")
class TestNumaOverviewSheet(unittest.TestCase):
    """The three host per-NUMA sheets are consolidated into NUMA_Overview."""

    def setUp(self):
        self.monitor = DummyMonitor()
        _populate_numa_data(self.monitor)
        # Empty log dir => parse_all_logs returns {} (no collection-tool sheets)
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_export_")
        self.output_file = os.path.join(self.log_dir, "resource_report.xlsx")

    def tearDown(self):
        for f in os.listdir(self.log_dir):
            os.unlink(os.path.join(self.log_dir, f))
        os.rmdir(self.log_dir)

    def _export(self):
        return export_to_excel(self.monitor, self.log_dir, numa_nodes=[0, 5], output_file=self.output_file)

    def _sheet_names(self):
        # Use a context manager so the file handle is released before tearDown
        # deletes the xlsx (Windows refuses to delete an open file).
        with pd.ExcelFile(self.output_file) as xl:
            return list(xl.sheet_names)

    def test_old_sheets_removed(self):
        """NUMA_CPU / NUMA_Memory / Hugepage_Per_NUMA must no longer exist."""
        self._export()
        sheets = self._sheet_names()
        self.assertNotIn("NUMA_CPU", sheets)
        self.assertNotIn("NUMA_Memory", sheets)
        self.assertNotIn("Hugepage_Per_NUMA", sheets)

    def test_new_sheet_present_with_expected_columns(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="NUMA_Overview")
        expected_cols = [
            "NUMA Node",
            "Avg CPU (%)",
            "Peak CPU (%)",
            "Avg Used (MB)",
            "Peak Used (MB)",
            "Avg Usage (%)",
            "HP Avg Total (MB)",
            "HP Avg Used (MB)",
            "HP Avg Usage (%)",
        ]
        self.assertEqual(list(df.columns), expected_cols)

    def test_node_rows_and_values(self):
        """Both NUMA nodes appear with correct aggregates from each source."""
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="NUMA_Overview").sort_values("NUMA Node").reset_index(drop=True)

        # Two nodes: 0 and 5
        self.assertEqual(list(df["NUMA Node"]), [0, 5])

        node0 = df.iloc[0]
        self.assertAlmostEqual(node0["Avg CPU (%)"], 20.0)  # mean(10,20,30)
        self.assertAlmostEqual(node0["Peak CPU (%)"], 30.0)
        self.assertAlmostEqual(node0["Avg Used (MB)"], 1100.0)  # mean(1000,1200)
        self.assertAlmostEqual(node0["Peak Used (MB)"], 1200.0)
        self.assertAlmostEqual(node0["Avg Usage (%)"], 55.0)  # mean(50,60)
        self.assertAlmostEqual(node0["HP Avg Total (MB)"], 4096.0)
        self.assertAlmostEqual(node0["HP Avg Used (MB)"], 1024.0)
        self.assertAlmostEqual(node0["HP Avg Usage (%)"], 25.0)

        node5 = df.iloc[1]
        self.assertAlmostEqual(node5["Avg CPU (%)"], 45.0)  # mean(40,50)
        self.assertAlmostEqual(node5["Peak CPU (%)"], 50.0)
        self.assertAlmostEqual(node5["Avg Used (MB)"], 2200.0)  # mean(2000,2400)
        self.assertAlmostEqual(node5["Peak Used (MB)"], 2400.0)
        self.assertAlmostEqual(node5["Avg Usage (%)"], 85.0)  # mean(80,90)
        self.assertAlmostEqual(node5["HP Avg Total (MB)"], 4096.0)
        self.assertAlmostEqual(node5["HP Avg Used (MB)"], 2048.0)
        self.assertAlmostEqual(node5["HP Avg Usage (%)"], 50.0)

    def test_partial_node_coverage(self):
        """A node present in only one source still appears with zeros elsewhere."""
        monitor = DummyMonitor()
        # Only CPU history for node 7 (no memory / hugepage data)
        monitor.numa_cpu_history[7] = [15.0, 25.0]
        monitor.numa_cpu_peak[7] = 25.0
        export_to_excel(monitor, self.log_dir, numa_nodes=[7], output_file=self.output_file)
        df = pd.read_excel(self.output_file, sheet_name="NUMA_Overview")
        self.assertEqual(list(df["NUMA Node"]), [7])
        row = df.iloc[0]
        self.assertAlmostEqual(row["Avg CPU (%)"], 20.0)
        self.assertAlmostEqual(row["Peak CPU (%)"], 25.0)
        # Memory + hugepage columns default to 0
        self.assertEqual(row["Avg Used (MB)"], 0)
        self.assertEqual(row["HP Avg Total (MB)"], 0)

    def test_empty_monitor_skips_sheet(self):
        """With no per-NUMA data at all, NUMA_Overview is omitted."""
        monitor = DummyMonitor()
        export_to_excel(monitor, self.log_dir, numa_nodes=None, output_file=self.output_file)
        sheets = self._sheet_names()
        self.assertNotIn("NUMA_Overview", sheets)


if __name__ == "__main__":
    unittest.main()
