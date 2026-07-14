#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parser.py - Session file parser for LLM Replay Server.

Provides pluggable parser architecture to support multiple session formats.
Currently supports browser-session JSONL format (Anthropic/Claude style).
"""

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Type

from .models import Turn, ToolCall, SessionMetadata


def _iso_to_ms(timestamp: Any) -> Optional[int]:
    """
    Convert timestamp to epoch milliseconds.

    Supports:
    - ISO8601 string (with or without 'Z')
    - Integer/float milliseconds
    - None (returns None)
    """
    if timestamp is None:
        return None

    if isinstance(timestamp, (int, float)):
        return int(timestamp)

    # ISO8601 string
    s = str(timestamp).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


class SessionParser(ABC):
    """
    Abstract base class for session file parsers.

    Each parser handles a specific session file format.
    """

    @classmethod
    @abstractmethod
    def can_parse(cls, path: str) -> bool:
        """
        Check if this parser can handle the given file.

        Args:
            path: Path to the session file

        Returns:
            True if this parser supports this format
        """
        pass

    @classmethod
    @abstractmethod
    def parse(cls, path: str) -> List[Turn]:
        """
        Parse the session file and extract turns.

        Args:
            path: Path to the session file

        Returns:
            List of Turn objects in chronological order

        Raises:
            ValueError: If file cannot be parsed
        """
        pass

    @classmethod
    @abstractmethod
    def format_name(cls) -> str:
        """Return the name of this format (for logging/config)."""
        pass


class BrowserSessionParser(SessionParser):
    """
    Parser for browser-session JSONL format.

    This is the format used by Anthropic Claude's session recordings:
    - Each line is a JSON object representing an event
    - Events with type="message" and role="assistant" are LLM responses
    - Contains text content and tool_calls

    Example format:
    {"type": "message", "timestamp": "2024-01-01T00:00:00Z", "message": {"role": "assistant", ...}}
    """

    @classmethod
    def format_name(cls) -> str:
        return "browser-session"

    @classmethod
    def can_parse(cls, path: str) -> bool:
        """Check if file is a JSONL with browser-session format."""
        if not os.path.isfile(path):
            return False

        # Check extension
        if not path.endswith(".jsonl"):
            return False

        # Check first line
        try:
            with open(path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if not first_line:
                    return False
                obj = json.loads(first_line)
                # Check for expected structure - support multiple formats
                # Format 1: type="message" with message field (original)
                # Format 2: type="session" (openclaw format) - check for session structure
                if obj.get("type") == "message" and "message" in obj:
                    return True
                # Check if file contains assistant messages
                return obj.get("type") == "session"
        except (json.JSONDecodeError, IOError):
            return False

    @classmethod
    def parse(cls, path: str) -> List[Turn]:
        """Parse browser-session JSONL file."""
        events = []

        # Read all events
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not events:
            raise ValueError(f"Session file is empty or cannot be parsed: {path}")

        # Get first timestamp for relative time calculation
        first_ts = _iso_to_ms(events[0].get("timestamp")) or 0

        turns = []
        for event in events:
            # Filter: only assistant messages
            if event.get("type") != "message":
                continue

            msg = event.get("message", {})
            if msg.get("role") != "assistant":
                continue

            # Calculate LLM duration
            outer_ts = _iso_to_ms(event.get("timestamp"))
            inner_ts = _iso_to_ms(msg.get("timestamp"))
            llm_duration = (outer_ts - inner_ts) if (outer_ts is not None and inner_ts is not None) else 0

            # Extract text content
            text_parts = []
            tool_calls = []

            for content in msg.get("content", []):
                if not isinstance(content, dict):
                    continue

                ctype = content.get("type")

                if ctype == "text":
                    text_parts.append(content.get("text", ""))

                elif ctype == "toolCall":
                    # Extract tool call
                    tc_id = content.get("id") or f"chatcmpl-tool-{len(turns)}"
                    tc_name = content.get("name", "")
                    tc_args = content.get("arguments", {})

                    # Ensure arguments is a dict
                    if not isinstance(tc_args, dict):
                        tc_args = {}

                    tool_calls.append(
                        ToolCall(
                            id=tc_id,
                            name=tc_name,
                            arguments=json.dumps(tc_args, ensure_ascii=False),
                        )
                    )

            # Build Turn object
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                usage = {
                    "prompt_tokens": usage.get("input", 0),
                    "completion_tokens": usage.get("output", 0),
                    "total_tokens": usage.get("total", 0),
                }
            else:
                usage = {}

            turns.append(
                Turn(
                    index=len(turns),
                    text="\n".join(text_parts) if text_parts else None,
                    tool_calls=tool_calls,
                    llm_duration_ms=max(0, int(llm_duration)),
                    model=msg.get("model", "unknown"),
                    response_id=msg.get("responseId") or f"chatcmpl-{len(turns)}",
                    usage=usage,
                    stop_reason=msg.get("stopReason", "toolUse"),
                    inner_ts_rel=(inner_ts - first_ts) if inner_ts else None,
                    outer_ts_rel=(outer_ts - first_ts) if outer_ts else None,
                )
            )

        return turns


class OpenAISessionParser(SessionParser):
    """
    Parser for OpenAI-style session format.

    Future implementation for OpenAI session recordings.
    Currently placeholder for extensibility.
    """

    @classmethod
    def format_name(cls) -> str:
        return "openai-session"

    @classmethod
    def can_parse(cls, path: str) -> bool:  # noqa: ARG003
        """Placeholder - not yet implemented."""
        return False

    @classmethod
    def parse(cls, path: str) -> List[Turn]:  # noqa: ARG003
        """Placeholder - not yet implemented."""
        raise NotImplementedError("OpenAI session parser not yet implemented")


# Registry of all parsers
PARSER_REGISTRY: List[Type[SessionParser]] = [
    BrowserSessionParser,
    OpenAISessionParser,
]


def auto_detect_parser(path: str) -> SessionParser:
    """
    Auto-detect the appropriate parser for a session file.

    Args:
        path: Path to the session file

    Returns:
        The appropriate parser class

    Raises:
        ValueError: If no parser can handle the file
    """
    for parser_cls in PARSER_REGISTRY:
        if parser_cls.can_parse(path):
            return parser_cls

    raise ValueError(f"No parser found for session file: {path}")


def parse_session(path: str, parser_type: Optional[str] = None) -> SessionMetadata:
    """
    Parse a session file and return metadata with turns.

    Args:
        path: Path to the session file
        parser_type: Optional parser type override ("auto", "browser-session", etc.)

    Returns:
        SessionMetadata with loaded turns

    Raises:
        ValueError: If parsing fails
    """
    path = os.path.abspath(path)
    name = Path(path).stem  # File name without extension

    # Select parser
    if parser_type and parser_type != "auto":
        parser_cls = next((p for p in PARSER_REGISTRY if p.format_name() == parser_type), None)
        if not parser_cls:
            raise ValueError(f"Unknown parser type: {parser_type}")
    else:
        parser_cls = auto_detect_parser(path)

    # Parse turns
    start_time = datetime.now()
    turns = parser_cls.parse(path)
    load_duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

    # Validate
    errors = []
    warnings = []

    # Check tool_call ID uniqueness
    all_ids = [tc.id for t in turns for tc in t.tool_calls]
    if len(all_ids) != len(set(all_ids)):
        errors.append("Duplicate tool_call IDs found")

    # Check timestamp monotonicity
    timestamps = [t.inner_ts_rel for t in turns if t.inner_ts_rel]
    if timestamps and timestamps != sorted(timestamps):
        warnings.append("Timestamps are not monotonically increasing")

    # Check turn index continuity
    indices = [t.index for t in turns]
    if indices != list(range(len(turns))):
        errors.append("Turn indices are not sequential")

    # Build metadata - collect all unique tool names
    all_tool_names = set()
    for t in turns:
        for tool_name in t.tool_names:
            all_tool_names.add(tool_name)
    unique_tools = sorted(all_tool_names)

    return SessionMetadata(
        name=name,
        path=path,
        total_turns=len(turns),
        total_llm_time_ms=sum(t.llm_duration_ms for t in turns),
        total_tool_calls=sum(len(t.tool_calls) for t in turns),
        unique_tools=unique_tools,
        model=turns[0].model if turns else "unknown",
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        load_time=start_time,
        load_duration_ms=load_duration_ms,
    )


def load_sessions_from_directory(
    directory: str,
    parser_type: Optional[str] = None,
) -> Dict[str, SessionMetadata]:
    """
    Load all session files from a directory.

    Args:
        directory: Path to directory containing session files
        parser_type: Optional parser type override

    Returns:
        Dict mapping session name to SessionMetadata
    """
    directory = os.path.abspath(directory)
    sessions = {}

    if not os.path.isdir(directory):
        return sessions

    for filename in os.listdir(directory):
        if not filename.endswith(".jsonl"):
            continue

        path = os.path.join(directory, filename)
        try:
            metadata = parse_session(path, parser_type)
            sessions[metadata.name] = metadata
        except ValueError as e:
            # Log error but continue loading other sessions
            print(f"[Warning] Failed to load {filename}: {e}")
            continue

    return sessions
