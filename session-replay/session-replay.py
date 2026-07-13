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
"""

# DEPRECATED WARNING
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

# Original implementation follows below (kept for reference)
# ---------------------------------------------------------------------------

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

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------------------------------------------------------------------------
# Session parsing
# ---------------------------------------------------------------------------
def _iso_to_ms(s):
    """Convert ISO8601 string or millisecond integer to epoch milliseconds."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)


def load_turns(session_path):
    """Extract assistant turns in order from the session file.

    Returns list[dict], each dict contains:
      index, text, tool_calls, llm_duration_ms, model, response_id, usage,
      stop_reason, inner_ts_rel, outer_ts_rel
    """
    events = []
    with open(session_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue

    if not events:
        raise ValueError(f"Session file is empty or cannot be parsed: {session_path}")

    first_ts = _iso_to_ms(events[0].get("timestamp")) or 0
    turns = []
    for e in events:
        if e.get("type") != "message":
            continue
        m = e.get("message", {})
        if m.get("role") != "assistant":
            continue

        outer = _iso_to_ms(e.get("timestamp"))
        inner = _iso_to_ms(m.get("timestamp"))
        llm_duration = (outer - inner) if (outer is not None and inner is not None) else 0

        text_parts = []
        tool_calls = []
        for c in m.get("content", []):
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "text":
                text_parts.append(c.get("text", ""))
            elif ctype == "toolCall":
                tool_calls.append(
                    {
                        "id": c.get("id") or f"chatcmpl-tool-{len(turns)}",
                        "type": "function",
                        "function": {
                            "name": c.get("name", ""),
                            "arguments": json.dumps(c.get("arguments", {}), ensure_ascii=False),
                        },
                    }
                )

        usage = m.get("usage") or {}
        turns.append(
            {
                "index": len(turns),
                "text": "\n".join(text_parts),
                "tool_calls": tool_calls,
                "llm_duration_ms": max(0, int(llm_duration)),
                "model": m.get("model", "GLM-4.7-W8A8"),
                "response_id": m.get("responseId") or f"chatcmpl-fake-{len(turns)}",
                "usage": usage,
                "stop_reason": m.get("stopReason", "toolUse"),
                "inner_ts_rel": (inner - first_ts) if inner is not None else None,
                "outer_ts_rel": (outer - first_ts) if outer is not None else None,
            }
        )
    return turns


# ---------------------------------------------------------------------------
# OpenAI format conversion
# ---------------------------------------------------------------------------
_FINISH_MAP = {
    "toolUse": "tool_calls",
    "endTurn": "stop",
    "stop": "stop",
    "max_tokens": "length",
}


def _finish_reason(reason):
    return _FINISH_MAP.get(reason, "stop")


def build_completion(turn, request_model):
    """Build a non-streaming ChatCompletion response."""
    msg = {"role": "assistant"}
    msg["content"] = turn["text"] if turn["text"] else None
    if turn["tool_calls"]:
        msg["tool_calls"] = turn["tool_calls"]
    usage = turn.get("usage") or {}
    return {
        "id": turn["response_id"],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model or turn["model"],
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": _finish_reason(turn["stop_reason"]),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input", 0),
            "completion_tokens": usage.get("output", 0),
            "total_tokens": usage.get("total", 0),
        },
    }


