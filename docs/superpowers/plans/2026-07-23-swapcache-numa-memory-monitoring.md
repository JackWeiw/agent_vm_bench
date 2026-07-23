# SwapCache / NUMA Memory / VM Total Memory Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend vm_monitor to collect per-NUMA SwapCache, full per-NUMA meminfo, and VM total memory over time, with 3 new Excel Timeline sheets and charts.

**Architecture:** Extend existing `get_numa_nodes_memory()` in base.py to parse full meminfo from `/sys/devices/system/node/nodeN/meminfo`, add `collect_vm_total_memory()` method for VM aggregate, and add 3 independent Timeline sheets in exporters.py.

**Tech Stack:** Python, psutil, pandas, openpyxl, unittest

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `vm_monitor/base.py` | Core data collection — extend NUMA meminfo, add VM total memory | Modify |
| `vm_monitor/exporters.py` | Excel export — 3 new Timeline sheets + charts | Modify |
| `vm_monitor/tests/test_numa_memory.py` | Tests for NUMA meminfo parsing and VM total memory | Create |
| `vm_monitor/tests/test_swap_stats.py` | Minor update — ensure backward compat | Modify (minor) |

---

### Task 1: Extend `get_numa_nodes_memory()` — Full per-NUMA meminfo parsing

**Files:**
- Modify: `vm_monitor/base.py:156-182` (the `get_numa_nodes_memory` method)
- Test: `vm_monitor/tests/test_numa_memory.py` (new file)

- [ ] **Step 1: Write the failing test for per-NUMA meminfo parsing**

Create `vm_monitor/tests/test_numa_memory.py`:

```python
"""Unit tests for per-NUMA meminfo parsing in vm_monitor/base.py"""

import os
import tempfile
import unittest
from unittest.mock import patch

from vm_monitor.base import VMMonitorBase


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


class TestGetNumaNodesMemory(unittest.TestCase):
    """Tests for get_numa_nodes_memory() with full per-NUMA meminfo"""

    def _create_mock_node_meminfo(self, node_id, fields):
        """Create a mock /sys/devices/system/node/nodeN/minfo file"""
        lines = []
        for field, value_kb in fields.items():
            lines.append(f"Node {node_id} {field}:     {value_kb} kB\n")
        content = "".join(lines)
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_extended_fields_parsed(self):
        """Should parse SwapCached, MemAvailable, Active, Inactive, AnonPages, FilePages"""
        node0_fields = {
            "MemTotal": 16384000,
            "MemFree": 1024000,
            "MemAvailable": 3000000,
            "SwapCached": 100000,
            "Active": 5000000,
            "Inactive": 2000000,
            "AnonPages": 4000000,
            "FilePages": 1500000,
        }
        node0_path = self._create_mock_node_meminfo(0, node0_fields)
        node_dirs = ["node0"]

        monitor = DummyMonitor()

        with patch("vm_monitor.base.os.listdir", return_value=node_dirs), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if f"node0/meminfo" in p else open(p, *a, **k)):
            nodes = monitor.get_numa_nodes_memory()

        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["node"], 0)
        self.assertAlmostEqual(node["total_mb"], 16384000 / 1024, places=2)
        self.assertAlmostEqual(node["free_mb"], 1024000 / 1024, places=2)
        self.assertAlmostEqual(node["available_mb"], 3000000 / 1024, places=2)
        self.assertAlmostEqual(node["swap_cached_mb"], 100000 / 1024, places=2)
        self.assertAlmostEqual(node["active_mb"], 5000000 / 1024, places=2)
        self.assertAlmostEqual(node["inactive_mb"], 2000000 / 1024, places=2)
        self.assertAlmostEqual(node["anon_pages_mb"], 4000000 / 1024, places=2)
        self.assertAlmostEqual(node["file_pages_mb"], 1500000 / 1024, places=2)

        os.unlink(node0_path)

    def test_backward_compat_aliases(self):
        """Old key names (total, used, free, usage) should still exist as aliases"""
        node0_fields = {
            "MemTotal": 16384000,
            "MemFree": 1024000,
        }
        node0_path = self._create_mock_node_meminfo(0, node0_fields)
        node_dirs = ["node0"]

        monitor = DummyMonitor()

        with patch("vm_monitor.base.os.listdir", return_value=node_dirs), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if f"node0/meminfo" in p else open(p, *a, **k)):
            nodes = monitor.get_numa_nodes_memory()

        node = nodes[0]
        # Old alias keys still present
        self.assertIn("total", node)
        self.assertIn("used", node)
        self.assertIn("free", node)
        self.assertIn("usage", node)
        # Values match new keys
        self.assertEqual(node["total"], node["total_mb"])
        self.assertEqual(node["used"], node["used_mb"])
        self.assertEqual(node["free"], node["free_mb"])
        self.assertEqual(node["usage"], node["usage_pct"])

        os.unlink(node0_path)

    def test_missing_fields_default_to_zero(self):
        """Fields not present in meminfo (e.g., MemAvailable) should default to 0"""
        node0_fields = {
            "MemTotal": 16384000,
            "MemFree": 1024000,
        }
        node0_path = self._create_mock_node_meminfo(0, node0_fields)
        node_dirs = ["node0"]

        monitor = DummyMonitor()

        with patch("vm_monitor.base.os.listdir", return_value=node_dirs), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if f"node0/meminfo" in p else open(p, *a, **k)):
            nodes = monitor.get_numa_nodes_memory()

        node = nodes[0]
        self.assertEqual(node["available_mb"], 0.0)
        self.assertEqual(node["swap_cached_mb"], 0.0)
        self.assertEqual(node["active_mb"], 0.0)
        self.assertEqual(node["inactive_mb"], 0.0)
        self.assertEqual(node["anon_pages_mb"], 0.0)
        self.assertEqual(node["file_pages_mb"], 0.0)

        os.unlink(node0_path)

    def test_multiple_numa_nodes(self):
        """Should parse all NUMA nodes from /sys/devices/system/node/"""
        node0_fields = {"MemTotal": 8000000, "MemFree": 2000000, "SwapCached": 50000}
        node5_fields = {"MemTotal": 8000000, "MemFree": 1000000, "SwapCached": 200000}
        node0_path = self._create_mock_node_meminfo(0, node0_fields)
        node5_path = self._create_mock_node_meminfo(5, node5_fields)
        node_dirs = ["node0", "node5"]
        paths_map = {"node0/meminfo": node0_path, "node5/meminfo": node5_path}

        monitor = DummyMonitor()

        def open_side_effect(path, *a, **k):
            for key, val in paths_map.items():
                if key in path:
                    return open(val)
            return open(path, *a, **k)

        with patch("vm_monitor.base.os.listdir", return_value=node_dirs), \
             patch("vm_monitor.base.open", side_effect=open_side_effect):
            nodes = monitor.get_numa_nodes_memory()

        self.assertEqual(len(nodes), 2)
        # Find NUMA5 specifically
        numa5 = [n for n in nodes if n["node"] == 5][0]
        self.assertAlmostEqual(numa5["swap_cached_mb"], 200000 / 1024, places=2)
        self.assertAlmostEqual(numa5["free_mb"], 1000000 / 1024, places=2)

        os.unlink(node0_path)
        os.unlink(node5_path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest vm_monitor/tests/test_numa_memory.py -v`
