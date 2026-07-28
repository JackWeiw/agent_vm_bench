"""
Coding Task Execution Module

Responsible for coding task execution inside E2B sandboxes, analogous to
task_runner.py's browser workflow. Each sandbox has an independent thread.

Simulates real AI coding agent workflow:
  Step 0: Setup — start dev server (persistent live preview, ~1.5GB baseline)
  Step 1: Checkout — reset source files to clean state (git checkout)
  Step 2: Edit — inject round marker into target file (triggers rebuild)
  Step 3: Build — production build (overlaps with dev server, ~3GB peak)
  Step 4: Test — run test suite (verify correctness)

Classes:
- CodingWarmupRunner: Starts dev server + initial build during warmup phase
- CodingTaskRunner: Executes coding tasks in fixed mode (continuous loop)
- CodingRoundRunner: Executes one round of coding operations in round-robin mode
"""

import random
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import Config
from .helpers import wait_for_port_ready
from .schemas import SandboxState, SandboxStatus


def _check_dev_server_running(sbx, project_dir: str) -> bool:
    """Check if the dev server is actually running inside the sandbox.

    Uses `ps aux | grep` instead of `pgrep -f` because pgrep -f can
    false-match its own process when executed via E2B commands.run().
    E2B's command execution mechanism may create a process whose /proc/PID/cmdline
    contains the full command string, causing pgrep -f to match itself.

    The grep pattern filters out common false-positive sources:
    - grep itself (ps aux | grep includes the grep command)
    - pgrep (if any pgrep process happens to be running)
    - sh -c (E2B commands.run() may use sh -c to execute, and its cmdline
      could contain the grep pattern string)

    Returns True if a dev server process (npm run dev / umi dev / max dev)
    is found running, False otherwise.
    """
    try:
        result = sbx.commands.run(
            # Match processes containing 'npm run dev', 'umi dev', or 'max dev'
            # Exclude grep/pgrep/sh processes to avoid false-positive from this command itself
            f"ps aux | grep -E 'npm run dev|umi dev|max dev'" f" | grep -v grep | grep -v pgrep | grep -v 'sh -c'",
            timeout=10,
            user="root",
        )
        # If there's output (matching lines) and exit_code=0, dev server is running
        return result.exit_code == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def _start_dev_server(sbx, project_dir: str, dev_cmd: str, dev_wait: int, sandbox_id: int) -> bool:
    """Start the dev server as a background process inside the sandbox.

    Uses E2B SDK's background=True parameter instead of shell '&' to ensure
    the dev server process persists after the commands.run() session ends.
    Shell '&' backgrounding is unreliable in E2B because commands.run() may
    clean up the entire shell session (including background children) when
    it returns, killing the dev server.

    Returns True if dev server appears to be running after startup, False otherwise.
    """
    try:
        # Use E2B's background=True to start dev server as a persistent process
        # This is the officially recommended way for long-running background processes
        print(f"[Sandbox{sandbox_id}] Starting dev server via background=True...")
        sbx.commands.run(
            f"cd {project_dir} && BROWSER=none {dev_cmd}",
            timeout=60,  # Give enough time for initial npm process spawn
            background=True,
            user="root",
        )

        # Wait for dev server startup
        print(f"[Sandbox{sandbox_id}] Waiting {dev_wait}s for dev server...")
        time.sleep(dev_wait)

        # Verify dev server started (use robust check)
        if _check_dev_server_running(sbx, project_dir):
            print(f"[Sandbox{sandbox_id}] Dev server: ready")
            return True
        else:
            print(f"[Sandbox{sandbox_id}] WARNING: Dev server may not be ready yet")
            return False
    except Exception as e:
        # Fallback: if background=True is not supported by this E2B SDK version,
        # try nohup+disown approach
        print(f"[Sandbox{sandbox_id}] background=True failed: {e}, trying nohup+disown fallback...")
        try:
            sbx.commands.run(
                f"cd {project_dir} && nohup bash -c 'BROWSER=none {dev_cmd}'" f" > /tmp/dev_server.log 2>&1 & disown",
                timeout=15,
                user="root",
            )
            print(f"[Sandbox{sandbox_id}] Waiting {dev_wait}s for dev server...")
            time.sleep(dev_wait)

            if _check_dev_server_running(sbx, project_dir):
                print(f"[Sandbox{sandbox_id}] Dev server: ready (nohup)")
                return True
            else:
                print(f"[Sandbox{sandbox_id}] WARNING: Dev server may not be ready yet (nohup)")
                return False
        except Exception as e2:
            print(f"[Sandbox{sandbox_id}] Dev server start failed (both methods): {e2}")
            return False


