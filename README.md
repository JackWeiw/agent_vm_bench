# Agent VM Bench - OpenStack VM Memory Overcommit Performance Testing

[中文说明](README_zh.md)

Test framework for OpenStack VM memory overcommit scenarios with comprehensive performance monitoring.

## Documentation

| Document | Description |
|----------|-------------|
| [Design](docs/design.md) | System architecture and flow design |
| [Design (EN)](docs/design-en.md) | English version of design doc |
| [Metrics Reference](docs/metrics-reference.md) | All 50+ metrics explained |
| [Usage Guide](docs/usage-guide.md) | Detailed tool usage and configuration |
| [vm_bench Usage](docs/vm_bench-usage-guide.md) | **Modular vm_bench package usage (recommended)** |
| [vm_bench Usage (中文)](docs/vm_bench-usage-guide-zh.md) | vm_bench 模块使用指南 |
| [E2B Bench Usage](docs/e2b-bench-usage.md) | E2B Sandbox batch performance testing |
| [E2B Bench Usage (中文)](docs/e2b-bench-usage-zh.md) | E2B沙箱批量性能测试指南 |
| [Docker Bench Usage](docs/docker-bench-usage.md) | Docker container browser automation testing |
| [Docker Bench Usage (中文)](docs/docker-bench-usage-zh.md) | Docker容器浏览器自动化性能测试指南 |

## Dependencies

```bash
pip install -r requirements.txt
```

Core: `psutil`, `paramiko`, `flask`, `yaml`

Optional (Excel): `pandas`, `openpyxl`

---

## Quick Start (E2B Sandbox Bench)

The complete testing workflow from environment setup to running benchmarks:

### Step 1: Download Web Pages

```bash
# Download all 10 Wikipedia pages (with images)
bash download_page.sh

# Or download specific pages
bash download_page.sh -p Weibo,China
```

Pages saved to `web_content/en.wikipedia.org/wiki/`.

### Step 2: Build Docker Image

```bash
# Build the base image (ARM64)
cd dockerfile_build
docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 .

# Or build for x86
docker build -f Dockerfile.x86 -t ubuntu-openclaw-chromium:24.04-linuxx86 .
```

### Step 3: Push to Harbor Registry

```bash
cd dockerfile_build
# Set Harbor IP (your E2B + Harbor server)
HARBOR_IP=71.14.96.192 bash push_to_harbor.sh
```

Image pushed to `HARBOR_IP:2900/e2b-orchestration/ubuntu-openclaw-chromium:custom`.

### Step 4: Build E2B Template

```bash
cd dockerfile_build
# Build template from Harbor image (alias = template name for later use)
python3 build_e2b.py --server-ip 71.14.96.192 --alias openclaw-browser-v1

# Or with custom Harbor IP and alias
python3 build_e2b.py --server-ip 71.14.96.192 --harbor-ip 71.14.96.192 --alias my-template
```

Requires `~/.e2b/config.json` with `accessToken` and `teamApiKey`.

### Step 5: Start Web Server

```bash
cd web_content/en.wikipedia.org/wiki
# Bind to NUMA node 2,3 for memory isolation
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

### Step 6: Modify Configuration

Edit `config/e2b_bench.yaml` — update the template name and web server URL to match your setup:

```yaml
sandbox:
  template: "openclaw-browser-v1"  # ← Your template alias from Step 4

browser:
  urls:
    - "http://YOUR_LOCAL_IP:8080/Hubble_Space_Telescope.html"  # ← Your web server
  warmup_urls:
    - "http://YOUR_LOCAL_IP:8080/China.html"
    # ... other pages

e2b_env:
  E2B_API_URL: "http://71.14.96.192:3000"  # ← Your E2B API server
```

### Step 7: Create Sandboxes

```bash
# Create sandboxes only (Phase 0 — left running for later use)
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# With sandbox ID persistence
python -m e2b_bench --config config/e2b_bench.yaml --create-only --sandbox-ids-file sandboxs.txt
```

### Step 8: Run Benchmark

```bash
# Detect existing sandboxes and benchmark (fixed mode)
python -m e2b_bench --config config/e2b_bench.yaml --detect