Expected: FAIL — `get_numa_nodes_memory()` only returns `total/used/free/usage` keys, no `swap_cached_mb`, `available_mb` etc.

- [ ] **Step 3: Rewrite `get_numa_nodes_memory()` with regex-based full meminfo parsing**

Replace the method at `vm_monitor/base.py:156-182` with:

```python
# ==================== NUMA Memory Statistics ====================
# Fields to extract from per-NUMA meminfo (field name in sysfs -> dict key)
_NUMA_MEMINFO_FIELDS = {
    "MemTotal": "total_mb",
    "MemFree": "free_mb",
    "MemAvailable": "available_mb",
    "SwapCached": "swap_cached_mb",
    "Active": "active_mb",
    "Inactive": "inactive_mb",
    "AnonPages": "anon_pages_mb",
    "FilePages": "file_pages_mb",
}

def get_numa_nodes_memory(self):
    """Collect full per-NUMA meminfo from /sys/devices/system/node/node{N}/meminfo

    Parses MemTotal, MemFree, MemAvailable, SwapCached, Active, Inactive,
    AnonPages, FilePages for each NUMA node. Computes used and usage_pct
    from total - free. Preserves backward-compat alias keys (total, used,
    free, usage).
    """
    numa_nodes = []
    try:
        node_dirs = [d for d in os.listdir("/sys/devices/system/node/")
                     if d.startswith("node") and d[4:].isdigit()]
        for node in sorted(node_dirs, key=lambda x: int(x[4:])):
            node_id = int(node[4:])
            path = f"/sys/devices/system/node/{node}/meminfo"
            with open(path) as f:
                lines = f.read().splitlines()

            # Parse all relevant fields using regex
            # Format: "Node N FieldName:     value kB"
            parsed_mb = {}
            for line in lines:
                match = re.match(r"Node\s+\d+\s+(\w+):\s+(\d+)\s+kB", line)
                if match:
                    field_name = match.group(1)
                    value_kb = int(match.group(2))
                    value_mb = round(value_kb / 1024, 2)
                    parsed_mb[field_name] = value_mb

            # Build result dict with canonical keys
            result = {"node": node_id}
            for sysfs_name, dict_key in self._NUMA_MEMINFO_FIELDS.items():
                result[dict_key] = parsed_mb.get(sysfs_name, 0.0)

            # Compute derived fields
            total_mb = result.get("total_mb", 0.0)
            free_mb = result.get("free_mb", 0.0)
            used_mb = round(total_mb - free_mb, 2)
            usage_pct = round(used_mb / total_mb * 100, 2) if total_mb > 0 else 0.0
            result["used_mb"] = used_mb
            result["usage_pct"] = usage_pct

            # Backward-compat aliases (no suffix)
            result["total"] = total_mb
            result["used"] = used_mb
            result["free"] = free_mb
            result["usage"] = usage_pct

            numa_nodes.append(result)
    except Exception:
        pass

    self.numa_memory_history.append(
        {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "nodes": numa_nodes}
    )
    return numa_nodes
```

