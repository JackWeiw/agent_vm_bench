#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
__main__.py - CLI entry point for LLM Replay Server.

Usage:
    python -m llm_replay --sessions-dir ./sessions --port 5199
    python -m llm_replay --config llm_replay/config/llm_replay.yaml
    python -m llm_replay --sessions ./sessions/browser-3.jsonl --port 5199
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .config import Config, create_default_config, merge_cli_args
from .prebuilt import SessionPool
from .server import FakeLLMServer


def setup_logging(config: Config):
    """Configure logging based on config."""
    level = getattr(logging, config.logging.level.upper(), logging.INFO)

    # Reset root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(
        logging.Formatter(
            config.logging.format,
            config.logging.date_format,
        )
    )
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if config.logging.file_enabled and config.logging.file_path:
        try:
            file_handler = logging.FileHandler(config.logging.file_path)
            file_handler.setLevel(level)
            file_handler.setFormatter(
                logging.Formatter(
                    config.logging.format,
                    config.logging.date_format,
                )
            )
            root_logger.addHandler(file_handler)
        except Exception as e:
            logging.warning(f"Failed to open log file: {e}")


def print_banner(config: Config, session_pool: SessionPool):
    """Print startup banner."""
    print("=" * 70)
    print("LLM Replay Server - High-Performance Session Replay")
    print("=" * 70)
    print(f"Sessions Directory: {config.sessions.directory}")
    print(f"Sessions Loaded:    {len(session_pool)}")

    for name in session_pool.list_session_names():
        session = session_pool.get_session(name)
        if session:
            print(
                f"  - {name}: {len(session)} turns, "
                f"{session.metadata.total_tool_calls} tool_calls, "
                f"{session.metadata.total_llm_time_ms / 1000:.1f}s LLM time"
            )

    print(f"Total Turns:        {session_pool.get_total_turns()}")
    print(f"Total Tool Calls:   {session_pool.get_total_tool_calls()}")
    print(f"Total LLM Time:     {session_pool.get_total_llm_time_ms() / 1000:.1f}s")
    print("-" * 70)
    print(f"Listen Address:     http://{config.server.host}:{config.server.port}")
    print(f"OpenAI BaseURL:     http://{config.server.host}:{config.server.port}/v1")
    print(f"Models Endpoint:    http://{config.server.host}:{config.server.port}/v1/models")
    print(f"Sessions Endpoint:  http://{config.server.host}:{config.server.port}/v1/sessions")
    print("-" * 70)
    print(f"Timing Scale:       {config.timing.scale}")
    print(f"No Sleep:           {config.timing.no_sleep}")
    print(f"Jitter:             {config.timing.jitter}s")
    print("-" * 70)
    print("Usage in Agent Config:")
    print(f"  base_url: http://{config.server.host}:{config.server.port}/v1")
    print(f"  model: <session_name>  # e.g., 'browser-session-3'")
    print("-" * 70)
    print("Press Ctrl+C to stop")
    print()


