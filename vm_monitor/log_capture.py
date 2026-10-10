# vm_monitor/log_capture.py
"""
Parallel Log Collection Module

Runs multiple log collection tools (devkit, ksys, ub_watch, smap_bw, getfre)
in parallel subprocesses and threads, synchronized with QEMU monitoring duration.

Log-rotation mode (``log_rotation=True``): devkit (top-down + memory in
parallel) -> ksys -> ``perf stat`` rotate one slot at a time for the whole
stress window instead of running everything concurrently; ub_watch / smap_bw /
getfre keep their all-parallel behavior. Each turn writes a timestamped log
under ``<rotation_log_dir>/{devkit/{topdown,memory},ksys,perf}/``.
"""

import os
import re
import subprocess
import threading
import time
from datetime import datetime

# Internal dependencies
from .config import _count_physical_cores, calculate_cpu_range_from_numa, load_getfre_config, numa_to_physical_cores

# Default perf stat event set for log-rotation mode (used when the caller does
# not pass --rotation-perf-events). Mirrors the feat/perf-split experiment's
# event list; override per-host via the monitor.log_rotation.perf_events YAML
# knob (bench-core) or --rotation-perf-events (vm-monitor CLI).
DEFAULT_ROTATION_PERF_EVENTS = (
    "cycles,cpu-clock,instructions,tlb:tlb_flush,l1d_tlb,l1d_tlb_refill,"
    "l2d_tlb,l2d_tlb_refill,l2i_tlb,l2i_tlb_refill,context-switches,sched:sched_switch"
)


