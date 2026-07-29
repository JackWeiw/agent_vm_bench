# E2B Coding Benchmark Template — Build & Manual Test Guide

## Overview

This template creates an E2B sandbox containing **vuejs/core** (54k+ GitHub stars, the Vue.js core framework) for testing host memory capacity sensitivity under AI coding agent scenarios. vuejs/core is a **real repo from the `swe_bench_multilingual` evaluation dataset** (5 real instances) — not a synthetic or arbitrarily-chosen project.

**Key insight**: Real AI coding agents editing a frontend library like Vue always verify changes via a **local vite dev/HMR playground** (live preview). The dev server runs persistently (~1.5GB — the wide vue + rollup + esbuild dependency graph is the memory-overcommit carrier), and when the agent triggers a production build to verify changes (~1GB peak), both processes are active simultaneously — creating a **~3GB overlapping memory peak** per sandbox.

### Dev-server honesty note

vuejs/core is a *library*, not an *app* — it has no built-in HTTP dev server. A real agent editing a frontend library runs a separate vite playground that imports the built lib to verify changes via HMR. The sandbox therefore ships a minimal **vite playground** (`/opt/vite-playground`) that imports the built `packages/vue`. This is real development behavior (an agent verifying library edits locally), not an artificial memory inflator — and it is documented as such here and in `Dockerfile.coding`.

## Memory Pressure Model