class CodingWarmupRunner(threading.Thread):
    """Warmup phase runner for coding workflow — starts dev server and initial build

    The dev server provides a persistent ~1.5GB memory baseline, simulating
    real coding agent environments (Devin, OpenHands, Claude Code all start
    npm run dev for live preview when working on web apps).

    After dev server startup, runs an initial build to verify project health
    and establish webpack cache.
    """

    def __init__(
        self,
        state: SandboxState,
        config: Config,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config

    def run(self) -> None:
        """Execute warmup phase for this sandbox — start dev server + initial build"""
        # Wait for sandbox ports ready
        if not wait_for_port_ready(self.state):
            print(f"[Sandbox{self.state.sandbox_id}] Cannot start warmup: {self.state.creation_metrics.status.value}")
            return

        sbx = self.state.sandbox_obj
        if not sbx:
            print(f"[Sandbox{self.state.sandbox_id}] No sandbox handle for warmup")
            self.state.warmup_done = True
            return

        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, "sandbox_id") else "N/A"
        project_dir = self.config.coding_project_dir

        # 1. Verify project exists
        try:
            result = sbx.commands.run(f"ls {project_dir}/package.json", timeout=30, user="root")
            if result.exit_code != 0:
                print(f"[Sandbox{self.state.sandbox_id}] Project not found at {project_dir}, skipping warmup")
                self.state.warmup_done = True
                return
        except Exception as e:
            print(f"[Sandbox{self.state.sandbox_id}] Failed to verify project: {e}")
            self.state.warmup_done = True
            return

        # 2. Reset source files
        try:
            sbx.commands.run(f"cd {project_dir} && git checkout -- src/", timeout=30, user="root")
        except Exception:
            print(f"[Sandbox{self.state.sandbox_id}] git checkout failed (may not be a git repo)")

        # 3. Start dev server (if not skipped)
        if not self.config.coding_skip_dev_server:
            dev_server_running = _check_dev_server_running(sbx, project_dir)
            if dev_server_running:
                print(f"[Sandbox{self.state.sandbox_id}] Dev server: already running")
            else:
                _start_dev_server(
                    sbx,
                    project_dir,
                    self.config.coding_dev_cmd,
                    self.config.coding_dev_wait,
                    self.state.sandbox_id,
                )

        # 4. Run initial build (if not skipped)
        if not self.config.coding_skip_build:
            try:
                print(f"[Sandbox{self.state.sandbox_id}] Running initial build...")
                build_result = sbx.commands.run(
                    f"cd {project_dir} && {self.config.coding_build_cmd}",
                    timeout=self.config.coding_build_timeout,
                    user="root",
                )
                if build_result.exit_code != 0:
                    print(f"[Sandbox{self.state.sandbox_id}] Initial build failed: exit_code={build_result.exit_code}")
                else:
                    print(f"[Sandbox{self.state.sandbox_id}] Initial build: success")
            except Exception as e:
                print(f"[Sandbox{self.state.sandbox_id}] Initial build exception: {e}")

        # Mark warmup complete
        self.state.warmup_done = True
        print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Coding warmup completed")


