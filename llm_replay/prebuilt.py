#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prebuilt.py - Pre-built response cache for maximum performance.

Pre-serializes all responses to JSON bytes during startup,
allowing zero-copy response serving during runtime.
"""

import json
import time
from typing import List, Dict, Any, Optional

from .models import Turn, ToolCall, PrebuiltTurn, PrebuiltSession, SessionMetadata
from .parser import parse_session, load_sessions_from_directory


def _build_openai_completion(turn: Turn, request_model: Optional[str] = None) -> Dict[str, Any]:
    """
    Build OpenAI ChatCompletion response dict.

    Args:
        turn: Turn object with response data
        request_model: Model name from request (optional override)

    Returns:
        Dict representing OpenAI ChatCompletion response
    """
    # Build message
    message: Dict[str, Any] = {"role": "assistant"}

    if turn.text:
        message["content"] = turn.text
    else:
        message["content"] = None

    if turn.tool_calls:
        message["tool_calls"] = [tc.to_openai_dict() for tc in turn.tool_calls]

    # Build response
    response = {
        "id": turn.response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_model or turn.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": turn.finish_reason,
            }
        ],
        "usage": turn.usage,
    }

    return response


def _build_openai_stream_chunks(
    turn: Turn,
    request_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Build OpenAI streaming SSE chunks.

    Args:
        turn: Turn object with response data
        request_model: Model name from request (optional override)

    Returns:
        List of chunk dicts for SSE streaming
    """
    response_id = turn.response_id
    created = int(time.time())
    model = request_model or turn.model

    chunks: List[Dict[str, Any]] = []

    # First chunk: role
    chunks.append({
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ],
    })

    # Text content chunk
    if turn.text:
        chunks.append({
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": turn.text},
                    "finish_reason": None,
                }
            ],
        })

    # Tool call chunks (each tool call is a separate chunk)
    for i, tc in enumerate(turn.tool_calls):
        chunks.append({
            "id": response_id,
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
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": tc.arguments,
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        })

    # Final chunk: finish_reason
    chunks.append({
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": turn.finish_reason,
            }
        ],
    })

    return chunks


def prebuild_turn(turn: Turn, default_model: Optional[str] = None) -> PrebuiltTurn:
    """
    Pre-build all response formats for a turn.

    Args:
        turn: Turn object to pre-build
        default_model: Default model name for responses

    Returns:
        PrebuiltTurn with pre-serialized JSON bytes
    """
    # Build non-streaming response
    completion_dict = _build_openai_completion(turn, default_model)
    completion_json = json.dumps(completion_dict, ensure_ascii=False).encode("utf-8")

    # Build streaming chunks
    chunks_dict = _build_openai_stream_chunks(turn, default_model)
    chunks_json = [
        json.dumps(chunk, ensure_ascii=False).encode("utf-8")
        for chunk in chunks_dict
    ]

    return PrebuiltTurn(
        turn=turn,
        completion_json=completion_json,
        stream_chunks_json=chunks_json,
        llm_duration_ms=turn.llm_duration_ms,
    )


def prebuild_session(metadata: SessionMetadata) -> PrebuiltSession:
    """
    Pre-build all responses for a session.

    Args:
        metadata: SessionMetadata with loaded turns

    Returns:
        PrebuiltSession with all turns pre-serialized
    """
    # Re-parse to get turns (metadata only contains summary)
    # Note: In production, we'd pass turns directly to avoid re-parsing
    # But for now, we re-parse for simplicity
    turns = []
    import importlib
    import llm_replay.parser as parser_module

    # Use the correct parser
    from llm_replay.parser import auto_detect_parser
    parser_cls = auto_detect_parser(metadata.path)
    raw_turns = parser_cls.parse(metadata.path)

    # Pre-build each turn
    default_model = metadata.model
    prebuilt_turns = [prebuild_turn(t, default_model) for t in raw_turns]

    return PrebuiltSession(
        metadata=metadata,
        turns=prebuilt_turns,
    )


class SessionPool:
    """
    Pool of pre-built sessions.

    Manages loading and caching of all sessions.
    """

    def __init__(self):
        self._sessions: Dict[str, PrebuiltSession] = {}
        self._load_order: List[str] = []

    def load_session(self, path: str) -> PrebuiltSession:
        """
        Load and pre-build a single session.

        Args:
            path: Path to session file

        Returns:
            PrebuiltSession

        Raises:
            ValueError: If session cannot be loaded
        """
        metadata = parse_session(path)
        prebuilt = prebuild_session(metadata)
        self._sessions[metadata.name] = prebuilt
        self._load_order.append(metadata.name)
        return prebuilt

    def load_directory(self, directory: str) -> List[PrebuiltSession]:
        """
        Load all sessions from a directory.

        Args:
            directory: Path to directory containing .jsonl files

        Returns:
            List of loaded PrebuiltSession objects
        """
        import os

        directory = os.path.abspath(directory)
        loaded = []

        if not os.path.isdir(directory):
            return loaded

        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".jsonl"):
                continue

            path = os.path.join(directory, filename)
            try:
                prebuilt = self.load_session(path)
                loaded.append(prebuilt)
            except ValueError as e:
                # Log but continue
                print(f"[Warning] Failed to load {filename}: {e}")
                continue

        return loaded

    def load_explicit(self, paths: List[str]) -> List[PrebuiltSession]:
        """
        Load explicit list of session files.

        Args:
            paths: List of paths to session files

        Returns:
            List of loaded PrebuiltSession objects
        """
        loaded = []
        for path in paths:
            try:
                prebuilt = self.load_session(path)
                loaded.append(prebuilt)
            except ValueError as e:
                print(f"[Warning] Failed to load {path}: {e}")
                continue
        return loaded

    def get_session(self, name: str) -> Optional[PrebuiltSession]:
        """
        Get a session by name.

        Args:
            name: Session name (file stem)

        Returns:
            PrebuiltSession or None if not found
        """
        return self._sessions.get(name)

    def get_all_sessions(self) -> Dict[str, PrebuiltSession]:
        """Get all loaded sessions."""
        return self._sessions.copy()

    def list_session_names(self) -> List[str]:
        """Get list of all session names."""
        return self._load_order.copy()

    def get_total_turns(self) -> int:
        """Get total turns across all sessions."""
        return sum(len(s) for s in self._sessions.values())

    def get_total_tool_calls(self) -> int:
        """Get total tool calls across all sessions."""
        return sum(s.metadata.total_tool_calls for s in self._sessions.values())

    def get_total_llm_time_ms(self) -> int:
        """Get total LLM time across all sessions."""
        return sum(s.metadata.total_llm_time_ms for s in self._sessions.values())

    def summary(self) -> str:
        """Return a summary string."""
        return (
            f"SessionPool: {len(self._sessions)} sessions, "
            f"{self.get_total_turns()} total turns, "
            f"{self.get_total_tool_calls()} total tool_calls, "
            f"{self.get_total_llm_time_ms() / 1000:.1f}s total LLM time"
        )

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, name: str) -> bool:
        return name in self._sessions

    def __iter__(self):
        return iter(self._sessions.values())