#!/usr/bin/env python3
"""Coding (TypeScript) scenario plugin: vuejs/core trace-faithful verify loop.

Per round the skeleton is the same find->read->edit->verify->diff as Go, but
verify is N independent `npx tsx` processes chained in ONE command with `&&`
(mirrors e2b_bench/coding_task_runner._run_verify ts path):

    cat > /tmp/bench_verify_0.mjs << 'EOF' ... EOF && npx tsx /tmp/bench_verify_0.mjs && \
    cat > /tmp/bench_verify_1.mjs << 'EOF' ... EOF && npx tsx /tmp/bench_verify_1.mjs && ...

Each body is stamped from a {template, assert} entry of the shared
DEFAULT_VERIFY_TEMPLATES pool (6 compiler-core baseParse cases), offset by
round_id % pool_len so consecutive rounds pick different N-subsets (mirrors
the agent rewriting its ad-hoc test per verify). The 8 agent globals + the
compiler-core dynamic import are verbatim the captured openclaw trajectory.

N (default 3) is the only lever proven to raise single-firecracker steady-
state CPU while staying trace-faithful: the real agent repeatedly spawns
independent `npx tsx` verifies within one issue. `&&` fail-fast keeps it as
one verify step in metrics (the agent's verify is one continuous action).
"""

import time

from bench_looper.coding_base import CodingBench
from bench_looper.core import load_operations, run_shell


def stamp_body(template: str, assert_code: str, globals_block: str, import_block: str) -> str:
    """Stamp a {template, assert} pair into a full ad-hoc verify .mjs body.

    8 agent globals + compiler-core import + baseParse(template) + assert +
    print. Ported from e2b_bench/schemas._stamp_verify_body.
    """
    return (
        globals_block + import_block + f"  const ast = m.baseParse({template!r}, {{ parseMode: 'html' }})\n"
        f"  {assert_code}\n"
        "  console.log('All tests passed!')\n"
        "})\n"
    )


class CodingTsBench(CodingBench):
    name = "coding-ts"

    def __init__(self, pairs, profile, pool: list[dict], verify_repeat: int, skip_verify: bool, verify_timeout: int):
        self.pairs = pairs
        self.skip_verify = skip_verify
        self.verify_timeout = verify_timeout
        self.project_dir = profile["project_dir"]
        self.checkout_paths = profile["checkout_paths"]
        self.source_find_names = tuple(profile["source_find_names"])
        self.source_find_root = profile["source_find_root"]
        self.heredoc_eof = profile["heredoc_eof"]
        self.run_cmd = profile["run_cmd"]
        self.globals_block = profile["_globals"]
        self.import_block = profile["_import"]
        self.pool = pool
        self.verify_repeat = verify_repeat

    @classmethod
    def build(cls, args) -> "CodingTsBench":
        ops = load_operations("coding_ts_pairs.json")
        profile = ops["profile"]
        verify_repeat = getattr(args, "verify_repeat", 0) or profile.get("verify_repeat", 3)
        return cls(
            ops["pairs"],
            profile,
            ops["verify_templates"],
            verify_repeat,
            args.skip_verify,
            getattr(args, "verify_timeout", profile.get("verify_timeout", 120)),
        )

    def _step_verify(self, pair: dict, round_id: int, steps: dict[str, float]) -> tuple[bool, str, bool]:
        # Multi-process verify: N independent npx tsx processes chained in one
        # command. Each body is stamped from a pool template; offset by round
        # so consecutive rounds differ. Temp files are indexed so the i-th
        # cat+run pair uses /tmp/bench_verify_{i}.mjs. `&&` fail-fast: first
        # non-zero exit stops the rest.
        n = max(1, self.verify_repeat)
        pool = self.pool
        offset = round_id % len(pool) if pool else 0
        parts = [f"cd {self.project_dir}"]
        for i in range(n):
            entry = pool[(offset + i) % len(pool)]
            body = stamp_body(entry["template"], entry["assert"], self.globals_block, self.import_block)
            path_i = f"/tmp/bench_verify_{i}.mjs"
            eof = self.heredoc_eof
            parts.append(f"cat > {path_i} << '{eof}'\n{body}{eof}\nnpx tsx {path_i}")
        cmd = " && ".join(parts)

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