class CodingTaskRunner(threading.Thread):
    """Coding task runner for fixed mode — one independent thread per sandbox

    Each iteration: checkout → edit → build → test → memory collection.
    Runs continuously until stop_event is set or sandbox goes offline.
    """

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.consecutive_errors = 0

    def run(self) -> None:
        """Task execution main loop"""
        # Wait for sandbox ports ready
        if not wait_for_port_ready(self.state, self.stop_event):
            print(f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}")
            return

        # Coding task execution loop
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break

            # Execute single coding task
            success, latency, build_success, test_success, timed_out = self._run_single_task()

            # Update metrics — timeout is determined by exception handling, not elapsed time
            self.state.coding_metrics.add(
                latency, success and not timed_out, timed_out, build_success=build_success, test_success=test_success
            )
            self.state.update_last_task_time(time.time())

            # Error handling
            if success and not timed_out:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    print(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break

            # Random interval to avoid request spike
            sleep_time = random.uniform(self.config.coding_interval_min, self.config.coding_interval_max)
            time.sleep(sleep_time)

        print(f"[Sandbox{self.state.sandbox_id}] Coding task runner ended")

    def _run_single_task(self) -> Tuple[bool, float, bool, bool, bool]:
        """Execute single coding task cycle

        Returns: (success, latency_seconds, build_success, test_success, timed_out)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0, False, False, False

        project_dir = self.config.coding_project_dir
        source_files = self.config.coding_source_files

        # Pick target file for this round
        if not source_files:
            return False, 0.0, False, False, False

        file_idx = self.state.coding_metrics.total_tasks % len(source_files)
        target_file = source_files[file_idx]

        start_time = time.perf_counter()
        build_success = False
        test_success = False
        timed_out = False

        try:
            # Step 0: Ensure dev server is running (for memory pressure overlap)
            if not self.config.coding_skip_dev_server:
                dev_running = _check_dev_server_running(sbx, project_dir)
                if not dev_running:
                    print(f"[Sandbox{self.state.sandbox_id}] Dev server not running, restarting...")
                    _start_dev_server(
                        sbx,
                        project_dir,
                        self.config.coding_dev_cmd,
                        self.config.coding_dev_wait,
                        self.state.sandbox_id,
                    )

            # Step 1: Checkout — reset source files
            sbx.commands.run(f"cd {project_dir} && git checkout -- src/", timeout=30, user="root")

            # Step 2: Edit — inject round marker into target file
            round_id = self.state.coding_metrics.total_tasks
            edit_result = sbx.commands.run(
                f"cd {project_dir} && sed -i '1i// Bench Round {round_id}' {target_file}",
                timeout=15,
                user="root",
            )
            if edit_result.exit_code != 0:
                # Fallback to config file if target file doesn't exist
                sbx.commands.run(
                    f"cd {project_dir} && sed -i '1i// Bench Round {round_id}' config/config.ts",
                    timeout=15,
                    user="root",
                )

            # Step 3: Build — production build (memory-intensive)
            if not self.config.coding_skip_build:
                # Clean build output and webpack cache to force full recompilation
                sbx.commands.run(
                    f"cd {project_dir} && rm -rf dist/ node_modules/.cache/",
                    timeout=15,
                    user="root",
                )
                build_result = sbx.commands.run(
                    f"cd {project_dir} && {self.config.coding_build_cmd}",
                    timeout=self.config.coding_build_timeout,
                    user="root",
                )
                build_success = build_result.exit_code == 0
                if not build_success:
                    error_detail = f"build failed: exit_code={build_result.exit_code}"
                    if build_result.stderr:
                        error_detail += f", stderr={build_result.stderr[:200]}"
                    self.state.coding_metrics.last_error = error_detail

            # Step 4: Test — run test suite
            if not self.config.coding_skip_test and build_success:
                test_result = sbx.commands.run(
                    f"cd {project_dir} && {self.config.coding_test_cmd}",
                    timeout=self.config.coding_test_timeout,
                    user="root",
                )
                test_success = test_result.exit_code == 0

            elapsed = time.perf_counter() - start_time
            # Bug #1 fix: skip_build=True means build_success should be True (skipped = not failed)
            # success = (skip_build or build_success) and (skip_test or test_success)
            success = (self.config.coding_skip_build or build_success) and (
                self.config.coding_skip_test or test_success
            )

            return success, elapsed, build_success, test_success, timed_out

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            # Bug #2 fix: timeout is determined by exception, not elapsed time
            timed_out = "timed out" in error_msg.lower() or "context deadline exceeded" in error_msg.lower()
            self.state.coding_metrics.last_error = error_msg
            print(f"[Sandbox{self.state.sandbox_id}] Coding task exception: {error_msg[:100]}")
            return False, elapsed, build_success, test_success, timed_out


class CodingRoundRunner(threading.Thread):
    """Runner for coding operations in round-robin benchmark mode

    Each round modifies a different source file, then builds and tests.
    This creates genuine memory pressure because each modification triggers
    webpack to re-compile affected modules, and the build step peaks at ~2GB.

    The dev server runs persistently (~1.5GB), so when build peaks overlap
    with the running dev server, total sandbox memory reaches ~3GB.

    Steps per round (with individual timing):
      0. ensure_dev — verify dev server is running (restart if crashed)
      1. checkout   — git checkout -- src/ (reset to clean state)
      2. edit       — sed inject round marker into target file
      3. build      — npm run build (MEMORY PEAK, overlaps with dev server)
      4. test       — npm test (verify correctness)

    Attributes:
        state: Sandbox state for metrics
        config: Test configuration
        stop_event: Global stop event
        round_id: Current round number
    """

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
        round_id: int,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.round_id = round_id
        self.consecutive_errors = 0

    def run(self) -> None:
        """Execute coding operations for this round"""
        sbx = self.state.sandbox_obj
        if not sbx:
            print(f"[Sandbox{self.state.sandbox_id}] No sandbox handle for coding round")
            return

        source_files = self.config.coding_source_files
        if not source_files:
            print(f"[Sandbox{self.state.sandbox_id}] No coding source files configured")
            return

        # Pick target file for this round (round-robin through source files)
        file_idx = self.round_id % len(source_files)
        target_file = source_files[file_idx]

        start_time = time.perf_counter()

        success, step_times, build_success, test_success, failed_step, error_detail, timed_out = self._execute_steps(
            sbx, target_file
        )

        elapsed = self._record_metrics(
            start_time, success, step_times, build_success, test_success, timed_out, error_detail
        )

        if success:
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            print(f"[Sandbox{self.state.sandbox_id}] Coding round completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(target_file, failed_step, error_detail)

    def _execute_steps(self, sbx, target_file: str) -> Tuple[bool, Dict[str, float], bool, bool, str, str, bool]:
        """Execute all steps: ensure_dev_server -> checkout -> edit -> build -> test

        Args:
            sbx: Sandbox object
            target_file: Source file path relative to project_dir

        Returns:
            Tuple of (success, step_times, build_success, test_success, failed_step, error_detail, timed_out)
        """
        success = True
        step_times = {}
        failed_step = None
        error_detail = ""
        build_success = False
        test_success = False
        timed_out = False
        project_dir = self.config.coding_project_dir

        try:
            # Step 0: Ensure dev server is running (persistent ~1.5GB baseline)
            # The dev server must be running for the build+dev overlap that creates
            # ~3GB memory pressure. Without it, peak is only ~2GB.
            if not self.config.coding_skip_dev_server:
                self._step_ensure_dev_server(sbx, project_dir, step_times)

            # Step 1: Checkout — reset source files to clean state
            checkout_success, checkout_error = self._step_checkout(sbx, project_dir, step_times)
            if not checkout_success:
                # Checkout failure is not fatal (sandbox may not be a git repo)
                # Log warning but continue
                print(f"[Sandbox{self.state.sandbox_id}] checkout warning: {checkout_error}")

            # Step 2: Edit — inject round marker into target file
            edit_success, edit_error = self._step_edit(sbx, project_dir, target_file, step_times)
            if not edit_success:
                failed_step = "edit"
                error_detail = edit_error
                success = False
                return success, step_times, build_success, test_success, failed_step, error_detail, timed_out

            # Step 3: Build — production build (memory-intensive)
            # Bug #4 fix: respect coding_skip_build flag, skipped = success
            if not self.config.coding_skip_build:
                build_success, build_error = self._step_build(sbx, project_dir, step_times)
                if not build_success:
                    failed_step = "build"
                    error_detail = build_error
                    success = False
                    # Still continue to collect memory metrics
            else:
                build_success = True  # skipped = not failed

            # Step 4: Test — run test suite (only if build succeeded or skipped)
            # Bug #4 fix: respect coding_skip_test flag, skipped = success
            if not self.config.coding_skip_test and build_success:
                test_success, test_error = self._step_test(sbx, project_dir, step_times)
                # Test failure is non-fatal for overall round success
            elif self.config.coding_skip_test:
                test_success = True  # skipped = not failed

        except Exception as e:
            success = False
            timed_out = "timed out" in str(e).lower() or "context deadline exceeded" in str(e).lower()
            failed_step, error_detail = self._classify_exception(e, step_times)

        return success, step_times, build_success, test_success, failed_step, error_detail, timed_out

    def _step_ensure_dev_server(self, sbx, project_dir: str, step_times: Dict[str, float]) -> None:
        """Step 0: Ensure dev server is running for memory pressure overlap.

        The dev server (~1.5GB) must overlap with the build step (~2GB) to
        create ~3GB memory pressure. Without the dev server, peak is only ~2GB.

        Non-fatal: if dev server cannot be started, the round continues without
        memory overlap (build will still run, just at lower peak).
        """
        step_start = time.perf_counter()
        dev_server_running = _check_dev_server_running(sbx, project_dir)
        check_elapsed = time.perf_counter() - step_start

        if dev_server_running:
            step_times["ensure_dev"] = check_elapsed
            return  # Already running, no action needed

        # Dev server not running — restart it
        print(f"[Sandbox{self.state.sandbox_id}] Dev server not running, restarting...")
        _start_dev_server(
            sbx,
            project_dir,
            self.config.coding_dev_cmd,
            self.config.coding_dev_wait,
            self.state.sandbox_id,
        )
        step_times["ensure_dev"] = time.perf_counter() - step_start

    def _step_checkout(self, sbx, project_dir: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 1: Reset source files via git checkout

        Returns: (success, error_detail) — checkout failure is non-fatal
        """
        step_start = time.perf_counter()
        result = sbx.commands.run(f"cd {project_dir} && git checkout -- src/", timeout=30, user="root")
        step_times["checkout"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            error_parts = [f"checkout warning: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            return False, " | ".join(error_parts)
        return True, ""

    def _step_edit(self, sbx, project_dir: str, target_file: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 2: Inject round marker into target file via sed

        Returns: (success, error_detail)
        """
        step_start = time.perf_counter()
        result = sbx.commands.run(
            f"cd {project_dir} && sed -i '1i// Bench Round {self.round_id}' {target_file}",
            timeout=15,
            user="root",
        )
        step_times["edit"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            # Fallback: try config file if target file doesn't exist
            fallback_result = sbx.commands.run(
                f"cd {project_dir} && sed -i '1i// Bench Round {self.round_id}' config/config.ts",
                timeout=15,
                user="root",
            )
            if fallback_result.exit_code != 0:
                error_parts = [f"edit failed: exit_code={result.exit_code}"]
                if result.stderr:
                    error_parts.append(f"stderr={result.stderr[:100]}")
                error_parts.append(f"file={target_file}")
                return False, " | ".join(error_parts)
        return True, ""

    def _step_build(self, sbx, project_dir: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 3: Production build (the memory-intensive step)

        Removes dist/ and node_modules/.cache/ first to force full recompilation.
        Keeps .umi/ intact when dev server is running.

        Returns: (success, error_detail)
        """
        # Clean build output and webpack cache before building
        step_start = time.perf_counter()
        sbx.commands.run(f"cd {project_dir} && rm -rf dist/ node_modules/.cache/", timeout=15, user="root")

        # Clean timing — only measure the actual build command
        build_start = time.perf_counter()
        result = sbx.commands.run(
            f"cd {project_dir} && {self.config.coding_build_cmd}",
            timeout=self.config.coding_build_timeout + 30,
            user="root",
        )
        step_times["build"] = time.perf_counter() - build_start

        if result.exit_code != 0:
            error_parts = [f"build failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:200]}")
            if result.stdout:
                error_parts.append(f"stdout={result.stdout[:200]}")
            return False, " | ".join(error_parts)
        return True, ""

    def _step_test(self, sbx, project_dir: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 4: Run test suite

        Returns: (success, error_detail)
        """
        step_start = time.perf_counter()
        result = sbx.commands.run(
            f"cd {project_dir} && {self.config.coding_test_cmd}",
            timeout=self.config.coding_test_timeout + 30,
            user="root",
        )
        step_times["test"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            error_parts = [f"test failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            return False, " | ".join(error_parts)
        return True, ""

    def _classify_exception(self, e: Exception, step_times: Dict[str, float]) -> Tuple[str, str]:
        """Classify exception to determine which step failed"""
        error_str = str(e)
        if "context deadline exceeded" in error_str or "timed out" in error_str:
            if "ensure_dev" not in step_times:
                return "ensure_dev", "ensure_dev_server timed out"
            elif "checkout" not in step_times:
                return "checkout", "checkout timed out"
            elif "edit" not in step_times:
                return "edit", "edit timed out"
            elif "build" not in step_times:
                return "build", f"build timed out after {self.config.coding_build_timeout}s"
            elif "test" not in step_times:
                return "test", "test timed out"
            else:
                return "unknown", f"operation timed out: {error_str[:100]}"
        else:
            return "exception", f"exception: {error_str[:100]}"

    def _record_metrics(
        self,
        start_time: float,
        success: bool,
        step_times: Dict[str, float],
        build_success: bool,
        test_success: bool,
        timed_out: bool,
        error_detail: str,
    ) -> float:
        """Record metrics for this coding round

        Bug #2 fix: timeout is determined by exception handling, not by
        comparing total elapsed time against coding_build_timeout.

        Returns: elapsed time in seconds
        """
        elapsed = time.perf_counter() - start_time
        # Bug #2 fix: use timed_out from exception handling, not elapsed > build_timeout
        self.state.coding_metrics.add(
            elapsed,
            success and not timed_out,
            timed_out,
            step_times=step_times,
            build_success=build_success,
            test_success=test_success,
        )
        self.state.update_last_task_time(time.time())

        if not success and error_detail:
            self.state.coding_metrics.last_error = error_detail

        return elapsed

    def _handle_failure(self, target_file: str, failed_step: str, error_detail: str) -> None:
        """Handle failure after metrics are recorded"""
        print(f"[Sandbox{self.state.sandbox_id}] File '{target_file}' failed at {failed_step}: {error_detail[:80]}")
        self.consecutive_errors += 1
        if self.consecutive_errors >= 3:
            self.state.is_alive = False
