# Sessions Directory

This directory contains session JSONL files for LLM Replay Server.

## Session File Format

Session files are JSONL (JSON Lines) format, where each line is a JSON object representing an event.

### Browser-Session Format (Anthropic/Claude style)

Each line should have the following structure:

```json
{
  "type": "message",
  "timestamp": "2024-01-01T00:00:00Z",
  "message": {
    "role": "assistant",
    "timestamp": "2024-01-01T00:00:00Z",
    "model": "GLM-4.7-W8A8",
    "content": [
      {"type": "text", "text": "Some text response"},
      {"type": "toolCall", "id": "tool-001", "name": "browser_open", "arguments": {"url": "https://example.com"}}
    ],
    "stopReason": "toolUse",
    "responseId": "chatcmpl-001",
    "usage": {"input": 100, "output": 50, "total": 150}
  }
}
```

### Loading Sessions

All `.jsonl` files in this directory are automatically loaded at server startup.

Session names are derived from file names (without `.jsonl` extension):
- `browser-session-3.jsonl` → session name: `browser-session-3`
- `qa-session-2.jsonl` → session name: `qa-session-2`

### Using Sessions in Agent Config

Configure your agent to use a specific session by setting the `model` parameter:

```yaml
# Agent configuration
llm:
  base_url: "http://127.0.0.1:5199/v1"
  model: "browser-session-3"  # This selects which session to replay
```

## Adding New Sessions

1. Record a session from a real agent interaction
2. Save as `.jsonl` file in this directory
3. Restart the server (or use API to load dynamically)
4. Configure agents to use the new session name

## Session Statistics

After recording, you can view session statistics:

```bash
python -m llm_replay --sessions ./sessions/new-session.jsonl --port 5199 --no-stats
# Server will print session info on startup:
#   - Total turns
#   - Total tool calls
#   - Total LLM time
#   - Unique tools used
```
