# LLM Replay Server

High-performance session replay server for agent benchmarking. Designed to handle 200+ concurrent connections without becoming a bottleneck.

## Overview

LLM Replay Server replays recorded LLM sessions, providing an OpenAI-compatible HTTP API. Agents can be configured to use this server instead of a real LLM, enabling:

- **Reproducible testing**: Same session = same sequence of responses
- **Performance benchmarking**: Isolate agent execution time from LLM latency
- **Multiple scenarios**: Load multiple sessions, agents choose which to use

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  LLM Replay Server (Async, High-Performance)                        │
│                                                                      │
│  Session Pool (Pre-built Responses)                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                          │
│  │browser-3 │  │ qa-2     │  │ search-1 │                          │
│  └──────────┘  └──────────┘  └──────────┘                          │
│                                                                      │
│  OpenAI-Compatible API                                               │
│  - /v1/chat/completions  (model=session_name)                       │
│  - /v1/models                                             │
│  - /v1/sessions          (list loaded sessions)                     │
│  - /health, /metrics                                                │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
                              │
          ┌──────────────────────────────────┐
          │  200 VMs (Agents)                 │
          │  Each agent configures:           │
          │    base_url: http://server:5199/v1│
          │    model: <session_name>          │
          └──────────────────────────────────┘
```

## Features

### High Performance

- **Async architecture**: Uses aiohttp for maximum concurrency
- **Pre-built responses**: JSON pre-serialized at startup, zero-copy serving
- **No global locks**: Each connection tracks its own turn progress
- **Handles 200+ concurrent connections**: Designed for VM-scale testing

### Multiple Sessions

- Load multiple session files at startup
- Agents select session via `model` parameter
- Each connection independently tracks turn progress

### Timing Simulation

- Real LLM response timing (configurable scale factor)
- Optional jitter for realism
- Debug mode (`--no-sleep`) for fast iteration

### Statistics

- Request tracking per session
- Per-connection stats
- Excel/JSON export

## Quick Start

### 1. Install Dependencies

```bash
pip install aiohttp pyyaml openpyxl
```

### 2. Prepare Session Files

Place your `.jsonl` session files in `llm_replay/sessions/` directory.

### 3. Start Server

```bash
# Load all sessions from default directory
python -m llm_replay --port 5199

# Load from custom directory
python -m llm_replay --sessions-dir ./my-sessions --port 5199

# Load specific sessions
python -m llm_replay --sessions ./sessions/browser-3.jsonl,./sessions/qa-2.jsonl

# Use config file
python -m llm_replay --config llm_replay/config/llm_replay.yaml
```

### 4. Configure Agents

Set your agent's LLM config to point to the server:

```yaml
llm:
  base_url: "http://127.0.0.1:5199/v1"
  model: "browser-session-3"  # Session name (file stem)
```

## CLI Options

```
python -m llm_replay [OPTIONS]

Options:
  --config PATH          YAML configuration file
  --sessions-dir DIR     Directory containing .jsonl files
  --sessions PATHS       Comma-separated list of session files
  --host HOST            Listen address (default: 0.0.0.0)
  --port PORT            Listen port (default: 5199)
  --scale FLOAT          Time scaling (1.0=real, 0.1=10x faster)
  --no-sleep             Skip timing simulation
  --jitter FLOAT         Random jitter in seconds
  --no-stats             Disable statistics collection
  --stats-dir DIR        Stats export directory
  --quiet                Reduce log output
  --verbose              Verbose log output
  --log-file PATH        Log file path
```

## API Endpoints

### /v1/chat/completions

Main endpoint for LLM requests. The `model` parameter specifies which session to use.

```json
POST /v1/chat/completions
{
  "model": "browser-session-3",
  "messages": [{"role": "user", "content": "..."}],
  "stream": false
}

Response:
{
  "id": "chatcmpl-001",
  "object": "chat.completion",
  "model": "browser-session-3",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "...",
      "tool_calls": [...]
    },
    "finish_reason": "tool_calls"
  }]
}
```

### /v1/models

List available sessions (as "models").

```json
GET /v1/models

Response:
{
  "object": "list",
  "data": [
    {"id": "browser-session-3", "object": "model", "owned_by": "llm-replay"},
    {"id": "qa-session-2", "object": "model", "owned_by": "llm-replay"}
  ]
}
```

### /v1/sessions

List loaded sessions with details.

```json
GET /v1/sessions

Response:
{
  "sessions": [
    {
      "name": "browser-session-3",
      "total_turns": 99,
      "total_tool_calls": 89,
      "total_llm_time_s": 285.3,
      "unique_tools": ["browser_open", "browser_click", ...],
      "model": "GLM-4.7-W8A8"
    }
  ]
}
```

### /health, /metrics

Health check and Prometheus metrics.

## Session File Format

Session files use JSONL format (JSON Lines). Each line is an event object.

See [sessions/README.md](sessions/README.md) for detailed format specification.

## Configuration File

See [config/llm_replay.yaml](config/llm_replay.yaml) for full configuration options.

## Performance Notes

### Why It's Fast

1. **Pre-built responses**: All JSON is serialized at startup
2. **Zero-copy serving**: Pre-serialized bytes returned directly
3. **Async I/O**: aiohttp handles connections with coroutines
4. **No locks**: Each connection has independent state

### Benchmarks (Expected)

| Scenario | Expected QPS |
|----------|--------------|
| 200 concurrent, --no-sleep | 10,000+ |
| 200 concurrent, scale=0.1 | 1,000+ |
| 200 concurrent, scale=1.0 | Limited by timing |

### Nginx Integration (Optional)

For even higher throughput or SSL termination:

```nginx
upstream llm_replay {
    server 127.0.0.1:5199;
}

server {
    listen 80;
    location /v1/ {
        proxy_pass http://llm_replay;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

## Integration with Bench Framework

See project documentation for integration with:
- `e2b_bench` - E2B sandbox testing
- `docker_bench` - Docker container testing
- `vm_bench_lite` - Browser automation benchmarking

## Deprecated

The old `session-replay/session-replay.py` is deprecated. Use this package instead.

## License

Part of Agent VM Bench project.