def build_stream_chunks(turn, request_model):
    """Build streaming SSE chunk sequence (OpenAI-compatible)."""
    cid = turn["response_id"]
    created = int(time.time())
    model = request_model or turn["model"]
    chunks = [
        {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    if turn["text"]:
        chunks.append(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": turn["text"]}, "finish_reason": None}],
            }
        )
    for i, tc in enumerate(turn["tool_calls"]):
        chunks.append(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": i,
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["function"]["name"],
                                        "arguments": tc["function"]["arguments"],
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
    chunks.append(
        {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": _finish_reason(turn["stop_reason"])}],
        }
    )
    return chunks


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class FakeLLMHandler(BaseHTTPRequestHandler):
    # Class-level shared state (injected by make_server)
    turns = []  # list[dict]
    lock = threading.Lock()
    log_enabled = True
    timing_scale = 1.0  # Time scaling, 1.0 = real speed
    no_sleep = False
    start_time = time.time()
    # Statistics
    stats_lock = threading.Lock()
    stats = {"calls": 0, "llm_ms_slept": 0, "by_turn": []}

    # ---- Basic utilities ----
    def log_message(self, fmt, *args):
        if self.log_enabled:
            sys.stderr.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # Use Connection: close for streaming so the client can finish on EOF (best compatibility)
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        for ch in chunks:
            self.wfile.write(b"data: " + json.dumps(ch, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health", "/v1/health"):
            with self.stats_lock:
                calls = self.stats["calls"]
                slept = self.stats["llm_ms_slept"]
            self._send_json(
                200,
                {
                    "status": "ok",
                    "turns_total": len(self.turns),
                    "calls_served": calls,
                    "llm_ms_slept": slept,
                    "uptime_s": round(time.time() - self.start_time, 1),
                    "timing_scale": self.timing_scale,
                    "no_sleep": self.no_sleep,
                },
            )
        elif path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": (self.turns[0]["model"] if self.turns else "GLM-4.7-W8A8"),
                            "object": "model",
                            "owned_by": "fake-llm",
                        }
                    ],
                },
            )
        else:
            self._send_json(404, {"error": {"message": "not found", "path": path}})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": {"message": "not found", "path": path}})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as ex:
            self._send_json(400, {"error": {"message": f"invalid json: {ex}"}})
            return

        request_model = req.get("model")
        stream = bool(req.get("stream", False))
        messages = req.get("messages", []) or []

        # Infer the current turn index (0-based) from the number of assistant messages in the request.
        # openclaw sends [system?] user assistant tool assistant tool ... each turn,
        # so the count of completed assistant messages == current turn index. Robust against retries/out-of-order.
        assistant_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")
        turn_index = assistant_count

        turns = self.turns
        if turn_index < len(turns):
            turn = turns[turn_index]
            sleep_ms = 0 if self.no_sleep else int(turn["llm_duration_ms"] * self.timing_scale)
        else:
            # Beyond recorded range (e.g. openclaw sent another request after close).
            # Return an empty stop response to let openclaw finish normally.
            turn = None
            sleep_ms = 0

        # Simulate model call time
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

        # Statistics
        with self.stats_lock:
            self.stats["calls"] += 1
            self.stats["llm_ms_slept"] += sleep_ms

        if turn is None:
            self.log_message("call#%d turn=%d (OUT OF RANGE, returning empty stop)", self.stats["calls"], turn_index)
            resp = {
                "id": "chatcmpl-fake-end",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request_model or "fake-llm",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            if stream:
                self._send_sse(
                    [
                        {
                            "id": resp["id"],
                            "object": "chat.completion.chunk",
                            "created": resp["created"],
                            "model": resp["model"],
                            "choices": [
                                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                            ],
                        },
                        {
                            "id": resp["id"],
                            "object": "chat.completion.chunk",
                            "created": resp["created"],
                            "model": resp["model"],
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        },
                    ]
                )
            else:
                self._send_json(200, resp)
            return

        # Return recorded content normally
        tool_names = ",".join(tc["function"]["name"] for tc in turn["tool_calls"]) or "-"
        rel = turn["outer_ts_rel"]
        self.log_message(
            "call#%d turn=%d/%d  sleep=%.2fs  tools=[%s]  rel_t=%.1fs  text=%dchars",
            self.stats["calls"],
            turn_index,
            len(turns) - 1,
            sleep_ms / 1000.0,
            tool_names,
            (rel / 1000.0) if rel is not None else -1,
            len(turn["text"]),
        )

        if stream:
            self._send_sse(build_stream_chunks(turn, request_model))
        else:
            self._send_json(200, build_completion(turn, request_model))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="fake_llm - Fake LLM service for replaying browser-session-3")
    ap.add_argument("--session", required=True, help="Path to browser-session-3 session file")
    ap.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5199, help="Listen port (default: 5199)")
    ap.add_argument("--scale", type=float, default=1.0, help="Time scaling (1.0=real speed, 0.1=10x speed)")
    ap.add_argument("--no-sleep", action="store_true", help="No waiting, return immediately (for debugging)")
    ap.add_argument("--quiet", action="store_true", help="Reduce log output")
    args = ap.parse_args()

    session_path = os.path.abspath(args.session)
    if not os.path.isfile(session_path):
        sys.stderr.write(f"Session file does not exist: {session_path}\n")
        sys.exit(2)

    turns = load_turns(session_path)
    if not turns:
        sys.stderr.write(f"No assistant messages in session file: {session_path}\n")
        sys.exit(2)

    total_llm_ms = sum(t["llm_duration_ms"] for t in turns)
    FakeLLMHandler.turns = turns
    FakeLLMHandler.timing_scale = max(0.0, args.scale)
    FakeLLMHandler.no_sleep = bool(args.no_sleep)
    FakeLLMHandler.log_enabled = not args.quiet
    FakeLLMHandler.stats = {"calls": 0, "llm_ms_slept": 0, "by_turn": []}

    sys.stderr.write("=" * 64 + "\n")
    sys.stderr.write("fake_llm - Fake LLM Service\n")
    sys.stderr.write("=" * 64 + "\n")
    sys.stderr.write(f"Session file     : {session_path}\n")
    sys.stderr.write(f"Total turns      : {len(turns)}\n")
    sys.stderr.write(f"Total LLM time   : {total_llm_ms / 1000.0:.1f}s\n")
    sys.stderr.write(f"Avg per turn     : {total_llm_ms / 1000.0 / len(turns):.2f}s\n")
    sys.stderr.write(f"Time scaling     : {args.scale}\n")
    sys.stderr.write(
        f"Wait mode        : {'Disabled (--no-sleep)' if args.no_sleep else 'Enabled (simulating model call time)'}\n"
    )
    sys.stderr.write(f"Listen address   : http://{args.host}:{args.port}\n")
    sys.stderr.write(f"OpenAI BaseURL   : http://{args.host}:{args.port}/v1\n")
    sys.stderr.write("Model name       : {} (can be any value in openclaw config)\n".format(turns[0]["model"]))
    sys.stderr.write("-" * 64 + "\n")
    sys.stderr.write("Point openclaw model BaseURL to the address above.\n")
    sys.stderr.write("Press Ctrl+C to exit.\n\n")

    server = ThreadingHTTPServer((args.host, args.port), FakeLLMHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[fake_llm] Received Ctrl+C, shutting down...\n")
        with FakeLLMHandler.stats_lock:
            s = FakeLLMHandler.stats
        sys.stderr.write(
            f"[fake_llm] Stats: {s['calls']} calls served, simulated LLM time {s['llm_ms_slept'] / 1000.0:.1f}s\n"
        )
        server.shutdown()


if __name__ == "__main__":
    main()
