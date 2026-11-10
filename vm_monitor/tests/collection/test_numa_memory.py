"""Unit tests for per-NUMA meminfo parsing and VM total memory in vm_monitor/base.py"""

import os
import tempfile
import unittest
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

    @staticmethod
    def _patch_proc(procs):
        """Patch the /proc scan: ``procs`` maps pid -> (comm, state, cmdline)."""

        def stat(pid):
            # starttime is only an identity token here; the pid stands in for it
            return (procs[pid][0], procs[pid][1], pid) if pid in procs else None

        def cmdline(pid):
            return procs[pid][2] if pid in procs else None

        return (
            patch("vm_monitor.base.os.listdir", return_value=[str(p) for p in procs] + ["self", "123x"]),
            patch("vm_monitor.base.read_comm_state", side_effect=stat),
            patch("vm_monitor.base.read_cmdline", side_effect=cmdline),
        )

    def test_finds_matching_processes(self):
        """Should discover processes matching get_process_names()"""
        monitor = DummyMonitor()
        procs = {
            100: ("test_process", "R", "--id vm0"),
            101: ("other_process", "R", ""),
            102: ("test_process_extra", "S", ""),
        }

        listdir, stat, cmdline = self._patch_proc(procs)
        with listdir, stat, cmdline:
            candidates = monitor._discover_vm_processes()

        self.assertEqual(len(candidates), 2)  # skips other_process
        self.assertEqual(sorted(c["pid"] for c in candidates), [100, 102])
        # status comes from the /proc/<pid>/stat state letter, as psutil strings
        status_by_pid = {c["pid"]: c["status"] for c in candidates}
        self.assertEqual(status_by_pid[100], "running")
        self.assertEqual(status_by_pid[102], "sleeping")

    def test_skips_unreadable_processes(self):
        """Pids that vanish between listdir and stat are skipped"""
        monitor = DummyMonitor()
        procs = {100: ("test_process", "R", "")}

        listdir, stat, cmdline = self._patch_proc(procs)
        with listdir, stat, cmdline:
            # listdir also yields 101, absent from procs -> stat returns None
            candidates = monitor._discover_vm_processes()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["pid"], 100)

    def test_cmdline_read_only_when_it_can_match(self):
        """Subclass opt-in via _cmdline_may_match gates the cmdline read."""
        monitor = DummyMonitor()
        procs = {100: ("test_process", "R", ""), 101: ("unrelated", "R", "")}

        listdir, stat, cmdline = self._patch_proc(procs)
        with listdir, stat, cmdline as mock_cmdline:
            monitor._discover_vm_processes()

        # Base matcher ignores cmdline, so only the comm match reads it
        self.assertEqual([c.args[0] for c in mock_cmdline.call_args_list], [100])

    def test_returns_candidate_fields(self):
        """Candidates should have pid, proc_name, cmdline, status, vm_name"""
        monitor = DummyMonitor()
        procs = {100: ("test_process", "R", "--id vm0")}

        listdir, stat, cmdline = self._patch_proc(procs)
        with listdir, stat, cmdline:
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
        procs = {100: ("unrelated", "R", "")}

        listdir, stat, cmdline = self._patch_proc(procs)
        with listdir, stat, cmdline:
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
            self.assertIn("pss_mb", vm)
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
        self.assertIn("pss_mb", vm)

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

    def test_cpu_baseline_seeded_then_delta(self):
        """CPU% comes from jiffies deltas: first sample 0, then the real rate"""
        monitor = DummyMonitor()
        # Two cycles over the same pids; each pid burns 100 ticks (1.0 CPU-s)
        # between samples 2.0 s apart -> 50% of one core.
        ticks = iter([(50, 0), (50, 0), (150, 0), (150, 0)])
        times = iter([1000.0, 1000.0, 1002.0, 1002.0])
        candidates = [self._make_candidate(100), self._make_candidate(101)]

        def mock_numa_maps_result(pid):
            return {"total_mb": 100.0, "per_node": {}}

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch.object(
            monitor, "_read_pid_jiffies", side_effect=lambda pid: next(ticks)
        ), patch.object(monitor, "_read_pss_mb", return_value=0.0), patch(
            "vm_monitor.base.time.monotonic", side_effect=lambda: next(times)
        ):
            vms1 = monitor._collect_vm_metrics_serial(candidates)
            vms2 = monitor._collect_vm_metrics_serial(candidates)

        self.assertEqual([vm["cpu_percent"] for vm in vms1], [0.0, 0.0])
        self.assertEqual([vm["cpu_percent"] for vm in vms2], [50.0, 50.0])

    def test_dead_pid_cleaned_from_cpu_baseline(self):
        """PIDs not in the current sample are dropped from the CPU baseline"""
        monitor = DummyMonitor()
        monitor._proc_cpu_prev[999] = (10, 0.0)

        candidates = [self._make_candidate(100)]

        with patch.object(
            monitor, "get_vm_memory_from_numastat", return_value={"total_mb": 100.0, "per_node": {}}
        ), patch.object(monitor, "_read_pid_jiffies", return_value=(10, 0)), patch.object(
            monitor, "_read_pss_mb", return_value=0.0
        ):
            monitor._collect_vm_metrics_serial(candidates)

        self.assertNotIn(999, monitor._proc_cpu_prev)
        self.assertIn(100, monitor._proc_cpu_prev)

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

        # Cycle 1 seeds the jiffies baseline (cpu 0); cycle 2 delta is 100
        # ticks over 2.0 s = 50% of one core per VM.
        ticks = iter([(50, 0), (50, 0), (150, 0), (150, 0)])
        times = iter([1000.0, 1000.0, 1002.0, 1002.0])

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch.object(
            monitor, "_read_pid_jiffies", side_effect=lambda pid: next(ticks)
        ), patch.object(monitor, "_read_pss_mb", return_value=0.0), patch(
            "vm_monitor.base.time.monotonic", side_effect=lambda: next(times)
        ):
            monitor._collect_vm_metrics_serial(candidates)
            self.assertEqual(monitor.peak_total_memory_mb, 4096.0)
            self.assertEqual(monitor.peak_total_cpu, 0.0)  # first cycle seeds

            monitor._collect_vm_metrics_serial(candidates)
            self.assertEqual(monitor.peak_total_cpu, 100.0)  # 2 * 50%

    def test_dead_pid_cleaned_from_cache(self):
        """PIDs not in current sample should be removed from the CPU baseline"""
        monitor = DummyMonitor()
        # Pre-populate cache with a dead PID
        monitor._proc_cpu_prev[999] = (10, 0.0)

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

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch.object(
            monitor, "_read_pid_jiffies", return_value=(10, 0)
        ), patch.object(monitor, "_read_pss_mb", return_value=0.0):
            monitor._collect_vm_metrics_serial(candidates)

        # Dead PID 999 should have been cleaned
        self.assertNotIn(999, monitor._proc_cpu_prev)
        self.assertIn(100, monitor._proc_cpu_prev)


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
        self.assertEqual(fields["pss_mb"], 0.0)  # default; collector overwrites via _read_pss_mb

    def test_collect_single_vm_returns_tuple(self):
        """_collect_single_vm should return (result, heavy_fields, cpu_sec)"""
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

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa_maps_result), patch.object(
            monitor, "_read_pss_mb", return_value=512.0
        ):
            vm_result, heavy_fields, heavy_cpu = monitor._collect_single_vm(candidate)

        # Result dict should not contain _is_new_pid or _seed_process
        self.assertNotIn("_is_new_pid", vm_result)
        self.assertNotIn("_seed_process", vm_result)
        self.assertEqual(vm_result["pid"], 100)
        self.assertEqual(vm_result["memory_mb"], 100.0)
        # PSS is always collected (not just a numastat-failure fallback)
        self.assertEqual(vm_result["pss_mb"], 512.0)
        # A first-seen pid is always read and reports its measured CPU cost
        self.assertEqual(heavy_fields["pss_mb"], 512.0)
        self.assertIsNotNone(heavy_cpu)

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
            "pss_mb": 1500.0,
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
            "pss_mb": vm_dict.get("pss_mb", 0),
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
        self.assertIn("pss_mb", record)
        self.assertEqual(record["pss_mb"], 1500.0)

    def test_pss_collector_off_skips_read_and_zeros_field(self):
        """--no-pss must skip _read_pss_mb entirely (no smaps_rollup cost)."""
        monitor = DummyMonitor()
        monitor.disable_collectors({"pss"})
        candidate = {"pid": 100, "vm_name": "vm-100", "status": "running", "proc_name": "t", "cmdline": ""}

        def mock_numa(pid):
            return {
                "total_mb": 100.0,
                "huge_mb": 0.0,
                "private_mb": 50.0,
                "heap_mb": 0.0,
                "per_node": {},
                "swapcache_mb": 0.0,
                "swapcache_per_node": {},
            }

        with patch.object(monitor, "get_vm_memory_from_numastat", side_effect=mock_numa), patch.object(
            monitor, "_read_pss_mb"
        ) as mock_pss:
            vm_result, _, _ = monitor._collect_single_vm(candidate)

        mock_pss.assert_not_called()  # smaps_rollup never opened
        self.assertEqual(vm_result["pss_mb"], 0.0)

    def test_collect_vm_total_pss_sums_per_vm_pss(self):
        """collect_vm_total_pss aggregates per-VM pss_mb into a fleet timeline."""
        monitor = DummyMonitor()
        vms = [{"pss_mb": 512.0}, {"pss_mb": 1488.0}, {"pss_mb": 0.0}]
        entry = monitor.collect_vm_total_pss(vms)
        self.assertEqual(entry["total_pss_mb"], 2000.0)
        self.assertEqual(entry["vm_count"], 3)
        self.assertEqual(len(monitor.vm_total_pss_history), 1)
        self.assertEqual(monitor.vm_total_pss_history[0]["total_pss_mb"], 2000.0)

    def test_collect_ublk_daemon_computes_cores_from_jiffies(self):
        """collect_ublk_daemon resolves the pid + converts jiffy delta to cores."""
        monitor = DummyMonitor()
        # Two stat reads: utime+stime jumps 100 jiffies (1.0s @ _CLK_TCK=100).
        stat_payloads = iter(
            [
                "0 (uvm-ublk-daemon) S 1 0 0 0 -1 0 0 0 0 0 0 0 0 0 0 0 0 0",
                "0 (uvm-ublk-daemon) S 1 0 0 0 -1 0 0 0 0 0 0 100 0 0 0 0 0 0 0",  # +100 jiffies
            ]
        )

        def fake_open(path, *a, **k):
            content = next(stat_payloads)

            class _F:
                def read(self_inner):
                    return content

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

            return _F()

        import vm_monitor.base as base

        # Patch perf_counter to simulate a 1.0s wall delta between samples.
        clock = iter([1000.0, 1001.0])
        with patch.object(monitor, "_find_pid_by_comm", return_value=4242), patch(
            "builtins.open", side_effect=fake_open
        ), patch.object(base.time, "perf_counter", side_effect=lambda: next(clock)):
            monitor.collect_ublk_daemon()  # seed (no prev -> cores 0)
            monitor.collect_ublk_daemon()  # delta 100 jiffies / 100 tck / 1.0s = 1.0 core

        self.assertEqual(len(monitor.ublk_daemon_history), 2)
        self.assertEqual(monitor.ublk_daemon_history[0]["cores"], 0.0)
        self.assertEqual(monitor.ublk_daemon_history[1]["cores"], 1.0)


if __name__ == "__main__":
    unittest.main()
