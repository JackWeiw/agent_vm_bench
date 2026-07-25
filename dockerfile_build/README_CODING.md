# E2B Coding Benchmark Template - Build & Manual Test Guide

## Overview

This template creates an E2B sandbox containing a React + webpack5 project for testing memory capacity sensitivity under AI coding agent scenarios.

**Memory pressure model**: Each `npm run build` (webpack5 production) creates a bursty peak of 500-800MB. When multiple sandboxes run concurrent builds, overlapping peaks stress host NUMA memory capacity.

## Project Structure (inside sandbox)

```
/opt/coding-bench/
├── package.json              # React + webpack5 + jest dependencies
├── webpack.config.js         # Webpack5 production config (split chunks, minification)
├── tsconfig.json             # TypeScript config
├── jest.config.js            # Jest test config
├── src/
│   ├── index.tsx             # Entry point
│   ├── App.tsx               # Main app (imports all 40 components)
│   ├── bench-marker.ts       # ← Modified each round (BENCH_ROUND constant)
│   ├── components/           # 40 React TypeScript components
│   │   ├── Dashboard.tsx     # Each imports BENCH_ROUND from bench-marker
│   │   ├── Header.tsx
│   │   ├── ... (40 files)
│   │   └── TaskBoard.tsx
│   ├── styles/
│   │   ├── App.css           # Main stylesheet
│   │   └── components.css    # Component styles
│   ├── utils/
│   │   ├── helpers.ts        # Utility functions
│   │   ├── api.ts            # API types and endpoints
│   │   ├── format.ts         # Formatting utilities
│   ├── __tests__/            # 10 Jest test files
├── public/
│   ├── index.html            # HTML template
├── node_modules/             # Pre-installed (no npm install needed)
├── dist/                     # Build output (pre-built)
├── .git/                     # Git repo for checkout/reset
└── bench_helper.sh           # Manual testing helper script
```

## Modification Strategy

Each benchmark round modifies `bench-marker.ts` to change the `BENCH_ROUND` constant:

```bash
# Round N: modify bench-marker.ts (regex matches any current value)
sed -i "s/export const BENCH_ROUND = .*/export const BENCH_ROUND = ${round_id};/" /opt/coding-bench/src/bench-marker.ts
```

**Why this works**:
- `npm run build` is webpack5 production mode = **full rebuild every time** (no persistent cache)
- Regex `export const BENCH_ROUND = .*` matches any current value → no need for git reset
- 40+ components import `BENCH_ROUND` → modification affects wide rebuild scope
- Build produces genuinely different output each round (different constant value in bundle)

## Build Steps

### 1. Build Docker Image

```bash
cd dockerfile_build
docker build -t ubuntu-coding-bench:24.04-linuxarm64 -f Dockerfile.coding .
```

This takes ~10-15 minutes (Node.js install + npm install + 40 component generation + initial build).

### 2. Push to Harbor

```bash
HARBOR_IP=<your_harbor_ip> bash push_to_harbor_coding.sh
```

This adds E2B-required packages (systemd, openssh-server, websocat) and pushes to Harbor.

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

**Note**: Memory=4096MB ensures webpack build can complete. Adjust based on memory measurements.

### 4. Manual Sandbox Testing

Create a sandbox and test coding operations:

```python
from e2b import Sandbox

# Create sandbox from coding template
sbx = Sandbox.create("openclaw-coding-v1")

# Step 1: Check project exists
result = sbx.commands.run("ls /opt/coding-bench/package.json", timeout=10, user="root")
print("Project exists:", result.exit_code == 0)

# Step 2: Check memory baseline
result = sbx.commands.run("free -m", timeout=5, user="root")
print("Memory baseline:", result.stdout)

# Step 3: Run initial build and measure
result = sbx.commands.run(
    "cd /opt/coding-bench && npm run build",
    timeout=300, user="root"
)
print("Build output:", result.stdout[-200:])
print("Build exit code:", result.exit_code)

# Step 4: Check memory after build
result = sbx.commands.run("free -m", timeout=5, user="root")
print("Memory after build:", result.stdout)

# Step 5: Modify bench-marker and rebuild (Round 1)
result = sbx.commands.run(
    "sed -i 's/export const BENCH_ROUND = .*/export const BENCH_ROUND = 1;/' /opt/coding-bench/src/bench-marker.ts",
    timeout=5, user="root"
)
print("Modification:", result.exit_code)

result = sbx.commands.run(
    "cd /opt/coding-bench && npm run build",
    timeout=300, user="root"
)
print("Round 1 build:", result.exit_code)

# Step 6: Run tests
result = sbx.commands.run(
    "cd /opt/coding-bench && npm test",
    timeout=120, user="root"
)
print("Test results:", result.stdout[-200:])

# Step 7: Quick helper script test
result = sbx.commands.run(
    "bash /opt/coding-bench/bench_helper.sh 2",
    timeout=300, user="root"
)
print("Helper output:", result.stdout)

# Cleanup
sbx.kill()
```

### 5. Memory Measurement Points

Key metrics to observe during manual testing:

| Observation | Command | What to Check |
|-------------|---------|---------------|
| Memory baseline | `free -m` | Before build, idle state |
| Build peak memory | `free -m` (during build) | Peak RSS during webpack |
| Build duration | Time `npm run build` | Should be 10-30 seconds |
| Build success | `npm run build` exit code | Should be 0 |
| Test duration | Time `npm test` | Should be 5-15 seconds |
| Per-round rebuild | Modify + rebuild | Same duration as initial build |
| Memory release | `free -m` (after build) | Memory should release back |

### 6. Adjusting Project Size

If build memory peak is too low (< 300MB) or too high (> 2GB), adjust `COMPONENT_COUNT` in `setup_coding_project.sh`:

| Component Count | Approx. Build Peak Memory | Build Duration |
|-----------------|--------------------------|----------------|
| 20 | 300-400MB | 5-10s |
| 40 | 400-600MB | 10-20s |
| 80 | 600-800MB | 20-40s |
| 100 | 800-1000MB | 30-60s |

For heavier builds (1-1.5GB), add heavy UI library dependencies to package.json:
```json
"@mui/material": "^6.0.0",
"@mui/icons-material": "^6.0.0",
"@emotion/react": "^11.13.0",
"@emotion/styled": "^11.13.0"
```
These libraries have hundreds of modules that significantly increase webpack build memory.

## Next Steps After Manual Testing

Once single sandbox behavior is verified and memory metrics are acceptable:

1. Record actual build peak memory and duration
2. Determine optimal sandbox memory configuration (memory_mb in build_e2b.py)
3. Adjust COMPONENT_COUNT if needed
4. Proceed to multi-sandbox implementation:
   - Add coding workflow to e2b_bench package
   - Create `config/e2b_coding_bench.yaml`
   - Implement `CodingRoundRunner` for round-robin benchmark
   - Test with smap_tool + vm_monitor for memory migration metrics
