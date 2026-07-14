# LLM Replay Server

High-performance session replay server for agent benchmarking. Designed to handle 200+ concurrent connections without becoming a bottleneck.

## Overview

LLM Replay Server replays recorded LLM sessions, providing an OpenAI-compatible HTTP API. Agents can be configured to use this server instead of a real LLM, enabling:

- **Reproducible testing**: Same session = same sequence of responses
- **Performance benchmarking**: Isolate agent execution time from LLM latency
- **Multiple scenarios**: Load multiple sessions, agents choose which to use

## How It Works

### Turn-Based Replay Mechanism

The server replays sessions **by turn index**, not by matching user input:

```
┌─────────────────────────────────────────────────────────────────┐
│  Session File (browser-session-3.jsonl)                        │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐          │
│  │ Turn 0  │  │ Turn 1  │  │ Turn 2  │  │ Turn 3  │          │
│  │ text+TC │  │ TC only │  │ text+TC │  │ TC only │          │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Request: POST /v1/chat/completions                            │
│  {                                                              │
│    "model": "browser-session-3",                               │
│    "messages": [                                                │
│      {"role": "user", "content": "你是一个银行理财顾问..."},    │
│      {"role": "assistant", "content": [...]},  // count = 1    │
│      {"role": "tool", "content": "..."},                       │
│    ]                                                            │
│  }                                                              │
│                                                                 │
│  → assistant_count = 1 → turn_index = 1 → Return Turn 1       │
└─────────────────────────────────────────────────────────────────┘
```

### Turn Index Inference

The server infers which turn to return by counting assistant messages in the request:

| Request # | Assistant Count in messages | Turn Index | Response |
|-----------|----------------------------|------------|----------|
| 1st | 0 | 0 | Turn 0 response |
| 2nd | 1 | 1 | Turn 1 response |
| 3rd | 2 | 2 | Turn 2 response |
| Nth | N-1 | N-1 | Turn N-1 response |
| Out of range | ≥ total_turns | N/A | Empty response (finish_reason: stop) |

**Important**: Each connection independently tracks its progress. Multiple agents can use the same session simultaneously without interference.

### Example: OpenClaw Agent Integration

When an OpenClaw agent sends a request:

```python
# Initial request (Turn 0)
POST /v1/chat/completions
{
  "model": "browser-session-3",
  "messages": [
    {"role": "user", "content": "你是一个银行理财顾问 Agent..."}
  ]
}

# Response: Turn 0
{
  "id": "chatcmpl-9c43e9c3d4e402ee",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "I understand. Let me first load the agent-browser...",
      "tool_calls": [{"id": "...", "type": "function", "function": {"name": "read", "arguments": "..."}}]
    },
    "finish_reason": "tool_calls"
  }]
}
```

After tool execution, the agent sends the next request:

```python
# Second request (Turn 1)
POST /v1/chat/completions
{
  "model": "browser-session-3",
  "messages": [
    {"role": "user", "content": "你是一个银行理财顾问 Agent..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "..."}
  ]
}

# assistant_count = 1 → turn_index = 1 → Returns Turn 1
```

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
│  - /v1/models                                                        │
│  - /v1/sessions          (list loaded sessions)                     │
│  - /health, /metrics                                                 │
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

See [sessions/README.md](sessions/README.md) for session file format details.

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

### 4. Configure Your Agent

Set your agent's LLM config to point to the server:

```yaml
llm:
  base_url: "http://127.0.0.1:5199/v1"
  model: "browser-session-3"  # Session name (file stem)
```

**For OpenClaw agents**, configure in your agent config:

```yaml
# OpenClaw agent configuration
model:
  provider: openai-completions
  modelId: browser-session-3  # Session name without .jsonl extension
  apiBase: http://127.0.0.1:5199/v1
```

### 5. Run Your Agent

When your agent sends requests to the llm-replay server:

1. First request: `assistant_count = 0` → returns Turn 0
2. Second request: `assistant_count = 1` → returns Turn 1
3. ...and so on until all turns are exhausted

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

### OpenClaw Format

OpenClaw session files have the following structure:

```jsonl
{"type":"session","version":3,"id":"...","timestamp":"...","cwd":"..."}
{"type":"model_change","id":"...","provider":"vllm","modelId":"GLM-4.7-W8A8"}
{"type":"message","id":"...","message":{"role":"user","content":"你的提示词..."}}
{"type":"message","id":"...","message":{"role":"assistant","content":[{"type":"text","text":"..."},{"type":"toolCall","id":"...","name":"read","arguments":{}}]}}
{"type":"message","id":"...","message":{"role":"toolResult","toolUseId":"...","content":"..."}}
...
```

The parser extracts:
- `type="message"` with `role="assistant"` → Turn objects
- Assistant content with `type="text"` → Turn text
- Assistant content with `type="toolCall"` → Tool calls

See [sessions/README.md](sessions/README.md) for more details.

## Configuration File

See [config/llm_replay.yaml](config/llm_replay.yaml) for full configuration options.

```yaml
server:
  host: "0.0.0.0"
  port: 5199

sessions:
  directory: "./sessions"
  parser_type: "auto"  # or "browser-session"

timing:
  scale: 1.0      # 1.0 = real speed, 0.1 = 10x faster
  no_sleep: false # Set true for max speed (debug)
  jitter: 0.0     # Random delay in seconds

stats:
  enabled: true
  export_dir: "./results/llm_replay"
  export_format: "excel"
```

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

## Troubleshooting

### "reuse_port not supported" on Windows

The server uses `reuse_port=True` for Linux performance. On Windows, this causes an error.

**Solution**: Run on Linux only, or modify `server.py` to remove `reuse_port=True`.

### Session not loading

Check:
1. File extension is `.jsonl`
2. File starts with `{"type": "session"}` or `{"type": "message"}`
3. File contains assistant messages (`role: "assistant"`)

### Out of range responses

If your agent receives empty responses with `finish_reason: "stop"` earlier than expected:

1. Check session has enough turns: `GET /v1/sessions`
2. Ensure messages array correctly includes all previous assistant responses
3. The turn index is inferred from `assistant_count`, so verify your messages structure

### Tool calls not matching

The server returns pre-recorded tool calls. If your agent expects different tools:

1. The session file contains specific tool calls recorded from a real session
2. You cannot change tool calls dynamically - they are replayed as recorded
3. Use a different session file if you need different tool call sequences

## Integration with Bench Framework

See project documentation for integration with:
- `e2b_bench` - E2B sandbox testing
- `docker_bench` - Docker container testing
- `vm_bench_lite` - Browser automation benchmarking

## Deprecated

The old `session-replay/session-replay.py` is deprecated. Use this package instead.

## License

Part of Agent VM Bench project.
