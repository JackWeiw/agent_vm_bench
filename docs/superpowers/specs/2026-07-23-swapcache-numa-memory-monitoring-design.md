# SwapCache / NUMA Memory / VM Total Memory Monitoring Design

**Date:** 2026-07-23
**Status:** Draft — awaiting review
**Branch:** round_refactor

## Problem Statement

vm_monitor currently collects basic NUMA memory stats (only `MemTotal`/`MemFree`) and aggregate swap info, but lacks:

1. **SwapCache per NUMA node** — critical for observing remote NUMA memory borrowing patterns
2. **Full per-NUMA meminfo** — each NUMA's `Available`, `SwapCached`, `Active`, `Inactive`, `AnonPages`, `FilePages`
3. **VM total memory over time** — a single timeline showing the aggregate memory consumption of all VMs, plus per-NUMA breakdown

All NUMA nodes (including NUMA5 which is a remote borrowing node) are treated uniformly — data comes from `/sys/devices/system/node/node{N}/meminfo`.

## Design Approach

**Approach A (chosen): Independent Timeline Sheets** — each new metric type gets its own Excel Timeline sheet, following the existing pattern (history list → Timeline sheet → chart). This keeps data cleanly separated, easy to read, and ready for future metrics_extractor integration.

## Data Collection Layer (base.py)

### 1. Extend `get_numa_nodes_memory()` — Full per-NUMA meminfo

**Source:** `/sys/devices/system/node/node{N}/meminfo`

The format is:
```
Node 0 MemTotal:     12345678 kB
Node 0 MemFree:       2345678 kB
Node 0 MemAvailable:  3456789 kB
Node 0 SwapCached:     100000 kB
Node 0 Active:        4567890 kB
Node 0 Inactive:      1234567 kB
Node 0 AnonPages:     5678901 kB
Node 0 FilePages:     234567 kB
```

Note: values in this file use the prefix `Node N` and units are kB (same as `/proc/meminfo`). The current parsing uses simple `line.split()` to find `MemTotal`/`MemFree` keywords — this needs to be replaced with a regex pattern `Node\s+(\d+)\s+(\w+):\s+(\d+)\s+kB` that captures node_id, field_name, and value, then converts all values from kB to MB (divide by 1024).

**New data structure per NUMA node:**
```python
{
    "node": 0,
    "total_mb": 12345.67,      # MemTotal / 1024
    "used_mb": 6789.00,        # total - free
    "free_mb": 5556.67,        # MemFree / 1024
    "available_mb": 4000.00,   # MemAvailable / 1024
    "swap_cached_mb": 100.00,  # SwapCached / 1024
    "active_mb": 3000.00,      # Active / 1024
    "inactive_mb": 2000.00,    # Inactive / 1024
    "anon_pages_mb": 5000.00,  # AnonPages / 1024
    "file_pages_mb": 1500.00,  # FilePages / 1024
    "usage_pct": 55.0,         # used / total * 100
}
```

**Backward compatibility:** The existing `total/used/free/usage` key names (no `_mb/_pct` suffix) are preserved as aliases in the dict. The exporters will use the new `_mb/_pct` fields, but the existing `print_numa_real_time()` and `print_final_numa_stats()` continue to work via the alias keys.

### 2. New `collect_vm_total_memory()` — Aggregate VM memory per sample

Called within `collect_sample()` after `get_vms_realtime()` returns the VM list.

**New attributes:**
```python
self.vm_total_memory_history = []  # List of {ts, total_mb, vm_count, per_numa: {N: mb}}
```

**Method logic:**
```python
def collect_vm_total_memory(self, vms: List[Dict]) -> Dict:
    total_mb = sum(vm["memory_mb"] for vm in vms)
    per_numa = defaultdict(float)
    for vm in vms:
        for node_id, mem in vm.get("memory_per_numa", {}).items():
            per_numa[node_id] += mem.get("total_mb", 0)

    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_mb": round(total_mb, 2),
        "vm_count": len(vms),
        "per_numa": {k: round(v, 2) for k, v in per_numa.items()},
    }
    self.vm_total_memory_history.append(entry)
    return entry
```

### 3. Integrate into `collect_sample()`

```python
def collect_sample(self):
    self.collect_hugepage_stats()
    self.collect_numa_cpu()
    self.collect_host_stats()
    self.collect_swap_stats()
    vms = self.get_vms_realtime()
    self.last_vm_count = len(vms)

    # NEW: aggregate VM total memory
    self.collect_vm_total_memory(vms)

    # ... rest unchanged
```

### 4. Update `__init__()`

Add new data containers:
```python
self.vm_total_memory_history = []
```

## Display Layer (base.py)

### Update `display_realtime_table()`

