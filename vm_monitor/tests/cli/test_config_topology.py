"""Unit tests for the host-topology auto-discovery helpers in vm_monitor/config.py.

These run on any host: every sysfs read is monkeypatched so the helpers never
touch a real /sys or /proc.
"""

import unittest
from unittest import mock

from vm_monitor.config import (
    _count_physical_cores,
    _discover_numa_nodes,
    _parse_cpulist,
    _physical_cores_for_numa,
    calculate_cpu_range_from_numa,
    numa_to_physical_cores,
)


def _open_text(path_to_text):
    """Build a fake open() whose path mapping decides what each file returns."""

    def fake_open(path, *a, **k):
        if path not in path_to_text:
            raise FileNotFoundError(path)
        from io import StringIO

        return StringIO(path_to_text[path])

    return fake_open


class TestParseCpulist(unittest.TestCase):
    def test_simple_range_and_singletons(self):
        self.assertEqual(_parse_cpulist("0-3,7,10-12"), [0, 1, 2, 3, 7, 10, 11, 12])

    def test_empty_and_whitespace(self):
        self.assertEqual(_parse_cpulist(""), [])
        self.assertEqual(_parse_cpulist("  "), [])

    def test_all_singletons(self):
        self.assertEqual(_parse_cpulist("0,1,2"), [0, 1, 2])


class TestPhysicalCoresForNuma(unittest.TestCase):
    def test_dedups_hyperthread_siblings(self):
        # NUMA node owns logical cores 0-3; each physical core has two siblings:
        # cpu0/cpu2 are siblings (0,2) -> physical 0; cpu1/cpu3 are siblings
        # (1,3) -> physical 1. Result should be [0, 1].
        files = {
            "/sys/devices/system/node/node0/cpulist": "0-3",
            "/sys/devices/system/cpu/cpu0/topology/thread_siblings_list": "0,2",
            "/sys/devices/system/cpu/cpu1/topology/thread_siblings_list": "1,3",
            "/sys/devices/system/cpu/cpu2/topology/thread_siblings_list": "0,2",
            "/sys/devices/system/cpu/cpu3/topology/thread_siblings_list": "1,3",
        }
        with mock.patch("builtins.open", _open_text(files)):
            result = _physical_cores_for_numa(0)
        self.assertEqual(result, [0, 1])

    def test_falls_back_to_logical_when_no_topology(self):
        # cpulist present but no thread_siblings files -> treat logical as physical.
        files = {"/sys/devices/system/node/node0/cpulist": "10-12"}
        with mock.patch("builtins.open", _open_text(files)):
            result = _physical_cores_for_numa(0)
        self.assertEqual(result, [10, 11, 12])

    def test_empty_when_node_missing(self):
        files = {}
        with mock.patch("builtins.open", _open_text(files)):
            result = _physical_cores_for_numa(9)
        self.assertEqual(result, [])


class TestNumaToPhysicalCores(unittest.TestCase):
    def test_applies_core_interval_sampling(self):
        files = {
            "/sys/devices/system/node/node0/cpulist": "0-7",
            **{f"/sys/devices/system/cpu/cpu{i}/topology/thread_siblings_list": str(i) for i in range(8)},
        }
        with mock.patch("builtins.open", _open_text(files)):
            result = numa_to_physical_cores([0], core_interval=2)
        # No real siblings (each cpu is its own sibling) -> physical == logical;
        # interval=2 picks every other core.
        self.assertEqual(result, {0: [0, 2, 4, 6]})

    def test_skips_node_with_no_cores(self):
        files = {"/sys/devices/system/node/node1/cpulist": "0-3"}
        with mock.patch("builtins.open", _open_text(files)):
            result = numa_to_physical_cores([0, 1])
        self.assertNotIn(0, result)
        self.assertIn(1, result)


class TestCalculateCpuRangeFromNuma(unittest.TestCase):
    def test_merges_ranges(self):
        files = {
            "/sys/devices/system/node/node0/cpulist": "0-3",
            "/sys/devices/system/node/node1/cpulist": "8-9",
        }
        with mock.patch("builtins.open", _open_text(files)):
            self.assertEqual(calculate_cpu_range_from_numa([0, 1]), "0-3,8-9")

    def test_returns_empty_when_all_reads_fail(self):
        files = {}
        with mock.patch("builtins.open", _open_text(files)):
            self.assertEqual(calculate_cpu_range_from_numa([0, 1]), "")

    def test_partial_failure_keeps_good_nodes(self):
        files = {"/sys/devices/system/node/node1/cpulist": "100-102"}
        with mock.patch("builtins.open", _open_text(files)):
            self.assertEqual(calculate_cpu_range_from_numa([0, 1]), "100-102")


class TestCountPhysicalCores(unittest.TestCase):
    def test_dedups_siblings(self):
        # Two physical cores, each with two HT siblings.
        files = {
            "/sys/devices/system/cpu/cpu0/topology/thread_siblings_list": "0,2",
            "/sys/devices/system/cpu/cpu1/topology/thread_siblings_list": "1,3",
            "/sys/devices/system/cpu/cpu2/topology/thread_siblings_list": "0,2",
            "/sys/devices/system/cpu/cpu3/topology/thread_siblings_list": "1,3",
        }
        with mock.patch(
            "vm_monitor.config.glob.glob",
            return_value=[
                "/sys/devices/system/cpu/cpu0",
                "/sys/devices/system/cpu/cpu1",
                "/sys/devices/system/cpu/cpu2",
                "/sys/devices/system/cpu/cpu3",
            ],
        ), mock.patch("builtins.open", _open_text(files)):
            self.assertEqual(_count_physical_cores(), 2)

    def test_fallback_to_os_cpu_count(self):
        # No topology files at all -> os.cpu_count() fallback.
        files = {}

        with mock.patch("vm_monitor.config.glob.glob", return_value=["/sys/devices/system/cpu/cpu0"]), mock.patch(
            "builtins.open", _open_text(files)
        ), mock.patch("vm_monitor.config.os.cpu_count", return_value=8):
            self.assertEqual(_count_physical_cores(), 8)


class TestDiscoverNumaNodes(unittest.TestCase):
    def test_lists_present_nodes(self):
        with mock.patch("vm_monitor.config.os.listdir", return_value=["node0", "node1", "node5", "other"]):
            self.assertEqual(_discover_numa_nodes(), [0, 1, 5])

    def test_defaults_to_zero_on_failure(self):
        with mock.patch("vm_monitor.config.os.listdir", side_effect=FileNotFoundError):
            self.assertEqual(_discover_numa_nodes(), [0])


if __name__ == "__main__":
    unittest.main()
