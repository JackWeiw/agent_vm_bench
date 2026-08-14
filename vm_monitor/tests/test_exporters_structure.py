"""Structural snapshot test for vm_monitor/exporters.py export_to_excel.

Locks the set of sheets, their column headers, row counts, and chart count
produced from a fully-populated monitor + parsed_logs fixture. Purpose: guard
the mechanical refactor of export_to_excel (extraction into _build_* helpers)
against any structural drift -- a dropped/renamed sheet or column, a lost
chart, or a changed row count will fail this test.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from vm_monitor.base import VMMonitorBase
from vm_monitor.exporters import PANDAS_AVAILABLE, export_to_excel
from vm_monitor.parsers import parse_devkit_mem

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None

# Real devkit_mem.log ships in the repo (log_example/) -- drive the real parser.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEVKIT_MEM_LOG = os.path.join(_REPO_ROOT, "log_example", "devkit_mem.log")


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


def _full_monitor():
    """A monitor populated across every history the exporter reads."""
    m = DummyMonitor()

    # Raw VM samples (2 VMs, 2 samples each) -> VM_Stats + Raw_VM_Data
    m.data = [
        {
            "timestamp": "2026-08-14 10:00:00",
            "vm_name": "vm0",
            "pid": 100,
            "cpu_percent": 10.0,
            "memory_mb": 2048.0,
            "memory_huge_mb": 1024.0,
            "memory_private_mb": 1000.0,
            "memory_heap_mb": 50.0,
        },
        {
            "timestamp": "2026-08-14 10:00:05",
            "vm_name": "vm0",
            "pid": 100,
            "cpu_percent": 20.0,
            "memory_mb": 2100.0,
            "memory_huge_mb": 1024.0,
            "memory_private_mb": 1000.0,
            "memory_heap_mb": 50.0,
        },
        {
            "timestamp": "2026-08-14 10:00:00",
            "vm_name": "vm1",
            "pid": 101,
            "cpu_percent": 5.0,
            "memory_mb": 4096.0,
            "memory_huge_mb": 2048.0,
            "memory_private_mb": 2000.0,
            "memory_heap_mb": 100.0,
        },
    ]
    m.last_vm_count = 2
    m.peak_total_cpu = 30.0
    m.peak_total_memory_mb = 6144.0

    # Host stats -> Summary
    m.host_cpu_history = [40.0, 50.0, 60.0]
    m.host_mem_history = [{"used_mb": 8000.0, "total_mb": 16384.0}, {"used_mb": 9000.0, "total_mb": 16384.0}]
    m.peak_host_cpu = 60.0
    m.peak_host_mem_mb = 9000.0

    # Hugepage totals -> Summary
    m.hugepage_total_mb = 8192.0
    m.hugepage_used_history = [1024.0, 2048.0]
    m.peak_hugepage_used_mb = 2048.0

    # NUMA CPU + memory + hugepage -> NUMA_Overview
    m.numa_cpu_history[0] = [10.0, 20.0]
    m.numa_cpu_peak[0] = 20.0
    m.numa_cpu_history[5] = [30.0, 40.0]
    m.numa_cpu_peak[5] = 40.0

    def _node(nid, used, usage):
        return {
            "node": nid,
            "total": 8192.0,
            "used": used,
            "free": 8192.0 - used,
            "usage": usage,
            "total_mb": 8192.0,
            "used_mb": used,
            "free_mb": 8192.0 - used,
            "available_mb": 7000.0,
            "swap_cached_mb": 100.0,
            "anon_pages_mb": 500.0,
            "usage_pct": usage,
        }

    m.numa_memory_history = [
        {"ts": "2026-08-14 10:00:00", "nodes": [_node(0, 1000.0, 12.0), _node(5, 2000.0, 24.0)]},
        {"ts": "2026-08-14 10:00:05", "nodes": [_node(0, 1200.0, 14.0), _node(5, 2400.0, 29.0)]},
    ]
    m.hugepage_per_numa_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "nodes": {
                0: {"total_mb": 4096.0, "used_mb": 1024.0, "usage_pct": 25.0, "free_mb": 3072.0},
                5: {"total_mb": 4096.0, "used_mb": 2048.0, "usage_pct": 50.0, "free_mb": 2048.0},
            },
        },
        {
            "ts": "2026-08-14 10:00:05",
            "nodes": {
                0: {"total_mb": 4096.0, "used_mb": 1024.0, "usage_pct": 25.0, "free_mb": 3072.0},
                5: {"total_mb": 4096.0, "used_mb": 2048.0, "usage_pct": 50.0, "free_mb": 2048.0},
            },
        },
    ]

    # Swap -> Summary + Swap_Timeline
    m.swap_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "capacity": {"used_mb": 100.0, "free_mb": 1900.0, "total_mb": 2000.0},
            "cache": {"cached_mb": 50.0, "cached_ratio_pct": 2.5},
            "activity": {
                "swap_in_rate": 10.0,
                "swap_out_rate": 5.0,
                "pswpin_cumulative": 100,
                "pswpout_cumulative": 50,
                "pswpin_delta": 0,
                "pswpout_delta": 0,
            },
        },
        {
            "ts": "2026-08-14 10:00:05",
            "capacity": {"used_mb": 200.0, "free_mb": 1800.0, "total_mb": 2000.0},
            "cache": {"cached_mb": 60.0, "cached_ratio_pct": 3.0},
            "activity": {
                "swap_in_rate": 20.0,
                "swap_out_rate": 10.0,
                "pswpin_cumulative": 120,
                "pswpout_cumulative": 60,
                "pswpin_delta": 20,
                "pswpout_delta": 10,
            },
        },
    ]
    m.peak_swap_used_mb = 200.0
    m.peak_swap_cached_mb = 60.0

    # VM total memory -> VM_Total_Memory_Timeline
    m.vm_total_memory_history = [
        {"ts": "2026-08-14 10:00:00", "total_mb": 6144.0, "vm_count": 2, "per_numa": {0: 3072.0, 5: 3072.0}},
        {"ts": "2026-08-14 10:00:05", "total_mb": 6200.0, "vm_count": 2, "per_numa": {0: 3100.0, 5: 3100.0}},
    ]
    return m


def _full_parsed_logs():
    """parsed_logs fixture covering all collection-tool sheets.

    devkit_mem comes from the real parser against log_example/devkit_mem.log;
    the rest are hand-built to match each parser's return schema.
    """
    parsed = {}

    # --- devkit_mem (real parser) ---
    parsed["devkit_mem"] = parse_devkit_mem(_DEVKIT_MEM_LOG, [0])

    # --- devkit_top_down ---
    parsed["devkit_top_down"] = {
        "report_count": 2,
        "cycles_avg": 1000000.0,
        "instructions_avg": 500000.0,
        "ipc_avg": 0.5,
        "ipc_max": 0.6,
        "ipc_min": 0.4,
        "ipc": [0.5, 0.55],
        "bad_speculation_avg": 5.0,
        "frontend_bound_avg": 10.0,
        "retiring_avg": 20.0,
        "backend_bound_avg": 65.0,
        "l3_bound_avg": 15.0,
        "mem_bound_avg": 50.0,
        "mem_latency_bound_avg": 30.0,
        "mem_bandwidth_bound_avg": 20.0,
        "timestamps": ["2026-08-14 10:00:00", "2026-08-14 10:00:05"],
        "timeline": {
            "timestamp": ["2026-08-14 10:00:00", "2026-08-14 10:00:05"],
            "ipc": [0.5, 0.55],
            "bad_speculation": [5.0, 5.1],
            "frontend_bound": [10.0, 10.1],
            "retiring": [20.0, 20.1],
            "backend_bound": [65.0, 65.1],
            "l3_bound": [15.0, 15.1],
            "mem_bound": [50.0, 50.1],
            "mem_latency_bound": [30.0, 30.1],
            "mem_bandwidth_bound": [20.0, 20.1],
        },
    }

    # --- ksys ---
    parsed["ksys"] = {
        "l2_miss_latency": {"cycles_max": 100, "cycles_min": 10, "cycles_avg": 50},
        "l3_miss_latency": {"cycles_max": 500, "cycles_min": 50, "cycles_avg": 250},
        "ipc": 1.2,
        "topdown": {
            "retiring": 25.0,
            "frontend_bound": 15.0,
            "bad_speculation": 5.0,
            "backend_bound": 55.0,
        },
    }

    # --- ub_watch ---
    parsed["ub_watch"] = {
        "latency": {
            "path": "N0->N2",
            "samples": 1000,
            "avg_r": 100,
            "avg_w": 110,
            "min_r": 80,
            "min_w": 90,
            "max_r": 200,
            "max_w": 210,
        },
        "bandwidth": [
            {
                "chip": 0,
                "ports": "0-1",
                "avg_wr": 1000.0,
                "avg_rd": 2000.0,
                "avg_sum": 3000.0,
                "max_wr": 1500.0,
                "max_rd": 2500.0,
                "max_sum": 4000.0,
            },
            {
                "chip": 1,
                "ports": "2-3",
                "avg_wr": 1100.0,
                "avg_rd": 2100.0,
                "avg_sum": 3200.0,
                "max_wr": 1600.0,
                "max_rd": 2600.0,
                "max_sum": 4200.0,
            },
        ],
    }

    # --- smap_bw ---
    parsed["smap_bw"] = {
        "cycles": [
            {
                "cycle_no": 1,
                "total_pages": 1000,
                "duration": 1.0,
                "bandwidth_gb_s": 10.0,
                "directions": {(0, 5): 600, (5, 0): 400},
            },
            {
                "cycle_no": 2,
                "total_pages": 2000,
                "duration": 2.0,
                "bandwidth_gb_s": 20.0,
                "directions": {(0, 5): 1200, (5, 0): 800},
            },
        ],
        "summary": {
            "total_cycles": 2,
            "total_pages": 3000,
            "avg_bandwidth_gb_s": 15.0,
            "min_bandwidth_gb_s": 10.0,
            "max_bandwidth_gb_s": 20.0,
        },
        "all_directions": {(0, 5), (5, 0)},
    }

    # --- getfre (2 NUMA nodes, each with 2 cores) ---
    parsed["getfre"] = {
        0: {
            "numa_avg": 2400.0,
            "numa_min": 2000.0,
            "numa_max": 2800.0,
            "sample_count": 10,
            "core_stats": {
                10: {"avg": 2400.0, "min": 2000.0, "max": 2800.0, "count": 5},
                11: {"avg": 2500.0, "min": 2100.0, "max": 2900.0, "count": 5},
            },
        },
        1: {
            "numa_avg": 2300.0,
            "numa_min": 1900.0,
            "numa_max": 2700.0,
            "sample_count": 10,
            "core_stats": {20: {"avg": 2300.0, "min": 1900.0, "max": 2700.0, "count": 5}},
        },
    }
    return parsed


def _snapshot(output_file):
    """Return (sheet_names, {sheet: [cols]}, {sheet: row_count}, chart_count)."""
    with pd.ExcelFile(output_file) as xl:
        sheet_names = list(xl.sheet_names)
        cols = {}
        rows = {}
        for s in sheet_names:
            df = pd.read_excel(xl, sheet_name=s)
            cols[s] = list(df.columns)
            rows[s] = len(df)
    chart_count = 0
    if load_workbook is not None:
        wb = load_workbook(output_file)
        for ws in wb.worksheets:
            chart_count += len(ws._charts)
        wb.close()
    return sheet_names, cols, rows, chart_count


@unittest.skipUnless(PANDAS_AVAILABLE and load_workbook is not None, "pandas/openpyxl required")
class TestExportStructure(unittest.TestCase):
    """Snapshot of export_to_excel output, frozen to guard the refactor."""

    def setUp(self):
        self.monitor = _full_monitor()
        self.parsed_logs = _full_parsed_logs()
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_struct_")
        self.output_file = os.path.join(self.log_dir, "analysis_report.xlsx")

    def tearDown(self):
        for f in os.listdir(self.log_dir):
            try:
                os.unlink(os.path.join(self.log_dir, f))
            except PermissionError:
                pass
        os.rmdir(self.log_dir)

    def _export(self):
        with patch("vm_monitor.exporters.parse_all_logs", return_value=self.parsed_logs):
            return export_to_excel(self.monitor, self.log_dir, numa_nodes=[0], output_file=self.output_file)

    def test_sheet_set_and_order(self):
        self._export()
        names, _cols, _rows, _charts = _snapshot(self.output_file)
        self.assertEqual(
            names,
            [
                "Summary",
                "NUMA_Overview",
                "VM_Stats",
                "DevKit_TopDown",
                "TopDown_Timeline",
                "DevKit_Memory",
                "NUMA_Bandwidth",
                "Memory_Timeline",
                "KSys",
                "UBWatch_Latency",
                "UBWatch_Bandwidth",
                "SMAPBW_Summary",
                "SMAPBW_Cycles",
                "Getfre_Summary",
                "Getfre_NUMA0",
                "Getfre_NUMA1",
                "Raw_VM_Data",
                "Swap_Timeline",
                "NUMA_Memory_Timeline",
                "VM_Total_Memory_Timeline",
            ],
        )

    def test_columns_and_row_counts(self):
        self._export()
        _names, cols, rows, _charts = _snapshot(self.output_file)

        expected_cols = {
            "Summary": ["Metric", "Value", "Unit"],
            "NUMA_Overview": [
                "NUMA Node",
                "Avg CPU (%)",
                "Peak CPU (%)",
                "Avg Used (MB)",
                "Peak Used (MB)",
                "Avg Usage (%)",
                "HP Avg Total (MB)",
                "HP Avg Used (MB)",
                "HP Avg Usage (%)",
            ],
            "VM_Stats": [
                "VM Name",
                "PID",
                "Samples",
                "Avg CPU (%)",
                "Max CPU (%)",
                "Avg Memory (MB)",
                "Max Memory (MB)",
                "Avg Hugepage (MB)",
            ],
            "DevKit_TopDown": ["Metric", "Value", "Report Count"],
            "DevKit_Memory": ["Metric", "Value", "Report Count"],
            "NUMA_Bandwidth": ["NUMA Node", "Read (MB/s)", "Write (MB/s)"],
            "KSys": ["Metric", "Value"],
            "UBWatch_Latency": ["Metric", "Value"],
            "UBWatch_Bandwidth": [
                "Chip",
                "Ports",
                "Avg Write (MB/s)",
                "Avg Read (MB/s)",
                "Avg Sum (MB/s)",
                "Max Write (MB/s)",
                "Max Read (MB/s)",
                "Max Sum (MB/s)",
            ],
            "SMAPBW_Summary": ["Metric", "Value"],
            "SMAPBW_Cycles": ["Cycle", "Pages", "Duration (s)", "Bandwidth (GB/s)", "N0->N5_pages", "N5->N0_pages"],
            "Getfre_Summary": [
                "NUMA",
                "Avg Frequency (MHz)",
                "Min Frequency (MHz)",
                "Max Frequency (MHz)",
                "Sample Count",
                "Core Count",
            ],
            "Getfre_NUMA0": [
                "Core ID",
                "Avg Frequency (MHz)",
                "Min Frequency (MHz)",
                "Max Frequency (MHz)",
                "Sample Count",
            ],
            "Getfre_NUMA1": [
                "Core ID",
                "Avg Frequency (MHz)",
                "Min Frequency (MHz)",
                "Max Frequency (MHz)",
                "Sample Count",
            ],
            "Raw_VM_Data": ["Timestamp", "VM Name", "PID", "CPU (%)", "Memory (MB)", "Hugepage (MB)"],
        }
        for sheet, expected in expected_cols.items():
            self.assertEqual(cols[sheet], expected, f"columns mismatch for {sheet}")

        expected_rows = {
            "Summary": 30,  # meta(4)+host(4)+hugepage(4)+swap-cap(4)+swap-cache(3)+swap-act(6)+vm(5)
            "NUMA_Overview": 2,
            "VM_Stats": 2,
            "DevKit_TopDown": 13,
            "NUMA_Bandwidth": 1,  # devkit_mem.log NUMA bandwidth filtered to numa_nodes=[0]
            "KSys": 11,  # 3+3+1+4
            "UBWatch_Latency": 8,
            "UBWatch_Bandwidth": 2,
            "SMAPBW_Summary": 5,
            "SMAPBW_Cycles": 2,
            "Getfre_Summary": 2,
            "Getfre_NUMA0": 2,
            "Getfre_NUMA1": 1,
            "Raw_VM_Data": 3,
            "Swap_Timeline": 2,
            "NUMA_Memory_Timeline": 2,
            "VM_Total_Memory_Timeline": 2,
        }
        for sheet, expected in expected_rows.items():
            self.assertEqual(rows[sheet], expected, f"row count mismatch for {sheet}")

    def test_chart_count(self):
        self._export()
        _names, _cols, _rows, charts = _snapshot(self.output_file)
        # 1 DevKit pie + 1 IPC line + 1 MemBound bar + 1 DDR line + 1 CacheMiss bar
        # + 2 Swap (in/out + SwapCache) + 2 NUMA_Memory_Timeline (8A + 8B) + 1 VM_Total
        self.assertEqual(charts, 10)


if __name__ == "__main__":
    unittest.main()
