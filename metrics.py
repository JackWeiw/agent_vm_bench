#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metrics Definitions - Shared metrics definitions across modules

This module defines all metrics used in:
- qemu_monitor.py (Excel generation)
- vm_bench_lite.py (Report generation)
- batch_test_scheduler.py (Metrics extraction and summary)

Adding new metrics: simply add to METRICS_DEFINITIONS list
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum


class MetricCategory(Enum):
    """Metric category enumeration"""
    BASIC = "basic"               # Test basic info (test_id, vm_count, etc.)
    BROWSER = "browser"           # Browser benchmark metrics
    VM_CPU = "vm_cpu"             # VM CPU stats from Summary sheet
    DEVKIT_TOPDOWN = "devkit_topdown"   # DevKit Top-Down analysis (13 metrics)
    DEVKIT_MEMORY = "devkit_memory"     # DevKit Memory metrics (cache miss, DDR)
    NUMA_BANDWIDTH = "numa_bandwidth"   # NUMA bandwidth per node
    KSYS = "ksys"                 # KSys metrics (latency, IPC, topdown)
    UBWATCH = "ubwatch"           # UBWatch latency metrics


@dataclass
class MetricDef:
    """Single metric definition"""
    key: str                      # Internal key used in code (e.g., "td_ipc_avg")
    display_name: str             # Display name in Excel/report (e.g., "IPC Avg")
    category: MetricCategory      # Category
    source_sheet: str             # Source Excel sheet name (e.g., "DevKit_TopDown")
    source_metric: str            # Original metric name in source file
    unit: str                     # Unit (e.g., "%", "MB/s", "ns", "cycles")
    description: str              # Description for documentation


# ============================================================================
# ALL METRICS DEFINITIONS
# ============================================================================

