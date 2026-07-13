#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session-replay.py - DEPRECATED: Use llm_replay package instead.

This file is kept for backward compatibility only.
For new implementations, use:

    python -m llm_replay --sessions-dir ./sessions --port 5199

The new llm_replay package provides:
  - High-performance async architecture (aiohttp)
  - Multiple session support with session pool
  - Pre-built responses for zero-copy serving
  - YAML configuration support
  - Statistics collection with Excel/JSON export
  - Designed to handle 200+ concurrent connections

Migration guide:
  - Old: session-replay.py --session <file> --port 5199
  - New: python -m llm_replay --sessions <file> --port 5199

See: llm_replay/__main__.py for full CLI options.

Original implementation (kept for reference):
--------------------------------------------

fake_llm.py - OpenAI-compatible fake LLM service for replaying browser-session-3 call paths and timing.

How it works:
  1. Parse the browser-session-3 session file and extract all assistant messages in chronological order (99 total).
     Each assistant record contains:
       - Text content (text)
       - Tool calls (tool_calls: name + arguments)
       - LLM call duration = event.timestamp(outer) - message.timestamp(inner)
  2. Start an OpenAI-compatible HTTP server (Python standard library only).
  3. On each /v1/chat/completions request from openclaw:
       - Infer the current turn index (0-based) from the number of assistant messages already in the request (robust against retries)
       - Sleep for the recorded LLM duration (simulating model call time)
       - Return the pre-recorded assistant content (including tool_calls) in OpenAI ChatCompletion format
  4. Also provides /v1/models and /health endpoints.

This way, openclaw + fake_llm (+ agent-browser MCP executing real tools) can fully replay
the browser-session-3 call path (99 tool call sequence) and timing (LLM time accounts for 99.2%).

Usage:
    py fake_llm.py --session d:\\source\\browser\\browser-session-3 --port 5199
    py fake_llm.py --session ./browser-session-3 --port 5199 --host 0.0.0.0
    py fake_llm.py --session ./browser-session-3 --port 5199 --scale 0.1   # 10x speed (for debugging)
    py fake_llm.py --session ./browser-session-3 --port 5199 --no-sleep     # No waiting (for debugging)

Then point the model BaseURL in openclaw config to http://127.0.0.1:5199/v1, model name can be anything.
"""

import sys
import warnings

warnings.warn(
    "session-replay.py is deprecated. Use 'python -m llm_replay' instead.",
    DeprecationWarning,
    stacklevel=2,
)

print("=" * 70)
print("DEPRECATED: session-replay.py is deprecated.")
print("Use the new llm_replay package instead:")
print("  python -m llm_replay --sessions-dir ./sessions --port 5199")
print("=" * 70)
print()

# Exit early - user should use llm_replay instead
sys.exit(0)