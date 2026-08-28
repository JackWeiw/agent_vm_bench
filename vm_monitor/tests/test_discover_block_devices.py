"""Unit tests for the block-device auto-discovery helper in vm_monitor/base.py.

_discover_block_devices reads /sys/block, which only exists on Linux. The tests
monkeypatch os.listdir / os.path.exists so they run on any host and assert the
virtual/software prefixes are filtered out while physical disks are kept.
"""

import unittest
from unittest import mock

from vm_monitor.base import _discover_block_devices


class TestDiscoverBlockDevices(unittest.TestCase):
    def _patch_sys_block(self, entries, stat_present=None):
        # entries: dict dev_name -> bool (whether /sys/block/<dev>/stat exists)
        if stat_present is None:
            stat_present = {dev: True for dev in entries}

        def fake_listdir(path):
            if path == "/sys/block":
                return list(entries)
            raise FileNotFoundError(path)

        def fake_exists(path):
            # /sys/block/<dev>/stat
            for dev, present in stat_present.items():
                if path == f"/sys/block/{dev}/stat":
                    return present
            return False

        return mock.patch("vm_monitor.base.os.listdir", side_effect=fake_listdir), mock.patch(
            "vm_monitor.base.os.path.exists", side_effect=fake_exists
        )

    def test_filters_virtual_layers_and_keeps_physical(self):
        entries = {
            "sda": True,
            "sdb": True,
            "nvme0n1": True,
            "vda": True,
            "loop0": True,
            "loop7": True,
            "ram0": True,
            "ram12": True,
            "sr0": True,
            "zram0": True,
            "md0": True,
            "md127": True,
            "dm-0": True,
            "dm-3": True,
        }
        with self._patch_sys_block(entries)[0], self._patch_sys_block(entries)[1]:
            result = _discover_block_devices()
        self.assertEqual(result, ["nvme0n1", "sda", "sdb", "vda"])

    def test_sorted_output(self):
        entries = {"sdc": True, "sda": True, "nvme1n1": True, "sdb": True, "nvme0n1": True}
        with self._patch_sys_block(entries)[0], self._patch_sys_block(entries)[1]:
            result = _discover_block_devices()
        self.assertEqual(result, sorted(["nvme0n1", "nvme1n1", "sda", "sdb", "sdc"]))

    def test_skips_devices_without_stat_file(self):
        # A device with no readable stat file is not a usable block device.
        entries = {"sda": True, "sdb": True}
        stat_present = {"sda": True, "sdb": False}
        patches = self._patch_sys_block(entries, stat_present)
        with patches[0], patches[1]:
            result = _discover_block_devices()
        self.assertEqual(result, ["sda"])

    def test_empty_when_sys_block_unavailable(self):
        # Non-Linux host or permission error -> graceful empty list, no raise.
        with mock.patch("vm_monitor.base.os.listdir", side_effect=FileNotFoundError):
            result = _discover_block_devices()
        self.assertEqual(result, [])

    def test_no_physical_devices_returns_empty(self):
        entries = {"loop0": True, "ram0": True, "sr0": True}
        with self._patch_sys_block(entries)[0], self._patch_sys_block(entries)[1]:
            result = _discover_block_devices()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
