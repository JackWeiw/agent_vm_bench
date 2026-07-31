# E2B Coding Benchmark Redesign — Trace-Faithful Loop (Revision)

**Date:** 2026-07-30
**PR:** #58 (`feat: add E2B coding benchmark for AI agent memory stress testing`)
**Branch:** `coding`
**Supersedes:** `2026-07-29-e2b-coding-redesign-design.md` (project swap + workflow steps). This revision narrows the workflow to match a **real captured openclaw agent trajectory** on vuejs/core, and removes steps absent from that trace.

## Goal

Make the coding benchmark's per-round workflow **identical in shape to a real AI coding agent's behavior** on the chosen repo, as evidenced by a captured trajectory — so that the memory pressure the benchmark induces is defensible to a technically strong customer reviewing "is this what an agent actually does?"

## Ground truth — the real vuejs/core agent trajectory

A real openclaw agent run solving a vuejs/core issue (v-pre + textarea tokenizer) was captured at:

```
C:\Users\jack\Desktop\harness\swe_bench\vue_jsonl\7c5622d8-*.trajectory.jsonl
```

Extracted agent actions, in order:

| Phase | What the agent actually did |
|-------|------------------------------|
| find  | `find … -name "*.ts"`, `ls packages/compiler-core/src/`, `grep -rn "textarea\|isPreTag\|V_PRE" …` |
| read  | `grep -n -A5 … tokenizer.ts`, `cat` the file |
| edit  | edited `packages/compiler-core/src/tokenizer.ts` |
| verify | wrote `/tmp/test_vpre_textarea.mjs` that does `import { baseParse } from '/root/.openclaw/workspace/vue/packages/compiler-core/src/index.ts'` (raw `.ts` source, **not** built dist), then ran **`npx tsx /tmp/test_vpre_textarea.mjs`** |
| diff  | `git diff -- packages/compiler-core/src/tokenizer.ts > patch.txt` |

**What is NOT in the real trace:**

- ❌ `npm run build` / `node scripts/build.js` (rollup production build) — the agent never runs it.
- ❌ `pnpm test` / vitest suite — the agent never runs it.
- ❌ `vite dev` / any resident dev server — never appears; verification is a **transient** `npx tsx` process.
- ❌ any resident process of any kind — `npx tsx` runs a few seconds and exits.

This matches the two other captured openclaw traces (`hugo` writes a standalone `test_alert.go`; `sqlfluff` does `pip install -e .` then inline `python3 -c` verification) — **none** of them run a production build or keep a resident server. The agent's verification is always a small, self-written, transient check.

## Background & Constraints

The current implementation (`coding_task_runner.py` + `Dockerfile.coding` + `e2b_coding_bench.yaml`) diverges from the trace in three places:

1. `node scripts/build.js` (rollup production build) as a per-round step — **not in trace**.
2. `pnpm test` (vitest) as a per-round step — **not in trace**.
3. A persistent `/opt/vite-playground` dev server as a background memory carrier — **fabricated, not in trace**.

These three also caused the runtime `spawn rollup ENOENT` failure during `--warmup-only`: the build-time `export PATH="$(pnpm bin):$PATH"` in the Dockerfile does not persist into the E2B sandbox runtime, so `scripts/build.js` spawning `rollup` fails. Removing the production build removes the failure at its root — no runtime PATH patch is needed because the new verification step uses only `npx` (already symlinked at `/usr/local/bin/npx`).

Hard constraints (unchanged):

