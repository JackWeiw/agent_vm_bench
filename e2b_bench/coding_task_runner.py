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
  Step 5: Memory — collect memory metrics (free -m)

Classes:
- CodingWarmupRunner: Starts dev server + initial build during warmup phase
- CodingTaskRunner: Executes coding tasks in fixed mode (continuous loop)
- CodingRoundRunner: Executes one round of coding operations in round-robin mode
"""

import random
import threading
import time
from typing import Dict, List, Tuple

from .config import Config
from .schemas import SandboxState, SandboxStatus


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
        while True:
            if self.state.creation_metrics.status == SandboxStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (
                SandboxStatus.FAILED,
                SandboxStatus.PORT_FAILED,
                SandboxStatus.OFFLINE,
                SandboxStatus.KILLED,
            ):
                print(
                    f"[Sandbox{self.state.sandbox_id}] Cannot start warmup: {self.state.creation_metrics.status.value}"
                )
                return
            time.sleep(0.5)

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
            try:
                # Check if dev server is already running
                check_result = sbx.commands.run("pgrep -f 'umi dev' || pgrep -f 'max dev'", timeout=10, user="root")
                if check_result.exit_code == 0 and check_result.stdout.strip():
                    print(f"[Sandbox{self.state.sandbox_id}] Dev server: already running")
                else:
                    print(f"[Sandbox{self.state.sandbox_id}] Starting dev server...")
                    sbx.commands.run(
                        f"cd {project_dir} && BROWSER=none npm run dev > /tmp/dev_server.log 2>&1 &",
                        timeout=10,
                        user="root",
                    )
                    # Wait for dev server startup
                    print(f"[Sandbox{self.state.sandbox_id}] Waiting {self.config.coding_dev_wait}s for dev server...")
                    time.sleep(self.config.coding_dev_wait)

                    # Verify dev server started
                    verify_result = sbx.commands.run(
                        "pgrep -f 'umi dev' || pgrep -f 'max dev'", timeout=10, user="root"
                    )
                    if verify_result.exit_code == 0 and verify_result.stdout.strip():
                        print(f"[Sandbox{self.state.sandbox_id}] Dev server: ready")
                    else:
                        print(f"[Sandbox{self.state.sandbox_id}] WARNING: Dev server may not be ready yet")
            except Exception as e:
                print(f"[Sandbox{self.state.sandbox_id}] Dev server start failed: {e}")

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
        while not self.stop_event.is_set():
            if self.state.creation_metrics.status == SandboxStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (
                SandboxStatus.FAILED,
                SandboxStatus.PORT_FAILED,
                SandboxStatus.OFFLINE,
                SandboxStatus.KILLED,
            ):
                print(
                    f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}"
                )
                return
            time.sleep(0.5)

        # Coding task execution loop
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break

            # Execute single coding task
            success, latency, build_success, test_success = self._run_single_task()

            # Update metrics
            timeout = latency > self.config.coding_build_timeout
            self.state.coding_metrics.add(
                latency, success and not timeout, timeout, build_success=build_success, test_success=test_success
            )
            self.state.update_last_task_time(time.time())

            # Error handling
            if success and not timeout:
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

    def _run_single_task(self) -> Tuple[bool, float, bool, bool]:
        """Execute single coding task cycle

        Returns: (success, latency_seconds, build_success, test_success)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0, False, False

        project_dir = self.config.coding_project_dir
        source_files = self.config.coding_source_files

        # Pick target file for this round
        if not source_files:
            return False, 0.0, False, False

        file_idx = self.state.coding_metrics.total_tasks % len(source_files)
        target_file = source_files[file_idx]

        start_time = time.perf_counter()
        build_success = False
        test_success = False

        try:
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
            success = build_success and (self.config.coding_skip_test or test_success)

            return success, elapsed, build_success, test_success

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            self.state.coding_metrics.last_error = error_msg
            print(f"[Sandbox{self.state.sandbox_id}] Coding task exception: {error_msg[:100]}")
            return False, elapsed, build_success, test_success


class CodingRoundRunner(threading.Thread):
    """Runner for coding operations in round-robin benchmark mode

    Each round modifies a different source file, then builds and tests.
    This creates genuine memory pressure because each modification triggers
    webpack to re-compile affected modules, and the build step peaks at ~2GB.

    The dev server runs persistently (~1.5GB), so when build peaks overlap
    with the running dev server, total sandbox memory reaches ~3GB.

    Steps per round (with individual timing):
      1. checkout — git checkout -- src/ (reset to clean state)
      2. edit     — sed inject round marker into target file
      3. build    — npm run build (MEMORY PEAK, overlaps with dev server)
      4. test     — npm test (verify correctness)
      5. memory   — free -m (collect memory snapshot)

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

        success, step_times, build_success, test_success, failed_step, error_detail = self._execute_steps(
            sbx, target_file
        )

        elapsed = self._record_metrics(start_time, success, step_times, build_success, test_success, error_detail)

        if success:
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            print(f"[Sandbox{self.state.sandbox_id}] Coding round completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(target_file, failed_step, error_detail)

    def _execute_steps(self, sbx, target_file: str) -> Tuple[bool, Dict[str, float], bool, bool, str, str]:
        """Execute all steps: checkout -> edit -> build -> test -> memory

        Args:
            sbx: Sandbox object
            target_file: Source file path relative to project_dir

        Returns:
            Tuple of (success, step_times, build_success, test_success, failed_step, error_detail)
        """
        success = True
        step_times = {}
        failed_step = None
        error_detail = ""
        build_success = False
        test_success = False
        project_dir = self.config.coding_project_dir

        try:
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
                return success, step_times, build_success, test_success, failed_step, error_detail

            # Step 3: Build — production build (memory-intensive)
            build_success, build_error = self._step_build(sbx, project_dir, step_times)
            if not build_success:
                failed_step = "build"
                error_detail = build_error
                success = False
                # Still continue to collect memory metrics

            # Step 4: Test — run test suite (only if build succeeded)
            if build_success:
                test_success, test_error = self._step_test(sbx, project_dir, step_times)
                # Test failure is non-fatal for overall round success

            # Step 5: Memory — collect memory snapshot
            self._step_memory(sbx, step_times)

        except Exception as e:
            success = False
            failed_step, error_detail = self._classify_exception(e, step_times)

        return success, step_times, build_success, test_success, failed_step, error_detail

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

    def _step_memory(self, sbx, step_times: Dict[str, float]) -> None:
        """Step 5: Collect memory snapshot via free -m

        Non-fatal — just records timing, doesn't affect round success.
        """
        step_start = time.perf_counter()
        sbx.commands.run("free -m", timeout=10, user="root")
        step_times["memory"] = time.perf_counter() - step_start

    def _classify_exception(self, e: Exception, step_times: Dict[str, float]) -> Tuple[str, str]:
        """Classify exception to determine which step failed"""
        error_str = str(e)
        if "context deadline exceeded" in error_str or "timed out" in error_str:
            if "checkout" not in step_times:
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
        error_detail: str,
    ) -> float:
        """Record metrics for this coding round

        Returns: elapsed time in seconds
        """
        elapsed = time.perf_counter() - start_time
        timeout = elapsed > self.config.coding_build_timeout
        self.state.coding_metrics.add(
            elapsed,
            success and not timeout,
            timeout,
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
