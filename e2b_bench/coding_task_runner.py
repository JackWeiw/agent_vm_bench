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
  Step 3: verify  - write an ad-hoc test file to /tmp + run it (npx tsx for ts,
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

import base64
import logging
import random
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import Config, _find_name_clause, get_coding_profile
from .helpers import wait_for_port_ready
from .schemas import SandboxState, SandboxStatus

logger = logging.getLogger(__name__)


def _build_edit_command(project_dir: str, target_file: str, find_str: str, replace_str: str) -> str:
    """Build a robust literal find->replace edit command.

    The earlier `sed -i 's|find|replace|'` broke on pairs whose source contains
    regex metacharacters: the hugo pair's find string holds `|`, which collides
    with sed's `|` delimiter ("sed: -e expression #1, char 60"). Worse, sed
    treats find as a regex, so `.`, `*`, `[`, `]`, `(`, `)`, `^`, `$` and
    backslash in any find string (the vuejs/core pairs have `.` and `()`
    everywhere) are interpreted as metacharacters, not literals - those pairs
    only matched by luck. A real agent edits a specific line literally, not
    via sed regex.

    So this invokes `python3` (present in the ubuntu base image of both coding
    images) to do a literal `str.replace` of the FIRST occurrence and write the
    file back. find/replace are carried as base64 so no quoting can break them
    - backticks, `|`, `$`, backslashes, quotes, newlines are all inert. Exit
    code 2 if the find string is absent (a no-op edit is surfaced as an
    explicit failure, not a silent sed success that would fake a verify pass).

    The script is fed to python3 via a quoted heredoc (`<< 'PYEOF' ... PYEOF`)
    and `python3 -` (read from stdin), NOT via `python3 -c "..."`. An earlier
    `python3 -c` form passed the script through a JSON-encoded double-quoted
    shell argument; the `\\n` / `\\"` it embedded survived python's own parsing
    but broke when E2B's commands.run serialization re-quoted it, producing
    `SyntaxError: File "<string>", line 1`. A quoted heredoc is passed
    verbatim (no shell expansion, no escape re-interpretation) - the same
    mechanism `_run_verify` uses successfully for the verify-step script body.
    """
    find_b64 = base64.b64encode(find_str.encode()).decode()
    repl_b64 = base64.b64encode(replace_str.encode()).decode()
    return (
        f"cd {project_dir} && python3 - {find_b64} {repl_b64} {target_file} <<'PYEOF'\n"
        "import base64, sys\n"
        "f = base64.b64decode(sys.argv[1]).decode()\n"
        "r = base64.b64decode(sys.argv[2]).decode()\n"
        "p = sys.argv[3]\n"
        "s = open(p, encoding='utf-8').read()\n"
        "if f not in s:\n"
        "    sys.exit(2)\n"
        "open(p, 'w', encoding='utf-8').write(s.replace(f, r, 1))\n"
        "PYEOF"
    )


def _run_verify(
    sbx,
    project_dir: str,
    config: Config,
    pair: Dict[str, str],
    step_times: Optional[Dict[str, float]] = None,
    round_id: int = 0,
) -> Tuple[bool, str, bool]:
    """Write ad-hoc test file(s) to /tmp + run them - the trace-faithful verify step.

    ts path (multi-process): spins up `coding_verify_repeat` (default 3) independent
    `npx tsx` processes serially. Each pays the fixed ~0.47s startup cost (node +
    esbuild transpile + module graph load) - the only lever proven (via sandbox
    probes) to raise single-firecracker steady-state CPU while staying trace-faithful:
    the real agent repeatedly spawns independent `npx tsx` verifies within one issue
    (its captured vuejs/core trace shows 12 independent npx tsx invocations). N
    processes are chained in ONE commands.run with `&&` fail-fast - the agent's
    verify is a single combined write+run; chaining N preserves "one verify step =
    one continuous verification action" and keeps it as one verify step in metrics.

    The N bodies come from the shared DEFAULT_VERIFY_TEMPLATES pool, offset by
    `round_id % pool_len` so different rounds pick different N-subsets (mirrors the
    agent rewriting its ad-hoc test between verifies). Each body = 8 agent globals +
    compiler-core import + baseParse(template) + assert + print (stamped via
    _stamp_verify_body). Distinct temp files /tmp/bench_verify_{i}.mjs.

    go path: `coding_verify_repeat` is ignored (go stays N=1). go's per-verify cost
    is the genuine compile (already heavy via `go clean -cache` cold-compile); N go
    runs would diverge from the trace. The go pre-verify `go clean -cache` runs as a
    SEPARATE commands.run (timed into step_times["verify_clean"], not in
    CODING_STEP_ORDER - the real trace has no cache-clear step), then the write+go
    run is a single newline-joined command.

    Returns: (success, error_detail, compile_only). compile_only is True when the
    pair declared `verify: compile_only` (no assertable edit semantics) - the N-chain
    still runs (generic health checks), honestly labeled.
    """
    profile = get_coding_profile(config.coding_language)
    compile_only = str(pair.get("verify", "")).lower() == "compile_only"

    # Optional pre-verify cache clear (go only). Separate command so its time is
    # measured apart from the write+run.
    if profile.pre_verify_cmd:
        clean_start = time.perf_counter()
        clean_res = sbx.commands.run(profile.pre_verify_cmd, timeout=60, user="root")
        if step_times is not None:
            step_times["verify_clean"] = time.perf_counter() - clean_start
        # `go clean -cache` exit code is irrelevant to verify success (a stale
        # cache clear failure mustn't fake a verify pass); log and proceed.
        if clean_res.exit_code != 0 and clean_res.stderr:
            logger.warning(f"[verify_clean] pre-verify cmd non-zero: {(clean_res.stderr or '').strip()[:120]}")

    eof = profile.heredoc_eof

    if config.coding_language == "ts":
        # Multi-process verify: N independent npx tsx processes chained in one command.
        # Each body is stamped from a pool template; offset by round so consecutive
        # rounds differ. Temp files are indexed so the i-th cat+run pair uses
        # /tmp/bench_verify_{i}.mjs. `&&` fail-fast: first non-zero exit stops the rest.
        from .schemas import DEFAULT_VERIFY_TEMPLATES, _stamp_verify_body

        n = max(1, config.coding_verify_repeat)
        pool = DEFAULT_VERIFY_TEMPLATES
        offset = round_id % len(pool)
        parts = [f"cd {project_dir}"]
        for i in range(n):
            entry = pool[(offset + i) % len(pool)]
            body = _stamp_verify_body(entry["template"], entry["assert"])
            path_i = f"/tmp/bench_verify_{i}.mjs"
            parts.append(f"cat > {path_i} << '{eof}'\n{body}{eof}\nnpx tsx {path_i}")
        cmd = " && ".join(parts)
    else:
        # go (and any non-ts profile): single write+run, no N-chain. Use the pair's
        # own verify_script if present, else the profile default. go ignores
        # coding_verify_repeat - its go clean -cache cold-compile is already real load.
        script_body = pair.get("verify_script") or profile.default_verify_script
        cmd = (
            f"cd {project_dir} && "
            f"cat > {profile.temp_test_path} << '{eof}'\n"
            f"{script_body}"
            f"{eof}\n"
            f"{config.coding_verify_cmd}"
        )

    run_start = time.perf_counter()
    result = sbx.commands.run(cmd, timeout=config.coding_verify_timeout + 30, user="root")
    if step_times is not None:
        step_times["verify"] = time.perf_counter() - run_start
    if result.exit_code != 0:
        error_parts = [f"verify failed: exit_code={result.exit_code}"]
        if result.stderr:
            error_parts.append(f"stderr={result.stderr[:800]}")
        if result.stdout:
            error_parts.append(f"stdout={result.stdout[:800]}")
        return False, " | ".join(error_parts), False
    return True, "", compile_only


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
        if not wait_for_port_ready(self.state):
            logger.warning(
                f"[Sandbox{self.state.sandbox_id}] Cannot start warmup: {self.state.creation_metrics.status.value}"
            )
            return

        sbx = self.state.sandbox_obj
        if not sbx:
            logger.info(f"[Sandbox{self.state.sandbox_id}] No sandbox handle for warmup")
            self.state.warmup_done = True
            return

        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, "sandbox_id") else "N/A"
        project_dir = self.config.coding_project_dir

        project_marker = (
            "go.mod"
            if self.config.coding_language == "go"
            else ("pyproject.toml" if self.config.coding_language == "python" else "package.json")
        )
        try:
            result = sbx.commands.run(f"ls {project_dir}/{project_marker}", timeout=30, user="root")
            if result.exit_code != 0:
                logger.warning(f"[Sandbox{self.state.sandbox_id}] Project not found at {project_dir}, skipping warmup")
                self.state.warmup_done = True
                return
        except Exception as e:
            logger.error(f"[Sandbox{self.state.sandbox_id}] Failed to verify project: {e}")
            self.state.warmup_done = True
            return

        profile = get_coding_profile(self.config.coding_language)
        try:
            result = sbx.commands.run(
                f"cd {project_dir} && git checkout -- {profile.checkout_paths}",
                timeout=30,
                user="root",
            )
            if result.exit_code != 0:
                logger.info(
                    f"[Sandbox{self.state.sandbox_id}] git checkout non-zero (exit {result.exit_code}): "
                    f"{(result.stderr or '').strip()[:120]}"
                )
        except Exception as e:
            logger.error(f"[Sandbox{self.state.sandbox_id}] git checkout failed: {e}")

        # One initial verify warms esbuild/node or Go compiler caches and confirms
        # project health. No resident dev server - none in the trace.
        if not self.config.coding_skip_verify:
            try:
                logger.info(f"[Sandbox{self.state.sandbox_id}] Running initial verify...")
                pair = self.config.coding_source_files[0] if self.config.coding_source_files else {}
                ok, err, _compile_only = _run_verify(sbx, project_dir, self.config, pair, round_id=0)
                if ok:
                    logger.info(f"[Sandbox{self.state.sandbox_id}] Initial verify: success")
                else:
                    logger.warning(f"[Sandbox{self.state.sandbox_id}] Initial verify failed: {err[:120]}")
            except Exception as e:
                logger.error(f"[Sandbox{self.state.sandbox_id}] Initial verify exception: {e}")

        self.state.warmup_done = True
        logger.info(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Coding warmup completed")


class CodingTaskRunner(threading.Thread):
    """Coding task runner for fixed mode - one independent thread per sandbox

    Each iteration: find -> read -> edit -> verify -> diff (one replacement
    pair per cycle). Runs continuously until stop_event is set or sandbox
    goes offline.
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
        if not wait_for_port_ready(self.state, self.stop_event):
            logger.warning(
                f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}"
            )
            return

        while not self.stop_event.is_set():
            if not self.state.is_alive:
                logger.info(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break

            success, latency, verify_success, compile_only, timed_out = self._run_single_task()

            self.state.coding_metrics.add(
                latency,
                success and not timed_out,
                timed_out,
                verify_success=verify_success,
                compile_only=compile_only,
            )
            self.state.update_last_task_time(time.time())

            if success and not timed_out:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    logger.warning(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break

            sleep_time = random.uniform(self.config.coding_interval_min, self.config.coding_interval_max)
            time.sleep(sleep_time)

        logger.info(f"[Sandbox{self.state.sandbox_id}] Coding task runner ended")

    def _run_single_task(self) -> Tuple[bool, float, bool, bool, bool]:
        """Execute single coding task cycle (find -> read -> edit -> verify -> diff)

        Returns: (success, latency_seconds, verify_success, compile_only, timed_out)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0, False, False, False

        project_dir = self.config.coding_project_dir
        source_files = self.config.coding_source_files
        profile = get_coding_profile(self.config.coding_language)

        if not source_files:
            return False, 0.0, False, False, False

        pair_idx = self.state.coding_metrics.total_tasks % len(source_files)
        pair = source_files[pair_idx]
        target_file = pair["file"]
        find_str = pair["find"]
        replace_str = pair["replace"]

        start_time = time.perf_counter()
        verify_success = False
        compile_only = False
        timed_out = False
        step_times: Dict[str, float] = {}

        try:
            t0 = time.perf_counter()
            sbx.commands.run(
                f"cd {project_dir} && git checkout -- {profile.checkout_paths} || true",
                timeout=30,
                user="root",
            )
            exists = sbx.commands.run(f"cd {project_dir} && test -f {target_file} && echo ok", timeout=15, user="root")
            step_times["find"] = time.perf_counter() - t0
            if exists.exit_code != 0 or "ok" not in (exists.stdout or ""):
                # Configured target missing - locate any source file and use a
                # generic comment-marker pair so the round still produces a verify peak.
                fallback = sbx.commands.run(
                    f"cd {project_dir} && find {profile.source_find_root} {_find_name_clause(profile.source_find_names)} 2>/dev/null | head -1",
                    timeout=15,
                    user="root",
                )
                found = (fallback.stdout or "").strip().splitlines()
                if found:
                    target_file = found[0]
                    # Python source uses '#' comments (not '//'); pick the marker
                    # style so a locate-fallback edit still lands as a real edit.
                    comment = "#" if self.config.coding_language == "python" else "//"
                    find_str, replace_str = f"{comment} bench marker", f"{comment} bench round\n{comment} bench marker"

            t1 = time.perf_counter()
            sbx.commands.run(f"cd {project_dir} && head -20 {target_file}", timeout=15, user="root")
            step_times["read"] = time.perf_counter() - t1

            t2 = time.perf_counter()
            edit_result = sbx.commands.run(
                _build_edit_command(project_dir, target_file, find_str, replace_str),
                timeout=15,
                user="root",
            )
            step_times["edit"] = time.perf_counter() - t2
            if edit_result.exit_code != 0:
                self.state.coding_metrics.last_error = f"edit failed: exit_code={edit_result.exit_code}"
                return False, time.perf_counter() - start_time, verify_success, compile_only, timed_out

            if not self.config.coding_skip_verify:
                t3 = time.perf_counter()
                verify_success, err, compile_only = _run_verify(
                    sbx, project_dir, self.config, pair, round_id=self.state.coding_metrics.total_tasks
                )
                step_times["verify"] = time.perf_counter() - t3
                if not verify_success:
                    self.state.coding_metrics.last_error = err
            else:
                verify_success = True  # skipped = not failed

            t4 = time.perf_counter()
            sbx.commands.run(
                f"cd {project_dir} && git diff > /tmp/bench_round_{self.state.coding_metrics.total_tasks}.patch",
                timeout=15,
                user="root",
            )
            step_times["diff"] = time.perf_counter() - t4

            elapsed = time.perf_counter() - start_time
            success = self.config.coding_skip_verify or verify_success

            return success, elapsed, verify_success, compile_only, timed_out

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            timed_out = "timed out" in error_msg.lower() or "context deadline exceeded" in error_msg.lower()
            self.state.coding_metrics.last_error = error_msg
            logger.error(f"[Sandbox{self.state.sandbox_id}] Coding task exception: {error_msg[:100]}")
            return False, elapsed, verify_success, compile_only, timed_out


class CodingRoundRunner(threading.Thread):
    """Runner for coding operations in round-robin benchmark mode

    Each round applies a different pre-configured replacement pair (a real,
    type-safe edit an agent would make), then verifies it by writing an ad-hoc
    test file to /tmp and running it (npx tsx / go run). This transient verify
    peak - not a production build - is the memory/CPU pressure source.

    Steps per round (with individual timing):
      0. find    - git checkout reset + verify/locate the target file
      1. read    - inspect the target file (agent confirming context)
      2. edit    - apply the find->replace pair (real semantic edit)
      3. verify  - write ad-hoc test file + run it (MEMORY PEAK)
      4. diff    - git diff -> patch file (agent verification artifact)

    No production build, no full test suite, no resident dev server - none
    appear in the real openclaw traces.

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
            logger.info(f"[Sandbox{self.state.sandbox_id}] No sandbox handle for coding round")
            return

        source_files = self.config.coding_source_files
        if not source_files:
            logger.info(f"[Sandbox{self.state.sandbox_id}] No coding source files configured")
            return

        pair_idx = self.round_id % len(source_files)
        pair = source_files[pair_idx]

        start_time = time.perf_counter()

        success, step_times, verify_success, compile_only, failed_step, error_detail, timed_out = self._execute_steps(
            sbx, pair
        )

        elapsed = self._record_metrics(
            start_time, success, step_times, verify_success, compile_only, timed_out, error_detail
        )

        if success:
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            logger.info(f"[Sandbox{self.state.sandbox_id}] Coding round completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(pair["file"], failed_step, error_detail)

    def _execute_steps(self, sbx, pair: Dict[str, str]) -> Tuple[bool, Dict[str, float], bool, str, str, bool, bool]:
        """Execute all steps: find -> read -> edit -> verify -> diff

        Args:
            sbx: Sandbox object
            pair: Replacement pair {"file": str, "find": str, "replace": str,
            "verify_script": str(optional), "verify": str(optional)}

        Returns:
            Tuple of (success, step_times, verify_success, compile_only,
            failed_step, error_detail, timed_out). compile_only is True only
            when verify passed via a compile-only check (no assertion).
        """
        success = True
        step_times = {}
        failed_step = None
        error_detail = ""
        verify_success = False
        compile_only = False
        timed_out = False
        project_dir = self.config.coding_project_dir
        target_file = pair["file"]
        find_str = pair["find"]
        replace_str = pair["replace"]

        try:
            locate_ok, locate_error, resolved_file, resolved_find, resolved_replace = self._step_find(
                sbx, project_dir, target_file, find_str, replace_str, step_times
            )
            if not locate_ok:
                logger.warning(f"[Sandbox{self.state.sandbox_id}] find warning: {locate_error}")
            target_file = resolved_file
            find_str = resolved_find
            replace_str = resolved_replace
            pair = {**pair, "file": target_file}

            self._step_read(sbx, project_dir, target_file, step_times)

            edit_success, edit_error = self._step_edit(sbx, project_dir, target_file, find_str, replace_str, step_times)
            if not edit_success:
                failed_step = "edit"
                error_detail = edit_error
                success = False
                return success, step_times, verify_success, compile_only, failed_step, error_detail, timed_out

            if not self.config.coding_skip_verify:
                verify_success, verify_error, compile_only = self._step_verify(sbx, project_dir, pair, step_times)
                if not verify_success:
                    failed_step = "verify"
                    error_detail = verify_error
                    success = False
            else:
                verify_success = True  # skipped = not failed

            self._step_diff(sbx, project_dir, step_times)

        except Exception as e:
            success = False
            timed_out = "timed out" in str(e).lower() or "context deadline exceeded" in str(e).lower()
            failed_step, error_detail = self._classify_exception(e, step_times)

        return success, step_times, verify_success, compile_only, failed_step, error_detail, timed_out

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
            f"cd {project_dir} && git checkout -- {profile.checkout_paths} || true",
            timeout=30,
            user="root",
        )
        exists = sbx.commands.run(f"cd {project_dir} && test -f {target_file} && echo ok", timeout=15, user="root")
        step_times["find"] = step_times.get("find", 0.0) + (time.perf_counter() - step_start)

        if exists.exit_code == 0 and "ok" in (exists.stdout or ""):
            return True, "", target_file, find_str, replace_str

        fallback = sbx.commands.run(
            f"cd {project_dir} && find {profile.source_find_root} {_find_name_clause(profile.source_find_names)} 2>/dev/null | head -1",
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
        """Step 2: Apply the find->replace pair via literal string replace (real semantic edit).

        Uses python3 str.replace (see _build_edit_command) - literal, not sed
        regex - so regex metacharacters in the find/replace strings are inert.

        Returns: (success, error_detail). Exit code 2 = find string absent
        (no-op edit surfaced as a failure, not a silent fake verify pass).
        """
        step_start = time.perf_counter()
        result = sbx.commands.run(
            _build_edit_command(project_dir, target_file, find_str, replace_str),
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
    ) -> Tuple[bool, str, bool]:
        """Step 3: Write ad-hoc test file(s) to /tmp + run them (trace-faithful verify).

        ts: N independent npx tsx processes chained in one command (raises steady-state
        CPU, the only faithful lever - see _run_verify). go: single write+go run.

        Timing is handled inside `_run_verify`: `step_times["verify"]` (write+run)
        and, for go, `step_times["verify_clean"]` (go clean -cache, kept out of
        CODING_STEP_ORDER). Returns (success, error_detail, compile_only).
        """
        return _run_verify(sbx, project_dir, self.config, pair, step_times=step_times, round_id=self.round_id)

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
                return "unknown", f"operation timed out: {error_str[:400]}"
        else:
            return "exception", f"exception: {error_str[:800]}"

    def _record_metrics(
        self,
        start_time: float,
        success: bool,
        step_times: Dict[str, float],
        verify_success: bool,
        compile_only: bool,
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
            compile_only=compile_only,
        )
        self.state.update_last_task_time(time.time())

        if not success and error_detail:
            self.state.coding_metrics.last_error = error_detail

        return elapsed

    def _handle_failure(self, target_file: str, failed_step: str, error_detail: str) -> None:
        """Handle failure after metrics are recorded"""
        logger.error(
            f"[Sandbox{self.state.sandbox_id}] File '{target_file}' failed at {failed_step}: {error_detail[:600]}"
        )
        self.consecutive_errors += 1
        if self.consecutive_errors >= 3:
            self.state.is_alive = False
