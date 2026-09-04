#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
__init__.py - LLM Replay package initialization.

High-performance session replay server for agent benchmarking.
Designed to handle 200+ concurrent connections without bottlenecking.

Exports main classes and functions for programmatic use.
"""

__version__ = "1.0.0"
__author__ = "Agent VM Bench Team"

from .config import (
    Config,
    LoggingConfig,
    ServerConfig,
    SessionsConfig,
    StatsConfig,
    TimingConfig,
    create_default_config,
    merge_cli_args,
)
from .models import (
    PrebuiltSession,
    PrebuiltTurn,
    SessionMetadata,
    ToolCall,
    Turn,
)
from .parser import (
    BrowserSessionParser,
    SessionParser,
    load_sessions_from_directory,
    parse_session,
)
from .prebuilt import (
    SessionPool,
    prebuild_session,
    prebuild_turn,
)
from .server import FakeLLMServer
from .stats import StatsCollector

__all__ = [
    # Models
    "Turn",
    "ToolCall",
    "SessionMetadata",
    "PrebuiltTurn",
    "PrebuiltSession",
    # Parser
    "SessionParser",
    "BrowserSessionParser",
    "parse_session",
    "load_sessions_from_directory",
    # Config
    "Config",
    "ServerConfig",
    "TimingConfig",
    "SessionsConfig",
    "StatsConfig",
    "LoggingConfig",
    "create_default_config",
    "merge_cli_args",
    # Prebuilt
    "SessionPool",
    "prebuild_turn",
    "prebuild_session",
    # Stats
    "StatsCollector",
    # Server
    "FakeLLMServer",
]
