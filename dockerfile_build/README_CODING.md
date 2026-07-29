# E2B Coding Benchmark Template — Build & Manual Test Guide

## Overview

This template creates an E2B sandbox containing **devias Material Kit React** (5.6k+ GitHub stars, popular React + MUI + TypeScript admin dashboard) for testing host memory capacity sensitivity under AI coding agent scenarios.

**Key insight**: Real AI coding agents working on web applications always start a **dev server** for live preview (Devin, OpenHands, Claude Code all do this). The dev server runs persistently (~1.5GB — MUI's wide dependency graph is the memory-overcommit carrier), and when the agent triggers a production build to verify changes (~1GB peak), both processes are active simultaneously — creating a **~3GB overlapping memory peak** per sandbox.

## Memory Pressure Model

```
┌─ Sandbox Memory Timeline ──────────────────────────────────────────┐
│                                                                     │
│  dev server (persistent)   ──────────────────────────── ~1.5GB     │
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
| Dev server (npm run dev) | ~1.5GB persistent | Every coding agent starts dev server for live preview — Devin, OpenHands, Claude Code all do this. MUI's wide dep graph is what keeps it resident. |
| Production build (npm run build) | ~1GB peak | Agent verifies changes with production build — standard coding workflow |
| Overlapping peak | ~3GB | Both processes active simultaneously — unavoidable in real agent environments |

**Why NOT language server / source maps**: These are not realistic for agent service scenarios. Language servers (tsserver) are IDE-internal components, not sandbox infrastructure. Source maps in production builds are uncommon. Dev server, however, is a universal coding agent action — customers immediately recognize this as real.

## Project Structure (inside sandbox)

```
/opt/coding-bench/                    # devias Material Kit React (git clone)
├── package.json                      # React + MUI + Next.js dependencies
├── next.config.mjs                   # Next.js configuration
├── src/
│   ├── config.ts                     # ← Site config (round-robin edit target)
│   ├── paths.ts                      # ← Route paths (round-robin edit target)
│   ├── app/                           # Next.js app router pages
│   │   ├── layout.tsx                 # ← Root layout (round-robin edit target)
│   │   ├── page.tsx                   # ← Home redirect (round-robin edit target)
│   │   └── dashboard/                 # Dashboard pages + nested layout
│   ├── components/                    # Shared components (MUI-based)
│   ├── contexts/                      # React contexts
│   ├── hooks/                         # Custom hooks
│   └── lib/                           # Utilities
├── node_modules/                     # Pre-installed (no npm install needed)
├── .next/                            # Next.js build output
├── .git/                             # Git repo for checkout/reset + diff
├── bench_helper.sh                   # Manual testing helper script
└── /tmp/
    ├── dev_server.log                # Dev server output
    ├── build_output.log              # Build output per round
    ├── test_output.log               # Test output per round
    └── bench_round_N.patch           # git diff artifact per round
```

## Modification Strategy (per round)

Each benchmark round simulates a real AI coding agent's verification cycle (matches observed agent traces: locate → inspect → edit → build → test → diff):

```
Step 0: find   — git checkout -- src/ (reset) + verify/locate target file
Step 1: read   — head -20 target file (agent confirming context)
Step 2: edit   — apply a pre-configured find→replace pair (real semantic edit, triggers rebuild)
Step 3: build  — rm -rf .next/ cache/ + npm run build (clean production build)
Step 4: test   — npm test (verify changes don't break tests)
Step 5: diff   — git diff > /tmp/bench_round_N.patch (verification artifact)
```

**Key design decisions**:

1. **git checkout -- src/ only** — Config files (next.config.mjs) are NOT reset, so dev server settings persist across rounds. This is realistic: agents revert source changes but keep infrastructure config.

2. **Real semantic edit, not comment injection** — Each round applies a pre-configured `find→replace` pair (e.g. `name: 'Devias Kit'` → `name: 'Devias Kit Pro'`). The pairs are type-safe string/value swaps that never break compilation, yet still trigger Next's full rebuild — more representative of a real agent edit than a bare comment injection.

3. **Replacement pairs are pre-configured & verified** — Each `source_files` entry is `{file, find, replace}` verified against the devias repo. Round-robins through the list so every round reliably triggers a rebuild and results are reproducible. A CLI raw-file path falls back to a generic comment-marker pair.

4. **Clean rebuild each round** — `rm -rf .next/ node_modules/.cache/` forces full recompilation (no filesystem cache). This is realistic for ephemeral sandbox environments where no persistent cache exists.

5. **No per-round `free -m`** — Memory pressure is observed at the host level via `vm_monitor` / `smap_tool`, not from a per-round `free -m` inside the sandbox (no useful value).

## Build Steps

### 1. Build Docker Image

```bash
cd dockerfile_build
docker build -t ubuntu-coding-bench:24.04-linuxarm64 -f Dockerfile.coding .
```

This takes ~10-15 minutes (Node.js install + devias clone + npm install + initial build).

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

# Inside sandbox: Step 0 — start dev server
cd /opt/coding-bench && BROWSER=none npm run dev &
sleep 20  # Wait for initial compilation

# Inside sandbox: Step 3 — production build (while dev server is running)
rm -rf .next/ node_modules/.cache/
npm run build

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
pkill -f 'next dev'; pkill -f 'npm run dev'
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
  BENCH_PROJECT_DIR   Project path (default: /opt/coding-bench)
  BENCH_DEV_WAIT      Dev server startup wait (default: 20)
```

Extensibility: `BENCH_PROJECT_DIR` allows the same script to work with different coding projects. `BENCH_DEV_WAIT` allows adjusting dev server startup time for faster/slower machines.

## Credibility Argument for Customers

> **Scenario**: AI coding agent 开发 web 应用的真实工作环境。
>
> 每个 coding agent（Devin、OpenHands、Claude Code）在开发 web 应用时都会启动 dev server 进行实时预览——这是标准操作，不是人为构造。dev server 常驻运行（1-1.5GB），当 agent 触发 production build 验证改动时，两个进程同时活跃，单沙箱峰值 ~3GB。
>
> 50+ 并发沙箱时，重叠峰值在宿主机产生 150GB+ 内存压力。这完全反映了客户卖 agent 服务时多用户并发使用的真实场景——每个用户都在沙箱里跑 coding agent，每个 agent 都会启动 dev server + 执行构建验证。
>
> **不采用不真实手段**: 没有添加语言服务（tsserver 是 IDE 内部组件，不是沙箱基础设施）、没有启用 source maps（生产构建中不常见）、没有人工膨胀依赖。所有内存压力来自真实 coding agent 的真实操作。

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