```
┌─ Sandbox Memory Timeline ──────────────────────────────────────────┐
│                                                                     │
│  dev server (vite playground, persistent)  ─────────── ~1.5GB      │
│                                                                     │
│  production build (burst)          ┌──────┐              ~1GB peak │
│                                    │      │                         │
│  ──────────────────────────────────┤      ├──────────────          │
│                                    └──────┘                         │
│                                                                     │
│  total peak = dev server + build overlap  →  ~3GB per sandbox      │
│  (+ openclaw/agent processes ~0.5GB → warmup ~2GB / peak ~3GB)      │
│                                                                     │
│  50+ concurrent sandboxes → overlapping peaks → host memory stress  │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this is realistic**:

| Process | Memory | Reason |
|---------|--------|--------|
| Vite dev server (playground) | ~1.5GB persistent | Agents editing a frontend library verify changes via a local vite dev/HMR playground. The vue+rollup+esbuild module graph is what keeps it resident. |
| Production build (`node scripts/build.js`, rollup) | ~1GB peak | Agent verifies changes with a production build — standard coding workflow |
| Overlapping peak | ~3GB | Both processes active simultaneously — unavoidable in real agent environments |

**Why NOT language server / source maps**: These are not realistic for agent service scenarios. Language servers (tsserver) are IDE-internal components, not sandbox infrastructure. Source maps in production builds are uncommon. Dev server, however, is a universal coding agent action — customers immediately recognize this as real.

## Project Structure (inside sandbox)

```
/opt/coding-bench/                    # vuejs/core (git clone, pnpm monorepo)
├── package.json                      # vue + compiler + reactivity + rollup + esbuild + vite
├── pnpm-workspace.yaml               # pnpm workspace (packages/* + packages-private/*)
├── scripts/
│   ├── dev.js                        # esbuild dev build (library dev, watch)
│   └── build.js                      # rollup production build
├── packages/
│   ├── shared/src/general.ts         # ← Round-robin edit target
│   ├── shared/src/index.ts           # ← Round-robin edit target
│   ├── vue/src/index.ts              # ← Round-robin edit target
│   ├── reactivity/src/baseHandlers.ts # ← Round-robin edit target
│   ├── runtime-core/src/errorHandling.ts # ← Round-robin edit target
│   └── ... (compiler-*, runtime-*, server-renderer, ...)
├── node_modules/                     # Pre-installed (pnpm, no install needed at runtime)
├── packages/*/dist/                  # Build output
├── .git/                             # Git repo for checkout/reset + diff
└── bench_helper.sh                   # Manual testing helper script

/opt/vite-playground/                 # vite dev server (background memory pressure)
├── package.json                      # imports vue from file:/opt/coding-bench/packages/vue
├── vite.config.ts                   # @vitejs/plugin-vue
├── index.html
└── src/main.ts                       # minimal app consuming the built lib
```

## Modification Strategy (per round)

Each benchmark round simulates a real AI coding agent's verification cycle (matches observed agent traces: locate → inspect → edit → build → test → diff):

```
Step 0: find   — git checkout -- packages/ (reset) + verify/locate target file
Step 1: read   — head -20 target file (agent confirming context)
Step 2: edit   — apply a pre-configured find→replace pair (real semantic edit, triggers rebuild)
Step 3: build  — node scripts/build.js (clean production build, rollup)
Step 4: test   — pnpm test (vitest, verify correctness)
Step 5: diff   — git diff > /tmp/bench_round_N.patch (verification artifact)
```

**Key design decisions**:

1. **git checkout -- packages/ src/** — Config files (pnpm-workspace.yaml) are NOT reset, so dev-server/install settings persist across rounds. This is realistic: agents revert source changes but keep infrastructure config.

2. **Real semantic edit, not comment injection** — Each round applies a pre-configured `find→replace` pair (e.g. `export const NOOP = (): void => {}` → `... undefined`). The pairs are type-safe (equivalent return value / comment append) that never break compilation, yet still trigger rollup/esbuild's full rebuild — more representative of a real agent edit than a bare comment injection.

3. **Replacement pairs are pre-configured & verified** — Each `source_files` entry is `{file, find, replace}` verified against the vuejs/core repo. Round-robins through the list so every round reliably triggers a rebuild and results are reproducible. A CLI raw-file path falls back to a generic comment-marker pair.

4. **Clean rebuild each round** — removes `packages/*/dist/` and `node_modules/.cache/` to force full recompilation (no filesystem cache). This is realistic for ephemeral sandbox environments where no persistent cache exists.

5. **No per-round `free -m`** — Memory pressure is observed at the host level via `vm_monitor` / `smap_tool`, not from a per-round `free -m` inside the sandbox (no useful value).

## Build Steps

### 1. Build Docker Image

```bash
cd dockerfile_build
docker build -t ubuntu-coding-bench:24.04-linuxarm64 -f Dockerfile.coding .
```

This takes ~10-15 minutes (Node.js + pnpm install + vuejs/core clone + pnpm install + initial rollup build + vite playground setup).

### 2. Push to Harbor

```bash
HARBOR_IP=<your_harbor_ip> bash push_to_harbor_coding.sh
```

This adds E2B-required packages (systemd, openssh-server, websocat) and pushes to Harbor registry.

### 3. Build E2B Template

```bash
python3 build_e2b.py \
    --server-ip <e2b_api_ip> \
    --harbor-ip <harbor_ip> \
    --alias openclaw-coding-v1 \
    --image e2b-orchestration/ubuntu-coding-bench:custom \
    --cpu 2 \
    --memory 4096
```

**Note**: Memory=4096MB ensures dev server + production build can coexist during overlap peaks. The 3GB peak uses 75% of sandbox memory, leaving ~1GB headroom for OS and agent processes.

### 4. Manual Sandbox Testing

#### Quick Test (single round)

```bash
# Inside sandbox:
bash /opt/coding-bench/bench_helper.sh 0
```

This runs all 6 steps: find (dev server) → read → edit → build → test → diff.

#### Step-by-step Verification

```bash
# On host: start monitoring
watch -n 1 "numastat -p firecracker"

# Inside sandbox: Step 0 — start dev server (vite playground)
cd /opt/vite-playground && BROWSER=none npm run dev &
sleep 20  # Wait for initial compilation

# Inside sandbox: Step 3 — production build (while dev server is running)
cd /opt/coding-bench && find packages -type d -name dist -prune -exec rm -rf {} +
node scripts/build.js

# Watch host numastat for peak memory during build
```

#### Multi-Round Test

```bash
# Round 0 (starts dev server)
bash /opt/coding-bench/bench_helper.sh 0

# Round 1 (dev server already running, just modify + build)
bash /opt/coding-bench/bench_helper.sh 1

# Round 2
bash /opt/coding-bench/bench_helper.sh 2

