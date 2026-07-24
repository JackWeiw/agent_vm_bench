# E2B Sandbox Bench - Usage Guide (EN)

E2B Sandbox batch performance testing tool for measuring sandbox startup performance and browser task execution performance under memory migration stress.

## Complete Testing Workflow

The full end-to-end workflow from environment setup to running benchmarks:

```text
download_page.sh → docker build → push_to_harbor.sh → build_e2b.py → http.server
     ↓                 ↓              ↓                 ↓            ↓
  Web pages     Docker image    Harbor registry    E2B template    Web server
                                                                       ↓
                              config/e2b_bench.yaml (template + URL)
                                       ↓
                              --create-only → --detect → benchmark → delete_sandbox.sh
```

### Step 1: Download Web Pages

Download Wikipedia pages (with images) for browser warmup and benchmark tasks:

```bash
# Download all 10 Wikipedia pages (China, Earth, Galaxy, Hubble, Human, etc.)
bash download_page.sh

# Or download specific pages
bash download_page.sh -p Weibo,China
```

Pages saved to `web_content/en.wikipedia.org/wiki/`. Each page includes HTML and images with fixed local links.

### Step 2: Build Docker Image

Build the base Docker image containing openclaw, agent-browser, Chromium, llama-server, and supervisor:

```bash
cd dockerfile_build

# Build for ARM64 (default)
docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 .

# Build for x86
docker build -f Dockerfile.x86 -t ubuntu-openclaw-chromium:24.04-linuxx86 .
```

The Dockerfile installs:
- Ubuntu 24.04 base
- Node24 + openclaw@2026.6.6 + agent-browser
- Chromium (via xtradeb PPA)
- llama-server + BGE embedding model
- supervisor (manages llama-server + openclaw-gateway)

### Step 3: Push to Harbor Registry

Push the built image to your Harbor registry (required for E2B template build):

```bash
cd dockerfile_build

# Set Harbor IP to your E2B/Harbor server
HARBOR_IP=71.14.96.192 bash push_to_harbor.sh
```

The script:
1. Checks base image exists
2. Starts temp container, installs systemd + openssh-server + websocat
3. Exports container as new image `ubuntu-openclaw-chromium:custom`
4. Tags and pushes to `HARBOR_IP:2900/e2b-orchestration/ubuntu-openclaw-chromium:custom`

Harbor access: `http://HARBOR_IP:2900/` (admin/Harbor12345)

### Step 4: Build E2B Template

Build an E2B template from the Harbor image. This creates the Firecracker microVM template used by sandbox.create():

```bash
cd dockerfile_build

# Build template (alias = template name for config)
python3 build_e2b.py --server-ip 71.14.96.192 --alias openclaw-browser-v1

# With custom Harbor IP and template settings
python3 build_e2b.py \
    --server-ip 71.14.96.192 \
    --harbor-ip 71.14.96.192 \
    --alias openclaw-browser-v1 \
    --cpu 2 \
    --memory 4096
```

**Prerequisites:** `~/.e2b/config.json` must exist with:
```json
{
  "teamId": "...",
  "accessToken": "sk_e2b_...",
  "teamApiKey": "e2b_..."
}
```

The script reads config, sets E2B environment variables, builds template from Harbor image, and creates a test sandbox.

### Step 5: Start Web Server

Start a local HTTP server to serve the downloaded pages. Bind to specific NUMA nodes for memory isolation:

```bash
cd web_content/en.wikipedia.org/wiki

# Bind to NUMA 2,3 (same as sandbox NUMA for local access)
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

Available pages: `http://YOUR_IP:8080/China.html`, `http://YOUR_IP:8080/Hubble_Space_Telescope.html`, etc.

### Step 6: Modify Configuration

Edit `config/e2b_bench.yaml` to match your environment:

```yaml
e2b_env:
  E2B_API_URL: "http://71.14.96.192:3000"  # Your E2B API server

sandbox:
  template: "openclaw-browser-v1"  # Template alias from Step 4
  total_count: 100
  numa_bind: 2

browser:
  urls:
    - "http://YOUR_LOCAL_IP:8080/Hubble_Space_Telescope.html"  # Your web server
  warmup_urls:
    - "http://YOUR_LOCAL_IP:8080/China.html"
    - "http://YOUR_LOCAL_IP:8080/Earth.html"
    - "http://YOUR_LOCAL_IP:8080/Galaxy.html"
    - "http://YOUR_LOCAL_IP:8080/Hubble_Space_Telescope.html"
    - "http://YOUR_LOCAL_IP:8080/Human.html"
    - "http://YOUR_LOCAL_IP:8080/List_of_paintings_by_Vincent_van_Gogh.html"
    - "http://YOUR_LOCAL_IP:8080/Solar_System.html"
    - "http://YOUR_LOCAL_IP:8080/United_States.html"
    - "http://YOUR_LOCAL_IP:8080/World_War_II.html"

test:
  benchmark_mode: "round_robin"
  round_size: 5
  round_count: 5
  round_interval: 5
```

