# Agent VM Bench - OpenStack VM Memory Overcommit Performance Testing

[中文说明](README_zh.md)

This directory contains tools for testing performance under OpenStack VM memory overcommit scenarios.

## Files

- `create_server.py` - Create OpenStack VMs
- `qemu_monitor.py` - Monitor QEMU process resources with optional log collection
- `stress_tool.cpp` - Stress tool for VM memory/CPU consumption
- `vm_bench_lite.py` - Benchmark script with browser and QA modes
- `download_page.sh` - Download Wikipedia warmup pages
- `requirements.txt` - Python dependencies

## Dependencies

Install required packages:

```bash
pip install -r requirements.txt
```

Core dependencies:
- `psutil` - System monitoring
- `paramiko` - SSH client
- `flask` - Web framework

Optional (for Excel export and charts):
- `pandas` - Data analysis
- `openpyxl` - Excel file generation
- `python-dotenv` - .env file support

## Quick Start

### 1. Terminal Setup (Execute First)

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. Configure Host Network Bridge

To allow VMs to access web pages on the host machine, add an IP address to the OpenStack bridge interface:

```bash
# Find the bridge interface name
ip a | grep brq
```

Example output:

```text
10: brqb3fa561d-67: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue state UP group default qlen 1000
11: tap8eee944d-02@if2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UP group default qlen 1000
12: vxlan-667: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
13: tap5cbb0361-f6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
14: tapafbc3810-87: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
```

Add an IP address to the bridge interface (use the bridge name from above):

```bash
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

After this configuration, VMs can access static web pages on the host at `http://192.168.110.10:8080/Weibo.html`.

### 3. Download Warmup Pages

Run this script to download Wikipedia pages and images for browser warmup:

```bash
bash download_page.sh
```

This creates a `web_content` directory with:

- `web_content/en.wikipedia.org/wiki/` - HTML pages
- `web_content/upload.wikimedia.org/` - Images

### 4. Start Warmup Web Server

Start a local HTTP server to serve the warmup pages:

```bash
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

The server runs on port 8080. Access pages at `http://<host_ip>:8080/<page>.html`

Available warmup pages:

- China.html
- World_War_II.html
- United_States.html
- Hubble_Space_Telescope.html
- Solar_System.html
- Earth.html
- Human.html
- List_of_paintings_by_Vincent_van_Gogh.html
- Galaxy.html
- Weibo.html

### 5. Create VMs

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

### 6. Resource Monitoring

After VM creation, wait for QEMU process CPU usage to stabilize around 1% before starting benchmark:

```bash
# Basic monitoring (60s default)
python3 qemu_monitor.py -t 300 -i 2

# With log collection (collects devkit, ksys, ub_watch logs)
python3 qemu_monitor.py -t 300 -i 2 --enable-capture

# Specify log output directory
python3 qemu_monitor.py -t 300 --enable-capture --log-dir /data/test_run_1

# Specify NUMA nodes to monitor
python3 qemu_monitor.py -t 300 --enable-capture --numa 0,1
```

#### Log Collection Configuration

Before using `--enable-capture`, configure tool paths in `.env` file:

```env
# DevKit collection tool path
DEVKIT_PATH=/path/to/devkit

# ksys collection tool path and config
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/config.yaml

# ub_watch collection tool path
UB_WATCH_PATH=/path/to/ub_watch

# DevKit top-down CPU core range (optional, auto-calculated from -numa if not set)
DEVKIT_CPU_RANGE=96-191
```

If `.env` is missing or paths are invalid, the tool will prompt for interactive input.

#### Output Files

After monitoring with `--enable-capture`, the output directory contains:

```text
logs_20240601_143052/
├── qemu_monitor.csv          # VM raw data
├── summary.csv               # Summary statistics
├── analysis_report.xlsx      # Comprehensive Excel report with charts
├── devkit_mem.log            # DevKit memory tuner output
├── devkit_top_down.log       # DevKit top-down tuner output
├── ksys.log                  # ksys collection output
├── ub_watch.log              # ub_watch output
└── *_report.json             # ksys generated report
```

#### Excel Report Sheets

The `analysis_report.xlsx` contains multiple sheets:

- **Summary** - Test overview (host CPU/memory, hugepage, swap, VM stats)
- **NUMA_CPU** - Per-NUMA node CPU statistics
- **NUMA_Memory** - Per-NUMA node memory statistics
- **Hugepage_Per_NUMA** - Per-NUMA hugepage statistics
- **VM_Stats** - Per-VM statistics
- **DevKit_TopDown** - CPU top-down analysis (with pie chart)
- **TopDown_Timeline** - Top-down metrics over time (with line chart)
- **DevKit_Memory** - Cache miss and DDR bandwidth (with bar chart)
- **Memory_Timeline** - Memory metrics over time (with line chart)
- **NUMA_Bandwidth** - Per-NUMA bandwidth statistics
- **KSys** - Miss latency and IPC data
- **UBWatch_Latency/Bandwidth** - NUMA interconnect metrics
- **Raw_VM_Data** - VM time series data

### 7. Run Benchmark (Two-Phase Execution)

Browser mode uses a two-phase execution: **warmup phase** (all VMs) then **benchmark phase** (partial VMs).

#### Phase 1: Warmup Phase (`-wp`)

Warmup phase connects all VMs to execute warmup tasks (visiting warmup pages to load browser memory), then exits:

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
    --batch-size 20 --batch-interval 5 \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-url "http://192.168.110.10:8080/Galaxy.html" \
    --warmup-url "http://192.168.110.10:8080/Hubble_Space_Telescope.html" \
    --warmup-url "http://192.168.110.10:8080/Human.html" \
    --warmup-url "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html" \
    --warmup-url "http://192.168.110.10:8080/Solar_System.html" \
    --warmup-url "http://192.168.110.10:8080/United_States.html" \
    --warmup-url "http://192.168.110.10:8080/World_War_II.html" \
    --warmup-loops 1 \
    --warmup-delay 2
```

#### Phase 2: Benchmark Phase (`-bsp`)

Benchmark phase connects only a portion of VMs (controlled by `-bsp` parameter) to run browser tests:

```bash
# Connect 50% of VMs (50 VMs out of 100) for benchmark
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

#### Parameter Reference

| Parameter | Description |
| --------- | ----------- |
| `-wp` / `--warmup-phase` | Run warmup phase only (all VMs execute warmup tasks then exit) |
| `-bsp` / `--browser-stress-percent` | Percentage of VMs to connect in benchmark phase (default 100%) |
| `--warmup-url` | Warmup page URL (can be specified multiple times) |
| `--warmup-loops` | Warmup loop count (default 1) |
| `--warmup-delay` | Delay between warmup pages in seconds (default 2) |

**Note:** In browser mode without LLM (`--browser-mode` without `--browser-use-llm`), the benchmark adds 10 seconds to each request latency to simulate LLM response delay. This provides realistic timing comparable to actual agent workflows.

### 8. Delete VMs

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all  # Check if deletion is complete
```

---

## Automated Batch Testing

The automation system enables running multiple tests with different parameter combinations without manual intervention.

### Overview

The system consists of:

| File | Description |
| ---- | ----------- |
| `auto_vm_test.py` | Core automation script - executes single complete test flow |
| `batch_test_scheduler.py` | Batch scheduler - orchestrates multiple tests with parameter matrix |
| `test_config_template.yaml` | Configuration template with dynamic parameter placeholders |
| `batch_config.yaml` | Batch configuration - defines test parameter matrix |

### Test Flow

Each automated test executes the following steps:

1. **Delete existing VMs** → Confirm deletion via virsh
2. **Create new VMs** (n count) via create_server.py
3. **Start smap_tool** (memory migration tool)
4. **Wait for VMs ready** → SSH, openclaw gateway, CPU < 5%
5. **Warmup phase** → Browser warmup on all VMs
6. **Start monitoring** → qemu_monitor.py with stress-file sync
7. **Benchmark phase** → Browser testing on active VM percentage
8. **Collect results** → Wait for Excel report generation
9. **Cleanup** → Stop smap_tool, delete VMs

### Quick Start

#### Prerequisites

1. Manually create hugepages (e.g., 200GB)
2. Start warmup web server (see Section 4)
3. Configure `.env` file for log collection tools

#### Run Batch Tests

```bash
# Preview tasks without execution
python3 batch_test_scheduler.py --config batch_config.yaml --dry-run

