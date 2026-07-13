#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py - Configuration management for LLM Replay Server.

Provides YAML-based configuration with CLI override support.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any
import yaml


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    host: str = "0.0.0.0"
    port: int = 5199
    max_connections: int = 500
    request_timeout: int = 60
    keep_alive_timeout: int = 30

    # Performance tuning
    tcp_nodelay: bool = True
    buffer_size: int = 65536  # 64KB


@dataclass
class TimingConfig:
    """Timing simulation configuration."""

    scale: float = 1.0
    no_sleep: bool = False
    jitter: float = 0.0  # Random jitter in seconds


@dataclass
class SessionsConfig:
    """Sessions loading configuration."""

    directory: str = "./sessions"
    parser_type: str = "auto"  # auto, browser-session, openai-session

    # Optional: explicit session list (overrides directory scan)
    explicit_sessions: Optional[List[str]] = None


@dataclass
class StatsConfig:
    """Statistics collection configuration."""

    enabled: bool = True
    export_dir: str = "./results/llm_replay"
    export_format: str = "excel"  # excel, json, both
    batch_write_interval: int = 10  # Seconds between batch writes
    per_connection_stats: bool = True


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    format: str = "[%(asctime)s] [%(levelname)s] %(message)s"
    date_format: str = "%H:%M:%S"

    # File logging
    file_enabled: bool = False
    file_path: Optional[str] = None


@dataclass
class Config:
    """
    Main configuration for LLM Replay Server.

    Can be loaded from YAML file or constructed directly.
    """

    server: ServerConfig = field(default_factory=ServerConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        """
        Load configuration from YAML file.

        Args:
            path: Path to YAML configuration file

        Returns:
            Config object

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If YAML parsing fails
        """
        path = os.path.abspath(path)

        if not os.path.isfile(path):
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Build Config from dictionary."""
        server_data = data.get("server", {})
        timing_data = data.get("timing", {})
        sessions_data = data.get("sessions", {})
        stats_data = data.get("stats", {})
        logging_data = data.get("logging", {})

        return cls(
            server=ServerConfig(
                host=server_data.get("host", "0.0.0.0"),
                port=server_data.get("port", 5199),
                max_connections=server_data.get("max_connections", 500),
                request_timeout=server_data.get("request_timeout", 60),
                keep_alive_timeout=server_data.get("keep_alive_timeout", 30),
                tcp_nodelay=server_data.get("tcp_nodelay", True),
                buffer_size=server_data.get("buffer_size", 65536),
            ),
            timing=TimingConfig(
                scale=timing_data.get("scale", 1.0),
                no_sleep=timing_data.get("no_sleep", False),
                jitter=timing_data.get("jitter", 0.0),
            ),
            sessions=SessionsConfig(
                directory=sessions_data.get("directory", "./sessions"),
                parser_type=sessions_data.get("parser_type", "auto"),
                explicit_sessions=sessions_data.get("explicit_sessions"),
            ),
            stats=StatsConfig(
                enabled=stats_data.get("enabled", True),
                export_dir=stats_data.get("export_dir", "./results/llm_replay"),
                export_format=stats_data.get("export_format", "excel"),
                batch_write_interval=stats_data.get("batch_write_interval", 10),
                per_connection_stats=stats_data.get("per_connection_stats", True),
            ),
            logging=LoggingConfig(
                level=logging_data.get("level", "INFO"),
                format=logging_data.get("format", "[%(asctime)s] [%(levelname)s] %(message)s"),
                date_format=logging_data.get("date_format", "%H:%M:%S"),
                file_enabled=logging_data.get("file_enabled", False),
                file_path=logging_data.get("file_path"),
            ),
        )

    def to_yaml(self, path: str) -> None:
        """
        Save configuration to YAML file.

        Args:
            path: Path to save YAML file
        """
        data = {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "max_connections": self.server.max_connections,
                "request_timeout": self.server.request_timeout,
                "keep_alive_timeout": self.server.keep_alive_timeout,
                "tcp_nodelay": self.server.tcp_nodelay,
                "buffer_size": self.server.buffer_size,
            },
            "timing": {
                "scale": self.timing.scale,
                "no_sleep": self.timing.no_sleep,
                "jitter": self.timing.jitter,
            },
            "sessions": {
                "directory": self.sessions.directory,
                "parser_type": self.sessions.parser_type,
                "explicit_sessions": self.sessions.explicit_sessions,
            },
            "stats": {
                "enabled": self.stats.enabled,
                "export_dir": self.stats.export_dir,
                "export_format": self.stats.export_format,
                "batch_write_interval": self.stats.batch_write_interval,
                "per_connection_stats": self.stats.per_connection_stats,
            },
            "logging": {
                "level": self.logging.level,
                "format": self.logging.format,
                "date_format": self.logging.date_format,
                "file_enabled": self.logging.file_enabled,
                "file_path": self.logging.file_path,
            },
        }

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def create_default_config() -> Config:
    """Create a default configuration."""
    return Config()


def merge_cli_args(config: Config, **kwargs) -> Config:
    """
    Merge CLI arguments into existing config.

    CLI args override config file values.

    Args:
        config: Existing Config object
        **kwargs: CLI argument overrides

    Returns:
        Updated Config object
    """
    # Server overrides
    if "host" in kwargs and kwargs["host"]:
        config.server.host = kwargs["host"]
    if "port" in kwargs and kwargs["port"]:
        config.server.port = kwargs["port"]

    # Timing overrides
    if "scale" in kwargs and kwargs["scale"]:
        config.timing.scale = kwargs["scale"]
    if "no_sleep" in kwargs and kwargs["no_sleep"]:
        config.timing.no_sleep = True

    # Sessions overrides
    if "sessions_dir" in kwargs and kwargs["sessions_dir"]:
        config.sessions.directory = kwargs["sessions_dir"]
    if "sessions" in kwargs and kwargs["sessions"]:
        config.sessions.explicit_sessions = kwargs["sessions"]

    # Stats overrides
    if "no_stats" in kwargs and kwargs["no_stats"]:
        config.stats.enabled = False

    # Logging overrides
    if "quiet" in kwargs and kwargs["quiet"]:
        config.logging.level = "WARNING"
    if "verbose" in kwargs and kwargs["verbose"]:
        config.logging.level = "DEBUG"

    return config


# Export LoggingConfig for external use
__all__ = [
    "Config",
    "ServerConfig",
    "TimingConfig",
    "SessionsConfig",
    "StatsConfig",
    "LoggingConfig",
    "create_default_config",
    "merge_cli_args",
]
