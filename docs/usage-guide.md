# Usage Guide

Detailed guide for using Agent VM Bench tools.

## Prerequisites

### 1. Terminal Setup

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. Host Network Bridge

Add IP to OpenStack bridge for VM access to host web server:

```bash
# Find bridge interface
ip a | grep brq

# Add IP (use your bridge name)
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

### 3. Warmup Web Server

Download warmup pages and start server:

```bash
# Download pages
bash download_page.sh

# Start server
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

### 4. Configure Log Collection

Create `.env` file for monitoring tools:

```env
DEVKIT_PATH=/path/to/devkit
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/config.yaml
UB_WATCH_PATH=/path/to/ub_watch
DEVKIT_CPU_RANGE=96-191
```

---

## Core Tools

### create_server.py - Create VMs

```bash
python3 create_server.py \
  --start_ip 192.168.110.11 \
  --n 10 \
  --subnet-prefix 192.168.110. \
  --network-id cc56708a-c0c0-4d75-a87e-ed1b1a8af844 \
  --az nova_zone:controller \
  --flavor 2U_4G_30G_4K \
  --image ubuntu-24.04
```

### qemu_monitor.py - Resource Monitoring

```bash
# Basic monitoring
python3 qemu_monitor.py -t 300 -i 2

# With log collection
python3 qemu_monitor.py -t 300 -i 2 --enable-capture

# Custom output directory
python3 qemu_monitor.py -t 300 --enable-capture --log-dir /data/test_run_1

# Specific NUMA nodes
python3 qemu_monitor.py -t 300 --enable-capture --numa 0,1
```

#### Parameters

| Parameter | Description |
|-----------|-------------|
| `-t` | Duration in seconds |
| `-i` | Sampling interval in seconds |
| `--enable-capture` | Enable log collection (devkit, ksys, ub_watch) |
| `--log-dir` | Output directory |
| `--numa` | NUMA nodes to monitor |
| `--stress-file` | Lock file for sync with benchmark |

---

## Log Collection Tools

When using `--enable-capture`, qemu_monitor.py invokes the following performance collection tools. These tools generate detailed metrics that are aggregated into `analysis_report.xlsx` and later extracted into batch summary.

### Tool Overview

| Tool | Purpose | Output File | Metrics Sheet | Metrics Count |
|------|---------|-------------|---------------|---------------|
| **devkit_top_down** | CPU pipeline top-down analysis | `devkit_top_down.log` | DevKit_TopDown | 13 |
| **devkit_mem** | Cache miss rates, DDR bandwidth | `devkit_mem.log` | DevKit_Memory, NUMA_Bandwidth | 6 + 2+ |
| **ksys** | Kernel-level cache latency, IPC | `ksys.log` | KSys | 11 |
| **ub_watch** | NUMA interconnect latency | `ub_watch.log` | UBWatch_Latency | 7 |

### Tool Details

#### devkit_top_down

CPU microarchitecture top-down analysis. Identifies pipeline bottlenecks:

- **Frontend Bound**: Instruction fetch/decode stalls
- **Bad Speculation**: Branch prediction failures
- **Retiring**: Useful work completed
- **Backend Bound**: Execution unit or memory stalls
  - L3 Bound, Mem Bound, Latency Bound, Bandwidth Bound

Key metrics: IPC, cycles, instructions, top-down percentages

#### devkit_mem

Memory subsystem analysis:

- **Cache Miss Rates**: L1D, L1I, L2D, L2I miss percentages
- **DDR Bandwidth**: Read/Write bandwidth (MB/s)
- **NUMA Bandwidth**: Per-node memory bandwidth

#### ksys

Kernel system performance collector:

- **Cache Latency**: L2/L3 miss latency (cycles)
- **IPC**: Instructions per cycle
- **Topdown**: Kernel-level top-down percentages

#### ub_watch

NUMA interconnect monitoring:

- **Latency**: Read/Write latency (ns) between NUMA nodes
- **Bandwidth**: Interconnect bandwidth per chip/port

### Configuration (.env)

```env
# DevKit collection tool path
DEVKIT_PATH=/path/to/devkit

# ksys collection tool path and config
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/config.yaml

# ub_watch collection tool path
UB_WATCH_PATH=/path/to/ub_watch

# DevKit top-down CPU core range (optional, auto-calculated from -numa)
DEVKIT_CPU_RANGE=96-191
```

If `.env` is missing or paths invalid, interactive input will be prompted.