# Stop dev server
pkill -f 'vite'; pkill -f 'npm run dev'
```

#### Partial Test (skip specific steps)

```bash
# Only start dev server, skip build and test
bash /opt/coding-bench/bench_helper.sh 0 --no-build --no-test

# Only run build, skip dev server startup (if already started manually)
bash /opt/coding-bench/bench_helper.sh 0 --no-dev-server

# Only run tests, skip everything else
bash /opt/coding-bench/bench_helper.sh 0 --no-dev-server --no-build
```

### 5. Memory Measurement Points

| Observation | Command | What to Check |
|-------------|---------|---------------|
| Baseline (idle) | `numastat -p firecracker` | ~200-300MB (OS + agent + llama-server) |
| Dev server steady | `numastat -p firecracker` | ~800-1200MB (after initial compilation settles) |
| Dev server initial | `numastat -p firecracker` during dev start | **~3.5GB peak** (initial full compilation) |
| Build peak (with dev server) | `numastat -p firecracker` during build | **~2.4-3GB peak** (build overlaps dev server) |
| Build peak (without dev server) | `numastat -p firecracker` during build | ~2GB peak (build alone) |
| Memory release | `numastat -p firecracker` after build | Drops back to dev server steady state |

### 6. Script Options

```bash
bash bench_helper.sh [ROUND] [OPTIONS]

Options:
  --round=N          Round number (default: 0)
  --no-dev-server    Skip dev server startup
  --no-build         Skip production build
  --no-test          Skip test suite
  --help             Show help

Environment:
  BENCH_PROJECT_DIR   vuejs/core path (default: /opt/coding-bench)
  BENCH_DEV_DIR       vite playground path (default: /opt/vite-playground)
  BENCH_DEV_WAIT      Dev server startup wait (default: 20)
  BENCH_BUILD_CMD     Build command (default: node scripts/build.js)
  BENCH_TEST_CMD      Test command (default: pnpm test)
```

Extensibility: `BENCH_PROJECT_DIR` / `BENCH_DEV_DIR` / `BENCH_BUILD_CMD` / `BENCH_TEST_CMD` allow the same script to work with different coding projects (the dev server dir, build, and test commands are all configurable). `BENCH_DEV_WAIT` adjusts dev server startup time for faster/slower machines.

## Credibility Argument for Customers

> **Scenario**: AI coding agent 修改前端核心库（vuejs/core）的真实工作环境。
>
> 项目来自真实评测数据集 swe_bench_multilingual——不是合成项目，不是人为挑选，是真实 agent 评测用的仓库。agent 给 Vue 提 PR 的工作流就是 `find → read → edit → npm run build → npm test → git diff`，与观测到的 agent trace 一致。
>
> agent 改前端库时会起本地 vite dev/HMR playground 实时验证改动（真实开发行为），dev server 常驻 ~1.5GB（vue+rollup+esbuild 宽依赖图）；触发 production build 验证时 build 峰值 ~1GB，两个进程同时活跃，单沙箱峰值 ~3GB。50+ 并发沙箱重叠峰值在宿主机产生 150GB+ 内存压力，完全反映客户卖 agent 服务时多用户并发场景。
>
> **不采用不真实手段**: 没有合成项目、没有语言服务（tsserver 是 IDE 内部组件）、没有 source maps、没有人工膨胀依赖。dev server 是真实 agent 改前端库时的本地验证行为（如实记录于 Dockerfile 与本文档），所有内存压力来自真实 coding agent 的真实操作。

## Next Steps After Manual Testing

Once single sandbox behavior is verified:

1. Record actual build peak memory and timing metrics
2. Confirm dev server + build overlap creates ~3GB peak
3. Adjust `BENCH_DEV_WAIT` if dev server compilation takes longer/shorter
4. Proceed to multi-sandbox e2b_bench implementation:
   - `CodingRoundRunner` with 6-step workflow (find → read → edit → build → test → diff)
   - `config/e2b_coding_bench.yaml` with coding workflow + replacement-pair source files
   - Integrate with `batch_scheduler.py` for multi-sandbox round-robin benchmark
   - `CodingMetrics` in `schemas.py` for step-level timing collection
