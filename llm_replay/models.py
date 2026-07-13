#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
models.py - Data models for LLM Replay Server.

Defines the core data structures for sessions, turns, and responses.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any


@dataclass
class ToolCall:
    """A single tool call within a turn."""
    id: str
    name: str
    arguments: str  # JSON-encoded arguments

    def to_openai_dict(self) -> Dict[str, Any]:
        """Convert to OpenAI tool_call format."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


@dataclass
class Turn:
    """
    A single turn (LLM response) in a session.

    Contains all information needed to reconstruct an OpenAI-compatible response.
    """
    index: int
    text: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    llm_duration_ms: int = 0
    model: str = "unknown"
    response_id: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    stop_reason: str = "toolUse"

    # Timestamps relative to session start (for debugging)
    inner_ts_rel: Optional[int] = None  # LLM call start
    outer_ts_rel: Optional[int] = None  # LLM call end

    @property
    def finish_reason(self) -> str:
        """Convert stop_reason to OpenAI finish_reason."""
        mapping = {
            "toolUse": "tool_calls",
            "endTurn": "stop",
            "stop": "stop",
            "max_tokens": "length",
        }
        return mapping.get(self.stop_reason, "stop")

    @property
    def has_tool_calls(self) -> bool:
        """Check if this turn contains tool calls."""
        return len(self.tool_calls) > 0

    @property
    def tool_names(self) -> List[str]:
        """Get list of tool names in this turn."""
        return [tc.name for tc in self.tool_calls]


@dataclass
class SessionMetadata:
    """
    Metadata about a loaded session.

    Provides summary information and validation results.
    """
    name: str
    path: str
    total_turns: int = 0
    total_llm_time_ms: int = 0
    total_tool_calls: int = 0
    unique_tools: List[str] = field(default_factory=list)
    model: str = "unknown"

    # Validation
    is_valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Load info
    load_time: Optional[datetime] = None
    load_duration_ms: int = 0

    def summary(self) -> str:
        """Return a one-line summary string."""
        return (
            f"Session '{self.name}': {self.total_turns} turns, "
            f"{self.total_tool_calls} tool_calls, "
            f"{self.total_llm_time_ms / 1000:.1f}s LLM time, "
            f"{len(self.unique_tools)} unique tools"
        )


@dataclass
class PrebuiltTurn:
    """
    Pre-built response data for a single turn.

    Contains pre-serialized JSON bytes for fast response.
    """
    turn: Turn

    # Pre-built JSON responses (bytes for zero-copy)
    completion_json: bytes = b""
    stream_chunks_json: List[bytes] = field(default_factory=list)

    # Timing info (for sleep simulation)
    llm_duration_ms: int = 0


@dataclass
class PrebuiltSession:
    """
    A fully loaded and pre-built session.

    Contains all turns with pre-serialized responses for maximum performance.
    """
    metadata: SessionMetadata
    turns: List[PrebuiltTurn] = field(default_factory=list)

    # Index for fast lookup
    _turn_index: Dict[int, PrebuiltTurn] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        """Build turn index after initialization."""
        for pt in self.turns:
            self._turn_index[pt.turn.index] = pt

    def get_turn(self, index: int) -> Optional[PrebuiltTurn]:
        """Get turn by index (O(1) lookup)."""
        return self._turn_index.get(index)

    def __len__(self) -> int:
        return len(self.turns)

    def __iter__(self):
        return iter(self.turns)


@dataclass
class RequestStats:
    """
    Statistics for a single request.

    Used for tracking performance metrics.
    """
    timestamp: datetime
    session_name: str
    turn_index: int
    llm_duration_ms: int
    actual_delay_ms: int  # Actual time waited
    response_type: str  # "completion" or "stream"
    client_ip: Optional[str] = None
    is_out_of_range: bool = False


@dataclass
class ConnectionStats:
    """
    Statistics for a single connection (client).

    Tracks the progress and performance of one agent connection.
    """
    connection_id: str
    session_name: str
    current_turn: int = 0
    total_requests: int = 0
    total_llm_time_ms: int = 0
    start_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    is_active: bool = True

    # Per-turn stats
    turn_stats: List[RequestStats] = field(default_factory=list)