### Step 7: Create Sandboxes

Create sandboxes without running tasks (Phase 0). Sandboxes are left running for later benchmark use:

```bash
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# Save sandbox IDs for cross-session reuse
python -m e2b_bench --config config/e2b_bench.yaml --create-only --sandbox-ids-file sandboxs.txt
```

### Step 8: Run Benchmark

Detect existing sandboxes and run the benchmark:

```bash
# Fixed mode (all sandboxes run tasks concurrently)
python -m e2b_bench --config config/e2b_bench.yaml --detect

# Round-robin mode (group rotation for memory migration testing)
python -m e2b_bench --config config/e2b_bench.yaml --detect -bm round_robin -rs 5 -rc 5

# Multi-phase approach: warmup first, then benchmark
python -m e2b_bench --config config/e2b_bench.yaml --detect --warmup-only  # Warmup phase
python -m e2b_bench --config config/e2b_bench.yaml --detect                # Benchmark phase

# With sandbox ID file
python -m e2b_bench --config config/e2b_bench.yaml --detect --sandbox-ids-file sandboxs.txt
```

### Step 9: Delete Sandboxes

Delete all running sandboxes when testing is complete:

```bash
cd e2b_bench/scripts

# Configure environment
cp .env.example .env
# Edit .env: E2B_API_URL=http://71.14.96.192:3000

# Delete all sandboxes
bash delete_sandbox.sh

# With custom .env path
bash delete_sandbox.sh path/to/.env
```

The script reads `E2B_API_URL` from `.env` and API credentials from `~/.e2b/config.json`, then deletes all sandboxes via E2B API.

## Features

- **Batch Sandbox Creation** - Support batched or full concurrent creation
- **Port Check** - Automatically check 18789 (openclaw-gateway) and 11436 (llama-server) port readiness
- **NUMA Binding** - Bind sandbox creation to specific NUMA node for controlled memory placement
- **Browser Warmup Phase** - Optional warmup phase with multi-tab memory allocation via agent-browser
- **Two Benchmark Modes** - Fixed mode (subset percentage) and Round-robin mode (group rotation with tab switching)
- **Step-Level Timing** - Separate timing for open_tab, page_load, snapshot, click, screenshot steps
- **Tail Latency Analysis** - P99/P50 ratio with severity classification (minimal/moderate/significant)
- **Round Comparison** - Per-round statistics table with success rate and latency breakdown
- **Error Classification** - Automatic error type classification (D-Bus, Gateway, Timeout, etc.)
- **smap_tool Integration** - Memory migration monitoring with configurable swap size, ratio, and NUMA nodes
- **vm_monitor Integration** - Performance monitoring with stress-file sync for benchmark phase detection
- **Sandbox ID Persistence** - Save/load sandbox IDs across sessions for reuse
- **Real-time Statistics** - Real-time display of creation time, port wait time, task latency
- **Performance Report** - Generate detailed TXT report (P50/P95/P99 latency, step timing, error details)
- **Batch Test Mode** - Matrix-based batch testing with sandbox/smap_tool reuse within groups
- **Offline Summary** - Generate aggregated Excel summary from existing test results
- **CLI > YAML > Defaults Priority** - Consistent config override chain across all fields
- **Four Running Modes** - Full workflow, create-only, detect existing, warmup-only

## Architecture

