# Multi-VM Agent Automation Test System Design Document

**中文版**: [2026-06-02-multi-vm-automation-design.md](2026-06-02-multi-vm-automation-design.md)

**Date**: 2026-06-02
**Author**: Jack
**Status**: Design Phase

---

## 1. Overview

### 1.1 Objective

Build a complete automation test system that implements the full workflow from VM creation, memory migration, warmup, testing to monitoring. Support batch testing with multiple parameter combinations and generate comparison reports.

### 1.2 Prerequisites

- Manually create hugepage memory (e.g., 200GB)
- OpenStack environment configured (~/.admin-openrc)
- Existing tools: create_server.py, vm_bench_lite.py, qemu_monitor.py, smap_tool
- Configure network bridge: `ip addr add 192.168.110.10/24 dev brqxxx`
- Start warmup web server: `cd web_content/en.wikipedia.org/wiki && python3 -m http.server 8080`

### 1.3 Test Flow Overview

```
Delete old VMs → Confirm deletion → Create new VMs (n count) → Start smap_tool → Wait for ready → Warmup → Test + Monitor → Collect results → Cleanup
```

---

## 2. System Architecture

### 2.1 Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Batch Scheduler Layer                     │
│  batch_test_scheduler.py                                        │
│  - Define test parameter matrix (VM count, ratio, active %)     │
│  - Loop call core test script                                   │
│  - Manage test queue and progress                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Core Test Layer                           │
│  auto_vm_test.py                                                │
│  - Execute single complete test flow                            │
│  - Delete old VMs → Create new VMs → Start smap_tool            │
│    → Wait for ready → Warmup → Test → Monitor → Cleanup         │
│  - Accept config file path as parameter                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Tool Execution Layer                      │
│  - create_server.py (Create VMs)                                │
│  - smap_tool (Memory migration)                                 │
│  - vm_bench_lite.py (Warmup + Test)                             │
│  - qemu_monitor.py (Monitor)                                    │
│  - openstack CLI / virsh (Delete VMs)                           │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Component Responsibilities

| Component | Responsibility | Input | Output |
|-----------|----------------|-------|--------|
| `batch_test_scheduler.py` | Batch scheduling management | Test parameter matrix config | Test result summary report |
| `auto_vm_test.py` | Single complete test execution | Config file path | Test result directory |
| `test_config_template.yaml` | Config template | - | - |
| Temporary config file | Specific test parameters | Template + dynamic values | Saved to result directory |

---

## 3. Configuration File Design

### 3.1 Config Template Structure

File: `test_config_template.yaml`

