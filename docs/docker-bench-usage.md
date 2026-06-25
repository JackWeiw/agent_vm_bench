# Docker Container Bench - Usage Guide (English)

Docker container browser automation performance testing tool for validating concurrent performance and stability of browser automation in Docker containerized deployment environments. Uses agent-browser CLI to implement a 4-step browser workflow.

## Features

- **Batch Container Creation** - Supports batched or full concurrent creation with CPU/Memory resource limits
- **Port Check** - Automatically checks 18789 and 11436 ports readiness
- **Complete Browser Workflow** - Executes 4-step browser workflow (open → snapshot → click → screenshot)
- **Element Reference Reuse** - Successfully clicked @eN references are saved for reuse, improving efficiency
- **QPS Statistics** - Uses queries per second (QPS) as the core performance metric
- **Real-time Statistics** - Real-time display of creation time, port wait time, task latency
- **Performance Report** - Generates detailed performance reports (P50/P95/P99 latency)
- **Three Run Modes** - Full workflow, create-only, detect existing
- **Proxy Environment Cleanup** - Automatically clears proxy environment variables to ensure browser works properly

## Test Scenario

Validates browser automation concurrent performance in containerized deployment:
- Based on `ubuntu-openclaw-chromium` image
- Batch creation of 2vCPU/2G container instances on host
- Each container runs Chromium browser independently
- Executes complete web page operation workflow via agent-browser CLI
- Uses overall QPS to evaluate system throughput capability

## Architecture

```text
docker_bench/
├── __init__.py           # Package initialization
├── __main__.py           # Module entry point
├── bench.py              # Main entry - test flow control
├── config.py             # Configuration management
├── container_manager.py  # Container lifecycle (create, port check, cleanup)
├── task_runner.py        # Browser task execution (4-step workflow)
├── stats_collector.py    # Statistics collection and report generation
├── schemas.py            # Data structure definitions
├── utils.py              # Utility functions
├── delete_containers.sh  # Batch delete containers script
└── requirements.txt      # Dependencies

config/
└── docker_bench.yaml     # Configuration template
```

## Browser Workflow (4 Steps = 1 Query)

Uses **agent-browser** CLI for browser automation:

```text
Step 1: agent-browser open [URL] && wait --load networkidle
        → Open page and wait for network idle (ensures page fully loaded)

Step 2: agent-browser snapshot -i
        → DOM snapshot, get element references @e1, @e2, ...

Step 3: agent-browser click @eN
        → Click element (reuse successful element reference, retry with fresh snapshot on failure)

Step 4: agent-browser screenshot
        → Visual screenshot

Post: agent-browser close --all (close browser session when test ends)
```

**Key Improvements:**
- No explicit `focus` operation needed (agent-browser handles automatically)
- Uses `@eN` element references (based on accessibility-tree, more stable)
- Successful element references are saved and reused, improving subsequent test efficiency
- All commands run in proxy-free environment (clears http_proxy etc.)

## Quick Start

### 1. Prepare Docker Image

Reference [dockerfile_build](../dockerfile_build/README.md) to build image, ensure agent-browser is installed:

```bash
cd dockerfile_build
docker build -t ubuntu-openclaw-chromium:arm64 .

# Or for x86_64 architecture
docker build -f Dockerfile.x86 -t ubuntu-openclaw-chromium:x86_64 .
```

**Image Requirements:**
- agent-browser CLI installed: `npm i -g agent-browser && agent-browser install`
- Chromium browser installed
- Necessary browser dependencies configured

### 2. Install Dependencies

```bash
pip install -r docker_bench/requirements.txt
```

Dependencies: `docker>=6.0.0`, `PyYAML>=6.0`

### 3. Configure Test Parameters

Edit `config/docker_bench.yaml`:

```yaml
docker:
  image: "ubuntu-openclaw-chromium:arm64"
  container_prefix: "oc-bench"
  cpu_limit: 2.0          # 2vCPU
  memory_limit: "2g"      # 2G memory

container:
  total_count: 10         # Create 10 containers

browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  task_timeout: 200       # Task timeout 200 seconds
  interval_min: 0.5       # Task interval minimum
  interval_max: 3.0       # Task interval maximum

test:
  duration: 160           # Test duration 160 seconds
```

### 4. Run Tests

#### Full Workflow Mode

Create containers, check ports, execute tasks, generate report:

```bash
# Use config file
python -m docker_bench --config config/docker_bench.yaml

# Override with CLI arguments
python -m docker_bench --config config/docker_bench.yaml --total 20 --duration 300

# Full CLI mode (no config file)
python -m docker_bench \
    --image ubuntu-openclaw-chromium:arm64 \
    --total 10 \
    --cpu 2 \
    --memory 2g \
    --duration 160 \
    --browser-url http://192.168.110.10:8080/Weibo.html
```

