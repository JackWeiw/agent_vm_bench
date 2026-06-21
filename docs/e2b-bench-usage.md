# E2B Sandbox Bench - Usage Guide (EN)

E2B Sandbox batch performance testing tool for measuring sandbox startup performance and browser task execution performance.

## Features

- **Batch Sandbox Creation** - Support batched or full concurrent creation
- **Port Check** - Automatically check 18789 (openclaw-gateway) and 11436 (llama-server) port readiness
- **Browser Task Execution** - Execute browser tasks and collect performance data
- **Real-time Statistics** - Real-time display of creation time, port wait time, task latency
- **Performance Report** - Generate detailed performance report (P50/P95/P99 latency)
- **Three Running Modes** - Full workflow, create-only, detect existing
- **Separate Batch Control** - Independent batch config for creation and task execution

## Architecture

```
e2b_bench/
├── __init__.py         # Package initialization
├── __main__.py         # Module entry point
├── bench.py            # Main entry - test workflow
├── config.py           # Configuration management
├── sandbox_manager.py  # Sandbox lifecycle (create, port check, kill)
├── task_runner.py      # Browser task execution (with batch control)
├── stats_collector.py  # Statistics collection and reporting
├── schemas.py          # Data structures
├── utils.py            # Utility functions
├── debug_demo.py       # Debug tool for troubleshooting
├── delete_sandbox.sh   # Delete all sandboxes script
├── .env.example        # Environment variables template
└── requirements.txt    # Dependencies

config/
└── e2b_bench.yaml      # Configuration template
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
  detect_existing: false  # Detect existing sandboxes mode
  create_only: false      # Create-only mode (Phase 0)

# Creation batch control (protect E2B API from overload)
create_batch:
  size: 20      # Sandboxes per creation batch
  interval: 30  # Seconds between creation batches

# Task execution batch control (protect target server from overload)
task_batch:
  size: 10      # Sandboxes to start tasks per batch
  interval: 5   # Seconds between task batches
```

### 3. Run Test

#### Full Workflow Mode

Create sandboxes, check ports, run tasks, generate report:

```bash
# Using config file
python -m e2b_bench --config config/e2b_bench.yaml

# With CLI overrides
python -m e2b_bench --config config/e2b_bench.yaml --total 50 --duration 300

# Full CLI mode (no config file)
python -m e2b_bench \
    --template openclaw-browser-v1 \
    --e2b-access-token your_token \
    --total 100 \
    --duration 600
```

#### Create-Only Mode (Phase 0)

Create sandboxes only, without running tasks. Sandboxes stay running for later use:

```bash
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# With creation batch control
python -m e2b_bench --config config/e2b_bench.yaml \
    --create-only \
    --create-batch-size 20 \
    --create-batch-interval 30
```

#### Detect Existing Mode

Detect existing running sandboxes and run benchmark on them:

```bash
python -m e2b_bench --config config/e2b_bench.yaml --detect

# With task batch control
python -m e2b_bench --config config/e2b_bench.yaml \
    --detect \
    --task-batch-size 10 \
    --task-batch-interval 5
```

## Running Modes Comparison

| Mode | Flag | Description | Sandbox Behavior |
|------|------|-------------|------------------|
| **Full Workflow** | (default) | Create → Port Check → Tasks → Report | Killed after test |
| **Create-Only** | `--create-only` | Create → Port Check → Exit | Left running |
| **Detect Existing** | `--detect` | Detect → Tasks → Report | Left running |

## Batch Control

### Two Independent Batch Controls

| Control | Purpose | Protects |
|---------|---------|----------|
| `create_batch` | Sandbox creation batching | E2B API server |
| `task_batch` | Task execution batching | Target web server |

### Why Separate?

- **Creation Batch**: Avoids E2B API overload when creating many sandboxes
- **Task Batch**: Avoids target server (e.g. 192.168.110.10:8080) overload when starting browser tasks

Example scenario:
```yaml
# Create 100 sandboxes in 5 batches of 20
create_batch:
  size: 20
  interval: 30  # Wait 30s between creation batches

# Start tasks in 10 batches of 10 sandboxes
task_batch:
  size: 10
  interval: 5  # Wait 5s between task batches
```

## CLI Arguments

```bash
python -m e2b_bench --help

Options:
  --config                  YAML configuration file path
  
  # E2B Environment
  --e2b-access-token        E2B access token
  --e2b-api-key             E2B API key
  
  # Sandbox Configuration
  --template                E2B template name
  --total                   Total sandbox count
  --create-timeout          Sandbox creation timeout
  --detect                  Detect existing sandboxes mode
  --create-only             Create-only mode (Phase 0)
  
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
  
  # Test Run
  --duration                Test duration seconds
  --stats-interval          Stats snapshot interval
  
  # Report
  --output-dir              Report output directory
  --filename-prefix         Report filename prefix
```

## Test Workflow

```
Phase 1: Create/Detect Sandboxes
    ├── [Full/Create-Only] Call sandbox.create() API
    ├── [Detect] Query existing sandboxes via Sandbox.list()
    ├── Record create_elapsed time (sandbox.create time)
    └── Start port check (18789 + 11436)

Phase 2: Port Check
    ├── Check 18789 (openclaw-gateway)
    ├── Check 11436 (llama-server)
    ├── Record port_wait_elapsed time
    └── Mark PORT_READY when both ports ready

[Create-Only Mode: Exit Here]

Phase 3: Start Browser Tasks
    ├── [With task_batch] Batched task start
    ├── [Without config] Full concurrent start
    └── Each sandbox has independent task thread

Phase 4: Run Test
    └── Collect real-time statistics

Phase 5: Stop and Report
    ├── [Created sandboxes] Kill all sandboxes
    ├── [Detect mode] Leave sandboxes running
    └── Generate performance report
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

### Debug Tool

When browser commands fail, use the debug tool to troubleshoot:

```bash
# Set environment variables
export E2B_ACCESS_TOKEN="your_token"
export E2B_API_KEY="your_key"
export E2B_TEMPLATE="openclaw-browser-v1"

# Run debug
python e2b_bench/debug_demo.py
```

### Common Issues

| Error | Possible Cause | Solution |
|-------|---------------|----------|
| `Response 400` | Invalid template name | Check E2B template exists |
| `GatewayClient error` | Gateway not started | Check port 18789 |
| `Port check failed` | Port timeout | Increase PORT_CHECK_MAX_WAIT |
| `Command exit_code=1` | Command syntax error | Check openclaw version |
| `SandboxPaginator error` | Wrong iteration | Use paginator.has_next and next_items() |

## Related Documentation

- [E2B Bench Usage (中文)](e2b-bench-usage-zh.md)
- [E2B Bench Design Spec](superpowers/specs/2026-06-16-e2b-sandbox-bench-design.md)
- [E2B Bench Implementation Plan](superpowers/plans/2026-06-16-e2b-sandbox-bench.md)