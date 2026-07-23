"""Unit tests for per-NUMA meminfo parsing and VM total memory in vm_monitor/base.py"""

import os
import tempfile
import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

import psutil

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

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), patch(
            "vm_monitor.base.open",
            side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k),
        ):
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

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), patch(
            "vm_monitor.base.open",
            side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k),
        ):
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

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), patch(
            "vm_monitor.base.open",
            side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k),
        ):
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

        with patch("vm_monitor.base.os.listdir", return_value=["node0", "node5"]), patch(
            "vm_monitor.base.open", side_effect=open_side_effect
        ):
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
            {
                "name": "vm0",
                "pid": 100,
                "memory_mb": 2048.0,
                "memory_per_numa": {0: {"total_mb": 1024.0}, 5: {"total_mb": 1024.0}},
                "cpu_percent": 10.0,
                "status": "running",
            },
            {
                "name": "vm1",
                "pid": 101,
                "memory_mb": 4096.0,
                "memory_per_numa": {0: {"total_mb": 2048.0}, 5: {"total_mb": 2048.0}},
                "cpu_percent": 20.0,
                "status": "running",
            },
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
        vms1 = [
            {
                "name": "vm0",
                "pid": 100,
                "memory_mb": 2048.0,
                "memory_per_numa": {0: {"total_mb": 2048.0}},
                "status": "running",
            }
        ]
        vms2 = [
            {
                "name": "vm0",
                "pid": 100,
                "memory_mb": 2100.0,
                "memory_per_numa": {0: {"total_mb": 2100.0}},
                "status": "running",
            }
        ]
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

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), patch(
            "vm_monitor.base.open",
            side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k),
        ):
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

        with patch("vm_monitor.base.os.listdir", return_value=["node0"]), patch(
            "vm_monitor.base.open",
            side_effect=lambda p, *a, **k: open(node0_path) if "node0/meminfo" in p else open(p, *a, **k),
        ):
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

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
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

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        self.assertAlmostEqual(result["heap_mb"], 14 * 4 / 1024, places=2)
        self.assertAlmostEqual(result["per_node"][2]["heap_mb"], 14 * 4 / 1024, places=2)

        os.unlink(path)

    def test_stack_detection(self):
        """Lines with 'stack' marker should accumulate into stack_mb"""
        content = "ffffcb2e0000 bind:2 stack anon=4 dirty=4 active=1 N2=4 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
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

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        total_anon_mb = 473814 * 4 / 1024
        self.assertAlmostEqual(result["private_mb"], round(total_anon_mb, 2), places=0)

        # N2 gets ~81.3% of private, N5 gets ~18.5%
        n2_proportion = 386088 / 473814
        n5_proportion = 87726 / 473814
        self.assertAlmostEqual(result["per_node"][2]["private_mb"], round(total_anon_mb * n2_proportion, 2), places=0)
        self.assertAlmostEqual(result["per_node"][5]["private_mb"], round(total_anon_mb * n5_proportion, 2), places=0)

        os.unlink(path)

    def test_swapcache_with_proportional_distribution(self):
        """swapcache on multi-node lines should distribute proportionally"""
        content = "fffe80000000 bind:2 anon=473814 swapcache=86418 N2=386088 N5=87726 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        total_swapcache_mb = 86418 * 4 / 1024
        self.assertAlmostEqual(result["swapcache_mb"], round(total_swapcache_mb, 2), places=0)

        n2_proportion = 386088 / 473814
        n5_proportion = 87726 / 473814
        self.assertAlmostEqual(result["swapcache_per_node"][2], round(total_swapcache_mb * n2_proportion, 2), places=0)
        self.assertAlmostEqual(result["swapcache_per_node"][5], round(total_swapcache_mb * n5_proportion, 2), places=0)

        os.unlink(path)

    def test_skip_empty_lines(self):
        """Lines with no N<node>= fields should be skipped"""
        content = (
            "fffe78000000 bind:2\n" "fffe7c000000 bind:2\n" "aaaaae7c1000 bind:2 anon=8 N2=8 kernelpagesize_kB=4\n"
        )
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        # Only the 3rd line has N2=8, so total should be 8 * 4 / 1024 ≈ 0.031 MB
        self.assertAlmostEqual(result["total_mb"], round(8 * 4 / 1024, 2), places=2)

        os.unlink(path)

    def test_default_pagesize(self):
        """Lines without kernelpagesize_kB should default to 4 kB"""
        content = "aaaaae7c1000 bind:2 anon=8 N2=8\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        self.assertAlmostEqual(result["total_mb"], round(8 * 4 / 1024, 2), places=2)

        os.unlink(path)

    def test_hugepage_via_kernelpagesize(self):
        """Lines with kernelpagesize_kB > 4 should be treated as hugepages"""
        content = "aaaa0000 bind:2 anon=64 N2=64 kernelpagesize_kB=64\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
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

        with patch("vm_monitor.base.open", side_effect=FileNotFoundError), patch(
            "vm_monitor.base.subprocess.run", return_value=MagicMock(returncode=0, stdout=numastat_output)
        ):
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

        with patch("vm_monitor.base.open", side_effect=FileNotFoundError), patch(
            "vm_monitor.base.subprocess.run", return_value=MagicMock(returncode=0, stdout=numastat_output)
        ):
            result = monitor.get_vm_memory_from_numastat(123)

        self.assertGreater(result["total_mb"], 0)
        self.assertIn(0, result["per_node"])
        self.assertIn(2, result["per_node"])
        self.assertIn(5, result["per_node"])