Also add the class attribute `_NUMA_MEMINFO_FIELDS` at the class level (before `__init__`) or as a constant at the module level. Since the design says to use it in the method, put it as a class-level dict:

At the top of `VMMonitorBase` class body (before `__init__`), add:

```python
# Fields to extract from per-NUMA meminfo (sysfs name -> dict key)
_NUMA_MEMINFO_FIELDS = {
    "MemTotal": "total_mb",
    "MemFree": "free_mb",
    "MemAvailable": "available_mb",
    "SwapCached": "swap_cached_mb",
    "Active": "active_mb",
    "Inactive": "inactive_mb",
    "AnonPages": "anon_pages_mb",
    "FilePages": "file_pages_mb",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest vm_monitor/tests/test_numa_memory.py -v`
Expected: ALL PASS

- [ ] **Step 5: Verify existing tests still pass**

Run: `python -m pytest vm_monitor/tests/ -v`
Expected: ALL PASS (backward compat aliases ensure existing tests work)

- [ ] **Step 6: Commit**

```bash
git add vm_monitor/base.py vm_monitor/tests/test_numa_memory.py
git commit -m "feat(vm_monitor): extend get_numa_nodes_memory with full per-NUMA meminfo"
```

---

### Task 2: Add `collect_vm_total_memory()` method and integrate into `collect_sample()`

**Files:**
- Modify: `vm_monitor/base.py:48-86` (`__init__` — add `vm_total_memory_history`)
- Modify: `vm_monitor/base.py:532-562` (`collect_sample` — call new method)
- Test: `vm_monitor/tests/test_numa_memory.py` (add test class)

- [ ] **Step 1: Write the failing test for `collect_vm_total_memory()`**

Add to `vm_monitor/tests/test_numa_memory.py`:

```python
class TestCollectVmTotalMemory(unittest.TestCase):
    """Tests for collect_vm_total_memory() aggregating VM memory"""

    def test_single_vm_total(self):
        """Should sum memory_mb from all VMs"""
        monitor = DummyMonitor()
        vms = [
            {"name": "vm0", "pid": 100, "memory_mb": 2048.0,
             "memory_per_numa": {0: {"total_mb": 1024.0}, 5: {"total_mb": 1024.0}},
             "cpu_percent": 10.0, "status": "running"},
            {"name": "vm1", "pid": 101, "memory_mb": 4096.0,
             "memory_per_numa": {0: {"total_mb": 2048.0}, 5: {"total_mb": 2048.0}},
             "cpu_percent": 20.0, "status": "running"},
        ]
        entry = monitor.collect_vm_total_memory(vms)
        self.assertAlmostEqual(entry["total_mb"], 2048.0 + 4096.0, places=2)
        self.assertEqual(entry["vm_count"], 2)
        self.assertAlmostEqual(entry["per_numa"][0], 1024.0 + 2048.0, places=2)
        self.assertAlmostEqual(entry["per_numa"][5], 1024.0 + 2048.0, places=2)
        self.assertEqual(len(monitor.vm_total_memory_history), 1)

    def test_empty_vms_list(self):
        """Should handle empty VM list with total_mb=0 and vm_count=0"""
        monitor = DummyMonitor()
        entry = monitor.collect_vm_total_memory([])
        self.assertEqual(entry["total_mb"], 0.0)
        self.assertEqual(entry["vm_count"], 0)
        self.assertEqual(entry["per_numa"], {})
        self.assertEqual(len(monitor.vm_total_memory_history), 1)

    def test_accumulates_history(self):
        """Multiple calls should accumulate history entries"""
        monitor = DummyMonitor()
        vms1 = [{"name": "vm0", "pid": 100, "memory_mb": 2048.0,
                 "memory_per_numa": {0: {"total_mb": 2048.0}}, "status": "running"}]
        vms2 = [{"name": "vm0", "pid": 100, "memory_mb": 2100.0,
                 "memory_per_numa": {0: {"total_mb": 2100.0}}, "status": "running"}]
        monitor.collect_vm_total_memory(vms1)
        monitor.collect_vm_total_memory(vms2)
        self.assertEqual(len(monitor.vm_total_memory_history), 2)
        self.assertAlmostEqual(monitor.vm_total_memory_history[0]["total_mb"], 2048.0, places=2)
        self.assertAlmostEqual(monitor.vm_total_memory_history[1]["total_mb"], 2100.0, places=2)

    def test_vm_without_per_numa(self):
        """Should handle VMs missing memory_per_numa gracefully"""
        monitor = DummyMonitor()
        vms = [
            {"name": "vm0", "pid": 100, "memory_mb": 2048.0, "status": "running"},
        ]
        entry = monitor.collect_vm_total_memory(vms)
        self.assertAlmostEqual(entry["total_mb"], 2048.0, places=2)
        self.assertEqual(entry["per_numa"], {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest vm_monitor/tests/test_numa_memory.py::TestCollectVmTotalMemory -v`