```
e2b_bench/
├── __init__.py            # Package initialization
├── __main__.py            # Module entry point (--batch or single mode)
├── bench.py               # Main entry - test workflow, SmapToolManager, VmMonitorManager
├── config.py              # Configuration management (YAML + CLI + defaults)
├── sandbox_manager.py     # Sandbox lifecycle (create, port check, NUMA bind, kill)
├── task_runner.py         # Task execution: WarmupRunner, BrowserTaskRunner, TabOperationRunner
├── round_robin.py         # Round-robin task manager (group rotation, cycling)
├── task_generator.py      # Batch task generation from matrix config
├── stats_collector.py     # Statistics collection, ErrorClassifier, ReportFormatter
├── schemas.py             # Data structures (BrowserMetrics with step-level timing)
├── metrics_extractor.py   # Extract metrics from vm_monitor + browser reports
├── report_aggregator.py   # Aggregate batch results into styled Excel
├── utils.py               # calc_percentiles, calc_tail_ratio, classify_tail_latency
├── tests/                 # Unit tests
├── .env.example           # Environment variables template
└── requirements.txt       # Dependencies

config/
├── e2b_bench.yaml         # Single test configuration
├── e2b_batch_matrix.yaml  # Batch test matrix (total_counts, ratios, percentages)
└── e2b_batch_template.yaml # Batch test template config
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r e2b_bench/requirements.txt
```

Dependencies: `e2b>=0.15.0`, `PyYAML>=6.0`

### 2. Configure Credentials

Edit `config/e2b_bench.yaml`:

```yaml
e2b_env:
  E2B_ACCESS_TOKEN: "your_real_token"
  E2B_API_KEY: "your_real_key"
  E2B_DOMAIN: "e2b.app"
  E2B_API_URL: "http://localhost:3000"  # Your E2B API server
  E2B_HTTP_SSL: "false"

sandbox:
  template: "openclaw-browser-v1"  # Your E2B template name
  create_timeout: 86400
  total_count: 100
  detect_existing: false
  create_only: false
  numa_bind: 2  # Bind sandbox creation to NUMA node 2 (null/omit = no binding)
  sandbox_ids_file: "sandboxs.txt"  # Save/load sandbox IDs (empty = disabled)

# Creation batch control (protect E2B API from overload)
create_batch:
  size: 20      # Sandboxes per creation batch
  interval: 30  # Seconds between creation batches

# Task execution batch control (protect target server from overload)
task_batch:
  size: 10      # Sandboxes to start tasks per batch
  interval: 5   # Seconds between task batches

# Browser task configuration
browser:
  urls:
    - "http://192.168.110.10:8080/Hubble_Space_Telescope.html"
  task_timeout: 200
  interval_min: 5
  interval_max: 15
  # Warmup configuration (opens multiple tabs via agent-browser)
  warmup_urls:
    - "http://192.168.110.10:8080/China.html"
    - "http://192.168.110.10:8080/Earth.html"
    - "http://192.168.110.10:8080/Galaxy.html"
    - "http://192.168.110.10:8080/Hubble_Space_Telescope.html"
    - "http://192.168.110.10:8080/Human.html"
    - "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html"
    - "http://192.168.110.10:8080/Solar_System.html"
    - "http://192.168.110.10:8080/United_States.html"
    - "http://192.168.110.10:8080/World_War_II.html"
  warmup_loops: 1      # Loop count (Note: in tab mode each URL opened once)
  warmup_delay: 5      # Delay between warmup pages (seconds)
  warmup_only: false   # Run warmup phase only, then exit

# Test run configuration
test:
  duration: 160
  stats_interval: 10
  benchmark_percent: 1.0  # Percentage of sandboxes for benchmark (fixed mode only)

  # Round-robin mode configuration
  benchmark_mode: "round_robin"   # "fixed" (default) or "round_robin"
  round_size: 5    # Sandboxes per round (group count = ceil(total / round_size))
  round_count: 5   # Max rounds to run (stops when reached or duration exceeded)
  round_interval: 5  # Seconds between rounds

# smap_tool configuration (memory migration monitoring)
smap_tool:
  enabled: false
  path: ""            # smap_tool binary path
  swap_size: 81920    # Swap size in MB
  ratio: 15           # Migration ratio
  src_nid: 2          # Source NUMA node
  dest_nid: 5         # Destination NUMA node

# vm_monitor configuration (performance monitoring)
vm_monitor:
  enabled: false
  vmm_type: "firecracker"
  duration: 600
  numa: "1"           # NUMA nodes to monitor (comma-separated)
  log_dir: "results/e2b/vm_monitor"
  stress_file: "/dev/shm/e2b_benchmark_lock"

# Report configuration
report:
  output_dir: "results/e2b"
  filename_prefix: "e2b_bench"
```

### 3. Run Test

#### Full Workflow Mode (Fixed Benchmark)

Create sandboxes, warmup, run tasks on subset, generate report:

```bash
# Using config file
python -m e2b_bench --config config/e2b_bench.yaml

# With CLI overrides
python -m e2b_bench --config config/e2b_bench.yaml --total 50 --duration 300 -bp 0.5

# Full CLI mode (no config file)
python -m e2b_bench \
    --template openclaw-browser-v1 \
    --e2b-access-token your_token \
    --total 100 \
    --duration 600
```

#### Round-Robin Mode (Memory Migration Stress Testing)

Rotate sandbox groups across rounds, each round opens a new tab to trigger memory allocation and swap:

```bash
# Round-robin with 5 sandboxes per round, 5 rounds total
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -rc 5 -ri 5

# Round-robin with unlimited rounds until duration reached
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -ri 5 --duration 600

# Round-robin with smap_tool for memory migration monitoring
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -rc 10 \
    --warmup-url http://server/page1.html
```

#### Create-Only Mode (Phase 0)

Create sandboxes only, without running tasks. Sandboxes stay running for later use:

```bash
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# With creation batch control and NUMA binding
python -m e2b_bench --config config/e2b_bench.yaml \
    --create-only \
    --create-batch-size 20 \
    --create-batch-interval 30

# Save sandbox IDs for later reuse
python -m e2b_bench --config config/e2b_bench.yaml \
    --create-only --sandbox-ids-file sandboxs.txt
```

#### Detect Existing Mode

Detect existing running sandboxes (from IDs file or API) and run benchmark on them:

```bash
# Detect all running sandboxes
python -m e2b_bench --config config/e2b_bench.yaml --detect

# Detect from saved IDs file
python -m e2b_bench --config config/e2b_bench.yaml \
    --detect --sandbox-ids-file sandboxs.txt

# With task batch control
python -m e2b_bench --config config/e2b_bench.yaml \
    --detect \
    --task-batch-size 10 \
    --task-batch-interval 5
```

#### Warmup-Only Mode

Run warmup phase only to preheat browser memory, then exit. Sandboxes stay running for later benchmark:

```bash
python -m e2b_bench --config config/e2b_bench.yaml --warmup-only

# With custom warmup pages
python -m e2b_bench --config config/e2b_bench.yaml \
    --warmup-only \
    --warmup-url http://192.168.110.10:8080/page1.html \
    --warmup-url http://192.168.110.10:8080/page2.html \
    --warmup-loops 1 \
    --warmup-delay 5

# Large-scale warmup (> 100 sandboxes creates in waves)
python -m e2b_bench --config config/e2b_bench.yaml \
    --warmup-only --total 200
```

#### Full Workflow with Warmup + Round-Robin

Create sandboxes, warmup (opens N tabs), then round-robin benchmark (each round opens a new tab):

```bash
python -m e2b_bench --config config/e2b_bench.yaml \
    --warmup-url http://192.168.110.10:8080/page1.html \
    -bm round_robin -rs 5 -rc 5 --duration 600
```

## Running Modes Comparison

| Mode | Flag | Description | Sandbox Behavior |
|------|------|-------------|------------------|
| **Full Workflow (Fixed)** | (default) | Create → Port Check → Tasks → Report | Killed after test |
| **Full Workflow (Round-Robin)** | `-bm round_robin` | Create → Port Check → Round-robin → Report | Killed after test |
| **Create-Only** | `--create-only` | Create → Port Check → Exit | Left running |
| **Detect Existing** | `--detect` | Detect → Tasks → Report | Left running |
| **Warmup-Only** | `--warmup-only` | Create/Detect → Warmup → Exit | Left running |

## Benchmark Modes

### Fixed Mode (Default)

A fixed percentage of sandboxes execute browser tasks concurrently for the test duration. Controlled by `benchmark_percent` (e.g., 0.5 = 50% of sandboxes).

```yaml
test:
  benchmark_mode: "fixed"
  benchmark_percent: 0.5  # 50% of sandboxes run tasks
```

### Round-Robin Mode

Sandboxes are divided into groups based on `round_size`. Each round activates one group, opening a new tab and executing operations. This creates continuous memory allocation, triggering swap-out events for memory migration stress testing.

**How groups are formed:**
- `round_size` determines group count: `group_count = ceil(total / round_size)`
- Sandboxes distributed evenly across groups (remainder goes to first groups)
- Example: 103 sandboxes, `round_size=5` → 21 groups (5,5,...,5,3)