# Round-robin mode (memory migration stress testing)
python -m e2b_bench --config config/e2b_bench.yaml --detect -bm round_robin -rs 5 -rc 5

# Warmup first, then benchmark
python -m e2b_bench --config config/e2b_bench.yaml --detect --warmup-only  # Phase 1: warmup
python -m e2b_bench --config config/e2b_bench.yaml --detect -bm round_robin  # Phase 2: benchmark
```

### Step 9: Delete Sandboxes

```bash
cd e2b_bench/scripts
# Configure .env with your E2B API server IP
cp .env.example .env
# Edit .env: set E2B_API_URL=http://YOUR_IP:3000

# Delete all running sandboxes
bash delete_sandbox.sh

# Or specify custom .env path
bash delete_sandbox.sh path/to/.env
```

### Workflow Summary

```text
download_page.sh → docker build → push_to_harbor.sh → build_e2b.py → http.server
     ↓                 ↓              ↓                 ↓            ↓
  Web pages     Docker image    Harbor registry    E2B template    Web server
                                                                       ↓
                              config/e2b_bench.yaml (template + URL)
                                       ↓
                              --create-only → --detect → benchmark → delete_sandbox.sh
```

---

## E2B Sandbox Bench

Browser automation performance testing in E2B Firecracker microVMs, with memory migration stress testing support.

### Quick Start

```bash
# Install dependencies
pip install -r e2b_bench/requirements.txt

# Full workflow (fixed mode)
python -m e2b_bench --config config/e2b_bench.yaml

# Round-robin mode (memory migration stress testing)
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -rc 5 -ri 5

# Create sandboxes only (Phase 0)
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# Detect existing sandboxes and benchmark
python -m e2b_bench --config config/e2b_bench.yaml --detect

# Warmup only (multi-tab memory preheating)
python -m e2b_bench --config config/e2b_bench.yaml --warmup-only

# Batch testing (matrix config)
python -m e2b_bench --batch --matrix config/e2b_batch_matrix.yaml

# Offline summary from existing results
python -m e2b_bench --batch --offline --result-dir results/e2b/batch
```

### Key Features

| Feature | Description | CLI Flag |
|---------|-------------|----------|
| **Fixed Benchmark** | Subset percentage of sandboxes run tasks | `-bp 0.5` |
| **Round-Robin Mode** | Group rotation with tab switching for swap stress | `-bm round_robin` |
| **Step-Level Timing** | Separate timing for open_tab, page_load, snapshot, click, screenshot | Automatic in round-robin |
| **Tail Latency Analysis** | P99/P50 ratio with severity classification | Automatic in reports |
| **Round Comparison** | Per-round statistics table | Automatic in round-robin reports |
| **Error Classification** | Auto-classify failures (D-Bus, Gateway, Timeout, etc.) | Automatic in reports |
| **smap_tool Integration** | Memory migration monitoring | YAML: `smap_tool.enabled` |
| **vm_monitor Integration** | Performance metrics with stress-file sync | YAML: `vm_monitor.enabled` |
| **NUMA Binding** | Bind sandbox creation to specific NUMA node | YAML: `sandbox.numa_bind` |
| **Sandbox ID Persistence** | Save/load IDs for cross-session reuse | `--sandbox-ids-file` |

### Running Modes

| Mode | Flag | Sandbox Behavior |
|------|------|------------------|
| Full Workflow (Fixed) | (default) | Killed after test |
| Full Workflow (Round-Robin) | `-bm round_robin` | Killed after test |
| Create-Only | `--create-only` | Left running |
| Detect Existing | `--detect` | Left running |
| Warmup-Only | `--warmup-only` | Left running |

### Files

| File | Description |
|------|-------------|
| `e2b_bench/bench.py` | Main orchestration, SmapToolManager, VmMonitorManager |
| `e2b_bench/round_robin.py` | Round-robin task manager (group rotation, cycling) |
| `e2b_bench/task_runner.py` | WarmupRunner, BrowserTaskRunner, TabOperationRunner |
| `e2b_bench/stats_collector.py` | Statistics, ErrorClassifier, ReportFormatter |
| `e2b_bench/batch_scheduler.py` | Batch test orchestration with sandbox reuse |
| `e2b_bench/config.py` | Configuration (YAML + CLI + defaults priority chain) |
| `e2b_bench/sandbox_manager.py` | Sandbox lifecycle (create, port check, NUMA bind) |
| `e2b_bench/metrics_extractor.py` | Extract metrics from vm_monitor + browser reports |
| `e2b_bench/report_aggregator.py` | Aggregate batch results into styled Excel |
| `config/e2b_bench.yaml` | Single test configuration template |
| `config/e2b_batch_matrix.yaml` | Batch test matrix |

See [E2B Bench Usage Guide](docs/e2b-bench-usage.md) for details.

---

## Docker Container Bench

Browser automation performance testing in Docker containers.

### Quick Start

```bash
# Install dependencies
pip install -r docker_bench/requirements.txt

