# vm_bench Module Usage Guide

> **Note**: This document describes the modularized `vm_bench` package. The original `create_server.py` and `vm_bench_lite.py` scripts have been refactored into this unified module.

## Overview

The `vm_bench` module provides a unified, modular interface for:
- **Phase 0**: OpenStack VM creation with batch control
- **Phase 1**: SSH connection to VMs
- **Phase 2**: Browser/QA/Stress task execution with warmup support
- **Phase 3**: Real-time statistics collection and report generation

## Quick Start

### Module Import

```python
from vm_bench import Config, VMManager, run_benchmark
from vm_bench.schemas import VMStatus, VMState
```

### CLI Entry Point

```bash
python -m vm_bench --config config/vm_bench.yaml
```

---

## Configuration

### YAML Configuration File

The recommended way to use `vm_bench` is via YAML configuration:

```yaml
# config/vm_bench.yaml

# OpenStack environment
openstack:
  auth_source: "~/.admin-openrc"

# VM creation (Phase 0)
vm_create:
  flavor: "2U_4G_40G"
  image: "ubuntu-24.04"
  network_id: "2661422b-37c4-4d84-90ce-521167c676c0"
  availability_zone: "nova_zone:controller"
  start_ip: "192.168.110.11"
  subnet_prefix: "192.168.110."
  vm_prefix: "test_openclaw"
  total_count: 80
  create_timeout: 1200
  create_only: false
  detect_existing: false

# Batch control
create_batch:
  size: 20
  interval: 3

task_batch:
  size: 10
  interval: 5

# Task mode
task:
  mode: "browser"
  duration: 600

# Browser configuration
browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  timeout: 200
  interval_min: 5
  interval_max: 10
  benchmark_percent: 1.0
  warmup_urls:
    - "http://192.168.110.10:8080/page1.html"
    - "http://192.168.110.10:8080/page2.html"
  warmup_loops: 1
  warmup_delay: 3

# SSH configuration
ssh:
  port: 22
  username: "root"
  password: "openEuler12#$"
```

### Configuration Priority

```
CLI arguments > YAML config > dataclass defaults
```

Example of CLI override:

```bash
# YAML has total_count=80, CLI overrides to 10
python -m vm_bench --config config/vm_bench.yaml -n 10 -t 300
```

---

## Usage Modes

### 1. Create VMs Only (Phase 0)

Create VMs via OpenStack and exit without benchmark:

```bash
# Using YAML config
python -m vm_bench --config config/vm_bench.yaml --create-only

# Pure CLI mode
python -m vm_bench --create-only \
    -n 20 \
    --start-ip 192.168.110.11 \
    --flavor 2U_4G_40G \
    --image ubuntu-24.04 \
    --network-id <network-id>
```

Output includes creation timing report:
```
[Creation Performance]
  Min:  15.2s
  Max:  45.8s
  Avg:  28.5s
  P50:  26.3s
  P95:  42.1s
  P99:  45.2s
```

### 2. Detect Existing VMs

Connect to existing VMs without creation:

```bash
python -m vm_bench --detect \
    -n 20 \
    --start-ip 192.168.110.11 \
    -t 300
```

Use case: VMs already running from previous `--create-only` session.

### 3. Warmup Only

Execute warmup phase only:

```bash
python -m vm_bench --warmup-only \
    --config config/vm_bench.yaml \
    -n 50
```

Warmup brings QEMU process memory to target value before benchmark.

### 4. Detect + Warmup

Connect existing VMs and run warmup:

```bash
python -m vm_bench --detect --warmup-only \
    -n 50 \
    --start-ip 192.168.110.11
```

### 5. Full Benchmark

Create VMs, connect, warmup, and benchmark:

```bash
python -m vm_bench --config config/vm_bench.yaml
```

### 6. Detect + Benchmark

Skip creation, connect existing VMs and benchmark:

```bash
python -m vm_bench --detect \
    --config config/vm_bench.yaml \
    -bsp 0.5 \
    -t 300
```

---

## CLI Arguments Reference

### VM Creation Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--config` | YAML config file path | None |
| `-n, --total` | Total VM count | 80 |
| `--start-ip` | Starting IP address | 192.168.110.11 |
| `--flavor` | OpenStack flavor | 2U_4G_40G |
| `--image` | OpenStack image | ubuntu-24.04 |
| `--network-id` | OpenStack network ID | (from YAML) |
| `--az` | Availability zone | nova_zone:controller |
| `--create-timeout` | VM creation timeout | 1200 |
| `--create-only` | Create VMs only, no benchmark | false |
| `--detect` | Detect existing VMs | false |

### Batch Control Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--create-batch-size` | VMs per creation batch | 20 |
| `--create-batch-interval` | Creation batch interval (s) | 3 |
| `--task-batch-size` | Tasks per batch | 10 |
| `--task-batch-interval` | Task batch interval (s) | 5 |

### SSH Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--ssh-port` | SSH port | 22 |
| `--ssh-username` | SSH username | root |
| `--ssh-password` | SSH password | openEuler12#$ |

### Browser Task Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--browser-url` | Browser URL (repeatable) | Weibo.html |
| `--browser-timeout` | Browser task timeout (s) | 200 |
| `--browser-interval-min` | Task interval min (s) | 5 |
| `--browser-interval-max` | Task interval max (s) | 10 |
| `--browser-use-llm` | Use LLM for browser | false |
| `--benchmark-percent` | VMs percentage for benchmark | 1.0 |

### Warmup Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--warmup-url` | Warmup URL (repeatable) | (from YAML) |
| `--warmup-loops` | Warmup loop count | 1 |
| `--warmup-delay` | Warmup page delay (s) | 3 |
| `--warmup-only` | Run warmup only | false |

### Task Mode Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--task-mode` | Task mode: browser/qa/stress/mixed | browser |
| `-t, --duration` | Benchmark duration (s) | 600 |

### QA Task Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--qa-timeout` | QA query timeout (s) | 600 |
| `--qa-interval` | QA interval (s) | 0.5 |
| `--qa-mode` | QA mode: cli/http | cli |

### Stress Task Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--stress-percent` | Stress VM percentage | 0.5 |
| `--stress-memory` | Stress memory MB | 2048 |
| `--no-keepalive` | Disable stress keepalive | false |

### Report Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--output-dir` | Report output directory | results/vm |
| `--filename-prefix` | Report filename prefix | vm_bench |
| `--stats-interval` | Stats snapshot interval (s) | 10 |

### Cleanup Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--delete-after-test` | Delete VMs after test | false |

---

## Python API

### Using Config

```python
from vm_bench import Config

# From YAML file
config = Config.load_from_yaml('config/vm_bench.yaml')

# Override with CLI arguments
from vm_bench.bench import build_arg_parser
parser = build_arg_parser()
args = parser.parse_args(['-n', '10', '-t', '300'])
config = Config.merge_with_args(config, args)

# Pure CLI mode
args = parser.parse_args(['-n', '20', '--start-ip', '192.168.110.50'])
config = Config.from_args(args)
```

### Using VMManager

```python
import threading
from vm_bench import Config, VMManager
from vm_bench.schemas import VMStatus

config = Config(
    total_count=20,
    start_ip="192.168.110.11",
    network_id="...",
    flavor="2U_4G_40G",
    create_only=True  # Only create, don't benchmark
)

stop_event = threading.Event()
manager = VMManager(config, stop_event)

# Create VMs
vm_states = manager.create_all()

# Check creation status
ready_count = sum(
    1 for s in vm_states.values()
    if s.creation_metrics.status == VMStatus.ACTIVE
)

print(f"Created: {ready_count}/{config.total_count}")
```

### Using run_benchmark