METRICS_DEFINITIONS: List[MetricDef] = [

    # ========== BASIC - Test Info ==========
    MetricDef("test_id", "Test ID", MetricCategory.BASIC, "-", "-", "-",
              "Test identifier from directory name"),
    MetricDef("vm_count", "VM Count", MetricCategory.BASIC, "config.yaml", "-", "-",
              "Total number of VMs"),
    MetricDef("ratio", "Ratio", MetricCategory.BASIC, "config.yaml", "-", "-",
              "Memory borrow ratio (0.10, 0.15, etc.)"),
    MetricDef("active_percent", "Active Percent", MetricCategory.BASIC, "config.yaml", "-", "-",
              "Active VM percentage for benchmark"),
    MetricDef("active_vm_count", "Active VM Count", MetricCategory.BASIC, "-", "-", "-",
              "Calculated: vm_count * active_percent"),
    MetricDef("success", "Success", MetricCategory.BASIC, "-", "-", "-",
              "Test success status"),

    # ========== BROWSER - Browser Benchmark Metrics ==========
    MetricDef("browser_success_rate", "Success Rate", MetricCategory.BROWSER,
              "bench_report", "Success Rate", "%",
              "Browser task success rate"),
    MetricDef("browser_avg_latency_ms", "Avg Latency", MetricCategory.BROWSER,
              "bench_report", "Avg Latency", "ms",
              "Average browser task latency"),
    MetricDef("browser_p99_latency_ms", "P99 Latency", MetricCategory.BROWSER,
              "bench_report", "P99 Latency", "ms",
              "P99 browser task latency"),
    MetricDef("browser_total_tasks", "Total Tasks", MetricCategory.BROWSER,
              "bench_report", "Total Tasks", "-",
              "Total browser tasks executed"),

    # ========== VM_CPU - VM CPU Stats from Summary Sheet ==========
    MetricDef("vm_avg_cpu_percent", "VM Avg CPU", MetricCategory.VM_CPU,
              "Summary", "VM Avg CPU", "%",
              "Average CPU usage across all VMs"),
    MetricDef("vm_max_cpu_percent", "VM Peak Total CPU", MetricCategory.VM_CPU,
              "Summary", "VM Peak Total CPU", "%",
              "Peak total CPU usage of all VMs"),

    # ========== DEVKIT_TOPDOWN - DevKit Top-Down Analysis (13 metrics) ==========
    MetricDef("td_cycles_avg", "Cycles Avg", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Cycles Avg", "-",
              "Average CPU cycles"),
    MetricDef("td_instructions_avg", "Instructions Avg", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Instructions Avg", "-",
              "Average CPU instructions"),
    MetricDef("td_ipc_avg", "IPC Avg", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "IPC Avg", "-",
              "Average Instructions Per Cycle"),
    MetricDef("td_ipc_max", "IPC Max", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "IPC Max", "-",
              "Maximum IPC"),
    MetricDef("td_ipc_min", "IPC Min", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "IPC Min", "-",
              "Minimum IPC"),
    MetricDef("td_bad_speculation", "Bad Speculation (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Bad Speculation (%)", "%",
              "Bad speculation percentage"),
    MetricDef("td_frontend_bound", "Frontend Bound (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Frontend Bound (%)", "%",
              "Frontend bound percentage"),
    MetricDef("td_retiring", "Retiring (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Retiring (%)", "%",
              "Retiring percentage"),
    MetricDef("td_backend_bound", "Backend Bound (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Backend Bound (%)", "%",
              "Backend bound percentage"),
    MetricDef("td_l3_bound", "L3 Bound (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "L3 Bound (%)", "%",
              "L3 cache bound percentage"),
    MetricDef("td_mem_bound", "Mem Bound (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Mem Bound (%)", "%",
              "Memory bound percentage"),
    MetricDef("td_latency_bound", "Latency Bound (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Latency Bound (%)", "%",
              "Memory latency bound percentage"),
    MetricDef("td_bandwidth_bound", "Bandwidth Bound (%)", MetricCategory.DEVKIT_TOPDOWN,
              "DevKit_TopDown", "Bandwidth Bound (%)", "%",
              "Memory bandwidth bound percentage"),

    # ========== DEVKIT_MEMORY - DevKit Memory Metrics (6 metrics) ==========
    MetricDef("mem_l1d_miss", "L1D Miss (%)", MetricCategory.DEVKIT_MEMORY,
              "DevKit_Memory", "L1D Miss (%)", "%",
              "L1 data cache miss rate"),
    MetricDef("mem_l1i_miss", "L1I Miss (%)", MetricCategory.DEVKIT_MEMORY,
              "DevKit_Memory", "L1I Miss (%)", "%",
              "L1 instruction cache miss rate"),
    MetricDef("mem_l2d_miss", "L2D Miss (%)", MetricCategory.DEVKIT_MEMORY,
              "DevKit_Memory", "L2D Miss (%)", "%",
              "L2 data cache miss rate"),
    MetricDef("mem_l2i_miss", "L2I Miss (%)", MetricCategory.DEVKIT_MEMORY,
              "DevKit_Memory", "L2I Miss (%)", "%",
              "L2 instruction cache miss rate"),
    MetricDef("mem_ddr_read", "DDR Read (MB/s)", MetricCategory.DEVKIT_MEMORY,
              "DevKit_Memory", "DDR Read (MB/s)", "MB/s",
              "DDR read bandwidth"),
    MetricDef("mem_ddr_write", "DDR Write (MB/s)", MetricCategory.DEVKIT_MEMORY,
              "DevKit_Memory", "DDR Write (MB/s)", "MB/s",
              "DDR write bandwidth"),

    # ========== NUMA_BANDWIDTH - NUMA Bandwidth (dynamic per-node) ==========
    MetricDef("numa_total_read", "NUMA Total Read", MetricCategory.NUMA_BANDWIDTH,
              "NUMA_Bandwidth", "Total Read", "MB/s",
              "Total DDR read across all NUMA nodes"),
    MetricDef("numa_total_write", "NUMA Total Write", MetricCategory.NUMA_BANDWIDTH,
              "NUMA_Bandwidth", "Total Write", "MB/s",
              "Total DDR write across all NUMA nodes"),
    # Per-node metrics are dynamically generated: numa{n}_read, numa{n}_write

    # ========== KSYS - KSys Metrics (11 metrics) ==========
    MetricDef("ksys_l2_latency_max", "L2 Miss Latency Max", MetricCategory.KSYS,
              "KSys", "L2 Miss Latency Max", "cycles",
              "L2 cache miss latency maximum"),
    MetricDef("ksys_l2_latency_min", "L2 Miss Latency Min", MetricCategory.KSYS,
              "KSys", "L2 Miss Latency Min", "cycles",
              "L2 cache miss latency minimum"),
    MetricDef("ksys_l2_latency_avg", "L2 Miss Latency Avg", MetricCategory.KSYS,
              "KSys", "L2 Miss Latency Avg", "cycles",
              "L2 cache miss latency average"),
    MetricDef("ksys_l3_latency_max", "L3 Miss Latency Max", MetricCategory.KSYS,
              "KSys", "L3 Miss Latency Max", "cycles",
              "L3 cache miss latency maximum"),
    MetricDef("ksys_l3_latency_min", "L3 Miss Latency Min", MetricCategory.KSYS,
              "KSys", "L3 Miss Latency Min", "cycles",
              "L3 cache miss latency minimum"),
    MetricDef("ksys_l3_latency_avg", "L3 Miss Latency Avg", MetricCategory.KSYS,
              "KSys", "L3 Miss Latency Avg", "cycles",
              "L3 cache miss latency average"),
    MetricDef("ksys_ipc", "IPC", MetricCategory.KSYS,
              "KSys", "IPC", "-",
              "IPC from KSys"),
    MetricDef("ksys_retiring", "Retiring (%)", MetricCategory.KSYS,
              "KSys", "Retiring (%)", "%",
              "Retiring percentage from KSys"),
    MetricDef("ksys_frontend_bound", "Frontend Bound (%)", MetricCategory.KSYS,
              "KSys", "Frontend Bound (%)", "%",
              "Frontend bound percentage from KSys"),
    MetricDef("ksys_bad_speculation", "Bad Speculation (%)", MetricCategory.KSYS,
              "KSys", "Bad Speculation (%)", "%",
              "Bad speculation percentage from KSys"),
    MetricDef("ksys_backend_bound", "Backend Bound (%)", MetricCategory.KSYS,
              "KSys", "Backend Bound (%)", "%",
              "Backend bound percentage from KSys"),

    # ========== UBWATCH - UBWatch Latency Metrics (7 metrics) ==========
    MetricDef("ub_samples", "Samples", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Samples", "-",
              "Number of latency samples"),
    MetricDef("ub_avg_read_ns", "Avg Read (ns)", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Avg Read (ns)", "ns",
              "Average read latency in nanoseconds"),
    MetricDef("ub_avg_write_ns", "Avg Write (ns)", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Avg Write (ns)", "ns",
              "Average write latency in nanoseconds"),
    MetricDef("ub_min_read_ns", "Min Read (ns)", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Min Read (ns)", "ns",
              "Minimum read latency in nanoseconds"),
    MetricDef("ub_min_write_ns", "Min Write (ns)", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Min Write (ns)", "ns",
              "Minimum write latency in nanoseconds"),
    MetricDef("ub_max_read_ns", "Max Read (ns)", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Max Read (ns)", "ns",
              "Maximum read latency in nanoseconds"),
    MetricDef("ub_max_write_ns", "Max Write (ns)", MetricCategory.UBWATCH,
              "UBWatch_Latency", "Max Write (ns)", "ns",
              "Maximum write latency in nanoseconds"),
]