- Per sandbox: 2vCPU / 4GB.
- CPU load: keep low (the trace's `npx tsx` verification is esbuild-transpiled, fast, low CPU — fits).
- Tech stack: stay JS/TS on vuejs/core (Node24 + pnpm environment already in `Dockerfile.coding`).
- Memory: host-level overcommit from **N concurrent sandboxes × transient verification peaks overlapping**, observed via `vm_monitor` / `smap_tool`. No per-round `free -m`.

## Decision 1 — Workflow: find → read → edit → verify → git diff

The per-round step order becomes the trace's exact shape:

```
round:
  find    — git checkout reset + grep/ls to locate the target source file
  read    — head/cat the file to confirm context
  edit    — apply a pre-configured find→replace pair (real semantic edit)
  verify  — (a) write an ad-hoc test file to /tmp, (b) run it via `npx tsx`
  diff    — git diff > patch file (agent's verification artifact)
```

The `verify` step explicitly mirrors the trace's two sub-actions — the agent
first writes a temporary test file, then runs it:

- **(a) write temp test file**: `cat > /tmp/bench_verify.mjs << 'EOF' ... EOF`,
  producing an ad-hoc `.mjs` that imports the **raw** target source
  (e.g. `import { baseParse } from '/opt/coding-bench/packages/compiler-core/src/index.ts'`,
  raw `.ts`, not built dist), sets the vue feature flags the source needs
  (`globalThis.__DEV__ = true` etc.), and asserts the edited symbol works.
  This mirrors the trace's `cat > /tmp/test_vpre_textarea.mjs << 'EOF'`.
- **(b) run via `npx tsx`**: `npx tsx /tmp/bench_verify.mjs` — the exact
  command the trace uses to execute the ad-hoc test.

`CODING_STEP_ORDER` in `schemas.py` changes from `[find, read, edit, build, test, diff]` to `[find, read, edit, verify, diff]`.

- **`build` step: removed.** Not in the real trace; an agent fixing a core-library file does not run a rollup production build to verify. (See "Should the production build be kept?" below.)
- **`test` step: removed** as a separate step. Replaced by `verify`, which is the trace-faithful equivalent: the agent's own ad-hoc test run via `npx tsx`. The full vitest suite is not run by the real agent.
- **`memory` (free -m) step: stays removed** (already removed in the prior redesign).
- **`git checkout -- packages/ src/` reset: kept** as setup inside the `find` step (agent reverting the previous round's edit).

### Should the production build (`node scripts/build.js`) be kept? — No.

Rationale, recorded for review:

1. **Trace fidelity.** The captured vuejs/core agent never runs a production build; it verifies with `npx tsx` against raw `.ts` source. A build step absent from the trace is exactly the kind of "added for memory, not for realism" insertion a strong reviewer flags.
2. **Redundancy.** `npx tsx` verification already loads esbuild + the full compiler-core/reactivity/shared TS module graph into memory (a real transient peak). Adding a rollup build on top manufactures extra peak without adding trace fidelity.
3. **Maintenance / failure surface.** The rollup PATH/`node_modules/.bin` issue already broke docker build and warmup (`spawn rollup ENOENT`). Removing the build step removes that failure class entirely — runtime only needs `/usr/local/bin/npx`, already symlinked.
4. **Memory pressure is still achieved** honestly: N sandboxes × staggered `npx tsx` verification peaks overlapping at the host = real overcommit pressure. No fabricated resident process required.

If a heavier single-sandbox peak is ever needed, the honest lever is **concurrency** (more sandboxes overlapping), not a non-trace build step.

## Decision 2 — Verify step: write temp test file + `npx tsx` against raw `.ts` source

The `verify` step mirrors the trace exactly, in two sub-actions:

**(a) Write the temporary test file.** A pre-staged ad-hoc test script (per
replacement pair, or a shared default) is written into the sandbox at
`/tmp/bench_verify.mjs` via `cat > /tmp/bench_verify.mjs << 'EOF' ... EOF`.
It imports the **raw** target source (e.g.
`import { baseParse } from '/opt/coding-bench/packages/compiler-core/src/index.ts'`)
— same as the trace, which imports `.ts`, not built dist. The global flags the
vue source needs (`__DEV__`, `__BROWSER__`, `__FEATURE_*`) are set at the top of
the script (the trace does this with `globalThis.__DEV__ = true` etc.). The
script asserts the edited symbol still works (no throw / expected value).

**(b) Run via `npx tsx`.** Executed via **`npx tsx /tmp/bench_verify.mjs`** —
the exact command the trace uses. `verify` step timing covers both sub-actions.

Why `npx tsx` and not `node`:

- vuejs/core source is TypeScript + uses `globalThis.__*` feature flags + bare-specifier imports resolvable only through the repo's tsconfig paths. `tsx` (esbuild-based) transpiles and resolves these; plain `node` cannot run the raw `.ts`.
- `npx tsx` is what the captured agent actually typed — mirroring it is the most defensible choice.

`tsx` ships as a devDependency of vuejs/core (`tsx` is in the pnpm install already); `npx` resolves it from `node_modules/.bin`. No PATH change needed at runtime beyond what `npx` already provides.

### Verification scripts

Each `source_file` pair gains an optional `verify_script` field — a small `.mjs` body that exercises the edited symbol. A shared default verify script covers pairs without a specific script. This mirrors the trace's "agent writes a focused test for the thing it changed."

**Resolved entry (implementation):** the default (and every pair's `verify_script`) imports **`compiler-core`** and runs `baseParse` — the agent's actual verify entry (its trace imports only `compiler-core`'s `baseParse`/`parse`). `compiler-core` is the heaviest trace-faithful entry that runs under a bare `npx tsx` without hitting the `__TEST__` build global: the vue/runtime-core/compiler-dom/compiler-sfc graphs all reach `compiler-dom/src/errors.ts` (references `__TEST__`) and crash on a real call; the parser alone avoids that path (~467ms user steady vs ~299ms for the lightweight `shared` package). Each pair's template/assertion differs so consecutive rounds don't repeat identical bytes (mirrors the agent rewriting its ad-hoc test per verify). `__TEST__` is intentionally NOT injected — the agent didn't either.

## Decision 3 — Vite playground dev server: removed entirely

The `/opt/vite-playground` Dockerfile stage, the `coding_dev_cmd` / `coding_dev_dir` / `coding_dev_wait` / `coding_skip_dev_server` config plumbing, the `_start_dev_server` / `_check_dev_server_running` / `_step_ensure_dev_server` runner code, and the warmup "start dev server" phase are all **removed**.

- No resident dev server. The trace has none.
- `CodingWarmupRunner` is reduced to: verify project present + run one initial `npx tsx` verification (to warm esbuild/node module caches and confirm project health). This establishes a real, trace-faithful warm state without a fabricated background process.
- The `vite`, `@vitejs/plugin-vue` devDependencies of the playground are gone; vuejs/core's own devDependencies (rollup/esbuild/tsx/vitest) remain — that's the real dependency surface an agent touches.

## Decision 4 — Edit mechanism: unchanged replacement pairs

The pre-configured `{file, find, replace}` replacement pairs from the prior redesign are kept (`DEFAULT_CODING_SOURCE_FILES` in `schemas.py`). They are real, type-safe, verified edits to vuejs/core source. The only change: each pair may carry a `verify_script` (Decision 2); the bare-file CLI fallback still normalizes to a generic comment-marker pair + default verify script.

## Memory pressure model (updated, trace-faithful)

```
Time → (per sandbox)
Warmup:    ████  one npx tsx verify  (esbuild + module graph loaded transiently)
Round:     find  read  edit  verify(peak)  diff
                              ↑
                       npx tsx loads TS module graph (transient peak)
                       × N concurrent sandboxes, staggered → host overcommit
```

- **Per-sandbox peak**: the `npx tsx` verification loading compiler-core + reactivity + shared TS graph (esbuild transpile + node execute). Transient — seconds, then released.
- **Host pressure**: N sandboxes' verification peaks overlapping (plus the natural spread from `coding_interval_*` / round staggering) → host memory overcommit, measured by `vm_monitor` / `smap_tool`.
- **No fabricated resident baseline.** If an idle-sandbox memory floor measurement is later required, the trace-faithful option is an in-repo watch-mode process (`tsx --watch` or `vitest --watch`) — **default off**, out of scope for this revision.

Customer-credibility argument: every step in the loop now appears verbatim in a captured openclaw vuejs/core trajectory. Nothing is added "for memory."

## Config changes (`config.py` + `config/e2b_coding_bench.yaml`)

Removed fields:

- `coding_dev_dir`, `coding_dev_wait`, `coding_dev_cmd`, `coding_skip_dev_server` (dev server gone).

Renamed/repurposed:

- `coding_build_cmd`, `coding_test_cmd`, `coding_build_timeout`, `coding_test_timeout`, `coding_skip_build`, `coding_skip_test` → replaced by a single `coding_verify_cmd` (default `npx tsx /tmp/bench_verify.mjs`) + `coding_verify_timeout` + `coding_skip_verify`.

Added:

- `coding_verify_script` (default body) and per-pair `verify_script` in `source_files`.

Minimal YAML shape:

```yaml
coding:
  project_dir: "/opt/coding-bench"
  verify_cmd: "npx tsx /tmp/bench_verify.mjs"
  verify_timeout: 120
  source_files:
    - file: "packages/reactivity/src/baseHandlers.ts"
      find: "export const mutableHandlers: ProxyHandler<object> ="
      replace: "export const mutableHandlers: ProxyHandler<object> = // bench"
      verify_script: |
        globalThis.__DEV__ = true
        globalThis.__BROWSER__ = false
        import('/opt/coding-bench/packages/reactivity/src/index.ts')
          .then(m => { if (!m.reactive) throw new Error('no reactive') })
  skip_verify: false
  interval_min: 2.0
  interval_max: 10.0
```

## Files to change

| File | Change |
|------|--------|
| `e2b_bench/coding_task_runner.py` | Remove `_step_ensure_dev_server`, `_step_build`, `_step_test`, `_start_dev_server`, `_check_dev_server_running`. Add `_step_verify` (`npx tsx` against raw `.ts`). New step order `find → read → edit → verify → diff` in both `CodingTaskRunner` (fixed) and `CodingRoundRunner`. `CodingWarmupRunner` reduced to project check + one initial `npx tsx` verify. |
| `e2b_bench/schemas.py` | `CODING_STEP_ORDER = [find, read, edit, verify, diff]`. `DEFAULT_CODING_SOURCE_FILES` entries gain optional `verify_script`. |
| `e2b_bench/metrics_extractor.py` | Step-key extraction updated to `verify` (drop `build`, `test`). |
| `e2b_bench/config.py` | Drop dev-server + build/test fields; add `coding_verify_cmd` / `coding_verify_timeout` / `coding_skip_verify` / `coding_verify_script`. YAML + CLI parsing updated. |
| `dockerfile_build/Dockerfile.coding` | Remove the `/opt/vite-playground` stage and its `vite`/`@vitejs/plugin-vue` install. Keep vuejs/core clone + `pnpm install` (with `PUPPETEER_SKIP_DOWNLOAD=1`). Remove the build-time `node scripts/build.js` (no longer needed) and its `export PATH="$(pnpm bin):$PATH"`. `CMD ["sleep","infinity"]` unchanged. |
| `dockerfile_build/bench_helper.sh` | New step sequence `find → read → edit → verify (npx tsx) → diff`; remove dev-server + build + test steps. |
| `dockerfile_build/README_CODING.md` | Update memory model, step sequence, verify instructions; document trace source. |
| `e2b_bench/stats_collector.py` | No dev-server labels remain; verify-step wording only. (Workflow-specific ready-label logic from prior commit stays.) |
| `e2b_bench/tests/test_coding_task_runner.py` | Update for new step_order + `verify` step; drop build/test assertions. |

## Resolved design defaults

1. **Production build kept?** — No (Decision 1 + "Should the production build be kept?").
2. **Verify runner** — `npx tsx` against raw `.ts` source, matching the trace verbatim.
3. **Dev server** — removed entirely.
4. **Idle memory floor** — out of scope (no resident process); optional `tsx --watch`/`vitest --watch` noted as a future honest lever, default off.

## Out of scope

- No Go/toolchain swap.
- No third workflow type (browser + coding only).
- No resident dev server / watch-mode baseline in this revision.
- `setup_coding_project.sh` left deprecated.