**How rounds execute:**
- Round 0 → group 0, Round 1 → group 1, ..., Round 20 → group 20
- After all groups complete, cycling continues: Round 21 → group 0 again
- Each round: `TabOperationRunner` opens new tab → snapshot → click → screenshot
- Round interval (`round_interval`) provides gap between rounds for memory migration

**Termination conditions (coexist):**
- `round_count`: Stops after N rounds (if specified)
- `duration`: Stops when test_duration is reached
- Test stops when EITHER condition is met

```yaml
test:
  benchmark_mode: "round_robin"
  round_size: 5       # 5 sandboxes per round (determines group count)
  round_count: 10     # Max 10 rounds (termination condition)
  round_interval: 5   # 5 seconds between rounds
  duration: 600       # Also stops if 600s elapsed
```

**Round-Robin + Warmup:**

The warmup phase opens multiple tabs (one per warmup_url), preloading browser memory. During round-robin benchmark, each round opens a **new tab**, adding memory pressure and triggering swap when accessing migrated memory.

## Browser Operations

### Warmup Phase (Tab Mode)

Warmup uses `agent-browser` to open multiple tabs:

```text
For each sandbox:
  Check agent-browser availability
  For each warmup_url:
    agent-browser tab new "{url}"       # Open tab
    agent-browser wait --load domcontentloaded --timeout 120000  # Wait for page
    agent-browser snapshot -i           # DOM snapshot (memory allocation)
    agent-browser click {element}       # Click element (memory allocation)
    agent-browser screenshot            # Screenshot (memory allocation)
    Wait warmup_delay seconds
  Mark warmup_done = True
```

**Note:** `warmup_loops` is ignored in tab mode — each URL is opened exactly once as a separate tab.

### Fixed Mode Browser Tasks

Each sandbox runs continuous browser tasks in a loop:

```text
For each sandbox (independent thread):
  While not stop_event:
    openclaw browser --browser-profile openclaw open '{url}'  # Open URL
    Wait random interval (interval_min to interval_max)
    If 3 consecutive failures → mark offline
```

### Round-Robin Mode Tab Operations (5 Steps)

Each round opens a new tab and executes a 5-step operation sequence with detailed timing:

```text
Step 1: agent-browser tab new "{url}"                         → open_tab timing
Step 2: agent-browser wait --load networkidle --timeout 60s   → page_load timing
Step 3: agent-browser snapshot -i                             → snapshot timing
Step 4: agent-browser click {element}                         → click timing
Step 5: agent-browser screenshot                              → screenshot timing
```

**Step timing provides granular performance analysis:**
- `open_tab`: Tab creation time (E2B process overhead)
- `page_load`: Network idle wait time (page rendering + resource loading)
- `snapshot`: DOM snapshot time (Chrome devtools protocol overhead)
- `click`: Element interaction time
- `screenshot`: Screenshot capture time

**Non-fatal steps:** Click and screenshot failures are logged but don't mark the task as failed.

## Tail Latency Analysis

The report includes tail latency analysis using P99/P50 ratio:

| Tail Ratio | Classification | Meaning |
|------------|---------------|---------|
| < 1.2x | minimal | Well-behaved distribution, no significant outliers |
| 1.2x ~ 1.5x | moderate | Some long-tail outliers present |
| > 1.5x | significant | Severe long-tail latency, outliers dominate |

Applied to both step-level timing and round comparison tables.

## Error Classification

Failed tasks are automatically classified into error types:

| Error Type | Pattern | Typical Cause |
|------------|---------|---------------|
| Open tab failed | `open_tab failed` | Tab creation failure |
| Page load failed | `page_load failed` | Network/page load timeout |
| Snapshot failed | `snapshot failed` | DOM snapshot timeout |
| Click failed | `click failed` | Element click timeout |
| Screenshot failed | `screenshot failed` | Screenshot capture failure |
| Chrome start failed | `chrome_start`, `failed to start chrome` | Chrome process crash |
| D-Bus connection error | `d-bus`, `dbus` | D-Bus daemon unavailable |
| Gateway connection error | `gateway`, `cdp`, `http_unreachable` | CDP gateway unreachable |
| Timeout | `timeout`, `timed out` | Command timeout |
| Other | (catch-all) | Uncategorized errors |

Each error includes detailed diagnostics: exit_code, stderr, stdout (truncated).

## Round Comparison Report

In round-robin mode, the report includes a round comparison table:

```text
[Round Comparison]
================================================================================

  Summary: 50 tasks across 10 rounds

Round  Tasks  Success%  Avg(s)  P50(s)  P95(s)  P99(s)  Tail
0      5      100.0     3.42    3.30    4.10    5.20    1.58x (significant)
1      5      80.0      4.10    3.80    5.30    7.10    1.87x (significant)
...
```

Each round shows:
- Task count (delta from previous round baseline)
- Success rate (percentage)
- Latency percentiles (Avg, P50, P95, P99)
- Tail ratio with severity classification

## smap_tool Integration

smap_tool monitors memory migration between NUMA nodes during benchmark:

```yaml
smap_tool:
  enabled: true
  path: "/path/to/smap_tool"  # smap_tool binary path
  swap_size: 81920             # Swap size in MB
  ratio: 15                    # Migration ratio (%)
  src_nid: 2                   # Source NUMA node
  dest_nid: 5                  # Destination NUMA node
```

**Lifecycle:**
- Started after sandbox creation (gets `pidof firecracker` for target PIDs)
- Logs to `smap_tool/` directory in result folder
- Stopped during cleanup phase
- Cleans up `/dev/shm/smap_config` before starting

## vm_monitor Integration

vm_monitor collects hardware performance metrics with stress-file sync mechanism:

```yaml
vm_monitor:
  enabled: true
  vmm_type: "firecracker"
  duration: 600        # Monitoring duration (should match test duration)
  numa: "1"            # NUMA nodes to monitor (comma-separated)
  log_dir: "results/e2b/vm_monitor"
  stress_file: "/dev/shm/e2b_benchmark_lock"
```

**Stress-file sync:**
- vm_monitor starts in background, waiting for stress file to appear
- When benchmark phase begins, stress file is created (`touch /dev/shm/e2b_benchmark_lock`)
- vm_monitor detects stress file and starts collecting metrics
- When benchmark ends, stress file is removed
- vm_monitor stops sampling and generates `analysis_report.xlsx`

**CLI flags passed to vm_monitor:**
- `--vmm firecracker` (from config)
- `--enable-capture` (always enabled)
- `--auto-skip` (skip unavailable tools)

## Batch Test Mode

### Overview

Batch mode runs multiple test scenarios defined by a matrix config, reusing sandboxes and smap_tool within groups.

```bash
# Online mode: run batch tests
python -m e2b_bench --batch --matrix config/e2b_batch_matrix.yaml

# Offline mode: generate summary from existing results
python -m e2b_bench --batch --offline --result-dir results/e2b/batch
```

### Matrix Configuration

Edit `config/e2b_batch_matrix.yaml`:

```yaml
test_matrix:
  total_counts: [10, 20, 50]
  benchmark_percentages: [0.5, 0.75, 1.0]
  ratios: [10, 20]

reuse_strategy:
  reuse_sandbox: true      # Reuse sandbox within same (total_count, ratio) group
  reuse_smap_tool: true    # Reuse smap_tool within same group

result:
  template_path: "config/e2b_batch_template.yaml"
  output_dir: "results/e2b/batch"
```

### Batch Workflow

```text
1. Generate task groups by (total_count, ratio)
2. For each group:
   a. Create shared sandboxes (group total_count)
   b. Start smap_tool (shared, logs to group_result_dir/smap_tool/)
   c. Warmup (shared, once)
   d. For each benchmark_percent:
      - Start vm_monitor (per-task, with stress-file sync)
      - Run benchmark
      - Stop vm_monitor sampling
      - Save bench_report.txt
      - Wait for analysis_report.xlsx
   e. Cleanup: stop smap_tool, kill sandboxes
3. Extract metrics from all results
4. Generate aggregated Excel summary (styled, with source grouping)
```

### Result Structure

```
results/e2b/batch/
├── batch_log_*.txt                           # Execution log
├── e2b_batch_summary_*.xlsx                  # Aggregated summary
├── tc10_ratio10_20260629_140636/             # Group directory
│   ├── smap_tool/
│   │   ├── smap_stdout.log
│   │   └── smap_stderr.log
│   ├── tc10_ratio10_bp0.5_20260629_140805/   # Task directory
│   │   ├── config_tc10_ratio10_bp0.5.yaml
│   │   ├── test_log.txt
│   │   ├── bench_report.txt
│   │   └── vm_monitor/
│   │       ├── analysis_report.xlsx
│   │       ├── monitor_stdout.log
│   │       └── monitor_stderr.log
│   └── tc10_ratio10_bp0.75_.../
│   └── tc10_ratio10_bp1.0_.../
└── tc20_ratio10_.../
```

