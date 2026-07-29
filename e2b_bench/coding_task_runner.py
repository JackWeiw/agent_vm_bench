"""
Coding Task Execution Module

Responsible for coding task execution inside E2B sandboxes, analogous to
task_runner.py's browser workflow. Each sandbox has an independent thread.

Simulates a real AI coding agent workflow (matches observed agent traces:
locate → inspect → edit → build → test → diff):
  Step 0: find    — reset source files (git checkout) + verify target file exists
  Step 1: read    — read the target file to confirm context (agent inspection)
  Step 2: edit    — apply a pre-configured find→replace pair (real semantic edit, triggers rebuild)
  Step 3: build   — production build (overlaps with dev server, ~3GB peak)
  Step 4: test    — run test suite (verify correctness)
  Step 5: diff    — git diff → patch file (agent's verification artifact)

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

    Returns True if a dev server process (npm run dev / vite / next dev / umi dev / max dev)
    is found running, False otherwise.
    """
    try:
        result = sbx.commands.run(
            # Match dev server processes: 'npm run dev', 'vite', 'next dev', 'umi dev', 'max dev'
            # Exclude grep/pgrep/sh processes to avoid false-positive from this command itself
            f"ps aux | grep -E 'npm run dev|vite|next dev|umi dev|max dev'"
            f" | grep -v grep | grep -v pgrep | grep -v 'sh -c'",
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
        # Dev server may live in a separate dir (e.g. a vite playground importing the lib)
        dev_dir = self.config.coding_dev_dir or project_dir

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
            sbx.commands.run(f"cd {project_dir} && git checkout -- packages/ src/ 2>/dev/null", timeout=30, user="root")
        except Exception:
            print(f"[Sandbox{self.state.sandbox_id}] git checkout failed (may not be a git repo)")

        # 3. Start dev server (if not skipped)
        if not self.config.coding_skip_dev_server:
            dev_server_running = _check_dev_server_running(sbx, dev_dir)
            if dev_server_running:
                print(f"[Sandbox{self.state.sandbox_id}] Dev server: already running")
            else:
                _start_dev_server(
                    sbx,
                    dev_dir,
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
        """Execute single coding task cycle (find → read → edit → build → test → diff)

        Returns: (success, latency_seconds, build_success, test_success, timed_out)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0, False, False, False

        project_dir = self.config.coding_project_dir
        source_files = self.config.coding_source_files

        # Pick replacement pair for this round
        if not source_files:
            return False, 0.0, False, False, False

        pair_idx = self.state.coding_metrics.total_tasks % len(source_files)
        pair = source_files[pair_idx]
        target_file = pair["file"]
        find_str = pair["find"]
        replace_str = pair["replace"]

        start_time = time.perf_counter()
        build_success = False
        test_success = False
        timed_out = False
        step_times: Dict[str, float] = {}

        try:
            # Step 0: Ensure dev server is running (for memory pressure overlap)
            if not self.config.coding_skip_dev_server:
                dev_dir = self.config.coding_dev_dir or project_dir
                dev_running = _check_dev_server_running(sbx, dev_dir)
                if not dev_running:
                    print(f"[Sandbox{self.state.sandbox_id}] Dev server not running, restarting...")
                    _start_dev_server(
                        sbx,
                        dev_dir,
                        self.config.coding_dev_cmd,
                        self.config.coding_dev_wait,
                        self.state.sandbox_id,
                    )

            # Step 1: find — reset source files + verify target file exists
            t0 = time.perf_counter()
            sbx.commands.run(f"cd {project_dir} && git checkout -- packages/ src/ 2>/dev/null", timeout=30, user="root")
            # Verify the target file exists (agent would locate the file)
            exists = sbx.commands.run(f"cd {project_dir} && test -f {target_file} && echo ok", timeout=15, user="root")
            step_times["find"] = time.perf_counter() - t0
            if exists.exit_code != 0 or "ok" not in (exists.stdout or ""):
                # Fallback: if the configured target doesn't exist, locate any source file
                fallback = sbx.commands.run(
                    f"cd {project_dir} && find packages src -name '*.ts' -o -name '*.tsx' -o -name '*.js' 2>/dev/null | head -1",
                    timeout=15,
                    user="root",
                )
                found = (fallback.stdout or "").strip().splitlines()
                if found:
                    target_file = found[0]
                    find_str, replace_str = "// bench marker", "// bench round\n// bench marker"

            # Step 2: read — inspect the target file (agent confirming context)
            t1 = time.perf_counter()
            sbx.commands.run(f"cd {project_dir} && head -20 {target_file}", timeout=15, user="root")
            step_times["read"] = time.perf_counter() - t1

            # Step 3: edit — apply the find→replace pair (real semantic edit)
            t2 = time.perf_counter()
            escaped_replace = replace_str.replace("/", "\\/").replace("&", "\\&")
            edit_result = sbx.commands.run(
                f"cd {project_dir} && sed -i 's|{find_str}|{escaped_replace}|' {target_file}",
                timeout=15,
                user="root",
            )
            step_times["edit"] = time.perf_counter() - t2
            if edit_result.exit_code != 0:
                self.state.coding_metrics.last_error = f"edit failed: exit_code={edit_result.exit_code}"
                return False, time.perf_counter() - start_time, build_success, test_success, timed_out

            # Step 4: build — production build (memory-intensive)
            if not self.config.coding_skip_build:
                sbx.commands.run(
                    f"cd {project_dir} && rm -rf dist/ .next/ node_modules/.cache/",
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

            # Step 5: test — run test suite
            if not self.config.coding_skip_test and build_success:
                test_result = sbx.commands.run(
                    f"cd {project_dir} && {self.config.coding_test_cmd}",
                    timeout=self.config.coding_test_timeout,
                    user="root",
                )
                test_success = test_result.exit_code == 0

            # Step 6: diff — produce the verification artifact (git diff → patch)
            t3 = time.perf_counter()
            sbx.commands.run(
                f"cd {project_dir} && git diff > /tmp/bench_round_{self.state.coding_metrics.total_tasks}.patch",
                timeout=15,
                user="root",
            )
            step_times["diff"] = time.perf_counter() - t3

            elapsed = time.perf_counter() - start_time
            # skip_build=True means build_success should be True (skipped = not failed)
            success = (self.config.coding_skip_build or build_success) and (
                self.config.coding_skip_test or test_success
            )

            return success, elapsed, build_success, test_success, timed_out

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            # timeout is determined by exception, not elapsed time
            timed_out = "timed out" in error_msg.lower() or "context deadline exceeded" in error_msg.lower()
            self.state.coding_metrics.last_error = error_msg
            print(f"[Sandbox{self.state.sandbox_id}] Coding task exception: {error_msg[:100]}")
            return False, elapsed, build_success, test_success, timed_out


class CodingRoundRunner(threading.Thread):
    """Runner for coding operations in round-robin benchmark mode

    Each round applies a different pre-configured replacement pair (a real,
    type-safe edit an agent would make), then builds and tests. Each edit
    triggers the bundler to re-compile affected modules; the build step peaks
    and overlaps with the running dev server (~1.5GB) → ~3GB per sandbox.

    Steps per round (with individual timing):
      0. find  — git checkout reset + verify/locate the target file
      1. read  — inspect the target file (agent confirming context)
      2. edit  — apply the find→replace pair (real semantic edit, triggers rebuild)
      3. build — npm run build (MEMORY PEAK, overlaps with dev server)
      4. test  — npm test (verify correctness)
      5. diff  — git diff → patch file (agent verification artifact)

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

        # Pick replacement pair for this round (round-robin through the list)
        pair_idx = self.round_id % len(source_files)
        pair = source_files[pair_idx]

        start_time = time.perf_counter()

        success, step_times, build_success, test_success, failed_step, error_detail, timed_out = self._execute_steps(
            sbx, pair
        )

        elapsed = self._record_metrics(
            start_time, success, step_times, build_success, test_success, timed_out, error_detail
        )

        if success:
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            print(f"[Sandbox{self.state.sandbox_id}] Coding round completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(pair["file"], failed_step, error_detail)

    def _execute_steps(self, sbx, pair: Dict[str, str]) -> Tuple[bool, Dict[str, float], bool, bool, str, str, bool]:
        """Execute all steps: find -> read -> edit -> build -> test -> diff

        Args:
            sbx: Sandbox object
            pair: Replacement pair {"file": str, "find": str, "replace": str}

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
        target_file = pair["file"]
        find_str = pair["find"]
        replace_str = pair["replace"]

        try:
            # Step 0: find — ensure dev server, reset source files, verify/locate target
            if not self.config.coding_skip_dev_server:
                self._step_ensure_dev_server(sbx, project_dir, step_times)

            locate_ok, locate_error, resolved_file, resolved_find, resolved_replace = self._step_find(
                sbx, project_dir, target_file, find_str, replace_str, step_times
            )
            if not locate_ok:
                # find failure is non-fatal (may not be a git repo / file moved); continue
                print(f"[Sandbox{self.state.sandbox_id}] find warning: {locate_error}")
            target_file = resolved_file
            find_str = resolved_find
            replace_str = resolved_replace

            # Step 1: read — inspect the target file
            self._step_read(sbx, project_dir, target_file, step_times)

            # Step 2: edit — apply the find→replace pair
            edit_success, edit_error = self._step_edit(sbx, project_dir, target_file, find_str, replace_str, step_times)
            if not edit_success:
                failed_step = "edit"
                error_detail = edit_error
                success = False
                return success, step_times, build_success, test_success, failed_step, error_detail, timed_out

            # Step 3: build — production build (memory-intensive)
            if not self.config.coding_skip_build:
                build_success, build_error = self._step_build(sbx, project_dir, step_times)
                if not build_success:
                    failed_step = "build"
                    error_detail = build_error
                    success = False
            else:
                build_success = True  # skipped = not failed

            # Step 4: test — run test suite (only if build succeeded or skipped)
            if not self.config.coding_skip_test and build_success:
                test_success, test_error = self._step_test(sbx, project_dir, step_times)
                # Test failure is non-fatal for overall round success
            elif self.config.coding_skip_test:
                test_success = True  # skipped = not failed

            # Step 5: diff — produce the verification artifact
            self._step_diff(sbx, project_dir, step_times)

        except Exception as e:
            success = False
            timed_out = "timed out" in str(e).lower() or "context deadline exceeded" in str(e).lower()
            failed_step, error_detail = self._classify_exception(e, step_times)

        return success, step_times, build_success, test_success, failed_step, error_detail, timed_out

    def _step_ensure_dev_server(self, sbx, project_dir: str, step_times: Dict[str, float]) -> None:
        """Pre-step: Ensure dev server is running for memory pressure overlap.

        The dev server (~1.5GB) must overlap with the build step to create ~3GB
        memory pressure. Non-fatal: if it can't start, the round continues at
        a lower peak. Timed under the `find` step (it's part of round setup,
        not a distinct agent action).

        Runs the dev server in `coding_dev_dir` when set (e.g. a vite playground
        importing the target lib), else in `project_dir`.
        """
        dev_dir = self.config.coding_dev_dir or project_dir
        step_start = time.perf_counter()
        dev_server_running = _check_dev_server_running(sbx, dev_dir)
        check_elapsed = time.perf_counter() - step_start

        if dev_server_running:
            step_times.setdefault("find", 0.0)
            step_times["find"] += check_elapsed
            return  # Already running, no action needed

        # Dev server not running — restart it
        print(f"[Sandbox{self.state.sandbox_id}] Dev server not running, restarting...")
        _start_dev_server(
            sbx,
            dev_dir,
            self.config.coding_dev_cmd,
            self.config.coding_dev_wait,
            self.state.sandbox_id,
        )
        step_times.setdefault("find", 0.0)
        step_times["find"] += time.perf_counter() - step_start

    def _step_find(
        self, sbx, project_dir: str, target_file: str, find_str: str, replace_str: str, step_times: Dict[str, float]
    ) -> Tuple[bool, str, str, str, str]:
        """Step 0: Reset source files via git checkout + verify/locate the target file.

        Returns: (success, error_detail, resolved_file, resolved_find, resolved_replace)
        — checkout/locate failure is non-fatal; on miss it falls back to a located file
        with a generic comment-marker pair so the round still triggers a rebuild.
        """
        step_start = time.perf_counter()
        result = sbx.commands.run(
            f"cd {project_dir} && git checkout -- packages/ src/ 2>/dev/null", timeout=30, user="root"
        )
        # Verify target file exists
        exists = sbx.commands.run(f"cd {project_dir} && test -f {target_file} && echo ok", timeout=15, user="root")
        step_times["find"] = step_times.get("find", 0.0) + (time.perf_counter() - step_start)

        if exists.exit_code == 0 and "ok" in (exists.stdout or ""):
            return True, "", target_file, find_str, replace_str

        # Fallback: locate any source file and use a generic comment-marker pair
        fallback = sbx.commands.run(
            f"cd {project_dir} && find src -name '*.tsx' -o -name '*.ts' | head -1",
            timeout=15,
            user="root",
        )
        found = (fallback.stdout or "").strip().splitlines()
        if found:
            return (
                False,
                f"target not found, fell back to {found[0]}",
                found[0],
                "// bench marker",
                "// bench round\n// bench marker",
            )
        return False, "checkout/locate failed", target_file, find_str, replace_str

    def _step_read(self, sbx, project_dir: str, target_file: str, step_times: Dict[str, float]) -> None:
        """Step 1: Read the target file (agent confirming context)."""
        step_start = time.perf_counter()
        sbx.commands.run(f"cd {project_dir} && head -20 {target_file}", timeout=15, user="root")
        step_times["read"] = time.perf_counter() - step_start

    def _step_edit(
        self, sbx, project_dir: str, target_file: str, find_str: str, replace_str: str, step_times: Dict[str, float]
    ) -> Tuple[bool, str]:
        """Step 2: Apply the find→replace pair via sed (real semantic edit, triggers rebuild).

        Returns: (success, error_detail)
        """
        step_start = time.perf_counter()
        escaped_replace = replace_str.replace("/", "\\/").replace("&", "\\&")
        result = sbx.commands.run(
            f"cd {project_dir} && sed -i 's|{find_str}|{escaped_replace}|' {target_file}",
            timeout=15,
            user="root",
        )
        step_times["edit"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            error_parts = [f"edit failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            error_parts.append(f"file={target_file}")
            return False, " | ".join(error_parts)
        return True, ""

    def _step_build(self, sbx, project_dir: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 3: Production build (the memory-intensive step)

        Removes dist/, .next/ (Next.js), and node_modules/.cache/ first to force
        full recompilation.

        Returns: (success, error_detail)
        """
        # Clean build output and bundler cache before building
        step_start = time.perf_counter()
        sbx.commands.run(f"cd {project_dir} && rm -rf dist/ .next/ node_modules/.cache/", timeout=15, user="root")

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

    def _step_diff(self, sbx, project_dir: str, step_times: Dict[str, float]) -> None:
        """Step 5: Produce the verification artifact (git diff → patch file)."""
        step_start = time.perf_counter()
        sbx.commands.run(
            f"cd {project_dir} && git diff > /tmp/bench_round_{self.round_id}.patch",
            timeout=15,
            user="root",
        )
        step_times["diff"] = time.perf_counter() - step_start

    def _classify_exception(self, e: Exception, step_times: Dict[str, float]) -> Tuple[str, str]:
        """Classify exception to determine which step failed"""
        error_str = str(e)
        if "context deadline exceeded" in error_str or "timed out" in error_str:
            if "find" not in step_times:
                return "find", "find timed out"
            elif "read" not in step_times:
                return "read", "read timed out"
            elif "edit" not in step_times:
                return "edit", "edit timed out"
            elif "build" not in step_times:
                return "build", f"build timed out after {self.config.coding_build_timeout}s"
            elif "test" not in step_times:
                return "test", "test timed out"
            elif "diff" not in step_times:
                return "diff", "diff timed out"
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

        timeout is determined by exception handling, not by comparing total
        elapsed time against coding_build_timeout.

        Returns: elapsed time in seconds
        """
        elapsed = time.perf_counter() - start_time
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
