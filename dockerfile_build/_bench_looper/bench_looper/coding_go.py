#!/usr/bin/env python3
"""Coding (Go) scenario plugin: gohugoio/hugo trace-faithful verify loop.

Per round (mirrors e2b_bench/coding_task_runner.CodingRoundRunner go path):
    find -> read -> edit -> verify -> diff

Verify is a single write + `go run /tmp/bench_verify.go`. Before it,
`go clean -cache` runs as a SEPARATE command (timed into step_times
"verify_clean", kept out of the find/read/edit/verify/diff step order) so
every verify is a real cold compile - the Go toolchain's GOCACHE would
otherwise make the 2nd+ run a ~10% cache hit that masks the real per-verify
CPU pressure the benchmark needs to measure. Go ignores --verify-repeat
(N=1); its cold-compile is already real load.

The hugo module graph is intentionally NOT pre-downloaded: verify scripts
import only the Go stdlib, so `go run` compiles without it (matching the
real agent trajectory, which never ran `go mod download`).
"""

import time

from bench_looper.coding_base import CodingBench
from bench_looper.core import load_operations, run_shell


class CodingGoBench(CodingBench):
    name = "coding-go"

    def __init__(self, pairs, profile, skip_verify, verify_timeout):
        self.pairs = pairs
        self.skip_verify = skip_verify
        self.verify_timeout = verify_timeout
        self.project_dir = profile["project_dir"]
        self.checkout_paths = profile["checkout_paths"]
        self.source_find_names = tuple(profile["source_find_names"])
        self.source_find_root = profile["source_find_root"]
        self.temp_test_path = profile["temp_test_path"]
        self.heredoc_eof = profile["heredoc_eof"]
        self.run_cmd = profile["run_cmd"]
        self.pre_verify_cmd = profile.get("pre_verify_cmd", "")
        self.default_verify_script = profile["default_verify_script"]

    @classmethod
    def build(cls, args) -> "CodingGoBench":
        ops = load_operations("coding_go_pairs.json")
        profile = ops["profile"]
        return cls(
            ops["pairs"], profile, args.skip_verify, getattr(args, "verify_timeout", profile.get("verify_timeout", 120))
        )

    def _step_verify(self, pair: dict, round_id: int, steps: dict[str, float]) -> tuple[bool, str, bool]:
        # Optional pre-verify cache clear (go only). Separate command so its
        # time is measured apart from the write+run (see module docstring).
        if self.pre_verify_cmd:
            t = time.perf_counter()
            run_shell(f"cd {self.project_dir} && {self.pre_verify_cmd}", 60)
            steps["verify_clean"] = time.perf_counter() - t

        # Single write+run (go ignores --verify-repeat; N go runs would diverge
        # from the trace). Use the pair's verify_script if present, else the
        # shared Go default (compiles + runs a no-op main).
        body = pair.get("verify_script") or self.default_verify_script
        eof = self.heredoc_eof
        cmd = (
            f"cd {self.project_dir} && "
            f"cat > {self.temp_test_path} << '{eof}'\n"
            f"{body}\n"
            f"{eof}\n"
            f"{self.run_cmd}"
        )
        t = time.perf_counter()
        code, out, err = run_shell(cmd, self.verify_timeout + 30)
        steps["verify"] = time.perf_counter() - t
        if code != 0:
            msg = f"verify failed: exit_code={code}"
            if err:
                msg += f" stderr={err[:800]}"
            if out:
                msg += f" stdout={out[:800]}"
            return False, msg, False
        return True, "", False