## Configuration Priority

The configuration follows a strict priority chain:

**CLI arguments > YAML config > Built-in defaults**

This means:
- If a CLI argument is explicitly provided, it overrides YAML config
- If no CLI argument is provided, YAML config value is used
- If neither CLI nor YAML specifies a value, built-in defaults are used
- CLI default values (like `None`) do NOT shadow YAML config values

This was fixed to prevent CLI parser defaults from incorrectly overriding YAML values.

## Sandbox ID Persistence

Save and load sandbox IDs across sessions for reuse:

```yaml
sandbox:
  sandbox_ids_file: "sandboxs.txt"  # One ID per line
```

**Behavior:**
- **Create-only mode**: Writes successful sandbox IDs to file after creation
- **Warmup-only mode**: Appends IDs after each warmup wave
- **Detect mode**: Loads IDs from file instead of querying API

This enables multi-phase testing workflows:
1. `--create-only --sandbox-ids-file ids.txt` → Create sandboxes, save IDs
2. `--warmup-only --detect --sandbox-ids-file ids.txt` → Warmup detected sandboxes
3. `--detect --sandbox-ids-file ids.txt -bm round_robin` → Benchmark on warmed-up sandboxes

## Wave-Based Warmup

When creating > 100 sandboxes in warmup-only mode, creation is split into waves of 100:

```text
Wave 1: Create 100 sandboxes → Warmup → Append IDs
Wave 2: Create remaining → Warmup → Append IDs
...
```

This avoids overwhelming E2B API with too many concurrent sandbox creations.

## CLI Arguments

```bash
python -m e2b_bench --help

Options:
  -c, --config              YAML configuration file path

  # E2B Environment
  --e2b-access-token        E2B access token
  --e2b-api-key             E2B API key
  --e2b-domain              E2B domain
  --e2b-api-url             E2B API URL
  --e2b-http-ssl            E2B HTTP SSL setting

  # Sandbox Configuration
  -t, --template            E2B template name
  -n, --total               Total sandbox count
  --create-timeout          Sandbox creation timeout
  -d, --detect              Detect existing sandboxes mode
  --create-only             Create-only mode (Phase 0)
  --sandbox-ids-file        File path to save/load sandbox IDs

  # Creation Batch Control
  --create-batch-size       Sandboxes per creation batch (None = full concurrent)
  --create-batch-interval   Creation batch interval seconds

  # Task Batch Control
  --task-batch-size         Sandboxes to start tasks per batch (None = full concurrent)
  --task-batch-interval     Task batch interval seconds

  # Browser Task
  --browser-url             Browser URL (can specify multiple)
  --browser-timeout         Browser task timeout
  --browser-interval-min    Task interval minimum
  --browser-interval-max    Task interval maximum

  # Warmup Phase
  -w, --warmup-url          Warmup page URL (can specify multiple)
  --warmup-loops            Warmup loop count (default: 2)
  --warmup-delay            Delay between warmup pages (default: 10)
  -wp, --warmup-only        Run warmup phase only, then exit

  # Benchmark Control
  -bp, --benchmark-percent  Percentage of sandboxes for benchmark (fixed mode, e.g., 0.5 = 50%)

  # Round-Robin Mode Control
  -bm, --benchmark-mode     Benchmark mode: 'fixed' (default) or 'round_robin'
  -rc, --round-count        Max number of rounds (termination condition)
  -rs, --round-size         Sandboxes per round (default: 5, determines group count)
  -ri, --round-interval     Round interval in seconds (default: 5)

  # Test Run
  --duration                Test duration seconds
  --stats-interval          Stats snapshot interval

  # Report
  -o, --output-dir          Report output directory
  --filename-prefix         Report filename prefix
```

### Batch Mode CLI

```bash
python -m e2b_bench --batch --help

Options:
  --matrix                  Test matrix YAML config path (required for online mode)
  --offline                 Generate summary from existing results
  --result-dir              Result directory for offline mode (required with --offline)
  --output                  Output Excel path for offline mode
  --continue-on-failure     Continue testing if a group fails
```

## Test Workflow

### Single Test (Fixed Mode)