```yaml
# ========================================
# Test Base Configuration
# ========================================

# OpenStack Configuration
openstack:
  openrc_path: "~/.admin-openrc"
  network_id: "cc56708a-c0c0-4d75-a87e-ed1b1a8af844"
  flavor: "2U_4G_30G_4K"
  image: "ubuntu-24.04"
  az: "nova_zone:controller"
  subnet_prefix: "192.168.110."

# VM Configuration (Dynamic Parameters)
vm:
  count: "{{VM_COUNT}}"
  start_ip: "{{START_IP}}"
  username: "root"
  password: "openEuler12#$"

# Memory Migration Tool Configuration
smap_tool:
  path: "/home/l30038718/vm/smap_tool"
  swap_size_gb: "{{SWAP_SIZE_GB}}"      # Hugepage size (GB), convert to MB: swap_size_gb * 1024
  ratio: "{{RATIO}}"                    # Borrow ratio, e.g., 0.15

# Test Configuration
test:
  duration: 160                         # Test duration (seconds)
  active_percent: "{{ACTIVE_PERCENT}}"  # Active VM percentage, e.g., 0.5
  batch_size: 10                        # Batch size
  batch_interval: 5                     # Batch interval (seconds)
  browser_interval_min: 5               # Browser task min interval (seconds)
  browser_interval_max: 15              # Browser task max interval (seconds)
  browser_url: "http://192.168.110.10:8080/Weibo.html"

# Warmup Configuration
warmup:
  urls:
    - "http://192.168.110.10:8080/China.html"
    - "http://192.168.110.10:8080/Earth.html"
    - "http://192.168.110.10:8080/Galaxy.html"
    - "http://192.168.110.10:8080/Hubble_Space_Telescope.html"
    - "http://192.168.110.10:8080/Human.html"
    - "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html"
    - "http://192.168.110.10:8080/Solar_System.html"
    - "http://192.168.110.10:8080/United_States.html"
    - "http://192.168.110.10:8080/World_War_II.html"
  loops: 1                              # Warmup loop count
  delay: 2                              # Page delay (seconds)
  batch_size: 20                        # Warmup batch size
  batch_interval: 5                     # Warmup batch interval (seconds)

# Monitor Configuration
monitor:
  interval: 2                           # Monitor sampling interval (seconds)
  numa_nodes: [0, 1]                    # NUMA nodes list
  enable_capture: true                  # Enable log collection (devkit/ksys/ub_watch)
  # Note: devkit/ksys/ub_watch paths use existing .env file config

# Wait Configuration
wait:
  ssh_timeout: 300                      # SSH connection timeout (seconds)
  service_timeout: 300                  # Service startup timeout (seconds)
  cpu_threshold: 5                      # CPU utilization threshold (%)
  check_interval: 10                    # Check interval (seconds)

# Result Configuration
result:
  base_dir: "results"
```

### 3.2 Dynamic Parameter Description

| Parameter | Description | Example Value |
|-----------|-------------|---------------|
| `{{VM_COUNT}}` | VM count | 100 |
| `{{START_IP}}` | Starting IP | "192.168.110.11" |
| `{{SWAP_SIZE_GB}}` | Hugepage size (GB) | 200 |
| `{{RATIO}}` | Borrow ratio | 0.15 |
| `{{ACTIVE_PERCENT}}` | Active VM percentage | 0.5 |

---

## 4. Core Automation Test Script Design

### 4.1 Script Entry

File: `auto_vm_test.py`

```bash
python auto_vm_test.py --config test_config.yaml
```

### 4.2 Execution Flow Details