#### Create-only Mode (Phase 0)

Only create containers, no task execution. Containers stay running for later use:

```bash
python -m docker_bench --config config/docker_bench.yaml --create-only

# With create batch control
python -m docker_bench --config config/docker_bench.yaml \
    --create-only \
    --create-batch-size 5 \
    --create-batch-interval 10
```

#### Detect Existing Mode

Detect currently running containers and execute benchmark on them:

```bash
python -m docker_bench --config config/docker_bench.yaml --detect

# With task batch control
python -m docker_bench --config config/docker_bench.yaml \
    --detect \
    --task-batch-size 5 \
    --task-batch-interval 5
```

### 5. Cleanup Containers

Batch delete containers after testing:

```bash
cd docker_bench
chmod +x delete_containers.sh
./delete_containers.sh

# Or delete containers with custom prefix
./delete_containers.sh my-prefix
```

## Run Mode Comparison

| Mode | Parameter | Description | Container Behavior |
|------|-----------|-------------|-------------------|
| **Full Workflow** | (default) | Create→Port check→Benchmark→Report | Delete after test |
| **Create-only** | `--create-only` | Create→Port check→Exit | Keep running |
| **Detect Existing** | `--detect` | Detect→Benchmark→Report | Keep running |

## Batch Control

### Two Independent Batch Controls

| Control Item | Purpose | Protection Target |
|--------------|---------|-------------------|
| `create_batch` | Container creation batching | Host resources |
| `task_batch` | Task execution batching | Target web server |

### Example Configuration

```yaml
# Create 20 containers, 4 batches of 5 each
create_batch:
  size: 5
  interval: 10  # Create batch interval 10 seconds

# Benchmark in 4 batches, 5 containers per batch
task_batch:
  size: 5
  interval: 5  # Task batch interval 5 seconds
```

## Configuration Reference

### YAML Configuration Parameters

| Config Item | Parameter | Description | Default |
|-------------|-----------|-------------|---------|
| `docker` | `image` | Docker image name | `ubuntu-openclaw-chromium:arm64` |
| `docker` | `container_prefix` | Container name prefix | `oc-bench` |
| `docker` | `cpu_limit` | Per-container CPU limit | `2.0` |
| `docker` | `memory_limit` | Per-container memory limit | `2g` |
| `container` | `total_count` | Total container count | 10 |
| `container` | `detect_existing` | Detect existing containers | false |
| `container` | `create_only` | Create-only mode | false |
| `create_batch` | `size` | Create batch size | optional |
| `create_batch` | `interval` | Create batch interval | optional |
| `task_batch` | `size` | Task batch size | optional |
| `task_batch` | `interval` | Task batch interval | optional |
| `browser` | `urls` | Browser test URL list | required |
| `browser` | `task_timeout` | Task timeout (seconds) | 200 |
| `browser` | `interval_min` | Task interval minimum (seconds) | 0.5 |
| `browser` | `interval_max` | Task interval maximum (seconds) | 3.0 |
| `port_check` | `ports` | Ports to check | [18789, 11436] |
| `test` | `duration` | Test duration (seconds) | 160 |
| `test` | `benchmark_percent` | Benchmark container percentage | 1.0 |
| `report` | `output_dir` | Report output directory | `results/docker` |

### CLI Arguments

```bash
python -m docker_bench --help

Options:
  --config                  YAML config file path

  # Docker config
  --image                   Docker image name
  --prefix                  Container name prefix
  --cpu                     Per-container CPU limit (--cpus)
  --memory                  Per-container memory limit (-m)
  --create-timeout          Container creation timeout

  # Container config
  --total                   Total container count
  --detect                  Detect existing containers mode
  --create-only             Create-only mode (Phase 0)

  # Create batch control
  --create-batch-size       Create batch size (full concurrent if not set)
  --create-batch-interval   Create batch interval seconds

  # Task batch control
  --task-batch-size         Task batch size (full concurrent if not set)
  --task-batch-interval     Task batch interval seconds

  # Browser task
  --browser-url             Browser URL (can specify multiple times)
  --browser-timeout         Browser task timeout
  --browser-interval-min    Task interval minimum
  --browser-interval-max    Task interval maximum

  # Test run
  --duration                Test duration seconds
  --stats-interval          Stats snapshot interval
  --benchmark-percent       Benchmark container percentage

  # Report
  --output-dir              Report output directory
  --filename-prefix         Report filename prefix
```

## Test Flow