# ============================================================================
# HELPER FUNCTIONS AND MAPPING TABLES
# ============================================================================

def get_metrics_by_category(category: MetricCategory) -> List[MetricDef]:
    """Get all metrics for a specific category"""
    return [m for m in METRICS_DEFINITIONS if m.category == category]


def get_metrics_by_sheet(sheet_name: str) -> List[MetricDef]:
    """Get all metrics from a specific Excel sheet"""
    return [m for m in METRICS_DEFINITIONS if m.source_sheet == sheet_name]


def get_metric_key(metric_def: MetricDef) -> str:
    """Get the internal key for a metric"""
    return metric_def.key


def build_excel_metric_key_map() -> Dict[str, Dict[str, str]]:
    """Build mapping table: sheet_name -> {source_metric_name: internal_key}

    Used by batch_test_scheduler.py to extract metrics from Excel.
    """
    mapping = {}
    for m in METRICS_DEFINITIONS:
        if m.source_sheet not in mapping:
            mapping[m.source_sheet] = {}
        if m.source_metric != "-":  # Skip non-Excel sources
            mapping[m.source_sheet][m.source_metric] = m.key
    return mapping


def build_browser_metric_key_map() -> Dict[str, str]:
    """Build mapping for browser metrics: source_metric_name -> internal_key

    Used by batch_test_scheduler.py to extract metrics from bench_report.txt.
    """
    mapping = {}
    for m in METRICS_DEFINITIONS:
        if m.category == MetricCategory.BROWSER and m.source_metric != "-":
            mapping[m.source_metric] = m.key
    return mapping


