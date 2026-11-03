"""Structural snapshot test for vm_monitor/exporters.py export_to_excel.

Locks the set of sheets, their column headers, row counts, and chart count
produced from a fully-populated monitor + parsed_logs fixture. Purpose: guard
the mechanical refactor of export_to_excel (extraction into _build_* helpers)
against any structural drift -- a dropped/renamed sheet or column, a lost
chart, or a changed row count will fail this test.
"""

import os
import shutil
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

# Real devkit_mem.log ships in the repo (vm_monitor/tests/fixtures/) -- drive
# the real parser.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_DEVKIT_MEM_LOG = os.path.join(_TESTS_DIR, "fixtures", "devkit_mem.log")


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

    # Raw VM samples (2 VMs, 2 samples each) -> VM_Stats
    # Carries every field collect_sample() writes (base.py record schema),
    # incl. the per-VM private/heap/swapcache scalars + per-NUMA breakdowns
    # (the raw history also feeds the timeline collector contract).
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
            "memory_swapcache_mb": 10.0,
            "memory_per_numa": {0: {"total_mb": 1000.0}, 5: {"total_mb": 1048.0}},
            "memory_swapcache_per_numa": {0: 4.0, 5: 6.0},
            "status": "running",
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
            "memory_swapcache_mb": 20.0,
            "memory_per_numa": {0: {"total_mb": 1000.0}, 5: {"total_mb": 1100.0}},
            "memory_swapcache_per_numa": {0: 8.0, 5: 12.0},
            "status": "running",
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
            "memory_swapcache_mb": 30.0,
            "memory_per_numa": {0: {"total_mb": 2000.0}, 5: {"total_mb": 2096.0}},
            "memory_swapcache_per_numa": {0: 10.0, 5: 20.0},
            "status": "running",
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
            # Reclaim-pressure signals collected per NUMA node per sample
            # (_NUMA_MEMINFO_FIELDS) but historically dropped from the xlsx.
            "active_mb": round(used * 0.6, 2),
            "inactive_mb": round(used * 0.3, 2),
            "file_pages_mb": round(used * 0.2, 2),
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

    # Swap -> Summary
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

    # VM total memory -> Summary (VM Peak Total Memory)
    # Carries swapcache_mb + swapcache_per_numa that collect_vm_total_memory
    # aggregates each sample.
    m.vm_total_memory_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "total_mb": 6144.0,
            "vm_count": 2,
            "per_numa": {0: 3072.0, 5: 3072.0},
            "swapcache_mb": 40.0,
            "swapcache_per_numa": {0: 14.0, 5: 26.0},
        },
        {
            "ts": "2026-08-14 10:00:05",
            "total_mb": 6200.0,
            "vm_count": 2,
            "per_numa": {0: 3100.0, 5: 3100.0},
            "swapcache_mb": 20.0,
            "swapcache_per_numa": {0: 8.0, 5: 12.0},
        },
    ]

    # Disk I/O (sda/sdb/sdc) + ublk -> Summary peaks
    m.target_disks = ["sda", "sdb", "sdc"]

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

    m.disk_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "disks": {
                "sda": dr(10.0, 20.0, 5.0, 1, 0.5, 2.0, 3.0),
                "sdb": dr(5.0, 15.0, 3.0, 2, 1.0, 4.0, 5.0),
                "sdc": dr(1.0, 2.0, 1.0, 0, 0.2, 1.0, 1.5),
            },
        },
        {
            "ts": "2026-08-14 10:00:05",
            "disks": {
                "sda": dr(12.0, 25.0, 6.0, 1, 0.8, 2.5, 3.5),
                "sdb": dr(6.0, 18.0, 4.0, 3, 1.2, 4.5, 5.5),
                "sdc": dr(2.0, 4.0, 2.0, 1, 0.3, 1.5, 2.0),
            },
        },
    ]
    m.peak_disk_write_mb_s = 25.0

    # Host mem detail -> Summary peaks
    m.host_mem_detail_history = [
        {"ts": "2026-08-14 10:00:00", "cached_mb": 1000.0, "buffers_mb": 50.0, "dirty_mb": 10.0, "writeback_mb": 5.0},
        {"ts": "2026-08-14 10:00:05", "cached_mb": 1100.0, "buffers_mb": 55.0, "dirty_mb": 20.0, "writeback_mb": 8.0},
    ]
    m.peak_dirty_mb = 20.0
    m.peak_writeback_mb = 8.0

    # ublk device count -> Summary peak
    m.ublk_history = [
        {"ts": "2026-08-14 10:00:00", "ublk_devices": 3},
        {"ts": "2026-08-14 10:00:05", "ublk_devices": 4},
    ]
    m.peak_ublk_devices = 4

    # Host page-cache pressure -> Summary peaks
    m.host_pressure_history = [
        {
            "ts": "2026-08-14 10:00:00",
            "page_scan_mib_s": 5.0,
            "page_reclaim_mib_s": 4.0,
            "file_refault_mib_s": 1.0,
            "anon_pages_mb": 2000.0,
            "file_cache_mb": 3000.0,
            "sreclaimable_mb": 100.0,
            "iowait_pct": 2.0,
            "procs_running": 4,
            "procs_blocked": 0,
        },
        {
            "ts": "2026-08-14 10:00:05",
            "page_scan_mib_s": 8.0,
            "page_reclaim_mib_s": 6.0,
            "file_refault_mib_s": 2.0,
            "anon_pages_mb": 2100.0,
            "file_cache_mb": 3100.0,
            "sreclaimable_mb": 110.0,
            "iowait_pct": 5.0,
            "procs_running": 6,
            "procs_blocked": 1,
        },
    ]
    m.peak_page_scan_mib_s = 8.0
    m.peak_page_reclaim_mib_s = 6.0
    m.peak_file_refault_mib_s = 2.0
    m.dirty_limit_mb = 500.0
    m.dirty_background_limit_mb = 250.0
    m._dirty_limits_read = True
    return m