Expected: FAIL — `collect_vm_total_memory()` method does not exist

- [ ] **Step 3: Add `vm_total_memory_history` to `__init__()` and implement `collect_vm_total_memory()`**

In `vm_monitor/base.py`, add to `__init__()` after the Swap Statistics block (after line 79):

```python
        # VM Total Memory Aggregation
        self.vm_total_memory_history = []
```

Add the new method after `collect_swap_stats()` (after line 435). Place it in a new section:

```python
    # ===================== Collect VM Total Memory =====================
    def collect_vm_total_memory(self, vms: List[Dict]) -> Dict:
        """Aggregate total memory consumption of all VMs per sample

        Sums memory_mb across all VMs and per_numa breakdown.
        Stores result in vm_total_memory_history for timeline export.

        Args:
            vms: List of VM dicts from get_vms_realtime()

        Returns:
            Dict with total_mb, vm_count, per_numa breakdown, timestamp
        """
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

- [ ] **Step 4: Integrate `collect_vm_total_memory()` into `collect_sample()`**

In `vm_monitor/base.py`, modify `collect_sample()` (line 532-562). After `self.last_vm_count = len(vms)` (line 539), add:

```python
        # Aggregate VM total memory
        self.collect_vm_total_memory(vms)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest vm_monitor/tests/test_numa_memory.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run all tests**

Run: `python -m pytest vm_monitor/tests/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add vm_monitor/base.py vm_monitor/tests/test_numa_memory.py
git commit -m "feat(vm_monitor): add collect_vm_total_memory for VM aggregate timeline"
```

---

### Task 3: Update display and summary methods in base.py

**Files:**
- Modify: `vm_monitor/base.py:275-285` (`print_numa_real_time` — show swapcache per NUMA)
- Modify: `vm_monitor/base.py:564-658` (`display_realtime_table` — add SwapCache/VM total lines)
- Modify: `vm_monitor/base.py:1026-1161` (`print_summary_report` — add SwapCache per NUMA / VM total stats)
- Modify: `vm_monitor/base.py:851-1023` (`export_summary_csv` — add SwapCache per NUMA / VM total stats)

- [ ] **Step 1: Update `print_numa_real_time()` to show SwapCached per NUMA**

Replace `print_numa_real_time()` (lines 275-285):

```python
    def print_numa_real_time(self):
        nodes = self.get_numa_nodes_memory()
        if not nodes:
            return
        print("=" * 100)
        print("NUMA Node Memory Real-time Usage")
        for n in nodes:
            sc = n.get("swap_cached_mb", 0)
            avail = n.get("available_mb", 0)
            print(
                f"    NUMA Node {n['node']:>2d} | Total {n['total']:>8.2f} MB | Used {n['used']:>8.2f} MB | "
                f"Free {n['free']:>8.2f} MB | Avail {avail:>8.2f} MB | SwapCache {sc:>6.1f} MB | Usage {n['usage']:>5.1f}%"
            )
        print("=" * 100)
```

- [ ] **Step 2: Update `display_realtime_table()` — add SwapCache per NUMA and VM total lines**

In `display_realtime_table()`, after the existing Swap display block (lines 614-630), add:

```python
        # SwapCache per NUMA
        if self.numa_memory_history:
            latest_numa = self.numa_memory_history[-1]["nodes"]
            if latest_numa:
                sc_parts = []
                total_sc = 0.0
                for n in latest_numa:
                    sc = n.get("swap_cached_mb", 0)
                    total_sc += sc
                    sc_parts.append(f"NUMA{n['node']} {sc:.1f} MB")
                sc_str = " | ".join(sc_parts)
                print(f"SwapCache: Total {total_sc:.1f} MB | {sc_str}", flush=True)
```

After the existing VM table section and the "Total: N virtual machines" line (before "Press Ctrl+C"), add:

```python
        # VM total memory
        if self.vm_total_memory_history:
            vt = self.vm_total_memory_history[-1]
            numa_parts = [f"NUMA{k}: {v:.0f} MB" for k, v in sorted(vt["per_numa"].items())]
            numa_str = " | ".join(numa_parts) if numa_parts else "N/A"
            print(f"VM Total Memory: {vt['total_mb']:.0f} MB ({vt['vm_count']} VMs) | {numa_str}", flush=True)
```

- [ ] **Step 3: Update `print_summary_report()` — add SwapCache per NUMA and VM total sections**

After the Swap Activity section (after line 1148), add two new sections:

```python
        # SwapCache per NUMA summary
        if self.numa_memory_history:
            print("[SwapCache per NUMA Node]")
            sc_summary = defaultdict(list)
            for entry in self.numa_memory_history:
                for n in entry["nodes"]:
                    sc_summary[n["node"]].append(n.get("swap_cached_mb", 0))
            for node_id in sorted(sc_summary.keys()):
                vals = sc_summary[node_id]
                avg_sc = round(sum(vals) / len(vals), 2)
                peak_sc = round(max(vals), 2)
                print(f"  NUMA {node_id:>2d} | Avg SwapCache {avg_sc:>8.2f} MB | Peak {peak_sc:>8.2f} MB")

        # VM total memory summary
        if self.vm_total_memory_history:
            print("[VM Total Memory]")
            total_vals = [h["total_mb"] for h in self.vm_total_memory_history]
            avg_total = round(sum(total_vals) / len(total_vals), 2)
            peak_total = round(max(total_vals), 2)
            print(f"  Avg Total: {avg_total:.0f} MB | Peak Total: {peak_total:.0f} MB")
            # Per-NUMA breakdown
            all_nodes = set()
            for h in self.vm_total_memory_history:
                all_nodes.update(h["per_numa"].keys())
            for node_id in sorted(all_nodes):
                node_vals = [h["per_numa"].get(node_id, 0) for h in self.vm_total_memory_history]
                avg_node = round(sum(node_vals) / len(node_vals), 2)
                print(f"  NUMA {node_id:>2d} | Avg VM Memory {avg_node:>8.0f} MB")
```

- [ ] **Step 4: Update `export_summary_csv()` — add SwapCache per NUMA and VM total rows**

After the Swap Activity rows (after line 980), add:

```python
            # SwapCache per NUMA
            if self.numa_memory_history:
                sc_summary = defaultdict(list)
                for entry in self.numa_memory_history:
                    for n in entry["nodes"]:
                        sc_summary[n["node"]].append(n.get("swap_cached_mb", 0))
                w.writerow([])
                w.writerow(["=== SwapCache per NUMA Node ==="])
                for node_id in sorted(sc_summary.keys()):
                    vals = sc_summary[node_id]
                    avg_sc = round(sum(vals) / len(vals), 2)
                    peak_sc = round(max(vals), 2)
                    w.writerow([f"NUMA{node_id} SwapCache Avg MB", avg_sc])
                    w.writerow([f"NUMA{node_id} SwapCache Peak MB", peak_sc])

            # VM Total Memory
            if self.vm_total_memory_history:
                w.writerow([])
                w.writerow(["=== VM Total Memory ==="])
                total_vals = [h["total_mb"] for h in self.vm_total_memory_history]
                avg_total = round(sum(total_vals) / len(total_vals), 2)
                peak_total = round(max(total_vals), 2)
                w.writerow(["VM Total Memory Avg MB", avg_total])
                w.writerow(["VM Total Memory Peak MB", peak_total])
                all_nodes = set()
                for h in self.vm_total_memory_history:
                    all_nodes.update(h["per_numa"].keys())
                for node_id in sorted(all_nodes):
                    node_vals = [h["per_numa"].get(node_id, 0) for h in self.vm_total_memory_history]
                    avg_node = round(sum(node_vals) / len(node_vals), 2)
                    w.writerow([f"NUMA{node_id} VM Memory Avg MB", avg_node])
```

