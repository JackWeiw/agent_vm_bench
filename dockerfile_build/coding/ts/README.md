# E2B Coding Benchmark Template (JS) — Build & Manual Test Guide

## Overview

This template creates an E2B sandbox containing **vuejs/core** (54k+ GitHub
stars, the Vue.js core framework) for testing host memory capacity sensitivity
under AI coding-agent scenarios. vuejs/core is a **real repo from the
`swe_bench_multilingual` evaluation dataset** (5 real instances) — not a
synthetic or arbitrarily-chosen project.

**Trace-faithful loop.** The per-round workflow mirrors a **real captured
openclaw agent trajectory** on vuejs/core (v-pre + textarea tokenizer issue):

```
find → read → edit → verify (write ad-hoc /tmp/test_*.mjs + npx tsx) → git diff
```

The agent NEVER runs a production build, NEVER runs the vitest suite, and NEVER
keeps a resident dev server. It verifies by writing a small ad-hoc `.mjs` that
imports the **raw** `.ts` source and running it via `npx tsx` — a transient
esbuild-transpile + node-execute process that loads the full TS module graph
(the memory peak). N concurrent sandboxes' staggered verify peaks overlapping
→ host memory overcommit.

This removed the earlier `spawn rollup ENOENT` failure class: no production
build means no `rollup` to resolve at runtime — the verify step uses only `npx`
(symlinked at `/usr/local/bin/npx`), which resolves `tsx` from
`node_modules/.bin` internally.

## Memory Pressure Model

```
┌─ Sandbox Memory Timeline ──────────────────────────────────────────┐
│                                                                     │
│  Warmup:  one npx tsx verify  ████  (esbuild + module graph loaded)│
│                                                                     │
│  Round:   find  read  edit  verify(peak)  diff                      │
│                              ↑                                      │
│                       npx tsx /tmp/bench_verify.mjs                │
│                       (esbuild transpile + node execute, transient)│
│                                                                     │
│  × N concurrent sandboxes, staggered → host memory overcommit       │
└─────────────────────────────────────────────────────────────────────┘
```

| Process | Memory | Reason |
|---------|--------|--------|
| `npx tsx` verify | transient peak | esbuild transpiles the imported TS; node executes it; the full compiler-core + reactivity + shared module graph loads into memory for seconds, then releases. |
| N sandboxes overlapping | host overcommit | Staggered verify peaks (spread by `coding_interval_*` / round timing) overlap at the host → real pressure, measured by `vm_monitor` / `smap_tool`. |

**Why NOT dev server / production build / vitest**: none of these appear in the
real captured trace. A resident vite playground or a rollup production build
would be "added for memory, not for realism" — exactly what a strong reviewer
flags. Pressure comes honestly from concurrent transient verify peaks.

## Project Structure (inside sandbox)

```
/opt/coding-bench/                    # vuejs/core (git clone, pnpm monorepo)
├── package.json                      # vue + compiler + reactivity + rollup + esbuild + tsx
├── pnpm-workspace.yaml               # pnpm workspace (packages/* + packages-private/*)
├── packages/
│   ├── shared/src/general.ts         # ← Round-robin edit target
│   ├── shared/src/index.ts           # ← Round-robin edit target
│   ├── vue/src/index.ts              # ← Round-robin edit target
│   ├── reactivity/src/baseHandlers.ts # ← Round-robin edit target
│   ├── runtime-core/src/errorHandling.ts # ← Round-robin edit target
│   └── ... (compiler-*, runtime-*, server-renderer, ...)
├── node_modules/                     # Pre-installed (pnpm, incl. tsx — npx resolves it)
├── .git/                             # Git repo for checkout/reset + diff
└── bench_helper.sh                   # Manual testing helper script
```

No `/opt/vite-playground` — the fabricated dev server was removed (not in the trace).

## Modification Strategy (per round)

Each benchmark round simulates a real AI coding agent's verification cycle
(verified against the captured openclaw trace):

```
Step 0: find    — git checkout -- packages/ src/ (reset) + verify/locate target file
Step 1: read    — head -20 target file (agent confirming context)
Step 2: edit    — apply a pre-configured find→replace pair (real semantic edit)
Step 3: verify  — write /tmp/bench_verify.mjs + npx tsx run (memory peak)
Step 4: diff    — git diff > /tmp/bench_round_N.patch (verification artifact)
```

**Key design decisions**:

