"""Tests for log-rotation mode in vm_monitor/log_capture.py.

``log_rotation=True`` replaces the all-parallel capture of devkit/ksys with a
devkit (top-down + memory in parallel) -> ksys -> perf stat rotation, one slot
per ``rotation_interval``; each turn writes a timestamped log under
``<rotation_log_dir>/{devkit/{topdown,memory},ksys,perf}/``. ub_watch /
smap_bw / getfre keep their all-parallel behavior.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vm_monitor.log_capture import DEFAULT_ROTATION_PERF_EVENTS, LogCapture


class _DummyProc:
    """Stand-in for subprocess.Popen -- turns only store it and wait briefly."""

    returncode = 0

    def poll(self):  # noqa: N802 - proc seam
        return 0

    def wait(self, timeout=None):  # noqa: N802 - proc seam
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class TestLogRotation(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.mkdtemp(prefix="log_rotation_")
        # devkit_cpu_range set so _rotation_devkit_turn never touches sysfs.
        self.config = {
            "devkit_path": "/fake/devkit",
            "devkit_cpu_range": "0-3",
            "ksys_path": "/fake/ksys",
            "ksys_config_path": "/fake/ksys_config.yaml",
        }

    def tearDown(self):
        shutil.rmtree(self.log_dir, ignore_errors=True)

    def _cap(self, **kwargs):
        kwargs.setdefault("log_rotation", True)
        return LogCapture(self.config, 30, self.log_dir, [0], **kwargs)

    @staticmethod
    def _spawn_recorder(spawned):
        """Popen side effect that records each cmd and returns a _DummyProc."""

        def fake_popen(cmd, *a, **k):
            spawned.append(cmd)
            return _DummyProc()

        return fake_popen

    def test_devkit_turn_spawns_both_in_parallel_with_interval(self):
        cap = self._cap()
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap._rotation_devkit_turn()
        self.assertEqual(len(spawned), 2)
        topdown = next(c for c in spawned if "top-down" in c)
        mem = next(c for c in spawned if "memory" in c)
        self.assertEqual(topdown[:4], ["/fake/devkit", "tuner", "top-down", "-d"])
        self.assertEqual(topdown[4], "15")
        self.assertIn("0-3", topdown)
        self.assertEqual(mem[:4], ["/fake/devkit", "tuner", "memory", "-d"])
        self.assertEqual(mem[4], "15")
        # Per-turn timestamped logs land in the devkit subfolders.
        for sub, tool in (("topdown", "devkit_top_down"), ("memory", "devkit_mem")):
            files = os.listdir(os.path.join(self.log_dir, "log_capture", "devkit", sub))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(".log"))
            expected = os.path.join(self.log_dir, "log_capture", "devkit", sub, files[0])
            self.assertEqual(cap.rotation_turns[tool], [expected])

    def test_devkit_turn_respects_disabled_devkit(self):
        cap = self._cap(disabled_devkit={"devkit_mem"})
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap._rotation_devkit_turn()
        self.assertEqual(len(spawned), 1)
        self.assertIn("top-down", spawned[0])

    def test_ksys_turn_cmd_and_log(self):
        cap = self._cap()
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap._rotation_ksys_turn()
        self.assertEqual(spawned, [["/fake/ksys", "collect", "-d", "15", "-i", "3", "-c", "/fake/ksys_config.yaml"]])
        files = os.listdir(os.path.join(self.log_dir, "log_capture", "ksys"))
        self.assertEqual(len(files), 1)
        # The collect-phase ksys process is tracked for later reaping.
        self.assertEqual(len(cap._rotation_ksys_procs), 1)

    def test_perf_turn_cmd_and_log(self):
        cap = self._cap(rotation_perf_events="cycles,instructions")
        with patch("vm_monitor.log_capture.subprocess.run") as mock_run:
            cap._rotation_perf_turn()
        cmd = mock_run.call_args.args[0]
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(cmd, ["perf", "stat", "-e", "cycles,instructions", "-a", "--", "sleep", "15"])
        # perf stat writes its counters to stderr; the turn must merge it into the log.
        self.assertEqual(kwargs.get("stderr"), subprocess.STDOUT)
        files = os.listdir(os.path.join(self.log_dir, "log_capture", "perf"))
        self.assertEqual(len(files), 1)

    def test_perf_default_events_used_when_none(self):
        cap = self._cap()
        with patch("vm_monitor.log_capture.subprocess.run") as mock_run:
            cap._rotation_perf_turn()
        self.assertEqual(mock_run.call_args.args[0][3], DEFAULT_ROTATION_PERF_EVENTS)

    def test_perf_missing_disables_future_turns(self):
        cap = self._cap()
        with patch("vm_monitor.log_capture.subprocess.run", side_effect=OSError("No such file")):
            cap._rotation_perf_turn()
            cap._rotation_perf_turn()  # second turn must not retry
        self.assertIn("perf", cap._rotation_skipped)
        self.assertEqual(cap.rotation_turns["perf"], [])
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "log_capture", "perf")))

    def test_thread_full_cycle_order_and_stop(self):
        """Timer mode: one full cycle devkit(topdown+memory) -> ksys, then stops.

        The ksys spawn sets the stop flag, so the loop must break before perf.
        """
        cap = self._cap()  # no stress_file -> timer mode, starts immediately
        spawned = []
        ran = []

        def fake_popen(cmd, *a, **k):
            spawned.append(cmd)
            if "collect" in cmd:  # ksys turn: stop the loop after this slot
                cap.rotation_stop_flag.set()
            return _DummyProc()

        def fake_run(cmd, *a, **k):
            ran.append(cmd)

        with (
            patch("vm_monitor.log_capture.subprocess.Popen", side_effect=fake_popen),
            patch("vm_monitor.log_capture.subprocess.run", side_effect=fake_run),
        ):
            cap.rotation_thread = threading.Thread(target=cap._rotation_thread_main)
            cap.rotation_thread.start()
            cap.rotation_thread.join(timeout=10)
        self.assertFalse(cap.rotation_thread.is_alive())
        self.assertEqual(len(spawned), 3)  # topdown + memory (parallel) + ksys
        self.assertIn("top-down", spawned[0])
        self.assertIn("memory", spawned[1])
        self.assertIn("collect", spawned[2])
        self.assertEqual(ran, [])  # perf never reached: the flag broke the loop first
        self.assertEqual(len(cap.rotation_turns["devkit_top_down"]), 1)
        self.assertEqual(len(cap.rotation_turns["devkit_mem"]), 1)
        self.assertEqual(len(cap.rotation_turns["ksys"]), 1)
        self.assertEqual(cap.rotation_turns["perf"], [])

    def test_thread_lock_mode_waits_for_lock(self):
        """Lock mode: no lock -> no turns even though the thread was started."""
        lock = os.path.join(self.log_dir, "absent.lock")
        cap = self._cap(stress_file=lock)
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap.rotation_thread = threading.Thread(target=cap._rotation_thread_main)
            cap.rotation_thread.start()
            time.sleep(0.3)  # give the thread a chance to (wrongly) run a turn
            cap.stop()
            cap.rotation_thread.join(timeout=10)
        self.assertFalse(cap.rotation_thread.is_alive())
        self.assertEqual(spawned, [])
        self.assertEqual(cap.rotation_turns, {"devkit_top_down": [], "devkit_mem": [], "ksys": [], "perf": []})
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "log_capture")))

    def test_start_rotation_mode_skips_parallel_devkit_ksys(self):
        cap = self._cap(stress_file=os.path.join(self.log_dir, "absent.lock"))
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            result = cap.start()
            cap.stop()
        # devkit/ksys must NOT start in parallel (they rotate instead); nothing
        # else is configured in self.config, so nothing spawns at all.
        self.assertEqual(spawned, [])
        self.assertIn("log_rotation", result["success"])
        self.assertNotIn("ksys", result["success"])

    def test_start_default_mode_still_parallel(self):
        cap = LogCapture(self.config, 5, self.log_dir, [0])  # log_rotation=False
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            result = cap.start()
            cap.stop()
        subcmds = [c[2] for c in spawned if c[0] == "/fake/devkit"]
        self.assertIn("memory", subcmds)
        self.assertIn("top-down", subcmds)
        self.assertTrue(any("collect" in c for c in spawned))
        self.assertNotIn("log_rotation", result["success"])
        self.assertIn("devkit_mem", result["success"])
        self.assertIn("ksys", result["success"])

    def test_get_results_rotation_files(self):
        cap = self._cap()
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder([])):
            cap._rotation_devkit_turn()
        with patch("vm_monitor.log_capture.subprocess.run"):
            cap._rotation_perf_turn()
        cap.rotation_thread = True  # non-None so get_results reports the carrier
        results = cap.get_results()
        self.assertIn("rotation_files", results)
        self.assertEqual(len(results["rotation_files"]["devkit_top_down"]), 1)
        self.assertEqual(len(results["rotation_files"]["devkit_mem"]), 1)
        self.assertEqual(len(results["rotation_files"]["perf"]), 1)
        self.assertEqual(results["rotation_files"]["ksys"], [])
        # The parallel single-file keys must be dropped in rotation mode.
        for tool in ("devkit_mem", "devkit_top_down", "ksys"):
            self.assertNotIn(tool, results["log_files"])
        self.assertIn("ub_watch", results["log_files"])


if __name__ == "__main__":
    unittest.main()
