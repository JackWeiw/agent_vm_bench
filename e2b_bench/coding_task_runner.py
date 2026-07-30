"""
Coding Task Execution Module

Responsible for coding task execution inside E2B sandboxes, analogous to
task_runner.py's browser workflow. Each sandbox has an independent thread.

Simulates a real AI coding agent workflow (verified against captured openclaw
trajectories on vuejs/core and gohugoio/hugo - locate -> inspect -> edit -> verify
-> diff):
  Step 0: find    - reset source files (git checkout) + verify target file exists
  Step 1: read    - read the target file to confirm context (agent inspection)
  Step 2: edit    - apply a pre-configured find->replace pair (real semantic edit)
  Step 3: verify  - write an ad-hoc test file to /tmp + run it (npx tsx for js,
                    go run for go). Mirrors the trace's combined write+run. This
                    is the transient memory peak (esbuild transpile / Go compile
                    + execute, loading the module graph).
  Step 4: diff    - git diff -> patch file (agent's verification artifact)

No production build, no full test suite, no resident dev server - none appear in
the real traces. Memory pressure comes from N concurrent sandboxes' transient
verify peaks overlapping, observed at the host level via vm_monitor/smap_tool.

Classes:
- CodingWarmupRunner: Runs one initial verify during warmup (warms caches, confirms
  project health) - no resident process.
- CodingTaskRunner: Executes coding tasks in fixed mode (continuous loop)
- CodingRoundRunner: Executes one round of coding operations in round-robin mode
"""

import random
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import Config, _find_name_clause, get_coding_profile
from .helpers import wait_for_port_ready
from .schemas import SandboxState, SandboxStatus


def _run_verify(sbx, project_dir: str, config: Config, pair: Dict[str, str]) -> Tuple[bool, str]:
    """Write an ad-hoc test file to /tmp + run it - the trace-faithful verify step.

    Mirrors the real openclaw agent: `cat > /tmp/test_*.mjs << 'EOF' ... EOF` then
    `npx tsx /tmp/test_*.mjs` (js), or `cat > /tmp/test_*.go << 'GOEOF' ... GOEOF`
    then `go run /tmp/test_*.go` (go). The write and run are a SINGLE command
    (newline-joined), exactly as the agent did - splitting them would diverge
    from the trace.

    The script body is the pair's `verify_script` if present, else the shared
    default for the active language (loads the module graph -> real transient peak).

    Returns: (success, error_detail)
    """
    profile = get_coding_profile(config.coding_language)
    # Pair-specific script wins; else shared default for the language.
    script_body = pair.get("verify_script") or profile.default_verify_script
    # For the js shared default, substitute the edited package dir into {pkg}.
    if "{pkg}" in script_body:
        # Derive packages/<name> from the edited file path (e.g.
        # packages/reactivity/src/baseHandlers.ts -> packages/reactivity).
        edited = pair.get("file", "")
        pkg = "/opt/coding-bench/" + edited.split("/src/")[0] if "/src/" in edited else "/opt/coding-bench/packages/vue"
        script_body = script_body.replace("{pkg}", pkg)

    eof = profile.heredoc_eof
    # Single command: cd project, heredoc-write the temp test file, then run it.
    # The write+run are newline-joined (not separate commands) to match the trace.
    cmd = (
        f"cd {project_dir} && "
        f"cat > {profile.temp_test_path} << '{eof}'\n"
        f"{script_body}"
        f"{eof}\n"
        f"{config.coding_verify_cmd}"
    )
    result = sbx.commands.run(cmd, timeout=config.coding_verify_timeout + 30, user="root")
    if result.exit_code != 0:
        error_parts = [f"verify failed: exit_code={result.exit_code}"]
        if result.stderr:
            error_parts.append(f"stderr={result.stderr[:200]}")
        if result.stdout:
            error_parts.append(f"stdout={result.stdout[:200]}")
        return False, " | ".join(error_parts)
    return True, ""