```python
from vm_bench import Config, run_benchmark

config = Config(
    start_ip="192.168.110.11",
    total_count=10,
    task_mode="browser",
    test_duration=60,
    detect_existing=True,  # Connect to existing VMs
    benchmark_percent=0.5,  # 50% VMs for benchmark
)

result = run_benchmark(config)
print(result['report'])
```

---

## Typical Workflow

### Two-Phase Browser Testing

```bash
# Phase 0: Create VMs
python -m vm_bench --create-only -n 100 --config config/vm_bench.yaml

# Phase 1a: Warmup (all 100 VMs)
python -m vm_bench --detect --warmup-only -n 100 --config config/vm_bench.yaml

# Phase 1b: Benchmark (50% VMs, 50 VMs)
python -m vm_bench --detect -bsp 0.5 -t 300 --config config/vm_bench.yaml
```

### Integration with auto_vm_test.py

The `auto_vm_test.py` script now uses the `vm_bench` module internally:

```bash
python auto_vm_test.py --config config/test_config.yaml
```

Flow:
1. Delete existing VMs
2. Create VMs (via `vm_bench.create_all()`)
3. Start smap_tool
4. Wait for ready
5. Warmup phase (via `vm_bench.run_benchmark(warmup_only=True)`)
6. Start monitoring
7. Benchmark phase (via `vm_bench.run_benchmark()`)
8. Collect results
9. Cleanup

---

## Report Output

### Report Structure

```
results/vm/
├── vm_bench_20240601_143052.txt
```

### Report Content

```
================================================================================
VM Bench - Performance Report
================================================================================

[Test Configuration]
  Total VMs:      80
  Task Mode:      browser
  Test Duration:  600s

[VM Status]
  Created (ACTIVE):  80
  Connected:         78
  Offline:           2

[VM Creation Performance]
  Min:  15.2s
  Max:  45.8s
  Avg:  28.5s
  P50:  26.3s
  P95:  42.1s
  P99:  45.2s

[SSH Connection Performance]
  Min:  1.2s
  Max:  5.8s
  Avg:  2.5s

[Browser Task Statistics]
  Total Tasks:   500
  Success:       485
  Failed:        15 (timeout: 5)
  Success Rate:  97.0%
  Avg Latency:   8500.0ms
  P99 Latency:   12000.0ms
================================================================================
```

---

## Default Parameters Summary

| Parameter | Default Value |
|-----------|---------------|
| `create_batch_size` | 20 |
| `create_batch_interval` | 3 |
| `task_batch_size` | 10 |
| `task_batch_interval` | 5 |
| `browser_interval_min` | 5 |
| `browser_interval_max` | 10 |
| `warmup_loops` | 1 |
| `warmup_delay` | 3 |

---

## Comparison with Original Scripts

| Original Script | vm_bench Module |
|-----------------|------------------|
| `create_server.py` | `VMManager.create_all()` |
| `vm_bench_lite.py --browser-mode -wp` | `run_benchmark(warmup_only=True)` |
| `vm_bench_lite.py --browser-mode -bsp 0.5` | `run_benchmark(benchmark_percent=0.5)` |
| CLI arguments only | YAML config + CLI override |

---

## Troubleshooting

### VM Creation Fails

```bash
# Check OpenStack environment
source ~/.admin-openrc
openstack server list

# Check network_id
openstack network show <network-id>
```

### SSH Connection Fails

```bash
# Check VM status
openstack server show <vm-uuid> -c status

# Manual SSH test
ssh root@192.168.110.11
```

### Import Error

```bash
# Install dependencies
pip install paramiko pyyaml

# Verify import
python -c "from vm_bench import Config, VMManager; print('OK')"
```

---

## See Also

- [Configuration Reference](config/vm_bench.yaml)
- [Test Suite](vm_bench/tests/)
- [Original Usage Guide](usage-guide.md) (for legacy scripts)