1. **git checkout -- packages/ src/** — Config files (pnpm-workspace.yaml) are NOT reset, so install settings persist across rounds. Agents revert source changes but keep infrastructure config.

2. **Real semantic edit, not comment injection** — Each round applies a pre-configured `find→replace` pair (e.g. `export const NOOP = (): void => {}` → `... undefined`). Pairs are type-safe (equivalent return value / comment append) that never break compilation.

3. **Verify = write ad-hoc test + `npx tsx`** — Mirrors the trace: the agent wrote `/tmp/test_vpre_textarea.mjs` importing raw `.ts` source and ran `npx tsx` on it. Each pair carries a `verify_script` body importing `compiler-core` (the agent's verify entry — its trace imports only compiler-core's `baseParse`/`parse`) and running `baseParse` on a pair-specific template; pairs without it fall back to a shared default of the same shape. `compiler-core` is the heaviest trace-faithful entry that runs under a bare `npx tsx` without hitting the `__TEST__` build global (the vue/runtime-core/compiler-dom/compiler-sfc graphs reach `compiler-dom/src/errors.ts` → `__TEST__` ReferenceError on a real call; the parser alone avoids that path). Each pair's template/assertion differs so consecutive rounds don't repeat identical bytes (mirrors the agent rewriting its ad-hoc test per verify).

4. **No per-round `free -m`** — Memory pressure is observed at the host level via `vm_monitor` / `smap_tool`, not from a per-round `free -m` inside the sandbox (no useful value).

## Build Steps

### 1. Build Docker Image

```bash
cd dockerfile_build/coding/ts
docker build -t ubuntu-coding-bench:24.04-linuxarm64 -f Dockerfile .
```

**x86_64:**

```bash
docker build -t ubuntu-coding-bench:24.04-x86_64 -f Dockerfile.x86 .
```

**openEuler (optional):**

The openEuler variants build from `Dockerfile.openeuler` / `Dockerfile.openeuler.x86`
on an `openeuler-24.03-lts-sp3:latest` base (loaded from tar — not a registry).

Step 0 — load the openEuler base tar:

```bash
# ARM64
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/aarch64/openEuler-docker.aarch64.tar.xz
xz -d openEuler-docker.aarch64.tar.xz
docker load -i openEuler-docker.aarch64.tar
# x86_64
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/x86_64/openEuler-docker.x86_64.tar.xz
xz -d openEuler-docker.x86_64.tar.xz
docker load -i openEuler-docker.x86_64.tar
```

Build:

```bash
# ARM64 — from the repo root. ARM uses dockerfile_build/ as context so the
# shared _bench_looper package is reachable for COPY.
docker build -f dockerfile_build/coding/ts/Dockerfile.openeuler \
  -t openeuler-coding-bench:24.03-lts-sp3-linuxarm64 dockerfile_build/
# x86_64 (minimal image, dir-scoped build)
cd dockerfile_build/coding/ts && docker build -t openeuler-coding-bench:24.03-lts-sp3-x86_64 -f Dockerfile.openeuler.x86 .
```

## In-image bench looper (openEuler ARM)

The ARM image vendors the shared `bench_looper` package and a
`coding-bench-ts` entry point at `/usr/local/bin`. Default CMD is
`sleep infinity` (long-running container for slicing); the entry point runs
the coding-ts scenario end-to-end (find -> read -> edit -> verify -> diff,
verify = N chained `npx tsx` processes) and writes JSON results. The browser
and Go images expose `browser-bench` and `coding-bench-go` the same way.

One-shot end-to-end:

```bash
docker run --rm -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-coding-bench:24.03-lts-sp3-linuxarm64 \
  coding-bench-ts --loops 100
```

Long-running container driven via `docker exec`:

```bash
docker run -d --name t1 -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-coding-bench:24.03-lts-sp3-linuxarm64
docker exec t1 coding-bench-ts --loops 100
```

Results land in `/results/coding-ts/<run-id>/{iterations.jsonl,summary.json}`.

Push (set `OS=openeuler`):

```bash
OS=openeuler HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
# x86_64:
OS=openeuler ARCH=x86 HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
```

This clones vuejs/core and runs `pnpm install` (with `PUPPETEER_SKIP_DOWNLOAD=1`).
No production build, no vite playground.

### 2. Push to Harbor

```bash
HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
```

For x86_64, set `ARCH=x86` (the default is `arm`):

```bash
ARCH=x86 HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
```

This adds E2B-required packages (systemd, openssh-server, websocat) and pushes to Harbor registry.

### 3. Build E2B Template

`build_e2b.py` is shared and lives at `dockerfile_build/build_e2b.py`:

```bash
python3 ../../build_e2b.py \
    --server-ip <e2b_api_ip> \
    --harbor-ip <harbor_ip> \
    --alias openclaw-coding-v1 \
    --image e2b-orchestration/ubuntu-coding-bench:custom \
    --cpu 2 \
    --memory 4096
```

### 4. Manual Sandbox Testing

```bash
# Inside sandbox:
bash /opt/coding-bench/bench_helper.sh 0       # Round 0, all steps
bash /opt/coding-bench/bench_helper.sh 1       # Round 1
bash /opt/coding-bench/bench_helper.sh --no-verify   # skip the verify step
```

### 5. Memory Measurement Points

| Observation | Command | What to Check |
|-------------|---------|---------------|
| Baseline (idle) | `numastat -p firecracker` | ~200-300MB (OS + agent) |
| Verify peak | `numastat -p firecracker` during `npx tsx` | transient peak (module graph loaded) |
| N sandboxes overlapping | `numastat -p firecracker` at peak | host overcommit pressure |

### 6. Script Options

```bash
bash bench_helper.sh [ROUND] [OPTIONS]
  --round=N       Round number
  --no-verify     Skip the verify step
  --help          Show help
```

## Language extensibility

`ts` is a value of `coding.language` (`"ts"`), not a hard-coded workflow. The
coding loop is shared across languages; only the verify mechanics differ,
captured as data in a `CodingLanguageProfile` registry (`e2b_bench/config.py`).
A Go variant (`gohugoio/hugo`, `go run` verify) ships alongside this one — see
[Go README](../go/README.md). Adding C++ later = one registry
entry + its default verify script. (The profile is keyed `ts` because
vuejs/core's source is TypeScript; the verify scripts import `.ts` source and
run via `npx tsx`, which transpiles TS on the fly.)