```
Step 1: Initialization
  ├─ Parse config file
  ├─ Create result directory: results/vm{n}_ratio{ratio}_active{percent}_timestamp/
  ├─ Save config file copy to result directory
  ├─ Initialize logging system
  └─ Setup OpenStack environment variables
      source ~/.admin-openrc
      unset http_proxy
      unset https_proxy

Step 2: Cleanup Environment (Delete existing VMs)
  ├─ Execute delete command
  │   openstack server list -c ID -f value | xargs openstack server delete --force
  ├─ Wait for deletion completion (poll check)
  │   while True:
  │     virsh list --all
  │     if no running VMs:
  │       break
  │     sleep 10
  ├─ Confirm deletion complete
  │   openstack server list -c ID -f value  # Should return empty
  │   virsh list --all                      # Should show shut off or empty
  └─ Record deletion time

Step 3: Create VMs
  ├─ Call create_server.py to create specified number of VMs
  │   python3 create_server.py \
  │     --start_ip {start_ip} \
  │     --n {count} \
  │     --subnet-prefix {subnet_prefix} \
  │     --network-id {network_id} \
  │     --az {az} \
  │     --flavor {flavor} \
  │     --image {image}
  ├─ Check creation result, record success/failed VMs
  ├─ Output creation statistics
  └─ Failure handling: creation failure exceeds threshold → Abort test

Step 4: Start Memory Migration Tool
  ├─ Clean old smap config
  │   rm -rf /dev/shm/smap_config
  ├─ Get all qemu-kvm process PIDs
  │   pidof qemu-kvm
  ├─ Calculate swap_size_mb = swap_size_gb * 1024
  ├─ Calculate ratio_percent = int(ratio * 100)  # e.g., 0.15 → 15
  ├─ Start smap_tool
  │   cd /home/l30038718/vm
  │   ./smap_tool {vm_count} `pidof qemu-kvm` --swap-size {swap_size_mb} --ratio {ratio_percent}
  │   # Example: ./smap_tool 100 `pidof qemu-kvm` --swap-size 204800 --ratio 10
  ├─ Record smap_tool process PID (need to kill after test)
  ├─ Verify smap_tool started successfully (check if process exists)
  └─ Failure handling: startup failed → Abort test

Step 5: Wait for VMs Ready
  ├─ Loop check each VM (parallel check):
  │   ├─ SSH connection successful
  │   │   ssh -o ConnectTimeout=10 root@{ip} "echo connected"
  │   ├─ openclaw gateway service running (SSH to VM internal check)
  │   │   # Check process
  │   │   pgrep -f openclaw
  │   │   # Check port 18789 listening
  │   │   ss -tln | grep 18789
  │   ├─ qemu-kvm process CPU utilization < {cpu_threshold}% (check on host)
  │   │   top -b -n 1 -p {pid} | grep qemu-kvm
  │   │   # Or use psutil to get CPU utilization
  │   └─ All conditions satisfied then mark VM ready
  ├─ Record wait time and ready VM list
  ├─ Timeout handling: record unready VMs and skip
  └─ Statistics ready VM count, output ready status

Step 6: Browser Mode Warmup
  ├─ Build warmup command (refer to README)
  │   python vm_bench_lite.py -n {count} --start-ip {start_ip} --browser-mode \
  │     -wp \
  │     --batch-size {warmup.batch_size} --batch-interval {warmup.batch_interval} \
  │     --warmup-url "{url1}" \
  │     --warmup-url "{url2}" \
  │     ... (all warmup urls) \
  │     --warmup-loops {loops} \
  │     --warmup-delay {delay}
  ├─ Execute warmup command
  ├─ Wait for warmup completion
  ├─ Collect warmup results
  └─ Record warmup time and success/failed VM count

Step 7: Start Monitor (background run, waiting for benchmark)
  ├─ Build monitor command (use --stress-file sync)
  │   python qemu_monitor.py -t {duration} -i {interval} \
  │     --enable-capture \
  │     --log-dir {result_dir}/qemu_monitor \
  │     --numa {numa_nodes} \
  │     --stress-file /tmp/vm_benchmark_running.lock
  ├─ Background start monitor process
  │   subprocess.Popen(monitor_cmd)
  ├─ Record monitor process PID
  └─ Monitor waits for lock file, not sampling yet

Step 8: Browser Mode Test
  ├─ Create lock file to signal monitor to start sampling
  │   touch /tmp/vm_benchmark_running.lock
  ├─ Build test command (refer to README)
  │   # Calculate active VM count: active_count = int(count * active_percent)
  │   python vm_bench_lite.py -n {count} --start-ip {start_ip} --browser-mode \
  │     -bsp {active_percent} \
  │     --batch-size {batch_size} --batch-interval {batch_interval} \
  │     --browser-url "{browser_url}" \
  │     --browser-interval-min {browser_interval_min} \
  │     --browser-interval-max {browser_interval_max} \
  │     -t {duration}
  ├─ Execute test command
  ├─ Wait for test completion (duration seconds)
  ├─ Remove lock file
  │   rm /tmp/vm_benchmark_running.lock
  ├─ Collect test report
  └─ Record test statistics (success rate, latency, etc.)

Step 9: Wait for Monitor Natural Completion and Collect Results
  ├─ Wait for monitor process natural end (after duration seconds)
  │   # Monitor generates Excel report on natural completion
  ├─ Verify monitor log file completeness
  │   - qemu_monitor.csv
  │   - devkit_mem.log
  │   - devkit_top_down.log
  │   - ksys.log
  │   - ub_watch.log
  │   - analysis_report.xlsx
  ├─ Move vm_bench_lite report to result directory
  │   - bench_report_xxx.txt → {result_dir}/vm_bench_lite/
  │   - warmup_summary_xxx.txt → {result_dir}/vm_bench_lite/
  ├─ Parse monitor log extract key metrics
  └─ Generate comprehensive test report

Step 10: Cleanup Environment
  ├─ Stop smap_tool process (use PID recorded in Step 4)
  │   kill {smap_tool_pid}
  │   # Or pkill -f smap_tool
  ├─ Delete test VMs
  │   openstack server list -c ID -f value | xargs openstack server delete --force
  ├─ Confirm deletion complete
  │   virsh list --all  # Check if still have running VMs
  │   openstack server list -c ID -f value  # Should return empty
  └─ Output test completion info

End: Return test result path
```

