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

## Quick Start

### 1. Terminal Setup

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. Configure Host Network Bridge

```bash
# Find bridge interface
ip a | grep brq

# Add IP to bridge
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

### 3. Download Warmup Pages

```bash
bash download_page.sh
```

### 4. Start Warmup Web Server

```bash
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

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

### 7. Run Benchmark

#### Warmup Phase (all VMs)

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
    --batch-size 20 --batch-interval 5 \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-loops 1 --warmup-delay 2
```

#### Benchmark Phase (partial VMs)

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

### 8. Delete VMs

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all
```

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

### Browser Workflow (5 steps = 1 query)

```text
Step 1: openclaw browser open [URL] --label [NAME]
Step 2: openclaw browser focus [TAB_ID]
Step 3: openclaw browser snapshot --limit 200
Step 4: openclaw browser click e218
Step 5: openclaw browser screenshot
```

### Files

| File | Description |
|------|-------------|
| `docker_bench/bench.py` | Main entry point |
| `docker_bench/container_manager.py` | Container lifecycle management |
| `docker_bench/task_runner.py` | Browser task execution |
| `docker_bench/stats_collector.py` | Statistics collection and reporting |
| `config/docker_bench.yaml` | Configuration template |

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