- [ ] **Step 5: Run all tests to verify nothing broke**

Run: `python -m pytest vm_monitor/tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add vm_monitor/base.py
git commit -m "feat(vm_monitor): update display and summary for SwapCache per NUMA and VM total memory"
```

---

### Task 4: Add 3 new Timeline sheets to exporters.py

**Files:**
- Modify: `vm_monitor/exporters.py:29-694` (add 3 new sheet sections + charts)

- [ ] **Step 1: Add SwapCache_Per_NUMA_Timeline sheet**

In `export_to_excel()`, after the existing `Swap_Timeline` sheet block (after line 571), add:

```python
            # ========== SwapCache_Per_NUMA_Timeline Sheet ==========
            if monitor.numa_memory_history and monitor.swap_history:
                # Determine all NUMA nodes from history
                all_numa_ids = sorted(set(
                    n["node"] for entry in monitor.numa_memory_history for n in entry["nodes"]
                ))
                sc_timeline_data = {
                    "Timestamp": [],
                    "SwapCache Total (MB)": [],
                }
                for nid in all_numa_ids:
                    sc_timeline_data[f"NUMA{nid} SwapCache (MB)"] = []

                min_len = min(len(monitor.swap_history), len(monitor.numa_memory_history))
                for i in range(min_len):
                    swap_entry = monitor.swap_history[i]
                    numa_entry = monitor.numa_memory_history[i]
                    ts = numa_entry["ts"]
                    sc_timeline_data["Timestamp"].append(ts)
                    sc_timeline_data["SwapCache Total (MB)"].append(swap_entry["cache"]["cached_mb"])
                    # Build lookup by node id
                    node_lookup = {n["node"]: n for n in numa_entry["nodes"]}
                    for nid in all_numa_ids:
                        node_data = node_lookup.get(nid, {})
                        sc_timeline_data[f"NUMA{nid} SwapCache (MB)"].append(
                            node_data.get("swap_cached_mb", 0)
                        )

                if sc_timeline_data["Timestamp"]:
                    pd.DataFrame(sc_timeline_data).to_excel(
                        writer, sheet_name="SwapCache_Per_NUMA_Timeline", index=False
                    )
```

- [ ] **Step 2: Add NUMA_Memory_Timeline sheet**

After the SwapCache_Per_NUMA_Timeline block, add:

```python
            # ========== NUMA_Memory_Timeline Sheet ==========
            if monitor.numa_memory_history:
                all_numa_ids = sorted(set(
                    n["node"] for entry in monitor.numa_memory_history for n in entry["nodes"]
                ))
                mem_timeline_data = {"Timestamp": []}
                for nid in all_numa_ids:
                    for field, label in [
                        ("total_mb", f"NUMA{nid} Total (MB)"),
                        ("used_mb", f"NUMA{nid} Used (MB)"),
                        ("free_mb", f"NUMA{nid} Free (MB)"),
                        ("available_mb", f"NUMA{nid} Available (MB)"),
                        ("swap_cached_mb", f"NUMA{nid} SwapCache (MB)"),
                        ("anon_pages_mb", f"NUMA{nid} AnonPages (MB)"),
                        ("usage_pct", f"NUMA{nid} Usage (%)"),
                    ]:
                        mem_timeline_data[label] = []

                for entry in monitor.numa_memory_history:
                    mem_timeline_data["Timestamp"].append(entry["ts"])
                    node_lookup = {n["node"]: n for n in entry["nodes"]}
                    for nid in all_numa_ids:
                        node_data = node_lookup.get(nid, {})
                        for field, label in [
                            ("total_mb", f"NUMA{nid} Total (MB)"),
                            ("used_mb", f"NUMA{nid} Used (MB)"),
                            ("free_mb", f"NUMA{nid} Free (MB)"),
                            ("available_mb", f"NUMA{nid} Available (MB)"),
                            ("swap_cached_mb", f"NUMA{nid} SwapCache (MB)"),
                            ("anon_pages_mb", f"NUMA{nid} AnonPages (MB)"),
                            ("usage_pct", f"NUMA{nid} Usage (%)"),
                        ]:
                            mem_timeline_data[label].append(node_data.get(field, 0))

                if mem_timeline_data["Timestamp"]:
                    pd.DataFrame(mem_timeline_data).to_excel(
                        writer, sheet_name="NUMA_Memory_Timeline", index=False
                    )
```

- [ ] **Step 3: Add VM_Total_Memory_Timeline sheet**

After the NUMA_Memory_Timeline block, add:

```python
            # ========== VM_Total_Memory_Timeline Sheet ==========
            if monitor.vm_total_memory_history:
                # Determine all NUMA nodes from VM per_numa history
                all_vm_numa_ids = sorted(set(
                    k for h in monitor.vm_total_memory_history for k in h.get("per_numa", {}).keys()
                ))
                vm_mem_data = {
                    "Timestamp": [],
                    "VM Total Memory (MB)": [],
                    "VM Count": [],
                }
                for nid in all_vm_numa_ids:
                    vm_mem_data[f"NUMA{nid} VM Memory (MB)"] = []

                for h in monitor.vm_total_memory_history:
                    vm_mem_data["Timestamp"].append(h["ts"])
                    vm_mem_data["VM Total Memory (MB)"].append(h["total_mb"])
                    vm_mem_data["VM Count"].append(h["vm_count"])
                    for nid in all_vm_numa_ids:
                        vm_mem_data[f"NUMA{nid} VM Memory (MB)"].append(
                            h.get("per_numa", {}).get(nid, 0)
                        )

                if vm_mem_data["Timestamp"]:
                    pd.DataFrame(vm_mem_data).to_excel(
                        writer, sheet_name="VM_Total_Memory_Timeline", index=False
                    )
```

- [ ] **Step 4: Add charts for the new sheets**

In the charts section (after the existing Chart 6: Swap In/Out Rate, around line 686), add three new charts:

```python
            # Chart 7: SwapCache per NUMA Timeline
            if "SwapCache_Per_NUMA_Timeline" in wb.sheetnames:
                ws = wb["SwapCache_Per_NUMA_Timeline"]
                if ws.max_row > 1:
                    from openpyxl.chart import LineChart, Reference
                    sc_chart = LineChart()
                    sc_chart.title = "SwapCache per NUMA Over Time"
                    sc_chart.style = 13
                    sc_chart.y_axis.title = "MB"
                    sc_chart.x_axis.title = "Time"
                    sc_chart.width = 22
                    sc_chart.height = 10

                    # Total SwapCache (col 2) + per-NUMA columns (col 3..)
                    data = Reference(ws, min_col=2, min_row=1, max_col=ws.max_column, max_row=ws.max_row)
                    cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
                    sc_chart.add_data(data, titles_from_data=True)
                    sc_chart.set_categories(cats)
                    ws.add_chart(sc_chart, "I2")

            # Chart 8: NUMA Free/Used Memory Timeline
            if "NUMA_Memory_Timeline" in wb.sheetnames:
                ws = wb["NUMA_Memory_Timeline"]
                if ws.max_row > 1:
                    # Select only Free and Used columns for chart readability
                    from openpyxl.chart import LineChart, Reference
                    numa_chart = LineChart()
                    numa_chart.title = "NUMA Free/Used Memory Over Time"
                    numa_chart.style = 13
                    numa_chart.y_axis.title = "MB"
                    numa_chart.x_axis.title = "Time"
                    numa_chart.width = 22
                    numa_chart.height = 10

                    # Determine Free and Used column indices
                    free_cols = []
                    used_cols = []
                    for col_idx in range(2, ws.max_column + 1):
                        header = ws.cell(row=1, column=col_idx).value
                        if header and "Free" in header:
                            free_cols.append(col_idx)
                        elif header and "Used" in header:
                            used_cols.append(col_idx)

                    # Add Free columns to chart
                    for col in free_cols:
                        data = Reference(ws, min_col=col, min_row=1, max_row=ws.max_row)
                        numa_chart.add_data(data, titles_from_data=True)
                    # Add Used columns to chart
                    for col in used_cols:
                        data = Reference(ws, min_col=col, min_row=1, max_row=ws.max_row)
                        numa_chart.add_data(data, titles_from_data=True)

                    cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
                    numa_chart.set_categories(cats)
                    ws.add_chart(numa_chart, "I2")

            # Chart 9: VM Total Memory Timeline
            if "VM_Total_Memory_Timeline" in wb.sheetnames:
                ws = wb["VM_Total_Memory_Timeline"]
                if ws.max_row > 1:
                    from openpyxl.chart import LineChart, Reference
                    vm_chart = LineChart()
                    vm_chart.title = "VM Total Memory Over Time"
                    vm_chart.style = 13
                    vm_chart.y_axis.title = "MB"
                    vm_chart.x_axis.title = "Time"
                    vm_chart.width = 18
                    vm_chart.height = 8

                    # Total Memory (col 2) + per-NUMA columns (col 4..)
                    data = Reference(ws, min_col=2, min_row=1, max_col=ws.max_column, max_row=ws.max_row)
                    cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
                    vm_chart.add_data(data, titles_from_data=True)
                    vm_chart.set_categories(cats)
                    ws.add_chart(vm_chart, "I2")
```