### 4.3 Key Command Reference (from README.md)

#### 4.3.1 Terminal Setup
```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

#### 4.3.2 Delete VMs and Confirm
```bash
# Delete all VMs
openstack server list -c ID -f value | xargs openstack server delete --force

# Confirm deletion complete
virsh list --all  # Check if still have running VMs
openstack server list -c ID -f value  # Should return empty
```

#### 4.3.3 Create VMs
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

#### 4.3.4 Start smap_tool
```bash
cd /home/l30038718/vm
rm -rf /dev/shm/smap_config
# Parameter description:
# - First parameter: VM count
# - pid_list: Obtained via `pidof qemu-kvm`
# --swap-size: Hugepage size (MB) = Hugepage GB × 1024
# --ratio: Borrow ratio (integer percentage, e.g., 10 means 10%)
./smap_tool 100 `pidof qemu-kvm` --swap-size 204800 --ratio 10
# Record smap_tool PID, need to kill after test
```

#### 4.3.5 Warmup Phase
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

#### 4.3.6 Test Phase
```bash
# Connect 50% of VMs for testing
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

#### 4.3.7 Monitor
```bash
# Basic monitoring
python3 qemu_monitor.py -t 300 -i 2

# With log collection
python3 qemu_monitor.py -t 300 -i 2 --enable-capture --log-dir /data/test_run_1

# Specify NUMA nodes
python3 qemu_monitor.py -t 300 --enable-capture --numa 0,1
```

### 4.4 Error Handling Strategy

| Step | Failure Handling | Retry Strategy |
|------|------------------|----------------|
| Step 2 (Delete VMs) | Force terminate residual VMs after timeout | virsh destroy |
| Step 3 (Create VMs) | Creation failure exceeds 30% → Abort test | No retry, record failed VMs |
| Step 4 (smap_tool) | Startup failed → Abort test | Retry once |
| Step 5 (Wait ready) | Timeout → Continue test, skip unready VMs | SSH reconnect 3 times |
| Step 7 (Monitor) | Startup failed → Log warning, continue test | No retry |
| Step 8 (Test) | Exception → Log exception, continue collecting data | No retry |

---

## 5. Batch Scheduler Script Design

### 5.1 Script Entry

File: `batch_test_scheduler.py`

```bash
python batch_test_scheduler.py --config batch_config.yaml
```

### 5.2 Execution Flow