# Execute batch tests
python3 batch_test_scheduler.py --config batch_config.yaml
```

### Configuration

#### Batch Configuration (`batch_config.yaml`)

Defines test parameter matrix:

```yaml
# Test Parameter Matrix - each combination generates one test task
test_matrix:
  vm_counts: [50, 100]           # Number of VMs to test
  ratios: [0.10, 0.15, 0.20]     # Memory borrow ratios (10%, 15%, 20%)
  active_percentages: [0.5, 0.8] # Active VM percentages for benchmark

# Fixed Parameters (applied to all tests)
fixed_params:
  start_ip: "192.168.110.11"     # Starting IP address
  swap_size_gb: 200              # Hugepage size in GB
  duration: 160                  # Test duration in seconds

# Scheduler Configuration
scheduler:
  continue_on_failure: true      # Continue after test failure

# Result Configuration
result:
  template_path: "test_config_template.yaml"
  base_dir: "results"
```

#### Configuration Template (`test_config_template.yaml`)

Contains all test parameters with dynamic placeholders:

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `{{VM_COUNT}}` | VM count | 100 |
| `{{START_IP}}` | Starting IP | "192.168.110.11" |
| `{{SWAP_SIZE_GB}}` | Hugepage size (GB) | 200 |
| `{{RATIO}}` | Memory borrow ratio | 0.15 |
| `{{ACTIVE_PERCENT}}` | Active VM percentage | 0.5 |
| `{{DURATION}}` | Test duration (seconds) | 160 |

### Result Organization

Each test creates a dedicated result directory:

```text
results/
├── batch_summary_20260602_143052.xlsx    # Batch summary report
├── batch_log_20260602_143052.txt         # Batch execution log
├── temp_configs/                         # Temporary config files
│   ├── config_vm50_ratio0.10_active0.5.yaml
│   └── ...
│
├── vm50_ratio0.10_active0.5_20260602_143052/  # Single test result
│   ├── config.yaml                       # Test configuration
│   ├── test_log.txt                      # Test execution log
│   │
│   ├── vm_bench_lite/                    # Benchmark outputs
│   │   ├── bench_report_xxx.txt          # Benchmark report
│   │   └── warmup_summary_xxx.txt        # Warmup summary
│   │
│   ├── qemu_monitor/                     # Monitoring outputs
│   │   ├── qemu_monitor.csv              # Raw monitoring data
│   │   ├── summary.csv                   # Summary statistics
│   │   ├── analysis_report.xlsx          # Excel report with charts
│   │   ├── devkit_mem.log                # DevKit memory log
│   │   ├── devkit_top_down.log           # DevKit top-down log
│   │   ├── ksys.log                      # ksys log
│   │   ├── ub_watch.log                  # ub_watch log
│   │   └── monitor_stdout.log            # Monitor stdout
│   │
│   └── summary/                          # Analysis summary
│       └── metrics_summary.json          # Key metrics JSON
│
└── ... (other test results)
```

### Monitor-Benchmark Synchronization

The system uses a lock file mechanism to ensure monitoring aligns with benchmark timing:

1. **Monitor starts** → Waits for `/tmp/vm_benchmark_running.lock`
2. **Benchmark starts** → Creates lock file → Monitor begins sampling
3. **Duration expires** → Monitor stops naturally → Generates Excel report
4. **Cleanup** → Lock file removed

This ensures:
- No idle sampling before benchmark
- Exact time alignment between monitoring and benchmark
- Complete Excel report generation

### Advanced Usage

#### Single Test Execution

Run a single test with specific configuration:

```bash
python3 auto_vm_test.py --config test_config.yaml
```

#### Custom Parameter Matrix

Modify `batch_config.yaml` to define custom test scenarios:

```yaml
test_matrix:
  vm_counts: [10, 20, 50, 100]
  ratios: [0.05, 0.10, 0.15, 0.20, 0.25]
  active_percentages: [0.3, 0.5, 0.7, 1.0]
```

This generates 4 × 5 × 4 = 80 test combinations.

#### Modify Test Template

Edit `test_config_template.yaml` to adjust:
- Warmup URLs and parameters
- Benchmark batch sizes and intervals
- Monitoring NUMA nodes and interval
- Wait timeouts and thresholds
