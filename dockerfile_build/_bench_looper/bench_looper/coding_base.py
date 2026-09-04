#!/usr/bin/env python3
"""Shared coding-bench skeleton: find -> read -> edit -> verify -> diff.

The loop (verified against captured openclaw agent trajectories on
gohugoio/hugo and vuejs/core) is identical across languages; only the
verify mechanics differ. Each language subclass (CodingGoBench,
CodingTsBench) implements _step_verify.

Step semantics (mirrors e2b_bench/coding_task_runner.CodingRoundRunner):
    0. find   - git checkout reset + verify/locate the target file
    1. read   - inspect the target file (agent confirming context)
    2. edit   - literal find->replace (real semantic edit)
    3. verify - write ad-hoc test file(s) to /tmp + run them (MEMORY PEAK)
    4. diff   - git diff -> patch file (agent verification artifact)

No production build, no full test suite, no resident dev server - none
appear in the real traces. Memory pressure comes from N containers' verify
peaks overlapping, observed at the host level via vm_monitor/smap_tool.
"""

import base64
import time

from bench_looper.core import BenchScenario, IterationResult, run_shell


def build_edit_command(project_dir: str, target_file: str, find_str: str, replace_str: str) -> str:
    """Literal find->replace via python3 str.replace; base64-inert quoting.

    Ported from e2b_bench/coding_task_runner._build_edit_command. find/replace
    are base64-encoded so backticks, `|`, `$`, backslashes, quotes and
    newlines are all inert (sed regex metacharacters broke the earlier form).
    Exit 2 if the find string is absent (a no-op edit surfaced as a failure,
    not a silent success that would fake a verify pass).
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


class CodingBench(BenchScenario):
    """Base for coding-go / coding-ts plugins.

    Subclasses set the profile fields and implement _step_verify. Inherits
    BenchScenario so run_warmup is a defined no-op (coding warmup is a no-op:
    go clears GOCACHE per verify, ts/esbuild re-transpiles per run - neither
    has a persistent cache to warm).
    """

    name = "coding"

    # --- profile fields (set by subclasses) ---
    project_dir: str = ""
    checkout_paths: str = ""
    source_find_names: tuple[str, ...] = ()
    source_find_root: str = "packages"
    pairs: list[dict] = []
    skip_verify: bool = False
    verify_timeout: int = 120

    def run_one_round(self, round_id: int) -> IterationResult:
        if not self.pairs:
            return IterationResult(
                round_id,
                False,
                failed_step="find",
                error_type="exception",
                error_message="no coding source files configured",
            )
        pair = self.pairs[round_id % len(self.pairs)]
        target_file = pair.get("file", "")
        find_str = pair.get("find", "")
        replace_str = pair.get("replace", "")
        steps: dict[str, float] = {}

        try:
            target_file, find_str, replace_str = self._step_find(target_file, find_str, replace_str, steps)
            self._step_read(target_file, steps)

            ok, err = self._step_edit(target_file, find_str, replace_str, steps)
            if not ok:
                return IterationResult(
                    round_id, False, steps, failed_step="edit", error_type="exit_code", error_message=err
                )

            verify_success = True
            compile_only = False
            if not self.skip_verify:
                verify_success, err, compile_only = self._step_verify(pair, round_id, steps)
                if not verify_success:
                    return IterationResult(
                        round_id,
                        False,
                        steps,
                        verify_success=False,
                        compile_only=compile_only,
                        failed_step="verify",
                        error_type="exit_code",
                        error_message=err,
                    )

            self._step_diff(round_id, steps)
            return IterationResult(round_id, True, steps, verify_success=verify_success, compile_only=compile_only)
        except Exception as exc:  # plugin/step bug must not abort the run
            return IterationResult(
                round_id, False, steps, failed_step="exception", error_type="exception", error_message=str(exc)[:800]
            )

    # --- shared steps ---

    def _step_find(self, target_file, find_str, replace_str, steps):
        """Step 0: git checkout reset + verify/locate the target file.

        checkout/locate failure is non-fatal; on miss it falls back to a
        located file with a generic comment-marker pair so the round still
        produces a verify peak (mirrors the host runner).
        """
        t = time.perf_counter()
        run_shell(f"cd {self.project_dir} && git checkout -- {self.checkout_paths} || true", 30)
        code, out, _ = run_shell(f"cd {self.project_dir} && test -f {target_file} && echo ok", 15)
        steps["find"] = time.perf_counter() - t
        if code == 0 and "ok" in out:
            return target_file, find_str, replace_str
        clause = self._find_name_clause()
        code, out, _ = run_shell(
            f"cd {self.project_dir} && find {self.source_find_root} {clause} 2>/dev/null | head -1", 15
        )
        found = (out or "").strip().splitlines()
        if found:
            return found[0], "// bench marker", "// bench round\n// bench marker"
        return target_file, find_str, replace_str

    def _find_name_clause(self) -> str:
        names = self.source_find_names or ("*.ts",)
        if len(names) == 1:
            return f"-name '{names[0]}'"
        inner = " -o ".join(f"-name '{n}'" for n in names)
        return f"\\( {inner} \\)"

    def _step_read(self, target_file, steps):
        t = time.perf_counter()
        run_shell(f"cd {self.project_dir} && head -20 {target_file}", 15)
        steps["read"] = time.perf_counter() - t

    def _step_edit(self, target_file, find_str, replace_str, steps) -> tuple[bool, str]:
        t = time.perf_counter()
        code, out, err = run_shell(build_edit_command(self.project_dir, target_file, find_str, replace_str), 15)
        steps["edit"] = time.perf_counter() - t
        if code != 0:
            msg = f"edit failed: exit_code={code}"
            if err:
                msg += f" stderr={err[:100]}"
            msg += f" file={target_file}"
            return False, msg
        return True, ""

    def _step_diff(self, round_id, steps):
        t = time.perf_counter()
        run_shell(f"cd {self.project_dir} && git diff > /tmp/bench_round_{round_id}.patch", 15)
        steps["diff"] = time.perf_counter() - t

    def _step_verify(self, pair: dict, round_id: int, steps: dict[str, float]):
        """Return (success, error_detail, compile_only). Override per language."""
        raise NotImplementedError