# Run with config file
python -m docker_bench --config config/docker_bench.yaml

# Create containers only (Phase 0)
python -m docker_bench --config config/docker_bench.yaml --create-only

# Detect existing containers and benchmark
python -m docker_bench --config config/docker_bench.yaml --detect

# Full CLI mode
python -m docker_bench \
    --image ubuntu-openclaw-chromium:arm64 \
    --total 10 \
    --cpu 2 \
    --memory 2g \
    --duration 160
```

### Files

| File | Description |
|------|-------------|
| `docker_bench/bench.py` | Main entry point |
| `docker_bench/container_manager.py` | Container lifecycle management |
| `docker_bench/task_runner.py` | Browser task execution |
| `docker_bench/stats_collector.py` | Statistics collection and reporting |
| `config/docker_bench.yaml` | Configuration template |

See [Docker Bench Usage Guide](docs/docker-bench-usage.md) for details.

---

## Automated Batch Testing

### Run Batch Tests

```bash
# Preview tasks
python3 batch_test_scheduler.py --config config/batch_config.yaml --dry-run

# Execute batch
python3 batch_test_scheduler.py --config config/batch_config.yaml

# Offline summary from existing results
python3 batch_test_scheduler.py --offline --result-dir results
```

### Single Test

```bash
python3 auto_vm_test.py --config config/test_config.yaml
```

### Result Structure

```text
results/
├── batch_summary_*.xlsx           # Batch summary (50+ metrics)
├── batch_log_*.txt                # Execution log
│
└── vm{n}_ratio{r}_active{p}_*/    # Single test result
    ├── config.yaml                # Test config
    ├── test_log.txt               # Execution log
    ├── vm_bench_lite/             # Benchmark reports
    ├── qemu_monitor/              # Monitoring data + Excel
    └── summary/                   # Metrics JSON
```

---

## Files

| File | Description |
|------|-------------|
| `create_server.py` | Create OpenStack VMs |
| `vm_monitor.py` | Monitor VM resources (QEMU/Firecracker) + log collection |
| `qemu_monitor.py` | (Deprecated) Legacy entry point for QEMU monitoring |
| `vm_bench_lite.py` | Browser/QA benchmark |
| `auto_vm_test.py` | Single test automation |
| `batch_test_scheduler.py` | Batch test orchestration |
| `stress_tool.cpp` | VM stress tool |
| `download_page.sh` | Download warmup pages |

---

## vm_monitor Package

The `vm_monitor` package provides a unified monitoring framework for multiple VMM types:

| VMM Type | Process Names | CLI Flag |
|----------|---------------|----------|
| QEMU | `qemu-kvm`, `qemu-system` | `--vmm qemu` (default) |
| Firecracker | `firecracker` | `--vmm firecracker` |

**Python API:**

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

# QEMU monitoring
qemu_monitor = QEMUMonitor()
qemu_monitor.start_monitoring(duration_seconds=60, interval_seconds=3)

# Firecracker monitoring
fc_monitor = FirecrackerMonitor()
fc_monitor.start_monitoring(duration_seconds=60, interval_seconds=3)
```