def _full_parsed_logs():
    """parsed_logs fixture covering all collection-tool sheets.

    devkit_mem comes from the real parser against vm_monitor/tests/fixtures/devkit_mem.log;
    the rest are hand-built to match each parser's return schema.
    """
    parsed = {}

    # --- devkit_mem (real parser) ---
    parsed["devkit_mem"] = parse_devkit_mem(_DEVKIT_MEM_LOG, [0])

    # --- devkit_top_down ---
    # Mirrors parse_devkit_top_down's real return: every metric carries
    # _avg/_max/_min (parser computes all three); the sheet historically
    # exported only _avg (+ ipc_max/min).
    parsed["devkit_top_down"] = {
        "report_count": 2,
        "cycles_avg": 1000000.0,
        "cycles_max": 1200000.0,
        "cycles_min": 800000.0,
        "instructions_avg": 500000.0,
        "instructions_max": 600000.0,
        "instructions_min": 400000.0,
        "ipc_avg": 0.5,
        "ipc_max": 0.6,
        "ipc_min": 0.4,
        "ipc": [0.5, 0.55],
        "bad_speculation_avg": 5.0,
        "bad_speculation_max": 6.0,
        "bad_speculation_min": 4.0,
        "frontend_bound_avg": 10.0,
        "frontend_bound_max": 11.0,
        "frontend_bound_min": 9.0,
        "retiring_avg": 20.0,
        "retiring_max": 22.0,
        "retiring_min": 18.0,
        "backend_bound_avg": 65.0,
        "backend_bound_max": 68.0,
        "backend_bound_min": 62.0,
        "l3_bound_avg": 15.0,
        "l3_bound_max": 17.0,
        "l3_bound_min": 13.0,
        "mem_bound_avg": 50.0,
        "mem_bound_max": 55.0,
        "mem_bound_min": 45.0,
        "mem_latency_bound_avg": 30.0,
        "mem_latency_bound_max": 33.0,
        "mem_latency_bound_min": 27.0,
        "mem_bandwidth_bound_avg": 20.0,
        "mem_bandwidth_bound_max": 23.0,
        "mem_bandwidth_bound_min": 17.0,
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
    # Carries the `timeline` {timestamp: {core_id: freq_mhz}} parse_getfre
    # returns; resource_report surfaces only the per-core summary sheet.
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
            "timeline": {
                "2026-08-14 10:00:00": {10: 2400, 11: 2500},
                "2026-08-14 10:00:05": {10: 2600, 11: 2700},
            },
        },
        1: {
            "numa_avg": 2300.0,
            "numa_min": 1900.0,
            "numa_max": 2700.0,
            "sample_count": 10,
            "core_stats": {20: {"avg": 2300.0, "min": 1900.0, "max": 2700.0, "count": 5}},
            "timeline": {
                "2026-08-14 10:00:00": {20: 2300},
                "2026-08-14 10:00:05": {20: 2500},
            },
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


def _charts_by_sheet(output_file):
    """Return ({sheet_name: chart_count}, {sheet_name: [series_count per chart]}).

    Companion to _snapshot for chart-shape assertions: the snapshot only carries
    a total chart count, but the dropped-metric charts land on specific sheets,
    so per-sheet counts + per-chart series counts pin them precisely.
    """
    by_sheet = {}
    series_by_sheet = {}
    wb = load_workbook(output_file)
    for ws in wb.worksheets:
        by_sheet[ws.title] = len(ws._charts)
        series_by_sheet[ws.title] = [len(c.series) for c in ws._charts]
    wb.close()
    return by_sheet, series_by_sheet


@unittest.skipUnless(PANDAS_AVAILABLE and load_workbook is not None, "pandas/openpyxl required")
class TestExportStructure(unittest.TestCase):
    """Snapshot of export_to_excel output, frozen to guard the refactor."""

    def setUp(self):
        self.monitor = _full_monitor()
        self.parsed_logs = _full_parsed_logs()
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_struct_")
        self.output_file = os.path.join(self.log_dir, "resource_report.xlsx")

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
        """Summary sheets only. Every per-sample timeline now lives in
        host_resources.xlsx (tests/test_raw_report.py), so a timeline sheet
        reappearing here means the split regressed."""
        self._export()
        names, _cols, _rows, _charts = _snapshot(self.output_file)
        self.assertEqual(
            names,
            [
                "Summary",
                "NUMA_Overview",
                "VM_Stats",
                "DevKit_TopDown",
                "DevKit_Memory",
                "NUMA_Bandwidth",
                "KSys",
                "UBWatch_Latency",
                "UBWatch_Bandwidth",
                "SMAPBW_Summary",
                "SMAPBW_Cycles",
                "Getfre_Summary",
                "Getfre_NUMA0",
                "Getfre_NUMA1",
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
                "Min Memory (MB)",
                "Last Memory (MB)",
                "Avg PSS (MB)",
                "Max PSS (MB)",
                "Avg Hugepage (MB)",
                "Max Hugepage (MB)",
                "Avg Private (MB)",
                "Max Private (MB)",
                "Avg Heap (MB)",
                "Max Heap (MB)",
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
        }
        for sheet, expected in expected_cols.items():
            self.assertEqual(cols[sheet], expected, f"columns mismatch for {sheet}")

        expected_rows = {
            "Summary": 39,  # meta(4)+host(4)+hugepage(4)+swap-cap(4)+swap-cache(3)+swap-act(6)+vm(6)+disk/mem(5)+pressure(3)
            "NUMA_Overview": 2,
            "VM_Stats": 2,
            "DevKit_TopDown": 33,  # 13 base (avg + ipc max/min) + 20 max/min for cycles/instructions + 8 topdown metrics
            "NUMA_Bandwidth": 1,  # devkit_mem.log NUMA bandwidth filtered to numa_nodes=[0]
            "KSys": 11,  # 3+3+1+4
            "UBWatch_Latency": 8,
            "UBWatch_Bandwidth": 2,
            "SMAPBW_Summary": 5,
            "SMAPBW_Cycles": 2,
            "Getfre_Summary": 2,
            "Getfre_NUMA0": 2,
            "Getfre_NUMA1": 1,
        }
        for sheet, expected in expected_rows.items():
            self.assertEqual(rows[sheet], expected, f"row count mismatch for {sheet}")

    def test_chart_count(self):
        self._export()
        _names, _cols, _rows, charts = _snapshot(self.output_file)
        # 1 DevKit TopDown pie + 1 MemBound bar + 1 cache-miss bar + 1 VM_Stats
        # memory-composition bar. The timeline sheets (and their charts) moved
        # to host_resources.xlsx -- tests/test_raw_report.py locks those.
        self.assertEqual(charts, 4)


@unittest.skipUnless(PANDAS_AVAILABLE and load_workbook is not None, "pandas/openpyxl required")
class TestDroppedMetricsExported(unittest.TestCase):
    """Value checks for metrics that were always collected (monitor histories
    or parsed logs) but never reached the xlsx. The exporter now surfaces them;
    these tests pin the values so a future refactor cannot silently drop them
    again. No new collection -- every value below originates in _full_monitor()
    / _full_parsed_logs(), which mirror the real base.py / parsers.py schemas.
    """

    def setUp(self):
        self.monitor = _full_monitor()
        self.parsed_logs = _full_parsed_logs()
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_dropped_")
        self.output_file = os.path.join(self.log_dir, "resource_report.xlsx")

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

    def test_vm_stats_carries_min_last_huge_private_heap(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="VM_Stats").sort_values("VM Name").reset_index(drop=True)
        vm0 = df.iloc[0]  # memory [2048, 2100]
        self.assertAlmostEqual(vm0["Min Memory (MB)"], 2048.0)
        self.assertAlmostEqual(vm0["Last Memory (MB)"], 2100.0)
        self.assertAlmostEqual(vm0["Max Hugepage (MB)"], 1024.0)
        self.assertAlmostEqual(vm0["Avg Private (MB)"], 1000.0)
        self.assertAlmostEqual(vm0["Max Private (MB)"], 1000.0)
        self.assertAlmostEqual(vm0["Avg Heap (MB)"], 50.0)
        self.assertAlmostEqual(vm0["Max Heap (MB)"], 50.0)
        vm1 = df.iloc[1]  # memory [4096]
        self.assertAlmostEqual(vm1["Min Memory (MB)"], 4096.0)
        self.assertAlmostEqual(vm1["Last Memory (MB)"], 4096.0)
        self.assertAlmostEqual(vm1["Max Hugepage (MB)"], 2048.0)
        self.assertAlmostEqual(vm1["Max Private (MB)"], 2000.0)
        self.assertAlmostEqual(vm1["Max Heap (MB)"], 100.0)

    def test_summary_carries_vm_peak_total_memory(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="Summary")
        peaks = dict(zip(df["Metric"], df["Value"]))
        self.assertIn("VM Peak Total Memory", peaks)
        self.assertAlmostEqual(peaks["VM Peak Total Memory"], 6144.0)

    def test_devkit_topdown_carries_max_min_rows(self):
        self._export()
        df = pd.read_excel(self.output_file, sheet_name="DevKit_TopDown")
        m = dict(zip(df["Metric"], df["Value"]))
        self.assertAlmostEqual(m["Cycles Max"], 1200000.0)
        self.assertAlmostEqual(m["Cycles Min"], 800000.0)
        self.assertAlmostEqual(m["Instructions Max"], 600000.0)
        self.assertAlmostEqual(m["Instructions Min"], 400000.0)
        self.assertAlmostEqual(m["Bad Speculation Max"], 6.0)
        self.assertAlmostEqual(m["Bad Speculation Min"], 4.0)
        self.assertAlmostEqual(m["Bandwidth Bound Max"], 23.0)
        self.assertAlmostEqual(m["Bandwidth Bound Min"], 17.0)


@unittest.skipUnless(PANDAS_AVAILABLE and load_workbook is not None, "pandas/openpyxl required")
class TestDroppedMetricCharts(unittest.TestCase):
    """Charts for the metrics surfaced in the dropped-metrics PR. The data
    already lands in the xlsx (TestDroppedMetricsExported pins the values);
    these tests pin that each high-value dataset also gets a chart so it is
    visually consumable, not just tabular.
    """

    def setUp(self):
        self.monitor = _full_monitor()
        self.parsed_logs = _full_parsed_logs()
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_charts_")
        self.output_file = os.path.join(self.log_dir, "resource_report.xlsx")

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

    def test_vm_stats_has_memory_composition_bar(self):
        """VM_Stats gains a grouped bar of the avg memory composition
        (Memory / Private / Heap / Hugepage) per VM."""
        self._export()
        by_sheet, series = _charts_by_sheet(self.output_file)
        self.assertEqual(by_sheet.get("VM_Stats"), 1)
        self.assertEqual(series["VM_Stats"], [4])


@unittest.skipUnless(PANDAS_AVAILABLE and load_workbook is not None, "pandas/openpyxl required")
class TestAtomicReportWrite(unittest.TestCase):
    """The final resource_report.xlsx must appear exactly once, atomically,
    complete with charts.

    bench-core's MonitorController.stop() reaps the vm_monitor subprocess the
    moment resource_report.xlsx appears (it polls the path, then SIGTERMs the
    process). export_to_excel used to write the final path TWICE: pandas wrote
    it (sheets, no charts), then _add_charts did load_workbook + wb.save on the
    SAME path. The reap fired after the pandas write and SIGTERMed the process
    mid re-save -- truncating the zip -> 'corrupt, can't open' in Excel. The
    fix: pandas builds to a temp, _add_charts re-saves the temp, and only an
    atomic os.replace(temp -> final) at the very end creates the final path.
    """

    def setUp(self):
        self.monitor = _full_monitor()
        self.parsed_logs = _full_parsed_logs()
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_atomic_")
        self.output_file = os.path.join(self.log_dir, "resource_report.xlsx")

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

    def test_final_xlsx_absent_during_chart_phase(self):
        """pandas must build to a temp; the final path may appear only via an
        atomic rename AFTER charts are added. Asserts output_file does NOT
        exist at the moment _add_charts runs."""
        import vm_monitor.exporters as exp

        seen = {}
        orig = exp._add_charts

        def spy(build_file):
            seen["exists_at_chart_time"] = os.path.exists(self.output_file)
            return orig(build_file)

        with patch.object(exp, "_add_charts", spy):
            self._export()
        self.assertIn("exists_at_chart_time", seen, "_add_charts was never called")
        self.assertFalse(
            seen["exists_at_chart_time"],
            "final resource_report.xlsx must not exist during the chart phase "
            "(MonitorController would reap + SIGTERM the wb.save mid-write -> corrupt zip)",
        )

    def test_final_xlsx_valid_when_charts_raise(self):
        """If chart generation raises, the final file must still be a valid xlsx
        (pandas-built sheets promoted) and export_to_excel must not propagate
        the exception -- a MonitorController waiting on the path would otherwise
        time out with no report at all."""
        import vm_monitor.exporters as exp

        with patch.object(exp, "_add_charts", side_effect=RuntimeError("boom")):
            self._export()  # must not raise
        self.assertTrue(os.path.exists(self.output_file))
        wb = load_workbook(self.output_file)
        self.assertIn("Summary", wb.sheetnames)
        wb.close()

    def test_no_build_temp_left_behind(self):
        """After a successful export, no .build temp lingers in the log dir --
        only the final resource_report.xlsx (and unrelated CSVs)."""
        self._export()
        leftovers = [f for f in os.listdir(self.log_dir) if f.endswith(".xlsx") and f != "resource_report.xlsx"]
        self.assertEqual(leftovers, [], f"build temp left behind: {leftovers}")


class TestRawCpuSourcePreferred(unittest.TestCase):
    """resource_report's VM_Stats / Summary must source host + per-VM CPU from the
    raw_data /proc delta series (same source as host_resources.xlsx) when present,
    NOT the psutil in-memory history -- so the CPU views never disagree with
    host_resources.xlsx / spotbox_monitor."""

    def setUp(self):
        self.monitor = _full_monitor()
        self.parsed_logs = _full_parsed_logs()
        self.log_dir = tempfile.mkdtemp(prefix="vm_monitor_rawcpu_")
        self.output_file = os.path.join(self.log_dir, "resource_report.xlsx")
        os.makedirs(os.path.join(self.log_dir, "raw_data"))

    def tearDown(self):
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def _export(self):
        with patch("vm_monitor.exporters.parse_all_logs", return_value=self.parsed_logs):
            return export_to_excel(self.monitor, self.log_dir, numa_nodes=[0], output_file=self.output_file)

    @staticmethod
    def _write_csv(path, header, rows):
        import csv

        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([header, *rows])

    def test_raw_cpu_overrides_psutil(self):
        # _full_monitor() seeds the psutil distractors host_cpu_history=[40,50,60]
        # and VM cpu_percent vm0=[10,20], vm1=[5]. raw_data below carries DIFFERENT
        # jiffies; the sheets must reflect the raw delta, not psutil.
        rd = os.path.join(self.log_dir, "raw_data")
        self._write_csv(
            os.path.join(rd, "host_cpu_raw.csv"),
            "timestamp,cpu_user,cpu_nice,cpu_system,cpu_idle,cpu_iowait,cpu_irq,cpu_softirq,cpu_steal,cpu_guest,cpu_guest_nice,ctxt,btime,processes,procs_running,procs_blocked,softirq_total".split(
                ","
            ),
            [
                [
                    "2026-08-14 10:00:00",
                    "100",
                    "0",
                    "100",
                    "800",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                ],
                [
                    "2026-08-14 10:00:02",
                    "110",
                    "0",
                    "100",
                    "808",
                    "2",
                    "0",
                    "10",
                    "10",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                ],
            ],
        )
        # 2s delta: d_total=40, d(idle+iowait)=10 -> host CPU busy = 75.0
        self._write_csv(
            os.path.join(rd, "vm_cpu_raw.csv"),
            "timestamp,pid,vm_name,utime,stime,pss_mb".split(","),
            [
                ["2026-08-14 10:00:00", "100", "vm0", "0", "0", "1024"],
                ["2026-08-14 10:00:02", "100", "vm0", "100", "0", "1024"],
                ["2026-08-14 10:00:00", "101", "vm1", "0", "0", "2048"],
                ["2026-08-14 10:00:02", "101", "vm1", "200", "0", "2048"],
            ],
        )
        # 2s delta (CLK_TCK=100): vm0 d=100 -> 0.5 core -> 50%, vm1 d=200 -> 1.0 -> 100%
        self._export()
        self.assertTrue(os.path.exists(self.output_file))

        # Host_CPU_Timeline moved to host_resources.xlsx; the raw-vs-psutil
        # contract is still observable here through Summary + VM_Stats.
        vm = pd.read_excel(self.output_file, sheet_name="VM_Stats")
        got = {r["VM Name"]: float(r["Avg CPU (%)"]) for _, r in vm.iterrows()}
        self.assertEqual(got.get("vm0"), 50.0, f"vm0 Avg CPU must be raw 50.0 not psutil 15; got {got}")
        self.assertEqual(got.get("vm1"), 100.0, f"vm1 Avg CPU must be raw 100.0 not psutil 5; got {got}")

        summary = pd.read_excel(self.output_file, sheet_name="Summary")
        host_avg = float(summary.loc[summary["Metric"] == "Host Avg CPU", "Value"].iloc[0])
        self.assertEqual(
            host_avg, 75.0, f"Summary Host Avg CPU must match raw-series mean, not psutil 50; got {host_avg}"
        )


if __name__ == "__main__":
    unittest.main()