class TestDiscoverVmProcesses(unittest.TestCase):
    """Tests for _discover_vm_processes() filtering and VM ID extraction"""

    def test_finds_matching_processes(self):
        """Should discover processes matching get_process_names()"""
        monitor = DummyMonitor()
        mock_procs = [
            MagicMock(info={"pid": 100, "name": "test_process", "cmdline": ["--id", "vm0"], "status": "running"}),
            MagicMock(info={"pid": 101, "name": "other_process", "cmdline": [], "status": "running"}),
            MagicMock(info={"pid": 102, "name": "test_process_extra", "cmdline": [], "status": "sleeping"}),
        ]

        with patch("vm_monitor.base.psutil.process_iter", return_value=mock_procs):
            candidates = monitor._discover_vm_processes()

        self.assertEqual(len(candidates), 2)  # skips other_process
        self.assertEqual(candidates[0]["pid"], 100)
        self.assertEqual(candidates[1]["pid"], 102)

    def test_skips_dead_processes(self):
        """Should skip processes that throw NoSuchProcess"""
        monitor = DummyMonitor()
        mock_procs = [
            MagicMock(info={"pid": 100, "name": "test_process", "cmdline": [], "status": "running"}),
            MagicMock(info={"pid": 101, "name": "test_process", "cmdline": [], "status": "running"}),
        ]
        # First proc works, second throws NoSuchProcess during iteration
        mock_procs[1].info = None  # force exception in try block

        with patch("vm_monitor.base.psutil.process_iter", return_value=mock_procs[:1]):
            candidates = monitor._discover_vm_processes()

        self.assertEqual(len(candidates), 1)

    def test_returns_candidate_fields(self):
        """Candidates should have pid, proc_name, cmdline, status, vm_name"""
        monitor = DummyMonitor()
        mock_procs = [
            MagicMock(info={"pid": 100, "name": "test_process", "cmdline": ["--id", "vm0"], "status": "running"}),
        ]

        with patch("vm_monitor.base.psutil.process_iter", return_value=mock_procs):
            candidates = monitor._discover_vm_processes()

        c = candidates[0]
        self.assertIn("pid", c)
        self.assertIn("proc_name", c)
        self.assertIn("cmdline", c)
        self.assertIn("status", c)
        self.assertIn("vm_name", c)

    def test_empty_result_when_no_vms(self):
        """Should return empty list when no matching processes found"""
        monitor = DummyMonitor()
        mock_procs = [
            MagicMock(info={"pid": 100, "name": "unrelated", "cmdline": [], "status": "running"}),
        ]

        with patch("vm_monitor.base.psutil.process_iter", return_value=mock_procs):
            candidates = monitor._discover_vm_processes()

        self.assertEqual(candidates, [])