### Output Files

```
logs_20240601_143052/
├── devkit_top_down.log    # DevKit top-down output
├── devkit_mem.log         # DevKit memory output
├── ksys.log               # ksys output
├── ub_watch.log           # ub_watch output
├── *_report.json          # ksys generated reports
└── analysis_report.xlsx   # Aggregated Excel report
```

### Metrics Flow

```
Collection Tools          →  analysis_report.xlsx  →  batch_summary.xlsx
│                           │                        │
├─ devkit_top_down.log  →  DevKit_TopDown sheet  →  td_* columns (13)
├─ devkit_mem.log       →  DevKit_Memory sheet   →  mem_* columns (6)
│                       →  NUMA_Bandwidth sheet  →  numa_* columns (2+)
├─ ksys.log             →  KSys sheet            →  ksys_* columns (11)
├─ ub_watch.log         →  UBWatch_Latency sheet →  ub_* columns (7)
│
└─ qemu_monitor (built-in) → Summary sheet       →  vm_avg/max_cpu (2)
```

For detailed metrics description, see [Metrics Reference](metrics-reference.md).

### vm_bench_lite.py - Benchmark

#### Warmup Phase (-wp)

Connects all VMs for warmup tasks:

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
    --batch-size 20 --batch-interval 5 \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-loops 1 \
    --warmup-delay 2
```

#### Benchmark Phase (-bsp)

Connects partial VMs for testing:

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

#### Parameters

| Parameter | Description |
|-----------|-------------|
| `-n` | Total VM count |
| `--start-ip` | Starting IP address |
| `--browser-mode` | Enable browser testing mode |
| `-wp` | Warmup phase only |
| `-bsp` | Browser stress percent (0.5 = 50% VMs) |
| `--warmup-url` | Warmup page URL (repeatable) |
| `--warmup-loops` | Warmup iterations |
| `--warmup-delay` | Delay between pages (seconds) |
| `--browser-url` | Benchmark target URL |
| `--browser-interval-min/max` | Task interval range |
| `-t` | Test duration (seconds) |

---

## Automated Testing

### auto_vm_test.py - Single Test

Runs complete test flow:

```bash
python3 auto_vm_test.py --config test_config.yaml
```

Test flow:
1. Delete existing VMs
2. Create new VMs
3. Start smap_tool
4. Wait for ready
5. Warmup phase
6. Start monitoring
7. Benchmark phase
8. Collect results
9. Cleanup

### batch_test_scheduler.py - Batch Tests

#### Preview Tasks

```bash
python3 batch_test_scheduler.py --config batch_config.yaml --dry-run
```

#### Execute Batch

```bash
python3 batch_test_scheduler.py --config batch_config.yaml
```

#### Offline Summary

Generate summary from existing results:

```bash
# From specific directory
python3 batch_test_scheduler.py --offline --result-dir results

# Use config's base_dir
python batch_test_scheduler.py --offline --config batch_config.yaml

# Custom output
python3 batch_test_scheduler.py --offline --result-dir results --output summary.xlsx
```

---

## Configuration

### batch_config.yaml

Test parameter matrix:

```yaml
test_matrix:
  vm_counts: [50, 100]
  ratios: [0.10, 0.15, 0.20]
  active_percentages: [0.5, 0.8]

fixed_params:
  start_ip: "192.168.110.11"
  swap_size_gb: 200
  duration: 160

scheduler:
  continue_on_failure: true

result:
  template_path: "test_config_template.yaml"
  base_dir: "results"
```

### test_config_template.yaml

Configuration template with placeholders:

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `{{VM_COUNT}}` | VM count | 100 |
| `{{START_IP}}` | Starting IP | "192.168.110.11" |
| `{{SWAP_SIZE_GB}}` | Hugepage size (GB) | 200 |
| `{{RATIO}}` | Memory borrow ratio | 0.15 |
| `{{ACTIVE_PERCENT}}` | Active VM percentage | 0.5 |
| `{{DURATION}}` | Test duration (seconds) | 160 |

---

## Monitor-Benchmark Sync

Lock file mechanism ensures aligned timing:

1. Monitor starts → waits for `/tmp/vm_benchmark_running.lock`
2. Benchmark starts → creates lock file → Monitor begins sampling
3. Duration expires → Monitor stops → generates Excel
4. Cleanup → removes lock file

---

## Delete VMs

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all  # Verify deletion
```