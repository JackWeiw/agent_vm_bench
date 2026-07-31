# E2B Coding (TS) — Raise Steady-State CPU via Multi-Process Verify

## Context

The e2b coding benchmark's TS (vuejs/core — 96% TypeScript) verify shows a per-firecracker CPU peak of ~58% on the first verify in a sandbox, collapsing to ~28% on every later verify. The customer wants the steady state raised (toward ~40%). This spec is the design for doing so without fabricating load.

> The language key is **`ts`** throughout (the prior `js` key was renamed to `ts` on main — vuejs/core is a TS project; `CODING_LANGUAGE_PROFILES`, `CODING_LANGUAGE_DEFAULT_SOURCE_FILES`, the dataclass default `coding_language="ts"`, and `config/e2b/coding_bench.yaml` all use `"ts"`). The Go profile (`"go"`) is unchanged.

## Ground truth — why 58%→28% happens (measured)

A series of sandbox probes (against `/opt/coding-bench` with `npx tsx` + vuejs/core source) established the following, each measured:

1. **The per-verify CPU is almost entirely `npx tsx` process startup + esbuild transpile of the compiler-core module graph + node module load.** A single verify is ~0.47s wall, ~0.47s user CPU.
2. **The actual compile work is negligible.** A single `baseCompile(template)` is ~4ms of real compute. Looping it 1000× inside one process costs only ~114ms of inner time — i.e. 1000 compiles add up to less than one process start. `baseParse` vs `baseCompile` show **no measurable difference** (both ~0.30-0.47s, the difference drowned in startup).
3. **Therefore single-process work amplification does NOT raise CPU.** Looping `baseParse`/`baseCompile` N× in one `npx tsx` process leaves the wall time flat (~0.30s for N=1/5/10/20). Confirmed dead: "loop more inside one verify" and "switch parse→compile" both fail to raise load.
4. **The collapse 58%→28% is OS page cache.** The first verify is cold (node + esbuild native binary + module graph all read from disk, ~977ms `sys`); the second onward finds everything in the OS page cache, `sys` collapses to ~70ms, only ~290ms `user` remains. The real openclaw agent exhibits the same shape — its trace runs `npx tsx /tmp/test_vpre_textarea.mjs` 10 times against only 6 rewrites of the file, i.e. it repeats near-identical verifies and rides the same warm-cache steady state. So the drop is faithful, surfaced explicitly (see the companion Decision in `2026-07-30-e2b-coding-trace-faithful-design.md`) so a reviewer does not mistake it for a defect.

## Decision — F: serial multi-process verify (configurable, default 3)

The only lever that raises single-firecracker CPU **and** stays trace-faithful is to make one verify step spin up **N independent `npx tsx` processes serially**, each paying the fixed ~0.47s startup cost. Measured: N=3 → 1.49s, N=5 → 2.36s, N=10 → 4.67s — **perfectly linear** (≈ N×0.47s), vs single-process looping which stays flat.

### Why faithful (defensible to a strong reviewer)

The real agent does not verify once and stop. In a single issue it repeatedly spins up independent `npx tsx /tmp/test_*.mjs` processes — the captured vuejs/core trace shows 12 independent `npx tsx` invocations across 6 ad-hoc-test rewrites. Each is a fresh process paying the full startup cost. Our current bench fires only 1 process per verify step; raising it to N mirrors the agent's actual per-issue verify intensity (the agent verifying multiple cases / iterating the test multiple times). It is **not** fabricated load — it is the agent's real behavior made denser, exactly as the agent itself would do on a harder issue. Contrast: cache eviction (`drop_caches`) would diverge (the agent never evicts), and single-process looping is pointless (compile is ~4ms). F is the only honest multiplier.

### N and duty cycle

`verify_repeat_count` is configurable (default 3). Duty cycle at `round_interval=3s`:
- N=3 → ~1.5s/verify → ~50% peak (target ~40%, headroom)
- N=2 → ~0.94s → ~31%
- N=4 → ~1.9s → ~63%

Default 3 targets ~40-50%, leaving margin. `verify_timeout` (120s) is never approached (max N=10 → ~4.7s).

### Implementation shape (single commands.run, N write+run chained)

The runner maintains a **shared, ordered template pool** `DEFAULT_VERIFY_TEMPLATES` (6-8 distinct templates + per-template assertions, all the compiler-core `baseParse` cases already sandbox-verified — div/interpolation, v-pre textarea, v-for, v-if/v-else, multi-root fragment, baseParse+props). For a verify step with `verify_repeat=N`, the runner takes N templates from the pool — offset by `round_id % pool_len` so different rounds pick different N-subsets (avoids every round repeating the identical N bytes, mirroring the agent rewriting its test between verifies) — emits N distinct bodies (8-global header + `import compiler-core + baseParse(template)`), writes each to `/tmp/bench_verify_{i}.mjs`, and chains them in **one** `commands.run`:

```bash
cd {project} && \
  cat > /tmp/bench_verify_0.mjs << 'EOF' <body_0> EOF && npx tsx /tmp/bench_verify_0.mjs && \
  cat > /tmp/bench_verify_1.mjs << 'EOF' <body_1> EOF && npx tsx /tmp/bench_verify_1.mjs && \
  ... (N total)
```

Rationale for one `commands.run` (not N separate calls): the agent's verify is a single combined write+run; N chained write+runs in one command preserves that "one verify step = one continuous verification action" shape and keeps it as **one** verify step in metrics (verify success is "all N passed", not "N separate passes"). Fail-fast: `&&` chains so the first non-zero exit stops the rest and reports that failure's stderr.

### Why a shared template pool (not per-pair `verify_templates`)

Separation of concerns, single source of truth: pairs own the *edit semantics* (`{file, find, replace}` — what the agent changed), the pool owns the *verify workload* (what templates stress the parser). The bench's verify was never assertion-tied to the edited symbol anyway — edits are comment/format changes and verify is a generic "project compiles + parser runs" health check, exactly as the real agent verifies project health by running its ad-hoc test. Decoupling means pair count, template count, and N all vary independently; maintenance touches one pool. Per-pair `verify_templates` would bloat yaml 6× and duplicate the same v-for/v-if templates across pairs.

## Components touched

| File | Change |
|------|--------|
| `e2b_bench/schemas.py` | Add `DEFAULT_VERIFY_TEMPLATES` (ordered list of `{template, assert}` — the 6 sandbox-verified compiler-core baseParse cases). `DEFAULT_CODING_SOURCE_FILES` pairs drop `verify_script` (edits are comment/format; verify uses the shared pool, not a per-pair script). `DEFAULT_CODING_VERIFY_SCRIPT_JS` repurposed as the single-template skeleton (8 globals + compiler-core baseParse) the runner stamps each pool template into. |
| `e2b_bench/config.py` | Add `coding_verify_repeat: int = 3` dataclass field; `_from_dict` reads `coding.verify_repeat` (yaml `verify_repeat`); `from_args` (both paths) wire CLI/yaml; add CLI `--coding-verify-repeat`. |
| `e2b_bench/coding_task_runner.py` | `_run_verify`: pick N templates from `DEFAULT_VERIFY_TEMPLATES` offset by `round_id % len`, stamp each into the skeleton → N bodies, write `/tmp/bench_verify_{i}.mjs`, chain N `cat>npx tsx` in one `commands.run` (`&&` fail-fast). Single wall timeout (N=3 ~1.5s ≪ `verify_timeout` 120s). Verify success = all N exit 0. `compile_only` label preserved (metrics), does not change the N-process run. Go path: `coding_verify_repeat` default 1 → single process, no chaining (go's cold-compile is already heavy). |
| `config/e2b/coding_bench.yaml` | Add `coding.verify_repeat: 3` (yaml-configurable; CLI `--coding-verify-repeat` overrides). Pairs simplify to `{file, find, replace}` (+ optional `verify: compile_only` label). |
| `config/e2b/coding_go_bench.yaml` | Optionally add `coding.verify_repeat: 1` for explicitness (go stays N=1 — its `go run` cold-compile is already real load). |
| `dockerfile_build/bench_helper.sh` | Manual helper: loop N `npx tsx` calls, each with a distinct pool template (offset by round), mirroring the runner. Add `BENCH_VERIFY_REPEAT` env override (default 3). |
| `e2b_bench/tests/test_coding_task_runner.py` | Assert `_run_verify` emits N chained write+run in one command; templates offset by round; `verify_repeat` config plumbing (yaml + CLI); go path unaffected (N=1 → single call, no chain); pairs no longer require `verify_script`. |

## Go note (out of scope for repeat)

Go's verify already runs a real cold-compile per round (`go clean -cache` before each `go run`, see the go design doc). Go does **not** benefit from multi-process repeat the way TS does — its per-verify cost is the genuine compile, already heavy. Go keeps `verify_repeat` default 1.

## Verification

1. `python -m pytest e2b_bench/tests/ -v` — new tests for N-chained verify + config plumbing.
2. Config load smoke for `e2b_coding_bench.yaml` (verify_repeat present, default 3).
3. Manual sandbox: re-run JS round_robin; expect per-firecracker steady-state peak ~40-50% (vs 28%), verify step wall ~1.5s (N=3), all rounds pass, Verify Success unaffected.
4. `pre-commit run --all-files` clean.

## Out of scope

- Cache eviction (`drop_caches`) — diverges from trace, and useless (esbuild has no cross-process content cache).
- Single-process looping / parse→compile — measured dead.
- Multi-sandbox overlap tuning (C) — raises host, not single firecracker; separate concern.
- Raising N per-sandbox adaptively — fixed configurable N is enough.