- [ ] **Step 5: Run all tests to verify nothing broke**

Run: `python -m pytest vm_monitor/tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add vm_monitor/exporters.py
git commit -m "feat(vm_monitor): add SwapCache_Per_NUMA, NUMA_Memory, VM_Total_Memory Timeline sheets"
```

---

### Task 5: Verify backward compatibility and final integration

**Files:**
- Modify: `vm_monitor/tests/test_numa_memory.py` (add backward compat integration test)
- Read: `vm_monitor/exporters.py` (verify existing NUMA_Memory sheet still works)

- [ ] **Step 1: Add backward compat integration test**

Add to `vm_monitor/tests/test_numa_memory.py`:

```python
class TestBackwardCompatIntegration(unittest.TestCase):
    """Ensure extended NUMA data does not break existing consumers"""

    def test_numa_history_entry_has_both_key_styles(self):
        """numa_memory_history entries should have both old and new key names"""
        monitor = DummyMonitor()
        node0_fields = {
            "MemTotal": 8000000,
            "MemFree": 2000000,
            "SwapCached": 50000,
            "MemAvailable": 3000000,
        }
        node0_path = self._create_mock_node_meminfo(0, node0_fields)

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k)):
            monitor.get_numa_nodes_memory()

        entry = monitor.numa_memory_history[0]
        node = entry["nodes"][0]
        # Both key styles present
        self.assertIn("total", node)
        self.assertIn("total_mb", node)
        self.assertIn("used", node)
        self.assertIn("used_mb", node)
        self.assertIn("free", node)
        self.assertIn("free_mb", node)
        self.assertIn("usage", node)
        self.assertIn("usage_pct", node)
        # Values identical
        self.assertEqual(node["total"], node["total_mb"])
        self.assertEqual(node["used"], node["used_mb"])

        os.unlink(node0_path)

    def test_print_numa_real_time_uses_compat_keys(self):
        """print_numa_real_time should work with both old and new keys"""
        monitor = DummyMonitor()
        node0_fields = {"MemTotal": 8000000, "MemFree": 2000000, "SwapCached": 50000}
        node0_path = self._create_mock_node_meminfo(0, node0_fields)

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k)):
            # Should not raise any exception
            monitor.print_numa_real_time()

        os.unlink(node0_path)
```

Note: `_create_mock_node_meminfo` is already defined in `TestGetNumaNodesMemory` class above. Since these tests are in the same file, we need the helper available. Move it to a module-level function or keep it in the existing class and reference from the new class. The simplest approach: define a module-level helper function `_create_mock_node_meminfo(node_id, fields)` at the top of the test file (after imports), then use it in both test classes.

- [ ] **Step 2: Run all tests**

Run: `python -m pytest vm_monitor/tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add vm_monitor/tests/test_numa_memory.py
git commit -m "test(vm_monitor): add backward compat integration tests for NUMA meminfo"
```

---

## Self-Review

### Spec Coverage

| Spec Requirement | Task |
|-----------------|------|
| SwapCache per NUMA collection | Task 1 (`get_numa_nodes_memory` extension) |
| Full per-NUMA meminfo (Available, Active, Inactive, AnonPages, FilePages) | Task 1 |
| VM total memory aggregation | Task 2 (`collect_vm_total_memory`) |
| Integrate into `collect_sample()` | Task 2 |
| Backward compat aliases | Task 1, Task 5 |
| Display updates (SwapCache per NUMA, VM total) | Task 3 |
| Summary report updates | Task 3 |
| SwapCache_Per_NUMA_Timeline sheet | Task 4 |
| NUMA_Memory_Timeline sheet | Task 4 |
| VM_Total_Memory_Timeline sheet | Task 4 |
| Charts (SwapCache per NUMA, NUMA Free/Used, VM Total) | Task 4 |
| CSV export updates | Task 3 |

All spec requirements covered. ✅

### Placeholder Scan

No TBD/TODO found. All steps have complete code. ✅

### Type Consistency

- `numa_memory_history[i]["nodes"]` entries have `"swap_cached_mb"` key → used by SwapCache_Per_NUMA_Timeline sheet and NUMA_Memory_Timeline sheet ✅
- `vm_total_memory_history[i]["total_mb"]` and `"per_numa"` → used by VM_Total_Memory_Timeline sheet ✅
- `_NUMA_MEMINFO_FIELDS` dict keys match the data structure keys in `get_numa_nodes_memory()` ✅
- `collect_vm_total_memory()` signature `(self, vms: List[Dict])` → called from `collect_sample()` with `vms` from `get_vms_realtime()` ✅
