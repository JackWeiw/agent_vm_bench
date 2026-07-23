"""Unit tests for per-NUMA meminfo parsing and VM total memory in vm_monitor/base.py"""

import os
import tempfile
import unittest
from collections import defaultdict
from unittest.mock import patch

from vm_monitor.base import VMMonitorBase


class DummyMonitor(VMMonitorBase):
    """Concrete subclass for testing (VMMonitorBase is abstract)"""

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


def create_mock_node_meminfo(node_id, fields):
    """Create a mock /sys/devices/system/node/nodeN/meminfo file

    Args:
        node_id: NUMA node number
        fields: dict of {field_name: value_in_kB}

    Returns:
        Path to temp file with mock content
    """
    lines = []
    for field, value_kb in fields.items():
        lines.append(f"Node {node_id} {field}:     {value_kb} kB\n")
    content = "".join(lines)
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".meminfo", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestGetNumaNodesMemory(unittest.TestCase):
    """Tests for get_numa_nodes_memory() with full per-NUMA meminfo"""

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
        node0_path = create_mock_node_meminfo(0, node0_fields)

        monitor = DummyMonitor()

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k)):
            nodes = monitor.get_numa_nodes_memory()

        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["node"], 0)
        self.assertAlmostEqual(node["total_mb"], 16384000 / 1024, places=1)
        self.assertAlmostEqual(node["free_mb"], 1024000 / 1024, places=1)
        self.assertAlmostEqual(node["available_mb"], 3000000 / 1024, places=1)
        self.assertAlmostEqual(node["swap_cached_mb"], 100000 / 1024, places=1)
        self.assertAlmostEqual(node["active_mb"], 5000000 / 1024, places=1)
        self.assertAlmostEqual(node["inactive_mb"], 2000000 / 1024, places=1)
        self.assertAlmostEqual(node["anon_pages_mb"], 4000000 / 1024, places=1)
        self.assertAlmostEqual(node["file_pages_mb"], 1500000 / 1024, places=1)

        os.unlink(node0_path)

    def test_backward_compat_aliases(self):
        """Old key names (total, used, free, usage) should still exist as aliases"""
        node0_fields = {
            "MemTotal": 16384000,
            "MemFree": 1024000,
        }
        node0_path = create_mock_node_meminfo(0, node0_fields)

        monitor = DummyMonitor()

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k)):
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
        node0_path = create_mock_node_meminfo(0, node0_fields)

        monitor = DummyMonitor()

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k)):
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
        """Should parse all NUMA nodes including remote ones like NUMA5"""
        node0_fields = {"MemTotal": 8000000, "MemFree": 2000000, "SwapCached": 50000}
        node5_fields = {"MemTotal": 8000000, "MemFree": 1000000, "SwapCached": 200000}
        node0_path = create_mock_node_meminfo(0, node0_fields)
        node5_path = create_mock_node_meminfo(5, node5_fields)
        paths_map = {"node0/meminfo": node0_path, "node5/meminfo": node5_path}

        monitor = DummyMonitor()

        def open_side_effect(path, *a, **k):
            for key, val in paths_map.items():
                if key in path:
                    return open(val)
            return open(path, *a, **k)

        with patch("vm_monitor.base.os.listdir", return_value=["node0", "node5"]), \
             patch("vm_monitor.base.open", side_effect=open_side_effect):
            nodes = monitor.get_numa_nodes_memory()

        self.assertEqual(len(nodes), 2)
        # Verify NUMA5 specifically (remote borrowing node)
        numa5 = [n for n in nodes if n["node"] == 5][0]
        self.assertAlmostEqual(numa5["swap_cached_mb"], 200000 / 1024, places=2)
        self.assertAlmostEqual(numa5["free_mb"], 1000000 / 1024, places=2)

        os.unlink(node0_path)
        os.unlink(node5_path)


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
        node0_path = create_mock_node_meminfo(0, node0_fields)

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
        node0_path = create_mock_node_meminfo(0, node0_fields)

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), \
             patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k)):
            # Should not raise any exception
            monitor.print_numa_real_time()

        os.unlink(node0_path)


if __name__ == "__main__":
    unittest.main()
