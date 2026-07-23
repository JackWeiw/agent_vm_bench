"""Unit tests for per-NUMA meminfo parsing and VM total memory in vm_monitor/base.py"""

import os
import tempfile
import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

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


class TestGetFocusNumaNodes(unittest.TestCase):
    """Tests for get_focus_numa_nodes() combining target + remote NUMA"""

    def test_focus_includes_target_and_remote(self):
        """Focus should include target_numa_nodes + NUMA5 if both exist"""
        monitor = DummyMonitor()
        monitor.target_numa_nodes = [0, 1]
        monitor.available_numa_nodes = [0, 1, 2, 5]
        focus = monitor.get_focus_numa_nodes()
        self.assertEqual(focus, [0, 1, 5])

    def test_remote_not_available_excluded(self):
        """NUMA5 should be excluded if not present on the system"""
        monitor = DummyMonitor()
        monitor.target_numa_nodes = [0, 1]
        monitor.available_numa_nodes = [0, 1, 2, 3]
        focus = monitor.get_focus_numa_nodes()
        self.assertEqual(focus, [0, 1])

    def test_target_overlaps_remote(self):
        """If target includes NUMA5, no duplicate in focus list"""
        monitor = DummyMonitor()
        monitor.target_numa_nodes = [0, 5]
        monitor.available_numa_nodes = [0, 1, 5]
        focus = monitor.get_focus_numa_nodes()
        self.assertEqual(focus, [0, 5])

    def test_empty_target_with_remote(self):
        """Empty target should still include NUMA5 if available"""
        monitor = DummyMonitor()
        monitor.target_numa_nodes = []
        monitor.available_numa_nodes = [0, 1, 5]
        focus = monitor.get_focus_numa_nodes()
        self.assertEqual(focus, [5])



