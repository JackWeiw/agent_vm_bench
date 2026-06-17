# E2B Sandbox Bench - Usage Guide (EN)

E2B Sandbox batch performance testing tool for measuring sandbox startup performance and browser task execution performance.

## Features

- **Batch Sandbox Creation** - Support batched or full concurrent startup
- **Port Check** - Automatically check 18789 (openclaw-gateway) and 11436 (llama-server) port readiness
- **Browser Task Execution** - Execute browser tasks and collect performance data
- **Real-time Statistics** - Real-time display of creation time, port wait time, task latency
- **Performance Report** - Generate detailed performance report (P50/P95/P99 latency)

## Architecture

```
e2b_bench/
├── __init__.py         # Package initialization
├── __main__.py         # Module entry point
├── bench.py            # Main entry - test workflow
├── config.py           # Configuration management
├── sandbox_manager.py  # Sandbox lifecycle (create, port check, kill)
├── task_runner.py      # Browser task execution
├── stats_collector.py  # Statistics collection and reporting
├── schemas.py          # Data structures
├── utils.py            # Utility functions
├── debug_demo.py       # Debug tool for troubleshooting
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
```

### 3. Run Test

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

## CLI Arguments

```bash
python -m e2b_bench --help

Options:
  --config                  YAML configuration file path
  --e2b-access-token        E2B access token
  --e2b-api-key             E2B API key
  --template                E2B template name
  --total                   Total sandbox count
  --batch-size              Sandboxes per batch (None = full concurrent)
  --batch-interval          Batch interval seconds
  --browser-url             Browser URL (can specify multiple)
  --browser-timeout         Browser task timeout
  --duration                Test duration seconds
  --output-dir              Report output directory
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

## Related Documentation

- [E2B Bench Usage (中文)](e2b-bench-usage-zh.md)
- [E2B Bench Design Spec](superpowers/specs/2026-06-16-e2b-sandbox-bench-design.md)
- [E2B Bench Implementation Plan](superpowers/plans/2026-06-16-e2b-sandbox-bench.md)