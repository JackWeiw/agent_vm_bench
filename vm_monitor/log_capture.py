# vm_monitor/log_capture.py
"""
Parallel Log Collection Module

Runs multiple log collection tools (devkit, ksys, ub_watch, smap_bw, getfre)
in parallel subprocesses and threads, synchronized with QEMU monitoring duration.
"""

import os
import re
import subprocess
import threading
import time
from datetime import datetime

# Internal dependencies
from .config import calculate_cpu_range_from_numa, load_getfre_config, numa_to_physical_cores


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

    def __init__(self, config: dict, duration: int, log_dir: str, numa_nodes: list, ksys_parse_timeout: int = None):
        """
        Args:
            config: paths from .env (devkit_path, ksys_path, ksys_config_path, ub_watch_path, devkit_cpu_range, getfre_path, getfre_config_path)
            duration: collection duration in seconds (same as qemu_monitor -t)
            log_dir: output directory for log files
            numa_nodes: list of NUMA nodes to monitor (for CPU range calculation)
            ksys_parse_timeout: extra timeout for ksys parse phase (default 600s)
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
        # getfre threading components
        self.getfre_threads = {}  # {numa_id: Thread}
        self.getfre_log_files = {}  # {numa_id: file handle}
        self.getfre_stop_flags = {}  # {numa_id: Event}

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

        total_cores = getfre_config.get("total_cores", 192)
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
            total_cores: total physical cores (192)
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

    def start(self) -> dict:
        """Start all collection processes in parallel using Popen

        Returns:
            {'success': [tool_names], 'failed': [(tool_name, error_msg)]}
        """
        self.start_time = datetime.now()
        success = []
        failed = []

        # Start DevKit memory tuner
        ok, err = self._start_devkit_mem()
        if ok:
            success.append("devkit_mem")
        elif err and "not configured" not in err:
            failed.append(("devkit_mem", err))
            self.failed_startup.append("devkit_mem")

        # Start DevKit top-down tuner
        ok, err = self._start_devkit_top_down()
        if ok:
            success.append("devkit_top_down")
        elif err and "not configured" not in err:
            failed.append(("devkit_top_down", err))
            self.failed_startup.append("devkit_top_down")

        # Start ksys
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

        return {"success": success, "failed": failed}

    def stop(self):
        """Stop all running processes and threads"""
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

        # Build getfre log files dict
        getfre_logs = {}
        for numa_id in self.getfre_log_files.keys():
            getfre_logs[f"getfre_NUMA{numa_id}"] = os.path.join(self.log_dir, f"getfre_NUMA{numa_id}.log")

        return {
            "success": success,
            "failed_startup": self.failed_startup,
            "failed_runtime": self.failed_runtime,
            "log_files": {
                "devkit_mem": os.path.join(self.log_dir, "devkit_mem.log"),
                "devkit_top_down": os.path.join(self.log_dir, "devkit_top_down.log"),
                "ksys": os.path.join(self.log_dir, "ksys.log"),
                "ub_watch": os.path.join(self.log_dir, "ub_watch.log"),
                "smap_bw": os.path.join(self.log_dir, "smap_bw.log"),
                **getfre_logs,  # Add getfre log files
            },
            "duration": actual_duration,
        }