class TestGetVmMemoryFromNumaMaps(unittest.TestCase):
    """Tests for get_vm_memory_from_numa_maps() fast-path parser"""

    # Real numa_maps sample from user's firecracker process
    SAMPLE_NUMA_MAPS = (
        "aaaaae5e0000 bind:2 file=/fc-versions/v1.13.1/firecracker mapped=432 mapmax=200 N1=432 kernelpagesize_kB=4\n"
        "aaaaae7c1000 bind:2 file=/fc-versions/v1.13.1/firecracker anon=8 dirty=8 active=0 N2=8 kernelpagesize_kB=4\n"
        "aaaaae7d0000 bind:2 file=/fc-versions/v1.13.1/firecracker anon=1 dirty=1 active=0 N2=1 kernelpagesize_kB=4\n"
        "aaaab9e69000 bind:2 heap anon=14 dirty=14 active=12 N2=14 kernelpagesize_kB=4\n"
        "fffe78000000 bind:2\n"
        "fffe78021000 bind:2\n"
        "fffe7c000000 bind:2\n"
        "fffe7c021000 bind:2\n"
        "fffe80000000 bind:2 anon=473814 dirty=431729 swapcache=86418 active=203383 N2=386088 N5=87726 kernelpagesize_kB=4\n"
        "ffff80000000 bind:2\n"
        "ffff80021000 bind:2\n"
        "ffff858c6000 bind:2\n"
        "ffff858d6000 bind:2 anon=1 dirty=1 N2=1 kernelpagesize_kB=4\n"
        "ffff85ad6000 bind:2\n"
        "ffff85ae6000 bind:2 anon=3 dirty=3 N2=3 kernelpagesize_kB=4\n"
        "ffff85d10000 bind:2\n"
        "ffff85d20000 bind:2\n"
        "ffff85f20000 bind:2\n"
        "ffff85f20000 bind:2 file=/usr/lib/aarch64-linux-gnu/libc.so.6 mapped=271 mapmax=721 N0=271 kernelpagesize_kB=4\n"
        "ffff860ac000 bind:2 file=/usr/lib/aarch64-linux-gnu/libc.so.6\n"
        "ffff860bc000 bind:2 file=/usr/lib/aarch64-linux-gnu/libc.so.6 anon=1 dirty=1 active=0 N2=1 kernelpagesize_kB=4\n"
        "ffff860c0000 bind:2 file=/usr/lib/aarch64-linux-gnu/libc.so.6 anon=2 dirty=2 active=0 N2=2 kernelpagesize_kB=4\n"
        "ffff860c2000 bind:2 anon=4 dirty=4 active=0 N2=4 kernelpagesize_kB=4\n"
        "ffff860d0000 bind:2 file=/usr/lib/aarch64-linux-gnu/libgcc_s.so.1 mapped=16 mapmax=230 N0=1 N1=8 N2=7 kernelpagesize_kB=4\n"
        "ffff8611d000 bind:2 anon=1 dirty=1 N2=1 kernelpagesize_kB=4\n"
        "ffff8611e000 bind:2 file=/memfd:iov_deque\\040(deleted) dirty=1 N2=1 kernelpagesize_kB=4\n"
        "ffff86120000 bind:2 file=/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 mapped=39 mapmax=717 N0=39 kernelpagesize_kB=4\n"
        "ffff86147000 bind:2 file=/memfd:iov_deque\\040(deleted) dirty=1 mapmax=2 active=0 N2=1 kernelpagesize_kB=4\n"
        "ffff86148000 bind:2 file=/memfd:iov_deque\\040(deleted) dirty=1 mapmax=2 active=0 N2=1 kernelpagesize_kB=4\n"
        "ffff86149000 bind:2 file=anon_inode:kvm-vcpu:1 dirty=1 active=0 N2=1 kernelpagesize_kB=4\n"
        "ffff8614b000 bind:2 file=anon_inode:kvm-vcpu:0 dirty=1 active=0 N2=1 kernelpagesize_kB=4\n"
        "ffff86153000 bind:2 anon=1 dirty=1 N2=1 kernelpagesize_kB=4\n"
        "ffff8615e000 bind:2 file=/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 anon=1 dirty=1 active=0 N2=1 kernelpagesize_kB=4\n"
        "ffff86160000 bind:2 file=/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 anon=1 dirty=1 active=0 N2=1 kernelpages_kB=4\n"
        "ffffcb2e0000 bind:2 stack anon=4 dirty=4 active=1 N2=4 kernelpagesize_kB=4\n"
    )

    def _write_mock_numa_maps(self, content):
        """Write mock numa_maps content to a temp file"""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".numa_maps", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_basic_parse(self):
        """Should parse numa_maps and produce correct total_mb and per_node"""
        path = self._write_mock_numa_maps(self.SAMPLE_NUMA_MAPS)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(2677236)

        self.assertGreater(result["total_mb"], 0)
        self.assertIn(0, result["per_node"])
        self.assertIn(1, result["per_node"])
        self.assertIn(2, result["per_node"])
        self.assertIn(5, result["per_node"])
        self.assertGreater(result["per_node"][2]["total_mb"], 0)
        self.assertGreater(result["per_node"][5]["total_mb"], 0)

        os.unlink(path)

    def test_heap_detection(self):
        """Lines with 'heap' marker should accumulate into heap_mb"""
        content = "aaaab9e69000 bind:2 heap anon=14 dirty=14 active=12 N2=14 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        self.assertAlmostEqual(result["heap_mb"], 14 * 4 / 1024, places=2)
        self.assertAlmostEqual(result["per_node"][2]["heap_mb"], 14 * 4 / 1024, places=2)

        os.unlink(path)

    def test_stack_detection(self):
        """Lines with 'stack' marker should accumulate into stack_mb"""
        content = "ffffcb2e0000 bind:2 stack anon=4 dirty=4 active=1 N2=4 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        self.assertAlmostEqual(result["stack_mb"], 4 * 4 / 1024, places=2)
        self.assertAlmostEqual(result["per_node"][2]["stack_mb"], 4 * 4 / 1024, places=2)

        os.unlink(path)

    def test_proportional_private_distribution(self):
        """anon pages on multi-node lines should distribute proportionally"""
        # anon=473814, N2=386088, N5=87726 → total_line_pages = 473814
        # proportion: N2 = 386088/473814 ≈ 81.3%, N5 = 87726/473814 ≈ 18.5%
        content = "fffe80000000 bind:2 anon=473814 dirty=431729 N2=386088 N5=87726 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        total_anon_mb = 473814 * 4 / 1024
        self.assertAlmostEqual(result["private_mb"], round(total_anon_mb, 2), places=0)

        # N2 gets ~81.3% of private, N5 gets ~18.5%
        n2_proportion = 386088 / 473814
        n5_proportion = 87726 / 473814
        self.assertAlmostEqual(
            result["per_node"][2]["private_mb"],
            round(total_anon_mb * n2_proportion, 2), places=0
        )
        self.assertAlmostEqual(
            result["per_node"][5]["private_mb"],
            round(total_anon_mb * n5_proportion, 2), places=0
        )

        os.unlink(path)

    def test_swapcache_with_proportional_distribution(self):
        """swapcache on multi-node lines should distribute proportionally"""
        content = "fffe80000000 bind:2 anon=473814 swapcache=86418 N2=386088 N5=87726 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        total_swapcache_mb = 86418 * 4 / 1024
        self.assertAlmostEqual(result["swapcache_mb"], round(total_swapcache_mb, 2), places=0)

        n2_proportion = 386088 / 473814
        n5_proportion = 87726 / 473814
        self.assertAlmostEqual(
            result["swapcache_per_node"][2],
            round(total_swapcache_mb * n2_proportion, 2), places=0
        )
        self.assertAlmostEqual(
            result["swapcache_per_node"][5],
            round(total_swapcache_mb * n5_proportion, 2), places=0
        )

        os.unlink(path)

    def test_skip_empty_lines(self):
        """Lines with no N<node>= fields should be skipped"""
        content = (
            "fffe78000000 bind:2\n"
            "fffe7c000000 bind:2\n"
            "aaaaae7c1000 bind:2 anon=8 N2=8 kernelpagesize_kB=4\n"
        )
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        # Only the 3rd line has N2=8, so total should be 8 * 4 / 1024 ≈ 0.031 MB
        self.assertAlmostEqual(result["total_mb"], round(8 * 4 / 1024, 2), places=2)

        os.unlink(path)

    def test_default_pagesize(self):
        """Lines without kernelpagesize_kB should default to 4 kB"""
        content = "aaaaae7c1000 bind:2 anon=8 N2=8\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        self.assertAlmostEqual(result["total_mb"], round(8 * 4 / 1024, 2), places=2)

        os.unlink(path)

    def test_hugepage_via_kernelpagesize(self):
        """Lines with kernelpagesize_kB > 4 should be treated as hugepages"""
        content = "aaaa0000 bind:2 anon=64 N2=64 kernelpagesize_kB=64\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)):
            result = monitor.get_vm_memory_from_numa_maps(123)

        expected_mb = 64 * 64 / 1024  # 4 MB
        self.assertAlmostEqual(result["huge_mb"], round(expected_mb, 2), places=2)
        self.assertAlmostEqual(result["per_node"][2]["huge_mb"], round(expected_mb, 2), places=2)

        os.unlink(path)

    def test_missing_numa_maps_triggers_fallback(self):
        """When numa_maps doesn't exist, get_vm_memory_from_numa_maps returns empty dict"""
        monitor = DummyMonitor()

        with patch("vm_monitor.base.open", side_effect=FileNotFoundError):
            result = monitor.get_vm_memory_from_numa_maps(99999)

        self.assertEqual(result["total_mb"], 0.0)
        self.assertEqual(result["per_node"], {})

    def test_fallback_to_numastat_subprocess(self):
        """get_vm_memory_from_numastat should fall back to subprocess when numa_maps fails"""
        monitor = DummyMonitor()

        # numastat -p <pid> real output format: header line with Node columns,
        # then data rows with label prefix and numeric values on the same line.
        # Real numastat uses "Node 0" (with space) format.
        numastat_output = (
            "Node 0           Node 2           Node 5           Total\n"
            "---              ---              ---              ---\n"
            "Huge             0.00             512.00           0.00             512.00\n"
            "Heap             100.0            200.0            0.0              300.0\n"
            "Stack            1.0              2.0              0.0              3.0\n"
            "Private          100.0            200.0            0.0              300.0\n"
            "Total            1000.0           2000.0           0.0              3000.0\n"
        )

        with patch("vm_monitor.base.open", side_effect=FileNotFoundError), \
             patch("vm_monitor.base.subprocess.run", return_value=MagicMock(returncode=0, stdout=numastat_output)):
            result = monitor.get_vm_memory_from_numastat(123)

        # Should have data from numastat fallback
        self.assertGreater(result["total_mb"], 0)
        self.assertAlmostEqual(result["total_mb"], 3000.0, places=1)
        self.assertAlmostEqual(result["heap_mb"], 300.0, places=1)
        self.assertIn(0, result["per_node"])
        self.assertIn(2, result["per_node"])
        self.assertIn(5, result["per_node"])
        self.assertAlmostEqual(result["per_node"][0]["total_mb"], 1000.0, places=1)
        self.assertAlmostEqual(result["per_node"][2]["total_mb"], 2000.0, places=1)

    def test_fallback_numastat_node0_format(self):
        """numastat parser should also handle Node0 (no space) format"""
        monitor = DummyMonitor()

        # Some numactl versions use "Node0" format (no space between Node and number)
        numastat_output = (
            "Node0            Node2            Node5            Total\n"
            "---              ---              ---              ---\n"
            "Huge             0.00             512.00           0.00             512.00\n"
            "Heap             100.0            200.0            0.0              300.0\n"
            "Total            1000.0           2000.0           0.0              3000.0\n"
        )

        with patch("vm_monitor.base.open", side_effect=FileNotFoundError), \
             patch("vm_monitor.base.subprocess.run", return_value=MagicMock(returncode=0, stdout=numastat_output)):
            result = monitor.get_vm_memory_from_numastat(123)

        self.assertGreater(result["total_mb"], 0)
        self.assertIn(0, result["per_node"])
        self.assertIn(2, result["per_node"])
        self.assertIn(5, result["per_node"])


if __name__ == "__main__":
    unittest.main()