class LogCapture:
    """Parallel log collection with devkit, ksys, ub_watch, smap_bw

    Runs collection tools in background, synchronized with QEMU monitoring duration.
    All output is redirected to log files, not interfering with terminal display.
    """

    # Default timeouts for different tools
    DEFAULT_TOOL_TIMEOUTS = {
        "devkit_mem": 60,  # DevKit usually completes quickly after duration
        "devkit_top_down": 60,
        "ub_watch": 60,
        "ksys": 600,  # ksys needs extra time for data parsing (can be minutes)
        "smap_bw": 60,  # smap_bw follows duration + some buffer
    }

    def __init__(
        self,
        config: dict,
        duration: int,
        log_dir: str,
        numa_nodes: list,
        ksys_parse_timeout: int = None,
        disabled_devkit: set[str] | None = None,
        log_rotation: bool = False,
        rotation_interval: int | dict = 15,
        rotation_perf_events: str | None = None,
        rotation_log_dir: str | None = None,
        stress_file: str | None = None,
    ):
        """
        Args:
            config: paths from .env (devkit_path, ksys_path, ksys_config_path, ub_watch_path, devkit_cpu_range, getfre_path, getfre_config_path)
            duration: collection duration in seconds (same as qemu_monitor -t)
            log_dir: output directory for log files
            numa_nodes: list of NUMA nodes to monitor (for CPU range calculation)
            ksys_parse_timeout: extra timeout for ksys parse phase (default 600s)
            disabled_devkit: subset of {"devkit_mem", "devkit_top_down"} to skip.
                devkit_path is shared by both sub-tools, so .env path control cannot
                run only one; this set lets the CLI (--no-devkit-mem /
                --no-devkit-topdown) split them. Default empty = both run.
                In rotation mode it filters the devkit slot the same way.
            log_rotation: rotation mode -- devkit (top-down + memory in parallel)
                -> ksys -> perf stat rotate one slot per rotation_interval instead
                of running all tools concurrently. devkit/ksys leave the parallel
                set (they rotate); ub_watch/smap_bw/getfre are unchanged. Each
                turn writes a timestamped log under rotation_log_dir.
            rotation_interval: per-slot collection duration in seconds (default 15).
                Accepts an int (applies to every tool) or a dict mapping
                ``{"devkit": sec, "ksys": sec, "perf": sec}`` so each tool runs
                its own interval; keys absent from the dict fall back to 15.
                ``self.rotation_interval`` keeps the largest value for join/stop
                budgets, and ``self.rotation_intervals`` holds the resolved
                per-tool map.
            rotation_perf_events: comma-separated perf stat events (default:
                DEFAULT_ROTATION_PERF_EVENTS)
            rotation_log_dir: output dir for rotation logs (default:
                <log_dir>/log_capture)
            stress_file: stress marker lock path (from --stress-file). The
                rotation keys off the lock lifecycle (appear = stress start,
                disappear = stress end) so a huge -t cannot push rotation past
                real stress. None -> timer mode: rotate for `duration` seconds.
        """
        self.config = config
        self.duration = duration
        self.log_dir = log_dir
        self.numa_nodes = numa_nodes
        self.processes = {}  # {tool_name: Popen process}
        self.log_files = {}  # {tool_name: file handle}
        self.failed_startup = []  # tools that failed to start
        self.failed_runtime = []  # tools that failed during runtime
        self.start_time = None
        self.ksys_parse_timeout = ksys_parse_timeout or self.DEFAULT_TOOL_TIMEOUTS["ksys"]
        self.disabled_devkit = set(disabled_devkit or ())
        # getfre threading components
        self.getfre_threads = {}  # {numa_id: Thread}
        self.getfre_log_files = {}  # {numa_id: file handle}
        self.getfre_stop_flags = {}  # {numa_id: Event}
        # log-rotation components (devkit -> ksys -> perf round-robin)
        self.log_rotation = log_rotation
        # rotation_interval may be a single int (all tools share it) or a dict
        # {"devkit": sec, "ksys": sec, "perf": sec} for per-tool intervals.
        self.rotation_intervals, self.rotation_interval = self._resolve_rotation_intervals(rotation_interval)
        self.rotation_perf_events = rotation_perf_events or DEFAULT_ROTATION_PERF_EVENTS
        self.rotation_log_dir = rotation_log_dir or os.path.join(log_dir, "log_capture")
        self.stress_file = stress_file
        self.rotation_stop_flag = threading.Event()
        self.rotation_thread = None
        self.rotation_turns = {  # per-tool list of per-turn log paths
            "devkit_top_down": [],
            "devkit_mem": [],
            "ksys": [],
            "perf": [],
        }
        self._rotation_ksys_procs = []  # (proc, log_path) pairs: ksys procs still parsing in background
        self._rotation_devkit_procs = []  # devkit processes still finishing in background
        self._rotation_perf_procs = []  # perf processes still finishing in background
        self._rotation_skipped = set()  # tools disabled for the rest of the run

    def _get_cpu_range(self) -> str:
        """Get CPU range for devkit top-down command"""
        # Use configured range if available
        if self.config.get("devkit_cpu_range"):
            return self.config["devkit_cpu_range"]

        # Calculate from NUMA nodes
        return calculate_cpu_range_from_numa(self.numa_nodes)

    def _start_tool(self, tool_name: str, cmd: list, log_filename: str, success_msg: str) -> tuple:
        """Helper to start a single tool process

        Args:
            tool_name: identifier for the tool (e.g., 'devkit_mem')
            cmd: command list to execute
            log_filename: log file name (e.g., 'devkit_mem.log')
            success_msg: message to print on success

        Returns:
            (success: bool, error_msg: str or None)
        """
        try:
            log_path = os.path.join(self.log_dir, log_filename)
            self.log_files[tool_name] = open(log_path, "w")
            print(f"  [CMD] {tool_name}: {' '.join(cmd)}")
            self.processes[tool_name] = subprocess.Popen(
                cmd, stdout=self.log_files[tool_name], stderr=self.log_files[tool_name], cwd=self.log_dir
            )
            print(f"  [OK] {success_msg}")
            return (True, None)
        except Exception as e:
            print(f"  [ERROR] Failed to start {tool_name}: {e}")
            return (False, str(e))

    def _start_devkit_mem(self) -> tuple:
        """Start DevKit memory tuner

        Returns:
            (success: bool, error_msg: str or None)
        """
        if not self.config.get("devkit_path"):
            return (False, "devkit_path not configured")

        cmd = [self.config["devkit_path"], "tuner", "memory", "-d", str(self.duration), "-i", "3"]
        return self._start_tool(
            "devkit_mem", cmd, "devkit_mem.log", f"Started devkit tuner memory (duration={self.duration}s)"
        )

    def _start_devkit_top_down(self) -> tuple:
        """Start DevKit top-down tuner

        Returns:
            (success: bool, error_msg: str or None)
        """
        if not self.config.get("devkit_path"):
            return (False, "devkit_path not configured")

        cpu_range = self._get_cpu_range()
        if not cpu_range:
            # No CPU range could be determined (sysfs cpulist unreadable and no
            # DEVKIT_CPU_RANGE configured). Don't launch devkit with a wrong/empty
            # range — let the caller skip it rather than collect garbage.
            return (False, "could not determine CPU range (set DEVKIT_CPU_RANGE in .env)")
        cmd = [self.config["devkit_path"], "tuner", "top-down", "-d", str(self.duration), "-i", "3", "-c", cpu_range]
        return self._start_tool(
            "devkit_top_down", cmd, "devkit_top_down.log", f"Started devkit tuner top-down (cpu_range={cpu_range})"
        )

    def _start_ksys(self) -> tuple:
        """Start ksys collector

        Returns:
            (success: bool, error_msg: str or None)
        """
        if not self.config.get("ksys_path"):
            return (False, "ksys_path not configured")
        if not self.config.get("ksys_config_path"):
            return (False, "ksys_config_path not configured")

        cmd = [
            self.config["ksys_path"],
            "collect",
            "-d",
            str(self.duration),
            "-i",
            "3",
            "-c",
            self.config["ksys_config_path"],
        ]
        return self._start_tool(
            "ksys", cmd, "ksys.log", f"Started ksys collect (config={self.config['ksys_config_path']})"
        )

    def _start_ub_watch(self) -> tuple:
        """Start ub_watch

        Returns:
            (success: bool, error_msg: str or None)
        """
        if not self.config.get("ub_watch_path"):
            return (False, "ub_watch_path not configured")

        cmd = [self.config["ub_watch_path"], "-t", str(self.duration), "-i", "3"]
        return self._start_tool("ub_watch", cmd, "ub_watch.log", f"Started ub_watch (duration={self.duration}s)")

    def _start_smap_bw(self) -> tuple:
        """Start smap_bw SMAP migration bandwidth monitor

        Returns:
            (success: bool, error_msg: str or None)
        """
        if not self.config.get("smap_bw_path"):
            return (False, "smap_bw_path not configured")

        # smap_bw requires sudo for dmesg access
        # Command: sudo python3 <script_path> --clear --duration <dur> --timeout <dur+10>
        timeout = self.duration + 10  # Extra buffer for cleanup
        cmd = [
            "sudo",
            "python3",
            self.config["smap_bw_path"],
            "--clear",
            "--duration",
            str(self.duration),
            "--timeout",
            str(timeout),
        ]
        return self._start_tool(
            "smap_bw", cmd, "smap_bw.log", f"Started smap_bw (duration={self.duration}s, timeout={timeout}s)"
        )

    def _start_getfre(self) -> tuple:
        """Start getfre core frequency collector

        Uses threading to collect frequency data from multiple cores per NUMA node.
        Each NUMA node has its own log file with aggregated data.

        Returns:
            (success: bool, error_msg: str or None)
        """
        if not self.config.get("getfre_path"):
            return (False, "getfre_path not configured")

        # Load getfre config from YAML
        getfre_config = load_getfre_config(self.config.get("getfre_config_path", ""))

        # Use getfre_path from .env if available, otherwise from YAML
        getfre_path = self.config.get("getfre_path") or getfre_config.get("getfre_path")
        if not getfre_path or not os.path.exists(getfre_path):
            return (False, f"getfre_path not found: {getfre_path}")

        total_cores = getfre_config.get("total_cores") or _count_physical_cores()
        interval = getfre_config.get("interval", 2)
        core_interval = getfre_config.get("core_interval", 1)
        numa_nodes = getfre_config.get("numa_nodes", self.numa_nodes)

        # Calculate physical cores per NUMA
        numa_cores = numa_to_physical_cores(numa_nodes, core_interval)

        if not numa_cores:
            return (False, "No valid NUMA nodes for getfre collection")

        # Print command info (consistent with other tools)
        total_cores_count = sum(len(c) for c in numa_cores.values())
        numa_info = ", ".join([f"{n}:{len(c)}" for n, c in numa_cores.items()])
        print(f"  [CMD] getfre: {getfre_path} {total_cores} (cores per NUMA: {numa_info})")

        # Create threads for each NUMA node
        self.getfre_threads = {}
        self.getfre_log_files = {}
        self.getfre_stop_flags = {}

        for numa_id, cores in numa_cores.items():
            log_filename = f"getfre_NUMA{numa_id}.log"
            log_path = os.path.join(self.log_dir, log_filename)

            try:
                log_file = open(log_path, "w")
                self.getfre_log_files[numa_id] = log_file

                # Write CSV header
                log_file.write("timestamp,core,freq_mhz\n")

                # Create stop flag for this thread
                stop_flag = threading.Event()
                self.getfre_stop_flags[numa_id] = stop_flag

                # Create and start thread
                thread = threading.Thread(
                    target=self._getfre_collector_thread,
                    args=(numa_id, cores, getfre_path, total_cores, interval, log_file, stop_flag),
                    name=f"getfre-NUMA{numa_id}",
                )
                self.getfre_threads[numa_id] = thread
                thread.start()

            except Exception as e:
                print(f"  [ERROR] Failed to start getfre for NUMA {numa_id}: {e}")
                return (False, str(e))

        # Print success message (single line, consistent with other tools)
        print(
            f"  [OK] Started getfre (NUMA {','.join(map(str, numa_cores.keys()))}, {total_cores_count} cores, interval={interval}s)"
        )

        return (True, None)

    def _getfre_collector_thread(
        self, numa_id: int, cores: list, getfre_path: str, total_cores: int, interval: int, log_file, stop_flag
    ):
        """Thread function to collect core frequencies for a NUMA node

        Args:
            numa_id: NUMA node ID
            cores: list of physical core IDs to collect
            getfre_path: path to getfre executable
            total_cores: total physical cores
            interval: sampling interval in seconds
            log_file: file handle to write data
            stop_flag: threading.Event to signal stop
        """
        start_time = time.time()
        duration = self.duration

        while not stop_flag.is_set() and (time.time() - start_time) < duration:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Collect frequency for each core in this NUMA
            for core_id in cores:
                try:
                    # Call getfre: ./getfre <total_cores> <core_id>
                    result = subprocess.run(
                        [getfre_path, str(total_cores), str(core_id)], capture_output=True, text=True, timeout=5
                    )

                    if result.returncode == 0:
                        # Parse output: "Core 0 : 2300"
                        output = result.stdout.strip()
                        freq_match = re.search(r"Core\s+\d+\s*:\s*(\d+)", output)
                        if freq_match:
                            freq_mhz = int(freq_match.group(1))
                            log_file.write(f"{timestamp},{core_id},{freq_mhz}\n")
                    else:
                        # Log error
                        log_file.write(f"{timestamp},{core_id},ERROR\n")

                except subprocess.TimeoutExpired:
                    log_file.write(f"{timestamp},{core_id},TIMEOUT\n")
                except Exception:
                    log_file.write(f"{timestamp},{core_id},ERROR\n")

            # Flush log file after each sampling cycle
            log_file.flush()

            # Wait for next interval (or stop signal)
            stop_flag.wait(timeout=interval)

        # Final flush
        log_file.flush()

    # ------------------------------------------------------------------
    # Log-rotation mode: devkit (top-down + memory in parallel) -> ksys ->
    # perf stat, one slot per rotation_interval, repeating while stress runs.
    # ------------------------------------------------------------------

    _ROTATION_TOOLS = ("devkit", "ksys", "perf")

    @classmethod
    def _resolve_rotation_intervals(cls, raw) -> tuple[dict[str, int], int]:
        """Normalize ``rotation_interval`` into a per-tool map + a budget value.

        ``raw`` is an int (every tool shares it) or a dict
        ``{"devkit": sec, "ksys": sec, "perf": sec}``; keys absent from the dict
        fall back to the default (15). Returns ``(intervals, max_interval)`` --
        ``intervals`` maps each rotation tool to a >= 1 second value, and
        ``max_interval`` is the largest (used for stop()/wait() join budgets).
        """
        default = 15
        if isinstance(raw, dict):
            base = int(raw.get("default", raw.get("_default", default)))
            intervals = {t: max(1, int(raw.get(t, base))) for t in cls._ROTATION_TOOLS}
        else:
            val = max(1, int(raw)) if raw is not None else default
            intervals = {t: val for t in cls._ROTATION_TOOLS}
        return intervals, max(intervals.values())

    def _rotation_log_path(self, tool: str, *sub: str) -> str | None:
        """Create (and remember) a per-turn timestamped log path under rotation_log_dir.

        Layout: <rotation_log_dir>/<tool>[/<sub>]/<YYYYmmdd-HHMMSS>.log, e.g.
        log_capture/devkit/topdown/20261009-142530.log. A filename collision
        (only possible with sub-second slot intervals) gets a -2/-3 suffix.
        """
        d = os.path.join(self.rotation_log_dir, tool, *sub)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"  [ERROR] rotation: cannot create log dir {d}: {e}")
            return None
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(d, base + ".log")
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(d, f"{base}-{n}.log")
        return path

    def _rotation_skip_tool(self, tool: str, reason: str) -> None:
        """Disable one tool's turns for the rest of the run (warn once)."""
        if tool not in self._rotation_skipped:
            print(f"  [WARN] rotation: skipping {tool} for the rest of the run ({reason})")
            self._rotation_skipped.add(tool)

    def _wait_for_stress_lock(self) -> bool:
        """Block until the stress lock appears (or stop is signaled / -t passes).

        Returns True when rotation may start, False when it should not run.
        """
        deadline = time.monotonic() + self.duration  # don't outlive vm_monitor's -t
        while not self.rotation_stop_flag.is_set() and time.monotonic() < deadline:
            if self.stress_file and os.path.exists(self.stress_file):
                return True
            self.rotation_stop_flag.wait(timeout=0.5)
        return False

    def _stress_active(self) -> bool:
        """True while stress is ongoing (lock present). False once it ends."""
        if not self.stress_file:
            return True  # timer mode (no lock) -> active until the deadline check stops us
        return os.path.exists(self.stress_file)

    @staticmethod
    def _ksys_parse_started(log_path: str) -> bool:
        """True once the ksys turn's log shows the collect->parse transition."""
        try:
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                return "Starting to parse data" in f.read()
        except OSError:
            return False

    def _rotation_devkit_turn(self) -> None:
        """One devkit slot: top-down + memory in parallel, each for one interval."""
        if not self.config.get("devkit_path"):
            self._rotation_skip_tool("devkit", "devkit_path not configured")
            return
        cpu_range = self._get_cpu_range()
        if not cpu_range:
            self._rotation_skip_tool("devkit", "could not determine CPU range (set DEVKIT_CPU_RANGE in .env)")
            return
        interval = str(self.rotation_intervals["devkit"])
        cmds = {}
        if "devkit_top_down" not in self.disabled_devkit and "devkit_top_down" not in self._rotation_skipped:
            cmds["devkit_top_down"] = [
                self.config["devkit_path"],
                "tuner",
                "top-down",
                "-d",
                interval,
                "-i",
                "3",
                "-c",
                cpu_range,
            ]
        if "devkit_mem" not in self.disabled_devkit and "devkit_mem" not in self._rotation_skipped:
            cmds["devkit_mem"] = [self.config["devkit_path"], "tuner", "memory", "-d", interval, "-i", "3"]
        if not cmds:
            # Both sub-tools out (CLI --no-X or skipped after a start failure):
            # the whole family is done for this run.
            self._rotation_skipped.add("devkit")
            return
        procs = {}
        handles = {}
        for tool, cmd in cmds.items():
            log_path = self._rotation_log_path("devkit", "topdown" if tool == "devkit_top_down" else "memory")
            if log_path is None:
                continue
            fh = None
            try:
                fh = open(log_path, "w")
                print(f"  [CMD] {tool} (rotation): {' '.join(cmd)}")
                procs[tool] = subprocess.Popen(cmd, stdout=fh, stderr=fh, cwd=self.rotation_log_dir)
                handles[tool] = fh
                self.rotation_turns[tool].append(log_path)
            except OSError as e:
                # Cannot exec (bad path, no exec bit) -- it will not heal mid-run,
                # so skip this sub-tool for the rest of the run instead of
                # retrying (and empty-logging) every slot.
                self._rotation_skip_tool(tool, f"failed to start: {e}")
                self.failed_runtime.append({"tool": tool, "error": str(e)})
                if fh is not None:
                    try:
                        fh.close()
                        os.unlink(log_path)
                        leaf = os.path.dirname(log_path)
                        os.rmdir(leaf)  # drop the empty per-turn dir
                        os.rmdir(os.path.dirname(leaf))  # drop devkit/ too when both sub-tools are out
                    except OSError:
                        pass
        # Wait for the collect duration only; a sub-tool that runs 1-2s over
        # (e.g. memory) continues in the background and is reaped in stop().
        # Closing the parent handle is safe: the child keeps its own dup.
        deadline = time.monotonic() + self.rotation_intervals["devkit"]
        while time.monotonic() < deadline:
            if all(proc.poll() is not None for proc in procs.values()):
                break
            time.sleep(0.5)
        for tool, proc in procs.items():
            if proc.poll() is None:
                self._rotation_devkit_procs.append(proc)
            elif proc.returncode != 0:
                self.failed_runtime.append({"tool": tool, "returncode": proc.returncode})
        for fh in handles.values():
            try:
                fh.close()
            except Exception:
                pass

    def _rotation_ksys_turn(self) -> None:
        """One ksys slot: collect for one interval; the parse phase keeps running in background.

        ksys has two phases: collect (-d interval) then parse (can take minutes).
        The turn waits only for the collect phase (interval seconds) -- not
        process exit, because ksys block-buffers its stdout so the
        "Starting to parse data" marker is unreadable until the process
        flushes near exit. A timer is the robust signal. The parse process is
        tracked in _rotation_ksys_procs and reaped in stop()/wait(); its CPU
        overlaps later slots, the same trade-off the all-parallel mode and
        perf-split make (ksys cannot collect without parsing).
        """
        if not self.config.get("ksys_path") or not self.config.get("ksys_config_path"):
            self._rotation_skip_tool("ksys", "ksys_path/ksys_config_path not configured")
            return
        interval = str(self.rotation_intervals["ksys"])
        # -o <ksys_log_dir>/ : ksys writes its _report.json into this dir
        # so it lands alongside the per-turn .log files instead of the
        # log_capture root (cwd).  The dir must exist before ksys starts.
        ksys_dir = os.path.join(self.rotation_log_dir, "ksys")
        os.makedirs(ksys_dir, exist_ok=True)
        cmd = [
            self.config["ksys_path"],
            "collect",
            "-d",
            interval,
            "-i",
            "3",
            "-c",
            self.config["ksys_config_path"],
            "-o",
            ksys_dir + "/",
        ]
        log_path = self._rotation_log_path("ksys")
        if log_path is None:
            return
        try:
            fh = open(log_path, "w")
        except OSError as e:
            print(f"  [ERROR] rotation ksys cannot open log {log_path}: {e}")
            self._rotation_skip_tool("ksys", f"cannot open log: {e}")
            return
        print(f"  [CMD] ksys (rotation): {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=fh, cwd=self.rotation_log_dir)
        except OSError as e:
            # Cannot exec (bad path, no exec bit) -- skip for the rest of the
            # run instead of retrying every slot.
            self._rotation_skip_tool("ksys", f"failed to start: {e}")
            self.failed_runtime.append({"tool": "ksys", "error": str(e)})
            try:
                fh.close()
                os.unlink(log_path)
                os.rmdir(os.path.dirname(log_path))  # drop the empty ksys dir
            except OSError:
                pass
            return
        self.rotation_turns["ksys"].append(log_path)
        self._rotation_ksys_procs.append((proc, log_path))
        # Wait for the collect duration only (interval seconds); parse runs on
        # in the background. ksys's "Starting to parse data" marker is block-
        # buffered (the binary flushes on exit, not per-line), so marker
        # detection via the log file is unreliable -- a timer is the robust
        # signal.  Closing the parent handle is safe: the child keeps its own
        # dup and writes the parse output to the same file until it exits.
        collect_deadline = time.monotonic() + self.rotation_intervals["ksys"]
        while not self.rotation_stop_flag.is_set() and proc.poll() is None and time.monotonic() < collect_deadline:
            time.sleep(0.5)
        try:
            fh.close()
        except Exception:
            pass

    def _rotation_perf_turn(self) -> None:
        """One perf slot: ``perf stat -e <events> -a -- sleep <interval>`` into a per-turn log."""
        if "perf" in self._rotation_skipped:
            return
        cmd = [
            "perf",
            "stat",
            "-e",
            self.rotation_perf_events,
            "-a",
            "-I",
            "3000",  # 3s sampling interval (ms), matches devkit/ksys -i 3
            "--",
            "sleep",
            str(self.rotation_intervals["perf"]),
        ]
        log_path = self._rotation_log_path("perf")
        if log_path is None:
            return
        try:
            fh = open(log_path, "w")
        except OSError as e:
            print(f"  [ERROR] rotation perf cannot open log {log_path}: {e}")
            return
        print(f"  [CMD] perf (rotation): {' '.join(cmd)}")
        self.rotation_turns["perf"].append(log_path)
        try:
            # perf stat writes its counters to stderr; merge into the log.
            proc = subprocess.Popen(
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                cwd=self.rotation_log_dir,
            )
        except OSError as e:
            # perf binary missing/unrunnable -- no point retrying every turn.
            self._rotation_skip_tool("perf", f"perf not runnable: {e}")
            if self.rotation_turns["perf"] and self.rotation_turns["perf"][-1] == log_path:
                self.rotation_turns["perf"].pop()
            try:
                fh.close()
            except Exception:
                pass
            try:
                os.unlink(log_path)  # drop the empty log from the failed turn
            except OSError:
                pass
            try:
                os.rmdir(os.path.dirname(log_path))  # drop the empty perf dir too
            except OSError:
                pass
            return
        # Wait for the collect duration only (interval seconds); a perf
        # process that runs over (startup overhead, system load) continues
        # in the background and is reaped in stop().  Closing the parent
        # handle is safe: the child keeps its own dup.
        deadline = time.monotonic() + self.rotation_intervals["perf"]
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is None:
            self._rotation_perf_procs.append(proc)
        elif proc.returncode != 0:
            self.failed_runtime.append({"tool": "perf", "returncode": proc.returncode})
        try:
            fh.close()
        except Exception:
            pass

    def _run_slot(self, turn, interval=None) -> None:
        """Run one rotation slot, then hold the slot open for its full interval.

        A slot is a fixed window: a tool that finishes early (fast exit, bad
        args, start failure) must not collapse the cadence into a hot spin of
        empty logs. The remainder is waited out interruptibly -- stop() or the
        end of stress breaks the pace immediately.
        """
        if interval is None:
            interval = self.rotation_interval
        t0 = time.monotonic()
        turn()
        while not self.rotation_stop_flag.is_set() and self._stress_active() and time.monotonic() - t0 < interval:
            self.rotation_stop_flag.wait(timeout=0.5)

    def _rotation_thread_main(self) -> None:
        """devkit(topdown+memory parallel) -> ksys -> perf, repeating while stress runs."""
        # Lock mode: idle until the stress lock appears (or stop/-t). Timer
        # mode (no stress_file): start immediately, bounded by `duration`.
        if self.stress_file and not self._wait_for_stress_lock():
            return
        t0 = time.monotonic()
        while not self.rotation_stop_flag.is_set() and self._stress_active() and time.monotonic() - t0 < self.duration:
            if {"devkit", "ksys", "perf"} <= self._rotation_skipped:
                print("  [WARN] rotation: no runnable tools left, rotation stopped")
                return
            # Stop between slots once stress ends; an in-flight tool finishes
            # its own bounded -d interval slot first. An unexpected exception
            # stops the rotation (one message) -- it must never kill the bench.
            try:
                if "devkit" not in self._rotation_skipped:
                    self._run_slot(self._rotation_devkit_turn, self.rotation_intervals["devkit"])
                if self.rotation_stop_flag.is_set() or not self._stress_active():
                    break
                if "ksys" not in self._rotation_skipped:
                    self._run_slot(self._rotation_ksys_turn, self.rotation_intervals["ksys"])
                if self.rotation_stop_flag.is_set() or not self._stress_active():
                    break
                if "perf" not in self._rotation_skipped:
                    self._run_slot(self._rotation_perf_turn, self.rotation_intervals["perf"])
            except Exception as e:  # noqa: BLE001 -- rotation must never kill the bench
                print(f"  [ERROR] rotation turn failed, stopping rotation: {e}")
                return
        print("  [OK] log rotation finished")

    def start(self) -> dict:
        """Start all collection processes in parallel using Popen

        Returns:
            {'success': [tool_names], 'failed': [(tool_name, error_msg)]}
        """
        self.start_time = datetime.now()
        success = []
        failed = []
        # Rotation mode moves devkit/ksys out of the parallel set (they rotate
        # on the rotation thread instead); ub_watch/smap_bw/getfre are unchanged.
        rotate = self.log_rotation

        # Start DevKit memory tuner
        if not rotate and "devkit_mem" not in self.disabled_devkit:
            ok, err = self._start_devkit_mem()
            if ok:
                success.append("devkit_mem")
            elif err and "not configured" not in err:
                failed.append(("devkit_mem", err))
                self.failed_startup.append("devkit_mem")

        # Start DevKit top-down tuner
        if not rotate and "devkit_top_down" not in self.disabled_devkit:
            ok, err = self._start_devkit_top_down()
            if ok:
                success.append("devkit_top_down")
            elif err and "not configured" not in err:
                failed.append(("devkit_top_down", err))
                self.failed_startup.append("devkit_top_down")

        # Start ksys
        if not rotate:
            ok, err = self._start_ksys()
            if ok:
                success.append("ksys")
            elif err and "not configured" not in err:
                failed.append(("ksys", err))
                self.failed_startup.append("ksys")

        # Start ub_watch
        ok, err = self._start_ub_watch()
        if ok:
            success.append("ub_watch")
        elif err and "not configured" not in err:
            failed.append(("ub_watch", err))
            self.failed_startup.append("ub_watch")

        # Start smap_bw
        ok, err = self._start_smap_bw()
        if ok:
            success.append("smap_bw")
        elif err and "not configured" not in err:
            failed.append(("smap_bw", err))
            self.failed_startup.append("smap_bw")

        # Start getfre core frequency collector
        ok, err = self._start_getfre()
        if ok:
            success.append("getfre")
        elif err and "not configured" not in err:
            failed.append(("getfre", err))
            self.failed_startup.append("getfre")

        # Start the rotation thread (devkit -> ksys -> perf, one slot at a time).
        # It idles until the stress lock appears, so starting it here is safe.
        if rotate:
            self.rotation_thread = threading.Thread(target=self._rotation_thread_main, name="log-rotation")
            self.rotation_thread.start()
            success.append("log_rotation")

        return {"success": success, "failed": failed}

    def stop(self):
        """Stop all running processes and threads"""
        # Rotation mode: signal the loop first so it stops between slots (an
        # in-flight tool finishes its own bounded -d interval slot), then reap
        # any ksys parse still running in the background (emergency path: a
        # kill here loses that turn's parse output, same as the parallel mode).
        self.rotation_stop_flag.set()
        if self.rotation_thread is not None and self.rotation_thread.is_alive():
            self.rotation_thread.join(timeout=self.rotation_interval + 90)
        for proc in self._rotation_devkit_procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        for proc in self._rotation_perf_procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        for proc, log_path in self._rotation_ksys_procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            # Rename ksys-generated *_report.json to match the .log
            # filename (same timestamp, .json extension) so all files in
            # the ksys/ dir share one naming scheme.
            ksys_dir = os.path.dirname(log_path)
            base = os.path.basename(log_path).removesuffix(".log")
            try:
                for f in os.listdir(ksys_dir):
                    if f.endswith("_report.json"):
                        src = os.path.join(ksys_dir, f)
                        dst = os.path.join(ksys_dir, base + "_report.json")
                        if src != dst:
                            os.rename(src, dst)
            except OSError:
                pass

        # Stop getfre threads first
        for numa_id, stop_flag in self.getfre_stop_flags.items():
            stop_flag.set()  # Signal threads to stop

        # Wait for getfre threads to finish
        for numa_id, thread in self.getfre_threads.items():
            thread.join(timeout=5)

        # Close getfre log files
        for numa_id, f in self.getfre_log_files.items():
            try:
                f.close()
            except Exception:
                pass

        # Stop other processes
        for tool_name, proc in self.processes.items():
            if proc.poll() is None:  # Still running
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                except Exception:
                    pass

        # Close log file handles
        for tool_name, f in self.log_files.items():
            try:
                f.close()
            except Exception:
                pass

    def _check_ksys_parse_progress(self) -> str:
        """Check ksys.log for parse progress

        Returns:
            'completed' - parse finished, data output started
            'parsing' - currently parsing data
            'collecting' - still collecting data
            'unknown' - cannot determine status
        """
        ksys_log_path = os.path.join(self.log_dir, "ksys.log")
        if not os.path.exists(ksys_log_path):
            return "unknown"

        try:
            with open(ksys_log_path, encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Check parse completion markers
            if "Starting to process and print data" in content:
                return "completed"
            if "CPU Metrics" in content or "Common Microarchitecture Metrics" in content:
                return "completed"
            if "Data saved successfully" in content:
                return "completed"

            # Check if parsing started
            if "Starting to parse data" in content:
                return "parsing"

            # Still in collection phase
            if "Starting to collect data" in content:
                return "collecting"

            return "unknown"
        except Exception:
            return "unknown"

    def _wait_for_ksys(self, proc) -> dict:
        """Wait for ksys with extended timeout for parse phase

        ksys has two phases:
        1. Collect phase: duration seconds (data collection)
        2. Parse phase: can take minutes for large data

        Returns:
            {'status': 'completed'|'timeout', 'returncode': int, 'elapsed': int}
        """
        tool_name = "ksys"
        start_time = time.time()

        # Phase 1: Wait for collection to complete (duration + buffer)
        collect_timeout = self.duration + 30
        print(f"  [{tool_name}] Waiting for collection phase ({self.duration}s)...")

        try:
            proc.wait(timeout=collect_timeout)
            elapsed = int(time.time() - start_time)
            if proc.returncode == 0:
                print(f"  [OK] {tool_name} completed in {elapsed}s")
                return {"status": "completed", "returncode": 0, "elapsed": elapsed}
            else:
                print(f"  [WARN] {tool_name} exited with code {proc.returncode} after {elapsed}s")
                return {"status": "completed", "returncode": proc.returncode, "elapsed": elapsed}
        except subprocess.TimeoutExpired:
            pass  # Continue to parse phase wait

        # Phase 2: Wait for parse phase with extended timeout
        parse_timeout = self.ksys_parse_timeout
        total_timeout = collect_timeout + parse_timeout
        print(f"  [{tool_name}] Collection done, waiting for parse phase (timeout={parse_timeout}s)...")

        last_status = "collecting"
        warning_thresholds = [0.5, 0.75, 0.9]
        warnings_given = [False, False, False]

        while time.time() - start_time < total_timeout:
            # Check if process completed
            try:
                proc.wait(timeout=5)
                elapsed = int(time.time() - start_time)
                if proc.returncode == 0:
                    print(f"  [OK] {tool_name} parse completed, total {elapsed}s")
                    return {"status": "completed", "returncode": 0, "elapsed": elapsed}
                else:
                    print(f"  [WARN] {tool_name} exited with code {proc.returncode} after {elapsed}s")
                    return {"status": "completed", "returncode": proc.returncode, "elapsed": elapsed}
            except subprocess.TimeoutExpired:
                pass

            # Check parse progress
            current_status = self._check_ksys_parse_progress()
            elapsed = int(time.time() - start_time)
            elapsed_parse = elapsed - collect_timeout

            if current_status != last_status:
                last_status = current_status
                if current_status == "parsing":
                    print(f"  [{tool_name}] Parse phase started ({elapsed}s total)")
                elif current_status == "completed":
                    print(f"  [{tool_name}] Parse completed ({elapsed}s total)")

            # Progress logging every 30s
            if elapsed_parse > 0 and elapsed_parse % 30 == 0:
                progress_ratio = elapsed_parse / parse_timeout
                print(
                    f"  [{tool_name}] Parse in progress... {elapsed_parse}s/{parse_timeout}s ({progress_ratio * 100:.0f}%)"
                )

            # Warnings at thresholds
            elapsed_ratio = elapsed_parse / parse_timeout
            for i, threshold in enumerate(warning_thresholds):
                if elapsed_ratio >= threshold and not warnings_given[i]:
                    warnings_given[i] = True
                    remaining = int(parse_timeout - elapsed_parse)
                    print(
                        f"  [WARN] [{tool_name}] Approaching parse timeout ({threshold * 100:.0f}% used), {remaining}s remaining"
                    )

            time.sleep(5)

        # Timeout - force terminate
        elapsed = int(time.time() - start_time)
        print(f"  [WARN] [{tool_name}] Parse timeout after {elapsed}s total, terminating...")
        proc.terminate()
        time.sleep(3)
        try:
            proc.kill()
        except ProcessLookupError:
            pass

        print(f"  [WARN] {tool_name} timeout suggestions:")
        print(f"      - Increase ksys_parse_timeout (current: {parse_timeout}s)")
        print("      - Check ksys.log size and parse progress")
        print("      - Reduce monitor sampling interval for less data")

        return {"status": "timeout", "returncode": None, "elapsed": elapsed}

    def wait(self):
        """Wait for all processes to complete, track failures

        Uses different timeouts for different tools:
        - devkit/ub_watch: duration + 60s (quick completion expected)
        - ksys: duration + ksys_parse_timeout (parse can take minutes)
        """
        # Rotation mode: the thread exits on stop flag / stress end once its
        # in-flight slot finishes; give it that slot's budget before moving on.
        if self.log_rotation and self.rotation_thread is not None:
            self.rotation_thread.join(timeout=self.rotation_interval + 120)
        for tool_name, proc in self.processes.items():
            try:
                # Get appropriate timeout for this tool
                if tool_name == "ksys":
                    # ksys has special handling with parse phase monitoring
                    result = self._wait_for_ksys(proc)
                    if result["status"] == "timeout":
                        self.failed_runtime.append(
                            {
                                "tool": tool_name,
                                "error": "parse_timeout",
                                "elapsed": result["elapsed"],
                            }
                        )
                    elif result["returncode"] != 0:
                        self.failed_runtime.append(
                            {
                                "tool": tool_name,
                                "returncode": result["returncode"],
                            }
                        )
                else:
                    # Other tools: simple timeout
                    timeout = self.duration + self.DEFAULT_TOOL_TIMEOUTS.get(tool_name, 60)
                    proc.wait(timeout=timeout)
                    if proc.returncode != 0:
                        self.failed_runtime.append(
                            {
                                "tool": tool_name,
                                "returncode": proc.returncode,
                            }
                        )
            except subprocess.TimeoutExpired:
                # Process didn't finish within timeout, force terminate
                timeout_used = self.duration + self.DEFAULT_TOOL_TIMEOUTS.get(tool_name, 60)
                print(f"[WARN] {tool_name} timed out after {timeout_used}s, terminating...")
                proc.terminate()
                time.sleep(3)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                self.failed_runtime.append(
                    {
                        "tool": tool_name,
                        "error": "timeout",
                    }
                )
            except Exception as e:
                self.failed_runtime.append(
                    {
                        "tool": tool_name,
                        "error": str(e),
                    }
                )

        # Rotation-mode ksys processes: collect already finished, only the
        # parse phase may still be running. Bounded by ksys_parse_timeout,
        # then terminated (a straggler's parse output is lost past that).
        for proc in self._rotation_ksys_procs:
            if proc.poll() is not None:
                continue
            deadline = time.time() + self.ksys_parse_timeout
            while proc.poll() is None and time.time() < deadline:
                time.sleep(5)
            if proc.poll() is None:
                print(f"  [WARN] rotation ksys parse exceeded {self.ksys_parse_timeout}s, terminating...")
                proc.terminate()
                time.sleep(3)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                self.failed_runtime.append({"tool": "ksys", "error": "parse_timeout"})
            elif proc.returncode != 0:
                self.failed_runtime.append({"tool": "ksys", "returncode": proc.returncode})

        # Close log file handles
        for tool_name, f in self.log_files.items():
            try:
                f.close()
            except Exception:
                pass

    def get_results(self) -> dict:
        """Return collection results and status

        Returns:
            {
                'success': [tool_names],
                'failed_startup': [tool_names],
                'failed_runtime': [{tool, returncode/error}],
                'log_files': {tool_name: file_path},
                'duration': actual_duration_seconds
            }
        """
        # Calculate actual duration
        actual_duration = 0
        if self.start_time:
            actual_duration = int((datetime.now() - self.start_time).total_seconds())

        # Determine successful tools
        all_tools = list(self.processes.keys())
        success = [
            t for t in all_tools if t not in self.failed_startup and t not in [f["tool"] for f in self.failed_runtime]
        ]

        # Add getfre if threads started successfully
        if self.getfre_threads and "getfre" not in self.failed_startup:
            success.append("getfre")

        # Rotation mode: the rotated tools never enter self.processes; report
        # the thread as the carrier of devkit/ksys/perf instead.
        if self.log_rotation and self.rotation_thread is not None:
            success.append("log_rotation")

        # Build getfre log files dict
        getfre_logs = {}
        for numa_id in self.getfre_log_files.keys():
            getfre_logs[f"getfre_NUMA{numa_id}"] = os.path.join(self.log_dir, f"getfre_NUMA{numa_id}.log")

        log_files = {
            "devkit_mem": os.path.join(self.log_dir, "devkit_mem.log"),
            "devkit_top_down": os.path.join(self.log_dir, "devkit_top_down.log"),
            "ksys": os.path.join(self.log_dir, "ksys.log"),
            "ub_watch": os.path.join(self.log_dir, "ub_watch.log"),
            "smap_bw": os.path.join(self.log_dir, "smap_bw.log"),
            **getfre_logs,  # Add getfre log files
        }
        if self.log_rotation:
            # devkit/ksys write per-turn timestamped logs instead of the single
            # files the exporters look for; expose them as rotation_files and
            # drop the nonexistent single-file keys.
            for tool in ("devkit_mem", "devkit_top_down", "ksys"):
                log_files.pop(tool, None)

        results = {
            "success": success,
            "failed_startup": self.failed_startup,
            "failed_runtime": self.failed_runtime,
            "log_files": log_files,
            "duration": actual_duration,
        }
        if self.log_rotation:
            results["rotation_files"] = {tool: list(paths) for tool, paths in self.rotation_turns.items()}
        return results
