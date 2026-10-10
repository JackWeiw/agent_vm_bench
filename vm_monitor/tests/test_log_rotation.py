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
        self.assertEqual(len(spawned), 1)
        cmd = spawned[0]
        self.assertEqual(cmd[:8], ["/fake/ksys", "collect", "-d", "15", "-i", "3", "-c", "/fake/ksys_config.yaml"])
        self.assertEqual(cmd[8], "-o")
        self.assertTrue(cmd[9].endswith("/log_capture/ksys/"))
        files = os.listdir(os.path.join(self.log_dir, "log_capture", "ksys"))
        self.assertEqual(len(files), 1)
        # The collect-phase ksys process is tracked for later reaping.
        self.assertEqual(len(cap._rotation_ksys_procs), 1)

    def test_perf_turn_cmd_and_log(self):
        cap = self._cap(rotation_perf_events="cycles,instructions")
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap._rotation_perf_turn()
        cmd = spawned[0]
        self.assertEqual(cmd, ["perf", "stat", "-e", "cycles,instructions", "-a", "-I", "3000", "--", "sleep", "15"])
        files = os.listdir(os.path.join(self.log_dir, "log_capture", "perf"))
        self.assertEqual(len(files), 1)

    def test_perf_default_events_used_when_none(self):
        cap = self._cap()
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap._rotation_perf_turn()
        self.assertEqual(spawned[0][3], DEFAULT_ROTATION_PERF_EVENTS)

    def test_perf_missing_disables_future_turns(self):
        cap = self._cap()
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=OSError("No such file")):
            cap._rotation_perf_turn()
            cap._rotation_perf_turn()  # second turn must not retry
        self.assertIn("perf", cap._rotation_skipped)
        self.assertEqual(cap.rotation_turns["perf"], [])
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "log_capture", "perf")))

    def test_devkit_turn_popen_failure_skips_subtools(self):
        cap = self._cap()
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=OSError("Permission denied")):
            cap._rotation_devkit_turn()
            cap._rotation_devkit_turn()  # second turn hits the empty-cmds path
        self.assertIn("devkit_mem", cap._rotation_skipped)
        self.assertIn("devkit_top_down", cap._rotation_skipped)
        self.assertIn("devkit", cap._rotation_skipped)  # family skip once both sub-tools are out
        self.assertEqual(cap.rotation_turns["devkit_mem"], [])
        self.assertEqual(cap.rotation_turns["devkit_top_down"], [])
        # No empty log files left behind from the failed starts.
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "log_capture", "devkit")))

    def test_ksys_turn_popen_failure_skips_tool(self):
        cap = self._cap()
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=OSError("Permission denied")):
            cap._rotation_ksys_turn()
        self.assertIn("ksys", cap._rotation_skipped)
        self.assertEqual(cap.rotation_turns["ksys"], [])
        self.assertFalse(os.path.exists(os.path.join(self.log_dir, "log_capture", "ksys")))

    def test_run_slot_paces_fast_turn_to_interval(self):
        # A turn that finishes instantly must still hold its slot open for the
        # full interval (no hot spin of empty logs when a tool misbehaves).
        cap = self._cap(rotation_interval=1)
        t0 = time.monotonic()
        cap._run_slot(lambda: None)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)

    def test_run_slot_pacing_interrupted_by_stop(self):
        # stop() must break the pace immediately -- no waiting out a dead run.
        cap = self._cap(rotation_interval=30)
        cap.rotation_stop_flag.set()
        t0 = time.monotonic()
        cap._run_slot(lambda: None)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_thread_full_cycle_order_and_stop(self):
        """Timer mode: one full cycle devkit(topdown+memory) -> ksys, then stops.

        The ksys spawn sets the stop flag, so the loop must break before perf.
        interval=1 keeps the post-devkit slot pacing well under the join timeout.
        """
        cap = self._cap(rotation_interval=1)  # no stress_file -> timer mode, starts immediately
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

    def test_per_tool_intervals_dict_resolves_each(self):
        cap = self._cap(rotation_interval={"devkit": 20, "ksys": 30, "perf": 15})
        self.assertEqual(cap.rotation_intervals, {"devkit": 20, "ksys": 30, "perf": 15})
        self.assertEqual(cap.rotation_interval, 30)  # max -> join/stop budget

    def test_per_tool_intervals_partial_falls_back(self):
        cap = self._cap(rotation_interval={"devkit": 25})
        self.assertEqual(cap.rotation_intervals, {"devkit": 25, "ksys": 15, "perf": 15})
        self.assertEqual(cap.rotation_interval, 25)

    def test_per_tool_intervals_int_applies_to_all(self):
        cap = self._cap(rotation_interval=12)
        self.assertEqual(cap.rotation_intervals, {"devkit": 12, "ksys": 12, "perf": 12})

    def test_per_tool_intervals_drive_each_turn_cmd(self):
        cap = self._cap(rotation_interval={"devkit": 20, "ksys": 30, "perf": 15})
        spawned = []
        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=self._spawn_recorder(spawned)):
            cap._rotation_devkit_turn()
            cap._rotation_ksys_turn()
            cap._rotation_perf_turn()
        # devkit top-down + memory both run for -d 20; ksys runs -d 30.
        for cmd in spawned:
            if "top-down" in cmd or "memory" in cmd:
                self.assertEqual(cmd[cmd.index("-d") + 1], "20")
            if "collect" in cmd:
                self.assertEqual(cmd[cmd.index("-d") + 1], "30")
        # perf stat sleeps for 15.
        perf_cmd = next(c for c in spawned if c[0] == "perf")
        self.assertEqual(perf_cmd[-1], "15")

    def test_ksys_turn_timer_exits_after_interval_not_interval_plus_30(self):
        """ksys turn must exit after interval seconds, not interval+30.

        The old marker-based detection waited up to interval+30s because
        ksys block-buffers its stdout. The timer-based fix must cap the
        wait at interval seconds even when the process stays alive.
        """
        cap = self._cap(rotation_interval=2)  # 2s -> ksys turn <= 2s

        class _AliveProc:
            """Simulates a ksys process that never exits (collect+parse running)."""

            returncode = None

            def poll(self):
                return None  # always alive

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        with patch("vm_monitor.log_capture.subprocess.Popen", return_value=_AliveProc()):
            t0 = time.monotonic()
            cap._rotation_ksys_turn()
            elapsed = time.monotonic() - t0
        # Must exit at ~2s (the interval), well under the old 2+30=32s deadline.
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(elapsed, 1.5)
        # Process tracked for background reaping.
        self.assertEqual(len(cap._rotation_ksys_procs), 1)

    def test_devkit_turn_timer_exits_after_interval(self):
        """devkit turn must exit after interval seconds even if sub-tools stay alive."""
        cap = self._cap(rotation_interval=2)

        class _AliveProc:
            returncode = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        spawned = []

        def fake_popen(cmd, *a, **k):
            spawned.append(cmd)
            return _AliveProc()

        with patch("vm_monitor.log_capture.subprocess.Popen", side_effect=fake_popen):
            t0 = time.monotonic()
            cap._rotation_devkit_turn()
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(elapsed, 1.5)
        # Both alive procs tracked for background reaping.
        self.assertEqual(len(cap._rotation_devkit_procs), 2)

    def test_run_slot_uses_per_tool_interval(self):
        cap = self._cap(rotation_interval={"devkit": 1, "ksys": 1, "perf": 1})
        t0 = time.monotonic()
        cap._run_slot(lambda: None, interval=1)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)


if __name__ == "__main__":
    unittest.main()