```text
Phase 1: Create/Detect Sandboxes
    ├── [Full/Create-Only] Call sandbox.create() API (with NUMA binding)
    ├── [Detect] Query existing or load from sandbox_ids_file
    ├── Record create_elapsed time
    └── Start port check (18789 + 11436)

Phase 2: Port Check
    ├── Check 18789 (openclaw-gateway)
    ├── Check 11436 (llama-server)
    ├── Record port_wait_elapsed time
    └── Mark PORT_READY when both ports ready

[Create-Only Mode: Exit Here]

Phase 3: Warmup Phase (optional)
    ├── [With warmup_urls] Open tabs via agent-browser
    ├── Execute snapshot → click → screenshot per tab
    └── Mark warmup_done when complete

[Warmup-Only Mode: Exit Here]

Phase 4: Start Browser Tasks
    ├── [Fixed mode] Select subset by benchmark_percent
    ├── [With task_batch] Batched task start
    └── [Without config] Full concurrent start

Phase 5: Run Test
    └── Collect real-time statistics for test_duration

Phase 6: Stop and Report
    ├── Kill sandboxes (if created, not detected)
    └── Generate performance report
```

### Single Test (Round-Robin Mode)

```text
Phase 1-3: Same as fixed mode

Phase 4: Round-Robin Benchmark
    ├── Divide sandboxes into groups (by round_size)
    ├── For each round:
    │   ├── Select group (with cycling)
    │   ├── Create TabOperationRunner per sandbox
    │   ├── Execute: open_tab → page_load → snapshot → click → screenshot
    │   ├── Wait for all runners to complete
    │   ├── Record round baseline for next round
    │   ├── Print round summary (step timing breakdown)
    │   └── Wait round_interval seconds
    └── Stop when round_count or duration reached

Phase 5: Stop and Report
    ├── Generate report with round comparison table
    └── Generate step-level timing table with tail analysis
```

### Batch Test

```text
Phase 1: Load matrix config and generate task groups

Phase 2: For each group:
    ├── Create shared sandboxes
    ├── Start smap_tool (if enabled)
    ├── Warmup (if warmup_urls configured)
    └── For each task (different benchmark_percent):
        ├── Start vm_monitor with stress-file
        ├── Trigger stress file (start sampling)
        ├── Run benchmark
        ├── Remove stress file (stop sampling)
        ├── Wait for analysis_report.xlsx
        └── Save bench_report.txt

Phase 3: Cleanup group (stop smap_tool, kill sandboxes)

Phase 4: Extract metrics from all results

Phase 5: Generate aggregated Excel summary
```

## Delete Sandboxes

### Using delete_sandbox.sh

Delete all running sandboxes:

```bash
# Setup environment
cp e2b_bench/.env.example e2b_bench/.env
# Edit .env with your E2B_API_URL and E2B_API_KEY

# Run delete script
cd e2b_bench
./delete_sandbox.sh

# Or specify custom env file
./delete_sandbox.sh path/to/.env
```

### Environment File (.env)

```bash
E2B_API_URL=http://141.61.17.196:3000
E2B_API_KEY=e2b_d8ced731a9db82628c1e7279bec5ca70d6f74a6f
```

## Debugging

### Common Issues

| Error | Possible Cause | Solution |
|-------|---------------|----------|
| `Response 400` | Invalid template name | Check E2B template exists |
| `GatewayClient error` | Gateway not started | Check port 18789 |
| `Port check failed` | Port timeout | Increase PORT_CHECK_MAX_WAIT |
| `Command exit_code=1` | Command syntax error | Check openclaw/agent-browser version |
| `SandboxPaginator error` | Wrong iteration | Use paginator.has_next and next_items() |
| `open_tab failed` | Tab creation timeout | Check E2B API connectivity |
| `page_load failed` | Page loading timeout | Increase timeout or check URL |
| `D-Bus connection error` | D-Bus unavailable | Check sandbox D-Bus daemon |
| `Gateway connection error` | CDP unreachable | Check openclaw-gateway status |

## Sandbox Status Flow

```text
PENDING → CREATING → CREATED → PORT_READY → (ACTIVE) → KILLED
                     ↓
                  FAILED
                     ↓
               PORT_FAILED
                     ↓
                  OFFLINE
```

## Related Documentation

- [E2B Bench Usage (中文)](e2b-bench-usage-zh.md)
- [Metrics Reference](metrics-reference.md) - All 50+ metrics explained
- [vm_monitor Usage](usage-guide.md) - vm_monitor tool configuration