async def async_main(config: Config):
    """Async main function."""
    # Setup logging
    setup_logging(config)

    # Load sessions
    session_pool = SessionPool()

    # Resolve directory path relative to config file (if used) or cwd
    sessions_dir = Path(config.sessions.directory)
    if not sessions_dir.is_absolute():
        # Get the config file's directory if --config was used
        import sys

        config_dir = None
        for i, arg in enumerate(sys.argv):
            if arg == "--config" and i + 1 < len(sys.argv):
                config_path = Path(sys.argv[i + 1])
                if config_path.exists():
                    config_dir = config_path.parent
                break

        # Try relative to config file directory first
        if config_dir:
            config_sessions = config_dir / sessions_dir
            if config_sessions.exists():
                sessions_dir = config_sessions
            else:
                # Try relative to config file's parent (project root style)
                project_sessions = config_dir.parent / sessions_dir
                if project_sessions.exists():
                    sessions_dir = project_sessions

        # Fallback: try relative to cwd
        if not sessions_dir.exists():
            cwd_sessions = Path.cwd() / sessions_dir
            if cwd_sessions.exists():
                sessions_dir = cwd_sessions
            else:
                # Try relative to package location
                package_root = Path(__file__).parent
                pkg_sessions = package_root / sessions_dir
                if pkg_sessions.exists():
                    sessions_dir = pkg_sessions

    config.sessions.directory = str(sessions_dir)

    if config.sessions.explicit_sessions:
        # Resolve explicit session file paths
        resolved_paths = []
        for session_path in config.sessions.explicit_sessions:
            p = Path(session_path)
            if not p.is_absolute():
                # Try relative to cwd first
                cwd_path = Path.cwd() / p
                if cwd_path.exists():
                    p = cwd_path
                else:
                    # Try relative to package
                    pkg_path = Path(__file__).parent.parent / p
                    if pkg_path.exists():
                        p = pkg_path
            resolved_paths.append(str(p))
        config.sessions.explicit_sessions = resolved_paths

        # Load explicit session files
        session_pool.load_explicit(resolved_paths)
    else:
        # Load all sessions from directory
        session_pool.load_directory(str(sessions_dir))

    if len(session_pool) == 0:
        logging.error("No sessions loaded. Check your sessions directory or file paths.")
        logging.error(f"Sessions directory resolved to: {sessions_dir}")
        logging.error(f"Directory exists: {sessions_dir.exists()}")
        if sessions_dir.exists():
            jsonl_files = list(sessions_dir.glob("*.jsonl"))
            logging.error(f"Found {len(jsonl_files)} .jsonl files in directory")
            for f in jsonl_files:
                logging.error(f"  - {f.name}")
        else:
            logging.error("Directory does not exist")
        sys.exit(1)

    # Print banner
    print_banner(config, session_pool)

    # Start server
    server = FakeLLMServer(config, session_pool)

    try:
        await server.start()

        # Keep running until interrupted
        while True:
            await asyncio.sleep(3600)  # Sleep for 1 hour, wake up for Ctrl+C

    except KeyboardInterrupt:
        print("\n[Interrupted] Shutting down...")
        await server.stop()

        # Print final stats
        stats = server.get_stats_summary()
        print("\nFinal Statistics:")
        print(f"  Total Requests: {stats.get('total_requests', 0)}")
        print(f"  Total LLM Time Simulated: {stats.get('total_llm_time_s', 0):.1f}s")
        print(f"  Requests/Second: {stats.get('requests_per_second', 0):.2f}")

        for name, s in stats.get("sessions", {}).items():
            print(f"  Session '{name}': {s.get('total_requests', 0)} requests")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="LLM Replay Server - High-performance session replay for agent benchmarking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Load all sessions from directory
  python -m llm_replay --sessions-dir ./sessions --port 5199

  # Load from config file
  python -m llm_replay --config llm_replay/config/llm_replay.yaml

  # Load specific session files
  python -m llm_replay --sessions ./sessions/browser-3.jsonl

  # Debug mode (fast, no sleep)
  python -m llm_replay --sessions-dir ./sessions --scale 0.1 --no-sleep

  # Production mode (real timing)
  python -m llm_replay --sessions-dir ./sessions --scale 1.0
""",
    )

    # Configuration
    parser.add_argument(
        "--config",
        type=str,
        help="Path to YAML configuration file",
    )

    # Sessions
    parser.add_argument(
        "--sessions-dir",
        type=str,
        default="./sessions",
        help="Directory containing session .jsonl files (default: ./sessions)",
    )
    parser.add_argument(
        "--sessions",
        type=str,
        help="Comma-separated list of explicit session files to load",
    )

    # Server
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Listen address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5199,
        help="Listen port (default: 5199)",
    )

    # Timing
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Time scaling factor (1.0=real speed, 0.1=10x faster)",
    )
    parser.add_argument(
        "--no-sleep",
        action="store_true",
        help="Skip timing simulation (for debugging)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.0,
        help="Random jitter in seconds (default: 0.0)",
    )

    # Stats
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="Disable statistics collection",
    )
    parser.add_argument(
        "--stats-dir",
        type=str,
        default="./results/llm_replay",
        help="Directory for stats export (default: ./results/llm_replay)",
    )

    # Logging
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce log output (WARNING level)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose log output (DEBUG level)",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        help="Path to log file",
    )

    args = parser.parse_args()

    # Build config
    if args.config:
        config = Config.from_yaml(args.config)
    else:
        config = create_default_config()

    # Merge CLI args
    config = merge_cli_args(
        config,
        host=args.host,
        port=args.port,
        scale=args.scale,
        no_sleep=args.no_sleep,
        sessions_dir=args.sessions_dir,
        sessions=args.sessions.split(",") if args.sessions else None,
        no_stats=args.no_stats,
        quiet=args.quiet,
        verbose=args.verbose,
    )

    # Override jitter
    config.timing.jitter = args.jitter

    # Override stats dir
    if args.stats_dir:
        config.stats.export_dir = args.stats_dir

    # Override log file
    if args.log_file:
        config.logging.file_enabled = True
        config.logging.file_path = args.log_file

    # Run async main
    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
