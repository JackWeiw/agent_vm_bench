# Batch Summary Metrics Reference

This document describes all metrics collected in the batch summary Excel report (`batch_summary_*.xlsx`).

For collection tool details, see [Usage Guide - Log Collection Tools](usage-guide.md#log-collection-tools).

## Overview

The batch summary collects **50+ metrics** from multiple sources for comprehensive performance comparison across test runs.

## Metrics Sources

| Source | Collection Tool | Metric Group | Count |
|--------|-----------------|--------------|-------|
| `bench_report_*.txt` | vm_bench_lite | Browser Metrics | 3 |
| `analysis_report.xlsx` - Summary | qemu_monitor (built-in) | VM CPU | 2 |
| `analysis_report.xlsx` - DevKit_TopDown | devkit_top_down | TopDown Analysis | 13 |
| `analysis_report.xlsx` - DevKit_Memory | devkit_mem | Cache & Memory | 6 |
| `analysis_report.xlsx` - NUMA_Bandwidth | devkit_mem | NUMA Bandwidth | 2+ (dynamic) |
| `analysis_report.xlsx` - KSys | ksys | Kernel Metrics | 11 |
| `analysis_report.xlsx` - UBWatch_Latency | ub_watch | Interconnect | 7 |
| `analysis_report.xlsx` - SMAPBW_Summary | smap_bw | SMAP Migration | 5 |
| `analysis_report.xlsx` - Getfre_Summary | getfre | Core Frequency | 5+ per NUMA |

---

## Test Parameters

Basic test configuration from directory name and `config.yaml`.

| Column | Source | Description |
|--------|--------|-------------|
| `test_id` | Directory name | Test identifier (vm{n}_ratio{ratio}_active{percent}_timestamp) |
| `vm_count` | config.yaml | Total VM count |
| `ratio` | config.yaml | Memory borrow ratio (0.10 = 10%) |
| `active_percent` | config.yaml | Active VM percentage for benchmark |
| `active_vm_count` | Calculated | Actual active VMs = vm_count × active_percent |
| `success` | File check | Test completion status |

---

## Browser Metrics

> **Source**: `vm_bench_lite/bench_report_*.txt`

From browser benchmark task performance statistics.

| Column | Description |
|--------|-------------|
| `browser_success_rate` | Task success rate (%) |
| `browser_avg_latency_ms` | Average task latency (ms) |
| `browser_p99_latency_ms` | P99 latency (ms) |

---

## VM CPU Metrics

> **Source**: qemu_monitor built-in monitoring → `analysis_report.xlsx` Summary sheet

| Column | Description |
|--------|-------------|
| `vm_avg_cpu_percent` | Average VM CPU utilization (%) |
| `vm_max_cpu_percent` | Peak VM CPU utilization (%) |

---

## DevKit TopDown Metrics (13)

> **Collection Tool**: `devkit_top_down` → `analysis_report.xlsx` DevKit_TopDown sheet
>
> See [Usage Guide - devkit_top_down](usage-guide.md#devkit_top_down) for tool details.

CPU pipeline top-down analysis. Identifies CPU performance bottlenecks:

| Column | Description |
|--------|-------------|
| `td_cycles_avg` | Average CPU cycles |
| `td_instructions_avg` | Average instructions |
| `td_ipc_avg` | Average IPC (Instructions Per Cycle) |
| `td_ipc_max` | Maximum IPC |
| `td_ipc_min` | Minimum IPC |
| `td_bad_speculation_percent` | Bad Speculation (%) - branch prediction failures |
| `td_frontend_bound_percent` | Frontend Bound (%) - instruction fetch bottlenecks |
| `td_retiring_percent` | Retiring (%) - useful work completed |
| `td_backend_bound_percent` | Backend Bound (%) - execution backend stalls |
| `td_l3_bound_percent` | L3 Bound (%) - L3 cache latency |
| `td_mem_bound_percent` | Mem Bound (%) - memory access bottlenecks |
| `td_latency_bound_percent` | Latency Bound (%) - memory latency bound |
| `td_bandwidth_bound_percent` | Bandwidth Bound (%) - memory bandwidth bound |

### TopDown Interpretation

- **High Frontend Bound**: Instruction fetch/decode bottleneck
- **High Bad Speculation**: Branch prediction issues
- **High Backend Bound**: Execution units or memory stalls
- **High Retiring**: Good - CPU doing useful work
- **Mem Bound > L3 Bound**: Memory subsystem is the bottleneck

---

## DevKit Memory Metrics (6)

> **Collection Tool**: `devkit_mem` → `analysis_report.xlsx` DevKit_Memory sheet
>
> See [Usage Guide - devkit_mem](usage-guide.md#devkit_mem) for tool details.

Cache miss rates and DDR bandwidth.

| Column | Description |
|--------|-------------|
| `mem_l1d_miss_percent` | L1 data cache miss rate (%) |
| `mem_l1i_miss_percent` | L1 instruction cache miss rate (%) |
| `mem_l2d_miss_percent` | L2 data cache miss rate (%) |
| `mem_l2i_miss_percent` | L2 instruction cache miss rate (%) |
| `mem_ddr_read_mb_s` | DDR read bandwidth (MB/s) |
| `mem_ddr_write_mb_s` | DDR write bandwidth (MB/s) |

---

## NUMA Bandwidth Metrics

> **Collection Tool**: `devkit_mem` → `analysis_report.xlsx` NUMA_Bandwidth sheet

Per-node memory bandwidth. Columns dynamically added based on system NUMA configuration.

| Column Pattern | Description |
|----------------|-------------|
| `numa_total_read_mb_s` | Total read bandwidth across all NUMA nodes |
| `numa_total_write_mb_s` | Total write bandwidth across all NUMA nodes |
| `numa{n}_read_mb_s` | Per-node read bandwidth (e.g., numa0_read_mb_s) |
| `numa{n}_write_mb_s` | Per-node write bandwidth (e.g., numa1_write_mb_s) |

---

## KSys Metrics (11)

> **Collection Tool**: `ksys` → `analysis_report.xlsx` KSys sheet
>
> See [Usage Guide - ksys](usage-guide.md#ksys) for tool details.

Kernel-level performance metrics.

| Column | Description |
|--------|-------------|
| `ksys_l2_latency_max` | L2 cache miss latency max (cycles) |
| `ksys_l2_latency_min` | L2 cache miss latency min (cycles) |
| `ksys_l2_latency_avg` | L2 cache miss latency avg (cycles) |
| `ksys_l3_latency_max` | L3 cache miss latency max (cycles) |
| `ksys_l3_latency_min` | L3 cache miss latency min (cycles) |
| `ksys_l3_latency_avg` | L3 cache miss latency avg (cycles) |
| `ksys_ipc` | IPC value from ksys |
| `ksys_retiring_percent` | Retiring (%) |
| `ksys_frontend_bound_percent` | Frontend Bound (%) |
| `ksys_bad_speculation_percent` | Bad Speculation (%) |
| `ksys_backend_bound_percent` | Backend Bound (%) |

---

## UBWatch Latency Metrics (7)

> **Collection Tool**: `ub_watch` → `analysis_report.xlsx` UBWatch_Latency sheet
>
> See [Usage Guide - ub_watch](usage-guide.md#ub_watch) for tool details.

NUMA interconnect latency measurements.

| Column | Description |
|--------|-------------|
| `ub_samples` | Number of latency samples |
| `ub_avg_read_ns` | Average read latency (ns) |
| `ub_avg_write_ns` | Average write latency (ns) |
| `ub_min_read_ns` | Minimum read latency (ns) |
| `ub_min_write_ns` | Minimum write latency (ns) |
| `ub_max_read_ns` | Maximum read latency (ns) |
| `ub_max_write_ns` | Maximum write latency (ns) |

---

## SMAPBW Metrics

> **Collection Tool**: `smap_bw` → `analysis_report.xlsx` SMAPBW_Summary, SMAPBW_Cycles sheets

SMAP (Secure Memory Access Protection) migration bandwidth measurements.

| Column | Description |
|--------|-------------|
| `smapbw_total_cycles` | Total migration cycles |
| `smapbw_total_pages` | Total migrated pages |
| `smapbw_avg_bandwidth_gb_s` | Average migration bandwidth (GB/s) |
| `smapbw_min_bandwidth_gb_s` | Minimum bandwidth (GB/s) |
| `smapbw_max_bandwidth_gb_s` | Maximum bandwidth (GB/s) |

---

## Getfre Core Frequency Metrics

> **Collection Tool**: `getfre` → `analysis_report.xlsx` Getfre_Summary, Getfre_NUMA sheets
>
> See [Usage Guide - getfre](usage-guide.md#getfre) for tool details and configuration.

Physical core frequency monitoring per NUMA node.

| Column | Description |
|--------|-------------|
| `getfre_numa{n}_avg_mhz` | Average frequency for NUMA {n} (MHz) |
| `getfre_numa{n}_min_mhz` | Minimum frequency for NUMA {n} (MHz) |
| `getfre_numa{n}_max_mhz` | Maximum frequency for NUMA {n} (MHz) |
| `getfre_numa{n}_overall_variance_mhz` | Frequency variance range (max - min) |
| `getfre_numa{n}_core_count` | Number of cores monitored |
| `getfre_numa{n}_sample_count` | Number of samples collected |

### Frequency Analysis

- **Low variance (< 100 MHz)**: Stable core frequency, good power management
- **High variance (> 100 MHz)**: Frequency fluctuation, potential thermal/power throttling
- **Per-core details**: High-variance cores (> 100 MHz) logged in Getfre_NUMA sheets

---

## Using Metrics for Analysis

### Performance Bottleneck Identification

1. **CPU Bottleneck**: Check `td_backend_bound_percent` and `td_ipc_avg`
2. **Memory Bottleneck**: Check `td_mem_bound_percent` and DDR bandwidth
3. **Cache Issues**: Check `mem_l1d_miss_percent` and `mem_l2d_miss_percent`
4. **NUMA Issues**: Compare per-node bandwidth (`numa{n}_read_mb_s`)

### Cross-Test Comparison

Use batch summary Excel to:
- Compare same VM count with different ratios
- Compare same ratio with different VM counts
- Analyze active_percent impact on latency
- Find optimal configuration for your workload

---

## Related Documentation

- [Usage Guide - Log Collection Tools](usage-guide.md#log-collection-tools): Detailed tool configuration
- [Design](design.md): System architecture and data flow