class CodingWarmupRunner(threading.Thread):
    """Warmup phase runner for coding workflow - runs one initial verify.

    No resident dev server (none in the real traces). Warmup runs one initial
    verify (write temp test + npx tsx/go run) to warm esbuild/node or Go compiler
    caches and confirm project health. This establishes a real, trace-faithful
    warm state without a fabricated background process.
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
        """Execute warmup phase for this sandbox - one initial verify (no resident process)."""
        # Wait for sandbox ready (coding workflow: command-based ready check)
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

        # 1. Verify project exists (js: package.json, go: go.mod)
        project_marker = "go.mod" if self.config.coding_language == "go" else "package.json"
        try:
            result = sbx.commands.run(f"ls {project_dir}/{project_marker}", timeout=30, user="root")
            if result.exit_code != 0:
                print(f"[Sandbox{self.state.sandbox_id}] Project not found at {project_dir}, skipping warmup")
                self.state.warmup_done = True
                return
        except Exception as e:
            print(f"[Sandbox{self.state.sandbox_id}] Failed to verify project: {e}")
            self.state.warmup_done = True
            return

        # 2. Reset source files
        profile = get_coding_profile(self.config.coding_language)
        try:
            sbx.commands.run(
                f"cd {project_dir} && git checkout -- {profile.checkout_paths} 2>/dev/null",
                timeout=30,
                user="root",
            )
        except Exception:
            print(f"[Sandbox{self.state.sandbox_id}] git checkout failed (may not be a git repo)")

        # 3. Run one initial verify (warms esbuild/node or Go compiler caches,
        #    confirms project health). No resident dev server - none in the trace.
        if not self.config.coding_skip_verify:
            try:
                print(f"[Sandbox{self.state.sandbox_id}] Running initial verify...")
                # Use the first configured pair's verify_script (or shared default).
                pair = self.config.coding_source_files[0] if self.config.coding_source_files else {}
                ok, err = _run_verify(sbx, project_dir, self.config, pair)
                if ok:
                    print(f"[Sandbox{self.state.sandbox_id}] Initial verify: success")
                else:
                    print(f"[Sandbox{self.state.sandbox_id}] Initial verify failed: {err[:120]}")
            except Exception as e:
                print(f"[Sandbox{self.state.sandbox_id}] Initial verify exception: {e}")

        # Mark warmup complete
        self.state.warmup_done = True
        print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Coding warmup completed")


class CodingTaskRunner(threading.Thread):
    """Coding task runner for fixed mode - one independent thread per sandbox

    Each iteration: checkout -> edit -> build -> test -> memory collection.
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
            success, latency, verify_success, timed_out = self._run_single_task()

            # Update metrics - timeout is determined by exception handling, not elapsed time
            self.state.coding_metrics.add(latency, success and not timed_out, timed_out, verify_success=verify_success)
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

    def _run_single_task(self) -> Tuple[bool, float, bool, bool]:
        """Execute single coding task cycle (find -> read -> edit -> verify -> diff)

        Returns: (success, latency_seconds, verify_success, timed_out)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0, False, False

        project_dir = self.config.coding_project_dir
        source_files = self.config.coding_source_files
        profile = get_coding_profile(self.config.coding_language)

        # Pick replacement pair for this round
        if not source_files:
            return False, 0.0, False, False

        pair_idx = self.state.coding_metrics.total_tasks % len(source_files)
        pair = source_files[pair_idx]
        target_file = pair["file"]
        find_str = pair["find"]
        replace_str = pair["replace"]

        start_time = time.perf_counter()
        verify_success = False
        timed_out = False
        step_times: Dict[str, float] = {}

        try:
            # Step 1: find - reset source files + verify target file exists
            t0 = time.perf_counter()
            sbx.commands.run(
                f"cd {project_dir} && git checkout -- {profile.checkout_paths} 2>/dev/null",
                timeout=30,
                user="root",
            )
            # Verify the target file exists (agent would locate the file)
            exists = sbx.commands.run(f"cd {project_dir} && test -f {target_file} && echo ok", timeout=15, user="root")
            step_times["find"] = time.perf_counter() - t0
            if exists.exit_code != 0 or "ok" not in (exists.stdout or ""):
                # Fallback: if the configured target doesn't exist, locate any source file
                fallback = sbx.commands.run(
                    f"cd {project_dir} && find packages src {_find_name_clause(profile.source_find_names)} 2>/dev/null | head -1",
                    timeout=15,
                    user="root",
                )
                found = (fallback.stdout or "").strip().splitlines()
                if found:
                    target_file = found[0]
                    find_str, replace_str = "// bench marker", "// bench round\n// bench marker"

            # Step 2: read - inspect the target file (agent confirming context)
            t1 = time.perf_counter()
            sbx.commands.run(f"cd {project_dir} && head -20 {target_file}", timeout=15, user="root")
            step_times["read"] = time.perf_counter() - t1

            # Step 3: edit - apply the find->replace pair (real semantic edit)
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
                return False, time.perf_counter() - start_time, verify_success, timed_out

            # Step 4: verify - write ad-hoc test file + run it (npx tsx / go run)
            if not self.config.coding_skip_verify:
                t3 = time.perf_counter()
                verify_success, err = _run_verify(sbx, project_dir, self.config, pair)
                step_times["verify"] = time.perf_counter() - t3
                if not verify_success:
                    self.state.coding_metrics.last_error = err
            else:
                verify_success = True  # skipped = not failed

            # Step 5: diff - produce the verification artifact (git diff -> patch)
            t4 = time.perf_counter()
            sbx.commands.run(
                f"cd {project_dir} && git diff > /tmp/bench_round_{self.state.coding_metrics.total_tasks}.patch",
                timeout=15,
                user="root",
            )
            step_times["diff"] = time.perf_counter() - t4

            elapsed = time.perf_counter() - start_time
            success = self.config.coding_skip_verify or verify_success

            return success, elapsed, verify_success, timed_out

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            # timeout is determined by exception, not elapsed time
            timed_out = "timed out" in error_msg.lower() or "context deadline exceeded" in error_msg.lower()
            self.state.coding_metrics.last_error = error_msg
            print(f"[Sandbox{self.state.sandbox_id}] Coding task exception: {error_msg[:100]}")
            return False, elapsed, verify_success, timed_out


class CodingRoundRunner(threading.Thread):
    """Runner for coding operations in round-robin benchmark mode

    Each round applies a different pre-configured replacement pair (a real,
    type-safe edit an agent would make), then builds and tests. Each edit
    triggers the bundler to re-compile affected modules; the build step peaks
    and overlaps with the running dev server (~1.5GB) -> ~3GB per sandbox.

    Steps per round (with individual timing):
      0. find  - git checkout reset + verify/locate the target file
      1. read  - inspect the target file (agent confirming context)
      2. edit  - apply the find->replace pair (real semantic edit, triggers rebuild)
      3. build - npm run build (MEMORY PEAK, overlaps with dev server)
      4. test  - npm test (verify correctness)
      5. diff  - git diff -> patch file (agent verification artifact)

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

        success, step_times, verify_success, failed_step, error_detail, timed_out = self._execute_steps(sbx, pair)

        elapsed = self._record_metrics(start_time, success, step_times, verify_success, timed_out, error_detail)

        if success:
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            print(f"[Sandbox{self.state.sandbox_id}] Coding round completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(pair["file"], failed_step, error_detail)

    def _execute_steps(self, sbx, pair: Dict[str, str]) -> Tuple[bool, Dict[str, float], bool, str, str, bool]:
        """Execute all steps: find -> read -> edit -> verify -> diff

        Args:
            sbx: Sandbox object
            pair: Replacement pair {"file": str, "find": str, "replace": str,
            "verify_script": str(optional)}

        Returns:
            Tuple of (success, step_times, verify_success, failed_step, error_detail, timed_out)
        """
        success = True
        step_times = {}
        failed_step = None
        error_detail = ""
        verify_success = False
        timed_out = False
        project_dir = self.config.coding_project_dir
        target_file = pair["file"]
        find_str = pair["find"]
        replace_str = pair["replace"]

        try:
            # Step 0: find - reset source files, verify/locate target
            locate_ok, locate_error, resolved_file, resolved_find, resolved_replace = self._step_find(
                sbx, project_dir, target_file, find_str, replace_str, step_times
            )
            if not locate_ok:
                # find failure is non-fatal (may not be a git repo / file moved); continue
                print(f"[Sandbox{self.state.sandbox_id}] find warning: {locate_error}")
            target_file = resolved_file
            find_str = resolved_find
            replace_str = resolved_replace
            # _step_find may mutate `pair`'s resolved values for verify substitution
            pair = {**pair, "file": target_file}

            # Step 1: read - inspect the target file
            self._step_read(sbx, project_dir, target_file, step_times)

            # Step 2: edit - apply the find->replace pair
            edit_success, edit_error = self._step_edit(sbx, project_dir, target_file, find_str, replace_str, step_times)
            if not edit_success:
                failed_step = "edit"
                error_detail = edit_error
                success = False
                return success, step_times, verify_success, failed_step, error_detail, timed_out

            # Step 3: verify - write ad-hoc test file + run it (npx tsx / go run)
            if not self.config.coding_skip_verify:
                verify_success, verify_error = self._step_verify(sbx, project_dir, pair, step_times)
                if not verify_success:
                    failed_step = "verify"
                    error_detail = verify_error
                    success = False
            else:
                verify_success = True  # skipped = not failed

            # Step 4: diff - produce the verification artifact
            self._step_diff(sbx, project_dir, step_times)

        except Exception as e:
            success = False
            timed_out = "timed out" in str(e).lower() or "context deadline exceeded" in str(e).lower()
            failed_step, error_detail = self._classify_exception(e, step_times)

        return success, step_times, verify_success, failed_step, error_detail, timed_out

    def _step_find(
        self, sbx, project_dir: str, target_file: str, find_str: str, replace_str: str, step_times: Dict[str, float]
    ) -> Tuple[bool, str, str, str, str]:
        """Step 0: Reset source files via git checkout + verify/locate the target file.

        Returns: (success, error_detail, resolved_file, resolved_find, resolved_replace)
        - checkout/locate failure is non-fatal; on miss it falls back to a located file
        with a generic comment-marker pair so the round still produces a verify peak.
        """
        profile = get_coding_profile(self.config.coding_language)
        step_start = time.perf_counter()
        result = sbx.commands.run(
            f"cd {project_dir} && git checkout -- {profile.checkout_paths} 2>/dev/null",
            timeout=30,
            user="root",
        )
        # Verify target file exists
        exists = sbx.commands.run(f"cd {project_dir} && test -f {target_file} && echo ok", timeout=15, user="root")
        step_times["find"] = step_times.get("find", 0.0) + (time.perf_counter() - step_start)

        if exists.exit_code == 0 and "ok" in (exists.stdout or ""):
            return True, "", target_file, find_str, replace_str

        # Fallback: locate any source file and use a generic comment-marker pair
        fallback = sbx.commands.run(
            f"cd {project_dir} && find packages src {_find_name_clause(profile.source_find_names)} 2>/dev/null | head -1",
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
        """Step 2: Apply the find->replace pair via sed (real semantic edit, triggers rebuild).

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

    def _step_verify(
        self, sbx, project_dir: str, pair: Dict[str, str], step_times: Dict[str, float]
    ) -> Tuple[bool, str]:
        """Step 3: Write an ad-hoc test file to /tmp + run it (the trace-faithful verify).

        Mirrors the real openclaw agent: `cat > /tmp/test_*.mjs << 'EOF' ... EOF`
        then `npx tsx` (js), or `cat > /tmp/test_*.go << 'GOEOF' ... GOEOF` then
        `go run` (go). The write+run is a single command (newline-joined). The
        script body is the pair's `verify_script` or the shared default for the
        language. This is the transient memory peak (esbuild transpile / Go
        compiler + execute, loading the module graph).

        Returns: (success, error_detail)
        """
        step_start = time.perf_counter()
        ok, err = _run_verify(sbx, project_dir, self.config, pair)
        step_times["verify"] = time.perf_counter() - step_start
        return ok, err

    def _step_diff(self, sbx, project_dir: str, step_times: Dict[str, float]) -> None:
        """Step 5: Produce the verification artifact (git diff -> patch file)."""
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
            elif "verify" not in step_times:
                return "verify", f"verify timed out after {self.config.coding_verify_timeout}s"
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
        verify_success: bool,
        timed_out: bool,
        error_detail: str,
    ) -> float:
        """Record metrics for this coding round

        timeout is determined by exception handling, not by comparing total
        elapsed time against coding_verify_timeout.

        Returns: elapsed time in seconds
        """
        elapsed = time.perf_counter() - start_time
        self.state.coding_metrics.add(
            elapsed,
            success and not timed_out,
            timed_out,
            step_times=step_times,
            verify_success=verify_success,
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
