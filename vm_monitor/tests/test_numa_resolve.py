"""Tests for the --numa argument resolver in vm_monitor/cli.py.

resolve_numa_nodes maps the CLI --numa string to a concrete node list:
"all" -> every node the host exposes (fallback [0] on non-NUMA hosts); an
explicit "0,1" list is parsed verbatim; a bogus value falls back to [0].
Pure function -- no /proc or /sys touched.
"""
from __future__ import annotations

import unittest

from vm_monitor.cli import resolve_numa_nodes


class TestResolveNumaNodes(unittest.TestCase):
    def test_all_uses_every_available_node(self):
        self.assertEqual(resolve_numa_nodes("all", [0, 1, 2, 3]), [0, 1, 2, 3])

    def test_all_is_case_insensitive(self):
        self.assertEqual(resolve_numa_nodes("ALL", [0, 1]), [0, 1])
        self.assertEqual(resolve_numa_nodes(" All ", [0, 1]), [0, 1])

    def test_all_falls_back_to_zero_on_non_numa_host(self):
        # no /sys/devices/system/node nodeN dirs -> empty available list
        self.assertEqual(resolve_numa_nodes("all", []), [0])

    def test_explicit_list_parsed_verbatim(self):
        self.assertEqual(resolve_numa_nodes("0,1", [0, 1, 2]), [0, 1])
        self.assertEqual(resolve_numa_nodes("3,0,2", [0, 1, 2, 3]), [3, 0, 2])

    def test_bogus_value_falls_back_to_zero(self):
        self.assertEqual(resolve_numa_nodes("abc", [0, 1]), [0])


if __name__ == "__main__":
    unittest.main()