```
Step 1: Define Test Parameter Matrix
  ├─ VM count list: [50, 100, 150]
  ├─ Ratio list: [0.10, 0.15, 0.20]
  ├─ Active percentage list: [0.5, 0.8, 1.0]
  └─ Calculate total test count: len(vm_counts) × len(ratios) × len(active_percentages)

Step 2: Generate Test Task List
  ├─ Traverse all parameter combinations
  │   for vm_count in vm_counts:
  │     for ratio in ratios:
  │       for active_percent in active_percentages:
  │         task = {
  │           'vm_count': vm_count,
  │           'ratio': ratio,
  │           'active_percent': active_percent,
  │           'task_id': f"vm{vm_count}_ratio{ratio}_active{active_percent}"
  │         }
  │         tasks.append(task)
  └─ Output task list summary

Step 3: Loop Execute Tests
  ├─ for i, task in enumerate(tasks):
  │   ├─ Print current task info
  │   │   print(f"[{i+1}/{len(tasks)}] Starting: {task['task_id']}")
  │   │
  │   ├─ Generate temp config file from template
  │   │   config_file = generate_config(
  │   │     template="test_config_template.yaml",
  │   │     output=f"temp_configs/config_{task['task_id']}.yaml",
  │   │     params=task
  │   │   )
  │   │
  │   ├─ Call core test script
  │   │   result = subprocess.run([
  │   │     "python", "auto_vm_test.py",
  │   │     "--config", config_file
  │   │   ], capture_output=True)
  │   │
  │   ├─ Wait for test completion
  │   │   Monitor process status, capture output and errors
  │   │
  │   ├─ Collect results
  │   │   - Save temp config file to result directory
  │   │   - Record test status (success/failure)
  │   │   - Record result path
  │   │
  │   ├─ Error handling
  │   │   if test failed and continue_on_failure:
  │   │     Record failure reason, continue next round
  │   │   elif test failed and not continue_on_failure:
  │   │     Abort batch test
  │   │
  │   ├─ Cleanup temp config file
  │   │   os.remove(config_file)  # Already moved to result directory
  │   │
  │   └─ Print progress
  │     print(f"Completed: {result_dir}")
  │
  └─ All tasks completed

Step 4: Generate Summary Report
  ├─ Traverse all result directories
  ├─ Parse key metrics from each test:
  │   - Browser task success rate, average latency, P99 latency
  │   - QEMU CPU utilization average
  │   - Memory bandwidth, IPC, etc.
  ├─ Generate comparison table (Excel format):
  │   | VM Count | Ratio | Active % | Success Rate | Avg Latency | P99 Latency | CPU % | Memory BW |
  │   |----------|-------|----------|--------------|-------------|-------------|-------|-----------|
  │   | 50       | 0.10  | 0.8      | 98%          | 1.2s        | 3.5s        | 15%   | 2.5GB/s   |
  ├─ Save summary report: results/batch_summary_timestamp.xlsx
  └─ Output summary info

End: All tests completed, output summary report path
```

### 5.3 Batch Config File

File: `batch_config.yaml`

```yaml
# Test Parameter Matrix
test_matrix:
  vm_counts: [50, 100, 150]
  ratios: [0.10, 0.15, 0.20]
  active_percentages: [0.5, 0.8, 1.0]

# Fixed Parameters
fixed_params:
  start_ip: "192.168.110.11"
  swap_size_gb: 200
  duration: 160

# Scheduler Configuration
scheduler:
  continue_on_failure: true    # Continue next round after failure
  cleanup_between_tests: true  # Cleanup VMs between tests

# Result Configuration
result:
  template_path: "test_config_template.yaml"
  base_dir: "results"
```

---

## 6. Result Organization Structure

### 6.1 Directory Structure

```
results/
├── batch_summary_20260602_143052.xlsx          # Batch test summary report
├── batch_log_20260602_143052.txt               # Batch scheduler log
│
├── vm50_ratio0.10_active0.5_20260602_143052/   # Single test result directory
│   ├── config.yaml                             # Config file copy for this test
│   ├── test_log.txt                            # Test execution log
│   │
│   ├── vm_bench_lite/                          # vm_bench_lite output
│   │   ├── bench_report_20260602_143520.txt    # Test report
│   │   └── warmup_summary_20260602_143450.txt  # Warmup summary
│   │
│   ├── qemu_monitor/                           # qemu_monitor output
│   │   ├── qemu_monitor.csv                    # QEMU monitor data
│   │   ├── summary.csv                         # Summary statistics
│   │   ├── analysis_report.xlsx                # Analysis report (Excel)
│   │   ├── devkit_mem.log                      # devkit memory log
│   │   ├── devkit_top_down.log                 # devkit top-down log
│   │   ├── ksys.log                            # ksys log
│   │   ├── ub_watch.log                        # ub_watch log
│   │   └── *_report.json                       # ksys report
│   │
│   └───── summary/                             # Comprehensive analysis summary
│       ├── test_summary.txt                    # Test summary
│       ├── metrics_summary.json                # Key metrics JSON
│       └── comparison_chart.png                # Comparison chart (optional)
│
├── vm50_ratio0.10_active0.8_20260602_150123/
│   └── ... (same structure)
│
└── ... (other test results)
```