After the existing Swap line, add a SwapCache per NUMA line:
```
SwapCache: NUMA0 120 MB | NUMA1 95 MB | NUMA5 200 MB | Total 415 MB
```

After the existing VM table, add VM total memory line:
```
VM Total Memory: 8192 MB (8 VMs) | NUMA0: 4096 MB | NUMA5: 2048 MB
```

### Update `print_summary_report()` and `export_summary_csv()`

Add SwapCache per NUMA avg/peak stats and VM total memory avg/peak stats.

## Excel Export Layer (exporters.py)

### New Sheet 1: SwapCache_Per_NUMA_Timeline

| Column | Source |
|--------|--------|
| Timestamp | from `numa_memory_history[i]["ts"]` |
| SwapCache Total (MB) | from `swap_history[i]["cache"]["cached_mb"]` |
| NUMA0 SwapCache (MB) | from `numa_memory_history[i]["nodes"][0]["swap_cached_mb"]` |
| NUMA1 SwapCache (MB) | ... |
| NUMA5 SwapCache (MB) | ... |
| (all NUMA nodes) | ... |

**Chart:** Line chart showing each NUMA's SwapCached over time, plus total SwapCached. This allows direct comparison of local vs remote NUMA swapcache behavior.

### New Sheet 2: NUMA_Memory_Timeline

| Column | Source |
|--------|--------|
| Timestamp | from `numa_memory_history[i]["ts"]` |
| NUMA{N} Total (MB) | from `numa_memory_history[i]["nodes"][N]["total_mb"]` |
| NUMA{N} Used (MB) | from `numa_memory_history[i]["nodes"][N]["used_mb"]` |
| NUMA{N} Free (MB) | from `numa_memory_history[i]["nodes"][N]["free_mb"]` |
| NUMA{N} Available (MB) | from `numa_memory_history[i]["nodes"][N]["available_mb"]` |
| NUMA{N} SwapCache (MB) | from `numa_memory_history[i]["nodes"][N]["swap_cached_mb"]` |
| NUMA{N} AnonPages (MB) | from `numa_memory_history[i]["nodes"][N]["anon_pages_mb"]` |
| NUMA{N} Usage (%) | from `numa_memory_history[i]["nodes"][N]["usage_pct"]` |

Repeated for each NUMA node present in the system.

**Chart:** Multi-series line chart showing Free and Used for all NUMA nodes over time, making remote NUMA (like NUMA5) trends visible against local NUMA patterns.

### New Sheet 3: VM_Total_Memory_Timeline

| Column | Source |
|--------|--------|
| Timestamp | from `vm_total_memory_history[i]["ts"]` |
| VM Total Memory (MB) | from `vm_total_memory_history[i]["total_mb"]` |
| VM Count | from `vm_total_memory_history[i]["vm_count"]` |
| NUMA{N} VM Memory (MB) | from `vm_total_memory_history[i]["per_numa"][N]` |

**Chart:** Line chart showing VM total memory over time, plus per-NUMA breakdown.

### Existing Sheets — No Changes

The existing NUMA_Memory sheet (avg/peak summary) and Swap_Timeline sheet remain unchanged. The new Timeline sheets are additive, complementary data.

## Metrics Extraction (metrics_extractor.py / batch_test_scheduler.py) — Future Work

The new Timeline sheets are not yet integrated into `MetricsExtractor` or `batch_test_scheduler.py`'s extraction logic. This is a separate task to be done after this feature is implemented and validated. The design intentionally keeps the data collection and export independent from extraction, matching how swap metrics were already collected but not extracted.

## Files to Modify

| File | Changes |
|------|---------|
| `vm_monitor/base.py` | Extend `get_numa_nodes_memory()`, add `collect_vm_total_memory()`, update `collect_sample()`, update `__init__()`, update display methods, update `export_summary_csv()`, update `print_summary_report()` |
| `vm_monitor/exporters.py` | Add 3 new Timeline sheets + 2-3 new line charts |
| `vm_monitor/tests/test_swap_stats.py` | Add tests for per-NUMA SwapCached extraction |

## Data Sources Summary

| Metric | Source File | Parsing Approach |
|--------|------------|-----------------|
| SwapCache total | `/proc/meminfo` SwapCached | Existing `_read_meminfo()` |
| SwapCache per NUMA | `/sys/devices/system/node/node{N}/meminfo` | Extended `get_numa_nodes_memory()` |
| NUMA free/available/active/inactive | `/sys/devices/system/node/node{N}/meminfo` | Extended `get_numa_nodes_memory()` |
| VM total memory | `numastat -p <vmm_process>` via `get_vms_realtime()` | New `collect_vm_total_memory()` |