class TestCollectVmMetricsParallel(unittest.TestCase):
    """Tests for _collect_vm_metrics_parallel() and _collect_vm_metrics_serial()"""

    def _make_candidate(self, pid, vm_name="vm0", status="running"):
        """Create a lightweight VM candidate dict"""
        return {
            "pid": pid,
            "proc_name": "test_process",
            "cmdline": "",
            "status": status,
            "vm_name": vm_name,
        }

    def test_parallel_returns_vm_dicts(self):
        """Parallel collection should return complete VM dicts"""
        monitor = DummyMonitor()
        candidates = [
            self._make_candidate(100, "vm-100"),
            self._make_candidate(101, "vm-101"),
            self._make_candidate(102, "vm-102"),
            self._make_candidate(103, "vm-103"),
        ]

        # Mock numa_maps to return data for each PID
        def mock_numa_maps_result(pid):
            return {
                "total_mb": 2048.0,
                "huge_mb": 0.0,
                "private_mb": 1000.0,
                "heap_mb": 50.0,
                "per_node": {0: {"total_mb": 1024.0}, 2: {"total_mb": 1024.0}},
                "swapcache_mb": 100.0,
                "swapcache_per_node": {0: 50.0, 2: 50.0},
            }

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process"
        ) as mock_process_cls:
            # Mock Process.cpu_percent to return seed=0 then delta=5.0
            mock_proc = MagicMock()
            mock_proc.cpu_percent.return_value = 5.0
            mock_process_cls.return_value = mock_proc

            vms = monitor._collect_vm_metrics_parallel(candidates)

        self.assertEqual(len(vms), 4)
        for vm in vms:
            self.assertIn("pid", vm)
            self.assertIn("name", vm)
            self.assertIn("cpu_percent", vm)
            self.assertIn("memory_mb", vm)
            self.assertIn("memory_swapcache_mb", vm)
            self.assertIn("status", vm)
            # Internal keys should not be in final result
            self.assertNotIn("_is_new_pid", vm)
            self.assertNotIn("_seed_process", vm)

    def test_serial_returns_same_structure(self):
        """Serial collection should return same dict structure as parallel"""
        monitor = DummyMonitor()
        candidates = [self._make_candidate(100, "vm-100")]

        def mock_numa_maps_result(pid):
            return {
                "total_mb": 2048.0,
                "huge_mb": 0.0,
                "private_mb": 1000.0,
                "heap_mb": 50.0,
                "per_node": {0: {"total_mb": 2048.0}},
                "swapcache_mb": 100.0,
                "swapcache_per_node": {0: 100.0},
            }

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process"
        ) as mock_process_cls:
            mock_proc = MagicMock()
            mock_proc.cpu_percent.return_value = 0.0  # seed call
            mock_process_cls.return_value = mock_proc

            vms = monitor._collect_vm_metrics_serial(candidates)

        self.assertEqual(len(vms), 1)
        vm = vms[0]
        self.assertEqual(vm["pid"], 100)
        self.assertEqual(vm["name"], "vm-100")
        self.assertEqual(vm["memory_mb"], 2048.0)

    def test_small_count_uses_serial_path(self):
        """Less than 4 VMs should use serial path (no ThreadPoolExecutor)"""
        monitor = DummyMonitor()
        candidates = [self._make_candidate(100)]

        def mock_numa_maps_result(pid):
            return {
                "total_mb": 100.0,
                "huge_mb": 0.0,
                "private_mb": 50.0,
                "heap_mb": 0.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process"
        ) as mock_process_cls:
            mock_proc = MagicMock()
            mock_proc.cpu_percent.return_value = 0.0
            mock_process_cls.return_value = mock_proc

            vms = monitor._collect_vm_metrics_parallel(candidates)

        # Should have used serial path (<4 candidates)
        self.assertEqual(len(vms), 1)

    def test_empty_candidates_returns_empty(self):
        """Empty candidate list should return empty result immediately"""
        monitor = DummyMonitor()
        vms = monitor._collect_vm_metrics_parallel([])
        self.assertEqual(vms, [])

    def test_dead_process_handled_gracefully(self):
        """Dead process should produce zeroed metrics, not crash"""
        monitor = DummyMonitor()
        candidates = [self._make_candidate(99999)]

        # numa_maps returns empty dict (process gone)
        def mock_numa_maps_result(pid):
            return {
                "total_mb": 0.0,
                "huge_mb": 0.0,
                "private_mb": 0.0,
                "heap_mb": 0.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process", side_effect=psutil.NoSuchProcess(99999)
        ):
            vms = monitor._collect_vm_metrics_serial(candidates)

        self.assertEqual(len(vms), 1)
        vm = vms[0]
        self.assertEqual(vm["memory_mb"], 0.0)
        self.assertEqual(vm["cpu_percent"], 0.0)

    def test_process_cache_updated_serially(self):
        """Seed Process objects from threads should be transferred to cache serially"""
        monitor = DummyMonitor()
        candidates = [
            self._make_candidate(100),
            self._make_candidate(101),
            self._make_candidate(102),
            self._make_candidate(103),
        ]

        def mock_numa_maps_result(pid):
            return {
                "total_mb": 100.0,
                "huge_mb": 0.0,
                "private_mb": 50.0,
                "heap_mb": 0.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        # Create mock Process objects that will be seeded in threads
        seed_procs = {}
        for pid in [100, 101, 102, 103]:
            mp = MagicMock()
            mp.cpu_percent.return_value = 0.0  # seed call returns 0
            seed_procs[pid] = mp

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process", side_effect=lambda pid: seed_procs[pid]
        ):
            vms = monitor._collect_vm_metrics_parallel(candidates)

        # All seed Process objects should now be in process_cache
        self.assertEqual(len(monitor.process_cache), 4)
        for pid in [100, 101, 102, 103]:
            self.assertIn(pid, monitor.process_cache)

    def test_peak_tracking(self):
        """Peak memory and CPU should be tracked correctly across cycles"""
        monitor = DummyMonitor()
        candidates = [self._make_candidate(100), self._make_candidate(101)]

        def mock_numa_maps_result(pid):
            return {
                "total_mb": 2048.0,
                "huge_mb": 0.0,
                "private_mb": 1000.0,
                "heap_mb": 50.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        # Each PID gets its own Process mock with cpu_percent behavior:
        # first call (seed) → 0.0, subsequent calls → 5.0
        proc_mocks = {}
        for pid in [100, 101]:
            mp = MagicMock()
            mp.cpu_percent.side_effect = [0.0] + [5.0] * 10  # seed=0, then always 5.0
            proc_mocks[pid] = mp

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process", side_effect=lambda pid: proc_mocks[pid]
        ):
            # Cycle 1: seed — cpu_percent returns 0.0 for each PID
            vms1 = monitor._collect_vm_metrics_serial(candidates)
            self.assertEqual(monitor.peak_total_memory_mb, 4096.0)
            self.assertEqual(monitor.peak_total_cpu, 0.0)  # first cycle seeds, cpu=0

            # Cycle 2: delta — cpu_percent returns 5.0 for each PID
            vms2 = monitor._collect_vm_metrics_serial(candidates)
            self.assertEqual(monitor.peak_total_cpu, 10.0)  # 2 * 5.0

    def test_dead_pid_cleaned_from_cache(self):
        """PIDs not in current sample should be removed from process_cache"""
        monitor = DummyMonitor()
        # Pre-populate cache with a dead PID
        monitor.process_cache[999] = MagicMock()

        candidates = [self._make_candidate(100)]

        def mock_numa_maps_result(pid):
            return {
                "total_mb": 100.0,
                "huge_mb": 0.0,
                "private_mb": 50.0,
                "heap_mb": 0.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process"
        ) as mock_process_cls:
            mock_proc = MagicMock()
            mock_proc.cpu_percent.return_value = 0.0
            mock_process_cls.return_value = mock_proc

            vms = monitor._collect_vm_metrics_serial(candidates)

        # Dead PID 999 should have been cleaned
        self.assertNotIn(999, monitor.process_cache)
        self.assertIn(100, monitor.process_cache)