```text
Phase 1: Create/Detect Containers
    ├── [Full/Create-only] Call docker run --cpus -m image
    ├── [Detect] Query existing containers via docker ps
    ├── Record create_elapsed time (container creation time)
    └── Start port check (18789 + 11436)

Phase 2: Port Check
    ├── Check 18789 port
    ├── Check 11436 port
    ├── Record port_wait_elapsed time
    └── Mark PORT_READY when both ports ready

[Create-only mode: exit here]

Phase 3: Start Browser Tasks
    ├── Check if agent-browser is installed
    ├── [With task_batch] Start tasks in batches
    ├── [Without config] Full concurrent start
    └── Each container has independent task thread

Phase 4: Run Test (4-step workflow)
    ├── Step 1: open + wait --load networkidle
    ├── Step 2: snapshot -i (get @eN refs)
    ├── Step 3: click @eN (reuse successful element)
    └── Step 4: screenshot
    └── Collect real-time statistics

Phase 5: Stop and Generate Report
    ├── Close browser session (agent-browser close --all)
    ├── [Created containers] Delete all containers
    ├── [Detect mode] Keep containers running
    └── Generate performance report (includes QPS)
```

## Container State Flow

```text
PENDING → CREATING → CREATED → PORT_READY → (ACTIVE) → KILLED
                     ↓
                  FAILED
                     ↓
               PORT_FAILED
                     ↓
                  OFFLINE
```

## Performance Report

### Report Content

1. **Test Configuration** - Image name, container spec, batch strategy, test duration
2. **Container Status** - Creation count, failure count, port failures
3. **Container Creation Performance** - docker run time (excluding port wait)
4. **Port Wait Performance** - Time waiting for ports ready
5. **Total Startup Performance** - create + port_wait total time
6. **Browser Query Statistics** - Success rate, latency (P50/P95/P99)
7. **QPS Statistics** - Queries per second
8. **Per-Step Latency Analysis** - Average latency for each step

### Report Example

```text
================================================================================
Docker Container Bench - Browser Automation Performance Report
================================================================================

[Test Configuration]
  Image:           ubuntu-openclaw-chromium:arm64
  Container Spec:  2.0vCPU / 2g
  Total Containers:20
  Mode:            Full workflow
  Create Batch:    4 batches x 5 containers
  Create Interval: 10s
  Task Batch:      4 batches x 5 containers
  Task Interval:   5s
  Test Duration:   300s

[Browser Workflow (4 steps = 1 query)]
  Step 1: agent-browser open + wait --load networkidle
  Step 2: agent-browser snapshot -i
  Step 3: agent-browser click @eN (reuse successful element)
  Step 4: agent-browser screenshot

[Container Status]
  Created (Docker):   20 / 20
  Ports Ready:        20 / 20
  Create Failed:      0
  Port Check Failed:  0
  Offline (runtime):  2

[Browser Query Statistics]
  (4-step workflow = 1 query)
  Total Queries: 1250
  Success:       1170
  Failed:        80 (timeout: 25)
  Success Rate:  93.6%
  Avg Latency:   2345.6ms
  P99 Latency:   5678.2ms

[Overall QPS]
  Total QPS:     3.90 queries/sec
  (Success queries / Test duration)

[Per-Step Latency Analysis]
  Open        : avg=856.2ms
  Snapshot    : avg=567.8ms
  Click       : avg=234.5ms
  Screenshot  : avg=563.7ms
```

## Error Handling

### Common Error Types

| Error | Cause | Solution |
|-------|-------|----------|
| `Operation timed out` | Slow page load, network delay | Increase interval_min/max |
| `CDP command timed out` | Large DOM tree, high concurrency pressure | Reduce concurrent containers |
| `Not attached to an active page` | Page not fully loaded | Added wait --load networkidle |
| `agent-browser not available` | agent-browser not installed in image | Install agent-browser in image |
| `409 container name conflict` | Container already exists | Auto-delete existing containers |

### High Concurrency Optimization Tips

1. **Reduce concurrency pressure**:
```yaml
browser:
  interval_min: 1.0   # Change from 0.5 to 1.0
  interval_max: 5.0   # Change from 3.0 to 5.0
```

2. **Start tasks in batches**:
```yaml
task_batch:
  size: 5
  interval: 10
```

3. **Increase task timeout**:
```yaml
browser:
  task_timeout: 300  # Change from 200 to 300
```

## Performance Comparison Note

When using the same configuration (container count, CPU/Memory, test duration) to run tests on ARM and x86 servers, the QPS comparison is **fair**:

- Both execute the same 4-step workflow
- `wait --load networkidle` waiting time is counted in latency
- Bottlenecks (Web Server or browser rendering) affect both equally
- Final QPS difference reflects true architectural performance difference

## Related Documentation

- [E2B Bench Usage Guide](e2b-bench-usage.md) - E2B sandbox performance testing
- [Dockerfile Build Guide](../dockerfile_build/README.md) - Docker image build
- [agent-browser Official Docs](https://github.com/nickfla1/agent-browser) - agent-browser CLI reference