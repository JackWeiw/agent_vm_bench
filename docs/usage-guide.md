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

### vm_monitor.py - Resource Monitoring

```bash
# Basic monitoring (QEMU, default)
python3 vm_monitor.py -t 300 -i 2

# Firecracker monitoring
python3 vm_monitor.py --vmm firecracker -t 300 -i 2

# With log collection
python3 vm_monitor.py -t 300 -i 2 --enable-capture

# Custom output directory
python3 vm_monitor.py -t 300 --enable-capture --log-dir /data/test_run_1

# Specific NUMA nodes
python3 vm_monitor.py -t 300 --enable-capture --numa 0,1

# Backward compatible (deprecated)
python3 qemu_monitor.py -t 300 -i 2
```

#### Parameters

| Parameter | Description |
|-----------|-------------|
| `-t` | Duration in seconds |
| `-i` | Sampling interval in seconds |
| `--vmm` | VMM type: `qemu` (default) or `firecracker` |
| `--enable-capture` | Enable log collection (devkit, ksys, ub_watch) |
| `--log-dir` | Output directory |
| `--numa` | NUMA nodes to monitor |
| `--stress-file` | Lock file for sync with benchmark |

#### VMM Types

| VMM Type | Process Names | Description |
|----------|---------------|-------------|
| `qemu` | `qemu-kvm`, `qemu-system` | QEMU/KVM virtual machines (default) |
| `firecracker` | `firecracker` | Firecracker microVMs (E2B sandboxes) |

#### Python API

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

# QEMU monitoring
qemu_monitor = QEMUMonitor()
qemu_monitor.target_numa_nodes = [0, 1]
qemu_monitor.start_monitoring(duration_seconds=60, interval_seconds=3)
qemu_monitor.analyze_and_export()

# Firecracker monitoring
fc_monitor = FirecrackerMonitor()
fc_monitor.start_monitoring(duration_seconds=60, interval_seconds=3)

# Custom VMM (extend VMMonitorBase)
class MyVMMMonitor(VMMonitorBase):
    def get_process_names(self):
        return ('my-vmm-process',)
    
    def extract_vm_id(self, pid, cmdline):
        return f"myvm-{pid}"
    
    def get_monitor_title(self):
        return "My VMM Monitoring"
    
    def get_no_vm_message(self):
        return "No running My VMM instances"
    
    def get_csv_filename_prefix(self):
        return "my_vmm_monitor"
    
    def get_vms_realtime(self):
        # Implement process discovery logic
        ...
```

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
| **smap_bw** | SMAP migration bandwidth | `smap_bw.log` | SMAPBW_Summary, SMAPBW_Cycles | 5+ |
| **getfre** | Core frequency monitoring | `getfre_NUMA*.log` | Getfre_Summary, Getfre_NUMA* | 5+ per NUMA |

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

#### smap_bw

SMAP migration bandwidth monitoring:

- **Bandwidth**: Memory migration bandwidth (GB/s) per cycle
- **Pages**: Total migrated pages count
- **Direction**: Per-direction migration statistics (NUMA node pairs)

#### getfre

Core frequency monitoring (物理核心频率采集):

- **Frequency**: Real-time core frequency (MHz) per physical core
- **NUMA Summary**: Average/min/max frequency per NUMA node
- **Per-Core Details**: Frequency statistics for each core
- **Variance Analysis**: High-variance cores (>100 MHz fluctuation)

**Configuration**: Requires `getfre_config.yaml` for sampling parameters:

```yaml
# getfre_config.yaml
getfre_path: /path/to/getfre    # executable path
total_cores: 192                # 192-core system
interval: 2                     # sampling interval (seconds)
core_interval: 1                # core sampling interval (1=all, 2=every other)
numa_nodes:                     # NUMA nodes to monitor
  - 0
  - 1
```

**NUMA to Physical Core Mapping** (192-core system with hyperthreading):

| NUMA | Physical Cores | Logical Cores |
|------|-----------------|---------------|
| 0    | 0-47            | 0-95          |
| 1    | 48-95           | 96-191        |
| 2    | 96-143          | 192-287       |
| 3    | 144-191         | 288-383       |

### Configuration (.env)

```env
# DevKit collection tool path
DEVKIT_PATH=/path/to/devkit

# ksys collection tool path and config
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/config.yaml

# ub_watch collection tool path
UB_WATCH_PATH=/path/to/ub_watch

# smap_bw script path
SMAP_BW_PATH=/path/to/smap_bw.py

# getfre executable path and config
GETFRE_PATH=/path/to/getfre
GETFRE_CONFIG_PATH=/path/to/getfre_config.yaml

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
├── smap_bw.log            # smap_bw output
├── getfre_NUMA0.log       # getfre NUMA 0 frequency data
├── getfre_NUMA1.log       # getfre NUMA 1 frequency data
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
├─ smap_bw.log          →  SMAPBW_Summary sheet  →  smapbw_* columns (5)
│                       →  SMAPBW_Cycles sheet   →  per-cycle details
├─ getfre_NUMA*.log     →  Getfre_Summary sheet  →  getfre_numa*_avg/min/max_mhz
│                       →  Getfre_NUMA* sheets   →  per-core frequency
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
python3 auto_vm_test.py --config config/test_config.yaml
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
python3 batch_test_scheduler.py --config config/batch_config.yaml --dry-run
```

#### Execute Batch

```bash
python3 batch_test_scheduler.py --config config/batch_config.yaml
```

#### Offline Summary

Generate summary from existing results:

```bash
# From specific directory
python3 batch_test_scheduler.py --offline --result-dir results

# Use config's base_dir
python batch_test_scheduler.py --offline --config config/batch_config.yaml

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