def build_display_name_map() -> Dict[str, str]:
    """Build mapping: internal_key -> display_name

    Used by generate_summary_report to create Excel column headers.
    """
    return {m.key: m.display_name for m in METRICS_DEFINITIONS}


# Pre-built mapping tables (cached for performance)
EXCEL_METRIC_KEY_MAP = build_excel_metric_key_map()
BROWSER_METRIC_KEY_MAP = build_browser_metric_key_map()
DISPLAY_NAME_MAP = build_display_name_map()


# ============================================================================
# SUMMARY REPORT COLUMN ORDER
# ============================================================================

def get_summary_report_columns() -> List[str]:
    """Get ordered list of metric keys for summary report Excel columns"""
    # Order: Basic -> Browser -> VM_CPU -> TopDown -> Memory -> NUMA -> KSys -> UBWatch
    columns = []
    for category in [
        MetricCategory.BASIC,
        MetricCategory.BROWSER,
        MetricCategory.VM_CPU,
        MetricCategory.DEVKIT_TOPDOWN,
        MetricCategory.DEVKIT_MEMORY,
        MetricCategory.NUMA_BANDWIDTH,
        MetricCategory.KSYS,
        MetricCategory.UBWATCH,
    ]:
        for m in get_metrics_by_category(category):
            columns.append(m.key)
    return columns


SUMMARY_REPORT_COLUMNS = get_summary_report_columns()


# ============================================================================
# DOCUMENTATION
# ============================================================================

def print_metrics_documentation():
    """Print formatted metrics documentation for README"""
    print("=" * 80)
    print("Metrics Definitions Documentation")
    print("=" * 80)

    for category in MetricCategory:
        metrics = get_metrics_by_category(category)
        if metrics:
            print(f"\n[{category.value.upper()}]")
            print("-" * 40)
            for m in metrics:
                print(f"  {m.key}")
                print(f"    Display: {m.display_name}")
                print(f"    Source:  {m.source_sheet} -> {m.source_metric}")
                print(f"    Unit:    {m.unit}")
                print(f"    Desc:    {m.description}")

    print("\n" + "=" * 80)
    print(f"Total metrics: {len(METRICS_DEFINITIONS)}")
    print("=" * 80)


if __name__ == "__main__":
    print_metrics_documentation()