### 6.2 Key Metrics Summary Format

File: `metrics_summary.json`

```json
{
  "test_id": "vm50_ratio0.10_active0.5",
  "test_time": "2026-06-02 14:30:52",
  "parameters": {
    "vm_count": 50,
    "ratio": 0.10,
    "active_percent": 0.5,
    "active_vm_count": 25,
    "duration": 160
  },
  "browser_metrics": {
    "total_tasks": 500,
    "success_count": 495,
    "success_rate": 99.0,
    "avg_latency": 1.25,
    "p99_latency": 3.5
  },
  "qemu_metrics": {
    "avg_cpu_percent": 15.2,
    "max_cpu_percent": 45.0,
    "avg_memory_mb": 2048,
    "max_memory_mb": 2560
  },
  "performance_metrics": {
    "ipc_avg": 0.85,
    "l3_miss_latency_avg": 120,
    "ddr_bandwidth_read_avg": 2.5,
    "ddr_bandwidth_write_avg": 1.2
  }
}
```

### 6.3 Batch Summary Report Format

File: `batch_summary_xxx.xlsx` contains the following columns:

| Column Name | Description |
|-------------|-------------|
| test_id | Test identifier |
| vm_count | Total VM count |
| ratio | Borrow ratio |
| active_percent | Active VM percentage |
| active_vm_count | Actual active VM count |
| success_rate | Browser task success rate |
| avg_latency | Average latency |
| p99_latency | P99 latency |
| avg_cpu | Average CPU utilization |
| max_cpu | Maximum CPU utilization |
| ipc | IPC average |
| ddr_read | DDR read bandwidth |
| ddr_write | DDR write bandwidth |

---

## 7. Implementation Plan

### 7.1 Development Order

1. **Config File Template** (`test_config_template.yaml`)
   - Define complete config structure
   - Mark dynamic parameter positions

2. **Core Automation Script** (`auto_vm_test.py`)
   - Implement single test complete flow
   - Error handling and logging
   - Result collection and summary

3. **Batch Scheduler Script** (`batch_test_scheduler.py`)
   - Parameter matrix generation
   - Config file dynamic generation
   - Batch execution and summary report

4. **Auxiliary Tool Optimization**
   - Optimize existing tool parameter interfaces
   - Ensure tools coordinate properly

### 7.2 Test Verification

1. Single test flow verification (small scale: n=10)
2. Batch scheduler verification (small scale parameter combination: 3 tests)
3. Complete test verification (full parameter matrix)

---

## 8. Risks and Constraints

### 8.1 Technical Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| VM creation failure | Test interrupted | Set failure threshold (30%), abort if exceeded |
| smap_tool startup failure | Memory migration ineffective | Verify startup success, abort if failed |
| Network unstable | SSH connection dropped | Reconnect mechanism (3 times), OpenStack status check |
| Delete VM stuck | Test cannot start | virsh destroy force terminate |
| Monitor tool startup failure | Data missing | Log warning, continue test |

### 8.2 Constraints

- Prerequisite depends on manual hugepage creation
- Stable OpenStack environment required during test
- Monitor tools (devkit/ksys) need correct path configuration
- Warmup web server needs to be started beforehand
- Network bridge needs IP address configuration