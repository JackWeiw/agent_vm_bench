"""Tests for the devkit top-down / memory split in vm_monitor/log_capture.py.

``disabled_devkit`` lets the caller run only one of the two devkit sub-tools
that share a single ``devkit_path`` (which ``.env`` path control cannot split).
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from vm_monitor.log_capture import LogCapture


class _DummyProc:
    """Stand-in for subprocess.Popen -- start() only stores it, never waits."""

    def poll(self):  # noqa: N802 - proc seam
        return 0

    def wait(self, timeout=None):  # noqa: N802 - proc seam
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class TestDevkitSplit(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="devkit_split_")
        # devkit_cpu_range set so _start_devkit_top_down never touches sysfs.
        self.config = {"devkit_path": "/fake/devkit", "devkit_cpu_range": "0-3"}

    def tearDown(self):
        for f in os.listdir(self.log_dir):
            try:
                os.unlink(os.path.join(self.log_dir, f))
            except PermissionError:
                pass
        os.rmdir(self.log_dir)

    def _start_with(self, disabled_devkit):
        """Call start() with the given disabled_devkit set; return spawned cmds."""
        spawned = []

        def fake_popen(cmd, *a, **k):
            spawned.append(cmd)
            return _DummyProc()

        cap = LogCapture(self.config, 5, self.log_dir, [0], disabled_devkit=disabled_devkit)
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=fake_popen):
            cap.start()
            cap.stop()  # close log file handles before tearDown deletes the dir
        return spawned

    @staticmethod
    def _devkit_subcmds(spawned):
        """Extract the devkit sub-command ('memory' / 'top-down') from spawned cmds."""
        out = []
        for c in spawned:
            if len(c) > 2 and c[0] == "/fake/devkit":
                out.append(c[2])
        return out

    def test_disable_topdown_spawns_only_mem(self):
        subcmds = self._devkit_subcmds(self._start_with({"devkit_top_down"}))
        self.assertIn("memory", subcmds)
        self.assertNotIn("top-down", subcmds)

    def test_disable_mem_spawns_only_topdown(self):
        subcmds = self._devkit_subcmds(self._start_with({"devkit_mem"}))
        self.assertIn("top-down", subcmds)
        self.assertNotIn("memory", subcmds)

    def test_default_both_spawned(self):
        subcmds = self._devkit_subcmds(self._start_with(set()))
        self.assertIn("memory", subcmds)
        self.assertIn("top-down", subcmds)


if __name__ == "__main__":
    unittest.main()
