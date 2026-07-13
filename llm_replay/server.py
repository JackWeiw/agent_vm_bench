#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - High-performance async HTTP server for LLM Replay.

Uses aiohttp for maximum concurrency with zero-copy response serving.
Designed to handle 200+ concurrent agent connections without becoming a bottleneck.
"""

import asyncio
import json
import logging
import random
import time
from typing import Dict, List, Optional, Any, Set

from aiohttp import web

from .config import Config
from .models import PrebuiltSession, PrebuiltTurn
from .prebuilt import SessionPool
from .stats import StatsCollector


logger = logging.getLogger("llm_replay.server")


class FakeLLMServer:
    """
    High-performance LLM Replay Server.

    Features:
    - Async architecture (aiohttp) for maximum concurrency
    - Pre-built responses for zero-copy serving
    - Per-connection turn tracking (no global locks)
    - Optional timing simulation with jitter
    - Statistics collection with batch writes

    Designed to handle 200+ concurrent connections without bottlenecking.
    """

    def __init__(self, config: Config, session_pool: SessionPool):
        self.config = config
        self.session_pool = session_pool
        self.stats_collector: Optional[StatsCollector] = None

        # Active connections tracking (for stats, not for locking)
        self._active_connections: Set[str] = set()

        # Server instance
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self):
        """Start the server."""
        # Initialize stats collector
        if self.config.stats.enabled:
            self.stats_collector = StatsCollector(
                export_dir=self.config.stats.export_dir,
                export_format=self.config.stats.export_format,
                batch_write_interval=self.config.stats.batch_write_interval,
                per_connection_stats=self.config.stats.per_connection_stats,
            )
            await self.stats_collector.start()

        # Build aiohttp app
        self._app = web.Application(
            client_max_size=1024 * 1024,  # 1MB max request size
        )

        # Configure middleware for timing and CORS
        self._app.middlewares.append(self._timing_middleware)
        self._app.middlewares.append(self._cors_middleware)

        # Register routes
        self._app.router.add_route("OPTIONS", "/{path:.*}", self._handle_options)
        self._app.router.add_get("/", self._handle_health)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/v1/health", self._handle_health)
        self._app.router.add_get("/v1/models", self._handle_models)
        self._app.router.add_get("/models", self._handle_models)
        self._app.router.add_get("/v1/sessions", self._handle_sessions)
        self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        self._app.router.add_post("/chat/completions", self._handle_chat_completions)
        self._app.router.add_get("/metrics", self._handle_metrics)

        # Start server
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(
            self._runner,
            self.config.server.host,
            self.config.server.port,
            reuse_address=True,
            reuse_port=True,
        )

        await self._site.start()

        logger.info(f"Server started on http://{self.config.server.host}:{self.config.server.port}")
        logger.info(f"Loaded sessions: {self.session_pool.list_session_names()}")
        logger.info(f"OpenAI BaseURL: http://{self.config.server.host}:{self.config.server.port}/v1")

    async def stop(self):
        """Stop the server."""
        if self.stats_collector:
            await self.stats_collector.stop()

        if self._site:
            await self._site.stop()

        if self._runner:
            await self._runner.cleanup()

        logger.info("Server stopped")

    @web.middleware
    async def _timing_middleware(self, request: web.Request, handler):
        """Middleware for request timing and stats."""
        start_time = time.time()

        # Track connection
        conn_id = f"{request.remote}:{request.transport.get_extra_info('socket').getsockname()[1]}"
        self._active_connections.add(conn_id)

        if self.stats_collector:
            await self.stats_collector.record_connection_open()

        try:
            response = await handler(request)
            return response
        finally:
            # Record timing
            elapsed_ms = int((time.time() - start_time) * 1000)

            # Connection close
            self._active_connections.discard(conn_id)
            if self.stats_collector:
                await self.stats_collector.record_connection_close()

            logger.debug(f"Request {request.path} completed in {elapsed_ms}ms")

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        """Middleware for CORS headers."""
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    async def _handle_options(self, request: web.Request) -> web.Response:
        """Handle OPTIONS requests (CORS preflight)."""
        return web.Response(status=204)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle health check requests."""
        summary = {
            "status": "ok",
            "sessions_loaded": len(self.session_pool),
            "total_turns": self.session_pool.get_total_turns(),
            "active_connections": len(self._active_connections),
            "timing_scale": self.config.timing.scale,
            "no_sleep": self.config.timing.no_sleep,
        }

        if self.stats_collector:
            summary["stats"] = self.stats_collector.get_summary()

        return web.json_response(summary)

    async def _handle_models(self, request: web.Request) -> web.Response:
        """Handle /v1/models requests."""
        # Each loaded session appears as a "model"
        models = []
        for session_name in self.session_pool.list_session_names():
            session = self.session_pool.get_session(session_name)
            if session:
                models.append(
                    {
                        "id": session_name,
                        "object": "model",
                        "owned_by": "llm-replay",
                        "created": int(time.time()),
                        "metadata": {
                            "total_turns": len(session),
                            "total_tool_calls": session.metadata.total_tool_calls,
                        },
                    }
                )

        return web.json_response(
            {
                "object": "list",
                "data": models,
            }
        )

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """Handle /v1/sessions requests (list available sessions)."""
        sessions = []
        for session_name in self.session_pool.list_session_names():
            session = self.session_pool.get_session(session_name)
            if session:
                sessions.append(
                    {
                        "name": session_name,
                        "total_turns": len(session),
                        "total_tool_calls": session.metadata.total_tool_calls,
                        "total_llm_time_s": session.metadata.total_llm_time_ms / 1000,
                        "unique_tools": session.metadata.unique_tools,
                        "model": session.metadata.model,
                        "is_valid": session.metadata.is_valid,
                    }
                )

        return web.json_response(
            {
                "sessions": sessions,
                "total": len(sessions),
            }
        )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Handle /metrics requests (Prometheus-style)."""
        if not self.stats_collector:
            return web.json_response({"error": "stats disabled"}, status=503)

        summary = self.stats_collector.get_summary()

        # Prometheus-style text format
        lines = []
        lines.append("# HELP llm_replay_requests_total Total requests served")
        lines.append("# TYPE llm_replay_requests_total counter")
        lines.append(f"llm_replay_requests_total {summary['total_requests']}")

        lines.append("# HELP llm_replay_active_connections Active connections")
        lines.append("# TYPE llm_replay_active_connections gauge")
        lines.append(f"llm_replay_active_connections {summary['active_connections']}")

        lines.append("# HELP llm_replay_llm_time_seconds Total simulated LLM time")
        lines.append("# TYPE llm_replay_llm_time_seconds counter")
        lines.append(f"llm_replay_llm_time_seconds {summary['total_llm_time_s']}")

        for name, s in summary.get("sessions", {}).items():
            lines.append(f'llm_replay_session_requests_total{{session="{name}"}} {s["total_requests"]}')

        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

    async def _handle_chat_completions(self, request: web.Request) -> web.Response:
        """
        Handle /v1/chat/completions requests.

        This is the main API endpoint. Each request:
        1. Extracts session name from 'model' parameter
        2. Infers turn index from assistant message count
        3. Simulates timing (if enabled)
        4. Returns pre-built response (zero-copy)

        Performance optimized:
        - No JSON parsing of request body beyond minimum needed
        - Pre-built response bytes returned directly
        - Each connection maintains its own turn progress
        """
        # Parse request body
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"message": "Invalid JSON"}},
                status=400,
            )

        # Extract parameters
        request_model = body.get("model", "")
        stream = body.get("stream", False)
        messages = body.get("messages", [])

        # Determine session from model name
        session_name = request_model
        session = self.session_pool.get_session(session_name)

        if not session:
            # Session not found
            available = self.session_pool.list_session_names()
            return web.json_response(
                {
                    "error": {
                        "message": f"Session '{session_name}' not found",
                        "available_sessions": available,
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )

        # Infer turn index from assistant message count
        # This is the robust method that works regardless of retry/connection state
        assistant_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")
        turn_index = assistant_count

        # Get the pre-built turn
        prebuilt_turn = session.get_turn(turn_index)

        if prebuilt_turn is None:
            # Out of range - return empty stop response
            logger.warning(f"Turn {turn_index} out of range for session '{session_name}' (max {len(session) - 1})")

            # Record stats
            if self.stats_collector:
                await self.stats_collector.record_request(
                    session_name=session_name,
                    turn_index=turn_index,
                    llm_duration_ms=0,
                    actual_delay_ms=0,
                    response_type="completion",
                    is_out_of_range=True,
                )

            # Build empty stop response
            response_data = {
                "id": "chatcmpl-end",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

            return web.json_response(response_data)

        # Simulate timing
        llm_duration_ms = prebuilt_turn.llm_duration_ms
        scaled_duration_ms = int(llm_duration_ms * self.config.timing.scale)

        if self.config.timing.no_sleep:
            actual_delay_ms = 0
        elif scaled_duration_ms > 0:
            # Add optional jitter
            jitter_ms = int(self.config.timing.jitter * 1000)
            if jitter_ms > 0:
                scaled_duration_ms += random.randint(0, jitter_ms)

            await asyncio.sleep(scaled_duration_ms / 1000.0)
            actual_delay_ms = scaled_duration_ms
        else:
            actual_delay_ms = 0

        # Log request
        tool_names = prebuilt_turn.turn.tool_names
        logger.info(
            f"[{session_name}] Turn {turn_index}/{len(session) - 1}: "
            f"{llm_duration_ms}ms LLM, {actual_delay_ms}ms actual, "
            f"tools={tool_names or '-'}"
        )

        # Record stats
        if self.stats_collector:
            await self.stats_collector.record_request(
                session_name=session_name,
                turn_index=turn_index,
                llm_duration_ms=llm_duration_ms,
                actual_delay_ms=actual_delay_ms,
                response_type="stream" if stream else "completion",
            )

        # Return response (zero-copy from pre-built bytes)
        if stream:
            return await self._serve_stream_response(request, prebuilt_turn, request_model)
        else:
            return self._serve_completion_response(prebuilt_turn, request_model)

    def _serve_completion_response(
        self,
        prebuilt_turn: PrebuiltTurn,
        request_model: str,
    ) -> web.Response:
        """
        Serve non-streaming completion response.

        Uses pre-built JSON bytes for zero-copy performance.
        """
        # Directly return pre-serialized bytes
        return web.Response(
            body=prebuilt_turn.completion_json,
            content_type="application/json",
        )

    async def _serve_stream_response(
        self,
        request: web.Request,
        prebuilt_turn: PrebuiltTurn,
        request_model: str,
    ) -> web.StreamResponse:
        """
        Serve streaming SSE response.

        Uses pre-built chunk bytes for performance.
        """
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

        await response.prepare(request)

        # Write pre-built chunks
        for chunk_bytes in prebuilt_turn.stream_chunks_json:
            await response.write(b"data: " + chunk_bytes + b"\n\n")

        # Write done marker
        await response.write(b"data: [DONE]\n\n")

        return response

    def get_stats_summary(self) -> Dict[str, Any]:
        """Get current stats summary."""
        if self.stats_collector:
            return self.stats_collector.get_summary()
        return {"stats": "disabled"}
