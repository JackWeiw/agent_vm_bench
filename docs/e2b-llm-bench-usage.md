# E2B LLM Benchmark Usage Guide

This guide explains how to use the LLM benchmark feature in e2b_bench.

## Overview

The LLM benchmark mode allows you to:
1. Create E2B sandboxes with OpenClaw agent
2. Dynamically configure OpenClaw to use MockLLM as LLM provider
3. Execute predefined scenarios by sending prompts via Gateway HTTP API
4. Collect performance metrics (latency, success rate, P99, etc.)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  e2b_bench (Control Plane)                                      │
│  1. Health check MockLLM service                                │
│  2. Load scenario config (scenarios.yaml)                       │
│  3. Dynamically configure OpenClaw (openclaw config set)        │
│  4. Trigger scenario execution via Gateway                      │
└─────────────────────────────────────────────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │   openclaw config set             │
              │   models.providers.llm-replay.*   │
              └─────────────────┬─────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Sandbox (OpenClaw Agent)                                        │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Dynamic Configuration (via CLI)                           │  │
│  │  openclaw config set models.providers.llm-replay.baseUrl  │  │
│  │                           'http://<MOCKLLM_IP>:5199/v1'   │  │
│  │  openclaw config set models.providers.llm-replay.models.0 │  │
│  │                           'browser-scenario-1'             │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                │                                 │
│                    Gateway Hot-Reload                           │
│                                │                                 │
│                    Agent -> MockLLM Request                      │
│                                │                                 │
└────────────────────────────────┼────────────────────────────────┘
                                 │
                                 ▼
                 ┌───────────────────────────────────┐
                 │  MockLLM Service (llm_replay)     │
                 │  - /v1/chat/completions           │
                 │  - Session-based replay           │
                 └───────────────────────────────────┘
```

**Key Feature**: OpenClaw is dynamically configured before each benchmark run using `openclaw config set` commands. The Gateway automatically hot-reloads after configuration changes.

## Prerequisites

1. **MockLLM Service**: Must be running before starting the benchmark
2. **Session Files**: JSONL files in `llm_replay/sessions/` directory
3. **Scenario Config**: `llm_replay/config/scenarios.yaml` defining prompts

**Note**: Unlike static configuration, you don't need to create separate sandbox templates for each scenario. The sandbox template only needs OpenClaw installed; configuration is applied dynamically at runtime.

## Quick Start

### 1. Start MockLLM Service

```bash
# Start MockLLM server
python -m llm_replay --sessions-dir ./llm_replay/sessions --port 5199
```

### 2. Configure Scenarios

Edit `llm_replay/config/scenarios.yaml`:

```yaml
scenarios:
  browser-scenario-1:
    prompt: "You are a bank financial advisor Agent. Please help users complete bank financial product queries."

  browser-scenario-2:
    prompt: "You are an e-commerce customer service Agent. Please help users complete order queries."

default: "browser-scenario-1"
```

### 3. Configure e2b_bench.yaml

```yaml
# Task mode
task_mode: "llm"

# LLM configuration
llm:
  enabled: true
  endpoint: "http://192.168.1.10:5199/v1"  # MockLLM service address
  model: "browser-scenario-1"               # Scenario name
  timeout: 600                               # Scenario timeout (seconds)
  request_timeout: 30                        # HTTP request timeout (seconds)
  health_check: true                         # Check MockLLM health before starting
  interval_min: 5.0                          # Min interval between scenarios
  interval_max: 15.0                         # Max interval between scenarios
```

**Note**: The `endpoint` field is used for health checking only. The actual LLM endpoint used by the agent is configured in the sandbox template's `openclaw.json`.

### 4. Run Benchmark

```bash
# Using config file
python -m e2b_bench --config config/e2b_bench.yaml

# Using CLI
python -m e2b_bench --task-mode llm --total 10 --duration 300
```

## Configuration Reference

### LLMConfig Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | false | Enable LLM benchmark mode |
| `endpoint` | str | "" | MockLLM service base URL (for health check only) |
| `model` | str | "" | Scenario/session name (must match openclaw.json) |
| `timeout` | int | 600 | Scenario-level timeout (seconds) |
| `request_timeout` | int | 30 | Single HTTP request timeout (seconds) |
| `health_check` | bool | true | Check MockLLM health before starting |
| `scenario_file` | str | "" | Custom scenarios.yaml path |
| `interval_min` | float | 5.0 | Min interval between scenarios (seconds) |
| `interval_max` | float | 15.0 | Max interval between scenarios (seconds) |

**Note**: The `endpoint` is used for both health checking and dynamic configuration of OpenClaw in sandboxes.

## How It Works

### Execution Flow

```
1. Create/detect sandboxes
   │
2. Wait for PORT_READY (18789 openclaw-gateway)
   │
3. Health check MockLLM service (if enabled)
   │
4. Load scenario config and get prompt
   │
5. Configure OpenClaw in all sandboxes:
   │   - openclaw config set models.providers.llm-replay.baseUrl
   │   - openclaw config set models.providers.llm-replay.models.0.id
   │   - Gateway hot-reloads automatically
   │
6. Start stats collector
   │
7. For each sandbox:
   │   a. Build Gateway HTTP request with prompt
   │   b. Execute via sandbox.commands.run()
   │   c. Wait for response
   │   d. Record metrics
   │   e. Random interval
   │   f. Repeat a-e (reset context each time)
   │
8. Run for test_duration
   │
8. Stop and generate report
```

### Gateway HTTP Request

The benchmark sends requests to sandbox internal Gateway:

```bash
curl -X POST http://127.0.0.1:18789/v1/chat/completions \
  -H 'Authorization: Bearer test-token-123' \
  -H 'Content-Type: application/json' \
  -d '{"model":"browser-scenario-1","messages":[{"role":"user","content":"<prompt>"}]}'
```

## Report Output

### LLM Mode Report Example

```text
================================================================================
E2B Sandbox Bench - Performance Report
================================================================================

[Test Configuration]
  Task Mode:       LLM Scenario
  LLM Endpoint:    http://192.168.1.10:5199/v1
  Scenario:        browser-scenario-1

[LLM Scenario Statistics]
  Total Scenarios: 1234
  Success:         1200
  Failed:          34 (timeout: 12)
  Success Rate:    97.4%
  Avg Latency:     8.5s
  P99 Latency:     18.7s

================================================================================
```

## Troubleshooting

### MockLLM Service Not Healthy

```
ERROR: MockLLM service is not healthy at http://192.168.1.10:5199/v1
```

Solution: Ensure MockLLM server is running:
```bash
python -m llm_replay --sessions-dir ./sessions --port 5199
```

### Scenario Not Found

```
ERROR: Scenario 'browser-scenario-x' not found in configuration
```

Solution: Check that the scenario name matches a key in `scenarios.yaml` and that a corresponding `.jsonl` file exists in the sessions directory.

### Sandbox Ports Not Ready

If sandboxes are stuck in CREATED status, check that:
1. OpenClaw agent is running inside sandbox
2. Port 18789 (openclaw-gateway) is listening

## Advanced Usage

### Multiple Scenarios

To run different scenarios on different sandboxes (future feature):

```yaml
llm:
  scenarios:
    - "browser-scenario-1"
    - "browser-scenario-2"
  session_strategy: "round_robin"  # or "random"
```

### Batch Testing

See `e2b_batch_matrix.yaml` for batch test configuration. The `task_mode: "llm"` setting will apply to all batch tests.