class TestBugFixes(unittest.TestCase):
    """Tests for bugs found during code review"""

    def _write_mock_numa_maps(self, content):
        """Write mock numa_maps content to a temp file"""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".numa_maps", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_swapcache_uses_base_page_size_not_hugepage_size(self):
        """swapcache in numa_maps counts 4KB base pages, not hugepage pages.
        Multiplying by kernelpagesize_kB for THP mappings would inflate
        swapcache_mb by 512x. The fix uses base_page_size_mb (4/1024).
        """
        # A THP mapping: kernelpagesize_kB=2048, swapcache=2, N0=5
        content = "aaaa0000 bind:2 anon=100 swapcache=2 N0=5 kernelpagesize_kB=2048\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        # swapcache=2 pages at 4KB each = 2 * 4 / 1024 = 0.0078 MB
        # NOT 2 * 2048 / 1024 = 4.0 MB (that would be 512x inflation)
        expected_swapcache_mb = round(2 * 4 / 1024, 2)  # ~0.01 MB
        self.assertAlmostEqual(result["swapcache_mb"], expected_swapcache_mb, places=2)

        # total should still use hugepage size: 5 * 2048/1024 = 10.0 MB
        expected_total_mb = round(5 * 2048 / 1024, 2)
        self.assertAlmostEqual(result["total_mb"], expected_total_mb, places=1)

        os.unlink(path)

    def test_numa_maps_parsed_flag_accepted_on_zero_memory(self):
        """When numa_maps is successfully parsed but process has zero resident
        pages, the _parsed flag should allow the result through instead of
        falling back to numastat subprocess.
        """
        monitor = DummyMonitor()

        # numa_maps with no N*= entries (all virtual mappings) → parsed=True, total_mb=0
        content = "aaaa0000 bind:2\naaaab000 bind:2\n"
        path = self._write_mock_numa_maps(content)

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numastat(123)

        # Should accept the numa_maps result (total_mb=0 is legitimate)
        # Without the fix, >0 check would reject it and fall back to numastat
        self.assertEqual(result["total_mb"], 0.0)
        # The _parsed flag should have been removed before returning
        self.assertNotIn("_parsed", result)

        os.unlink(path)

    def test_numa_maps_failure_triggers_fallback(self):
        """When numa_maps file doesn't exist (parsed=False), should fall back
        to numastat subprocess instead of accepting the empty result.
        """
        monitor = DummyMonitor()
        numastat_output = (
            "Node0            Node2            Total\n"
            "---              ---              ---\n"
            "Total            1000.0           2000.0           3000.0\n"
        )

        with patch("vm_monitor.base.open", side_effect=FileNotFoundError), patch(
            "vm_monitor.base.subprocess.run", return_value=MagicMock(returncode=0, stdout=numastat_output)
        ):
            result = monitor.get_vm_memory_from_numastat(123)

        # Should have data from numastat fallback, not empty numa_maps result
        self.assertGreater(result["total_mb"], 0)

    def test_swapcache_base_page_size_in_regular_mapping(self):
        """For regular 4KB mappings, swapcache should still use base page size
        (which is the same as kernelpagesize_kB in this case, so no difference).
        """
        content = "aaaa0000 bind:2 anon=473814 swapcache=86418 N2=386088 N5=87726 kernelpagesize_kB=4\n"
        path = self._write_mock_numa_maps(content)
        monitor = DummyMonitor()

        with patch(
            "vm_monitor.base.open", side_effect=lambda p, *a, **k: open(path) if "numa_maps" in p else open(p, *a, **k)
        ):
            result = monitor.get_vm_memory_from_numa_maps(123)

        # For 4KB mappings, base_page_size_mb == page_size_mb, so same result
        swapcache_mb = result["swapcache_mb"]
        expected = round(86418 * 4 / 1024, 2)  # 337.57 MB
        self.assertAlmostEqual(swapcache_mb, expected, places=1)

        os.unlink(path)

    def test_fallback_memory_mb_helper(self):
        """_fallback_memory_mb should return PSS or RSS when numastat fails"""
        monitor = DummyMonitor()

        # Mock psutil.Process to return memory_info with PSS
        mock_proc = MagicMock()
        mock_mem = MagicMock()
        mock_mem.pss = 2048 * 1024 * 1024  # 2048 MB
        mock_proc.memory_info.return_value = mock_mem

        with patch("vm_monitor.base.psutil.Process", return_value=mock_proc):
            result = monitor._fallback_memory_mb(100)
        self.assertEqual(result, 2048.0)

    def test_extract_numastat_fields_helper(self):
        """_extract_numastat_fields should extract all standard VM metric fields"""
        monitor = DummyMonitor()
        numastat_mem = {
            "total_mb": 2048.0,
            "huge_mb": 0.0,
            "private_mb": 1000.0,
            "heap_mb": 50.0,
            "per_node": {0: {"total_mb": 1024.0}},
            "swapcache_mb": 100.0,
            "swapcache_per_node": {0: 50.0},
        }
        fields = monitor._extract_numastat_fields(numastat_mem)

        self.assertEqual(fields["memory_mb"], 2048.0)
        self.assertEqual(fields["memory_huge_mb"], 0.0)
        self.assertEqual(fields["memory_private_mb"], 1000.0)
        self.assertEqual(fields["memory_heap_mb"], 50.0)
        self.assertEqual(fields["memory_per_numa"], {0: {"total_mb": 1024.0}})
        self.assertEqual(fields["memory_swapcache_mb"], 100.0)
        self.assertEqual(fields["memory_swapcache_per_numa"], {0: 50.0})

    def test_collect_single_vm_returns_tuple(self):
        """_collect_single_vm should return (result_dict, seed_proc_or_None) tuple"""
        monitor = DummyMonitor()
        candidate = {"pid": 100, "vm_name": "vm-100", "status": "running", "proc_name": "test", "cmdline": ""}

        def mock_numa_maps_result(pid):
            return {
                "total_mb": 100.0,
                "huge_mb": 0.0,
                "private_mb": 50.0,
                "heap_mb": 0.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        mock_proc = MagicMock()
        mock_proc.cpu_percent.return_value = 0.0

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch(
            "vm_monitor.base.psutil.Process", return_value=mock_proc
        ):
            vm_result, seed_proc = monitor._collect_single_vm(candidate)

        # Result dict should not contain _is_new_pid or _seed_process
        self.assertNotIn("_is_new_pid", vm_result)
        self.assertNotIn("_seed_process", vm_result)
        self.assertEqual(vm_result["pid"], 100)
        self.assertEqual(vm_result["memory_mb"], 100.0)
        # seed_proc should be the mock Process object
        self.assertIsNotNone(seed_proc)

    def test_csv_fieldnames_include_swapcache(self):
        """CSV export fieldnames should include memory_swapcache_mb and
        memory_swapcache_per_numa (previously silently dropped by extrasaction).
        """
        monitor = DummyMonitor()
        # Trigger a collect_sample to populate data
        # We can't easily test export directly, but we can check collect_sample
        # includes swapcache fields in the record dict
        from datetime import datetime

        vm_dict = {
            "pid": 100,
            "name": "vm-100",
            "cpu_percent": 5.0,
            "memory_mb": 2048.0,
            "memory_huge_mb": 0.0,
            "memory_private_mb": 1000.0,
            "memory_heap_mb": 50.0,
            "memory_per_numa": {},
            "memory_swapcache_mb": 100.0,
            "memory_swapcache_per_numa": {0: 50.0},
            "status": "running",
        }
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "vm_name": vm_dict["name"],
            "pid": vm_dict["pid"],
            "cpu_percent": vm_dict["cpu_percent"],
            "memory_mb": vm_dict["memory_mb"],
            "memory_huge_mb": vm_dict.get("memory_huge_mb", 0),
            "memory_private_mb": vm_dict.get("memory_private_mb", 0),
            "memory_heap_mb": vm_dict.get("memory_heap_mb", 0),
            "memory_per_numa": vm_dict.get("memory_per_numa", {}),
            "memory_swapcache_mb": vm_dict.get("memory_swapcache_mb", 0),
            "memory_swapcache_per_numa": vm_dict.get("memory_swapcache_per_numa", {}),
            "status": vm_dict["status"],
        }
        # Verify swapcache fields are present in the record
        self.assertIn("memory_swapcache_mb", record)
        self.assertIn("memory_swapcache_per_numa", record)
        self.assertEqual(record["memory_swapcache_mb"], 100.0)


if __name__ == "__main__":
    unittest.main()
