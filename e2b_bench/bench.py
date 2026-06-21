#!/usr/bin/env python3
"""
E2B Sandbox Bench - Main Entry Point

Integrates all components, runs test workflow:
Create sandboxes -> Start stats -> Start tasks -> Run duration -> Stop -> Report

Supports three modes:
1. Full workflow: create -> port check -> tasks -> stats
2. Create-only: create -> port check -> exit (Phase 0)
3. Detect existing: detect -> tasks -> stats
"""

import time
import argparse
import threading

from .config import Config
from .sandbox_manager import SandboxManager
from .task_runner import TaskManager
from .stats_collector import StatsCollector
from .schemas import SandboxStatus


def run_benchmark(config: Config) -> dict:
    """Run E2B sandbox performance test

    Args:
        config: Test configuration object

    Returns:
        {'report': str, 'filepath': str}
    """
    # 1. Setup E2B environment variables
    config.setup_e2b_env()

    print("=" * 80)
    print("E2B Sandbox Bench - Batch Performance Test")
    print("=" * 80)
    print(f"  Template: {config.template}")

    # Mode display
    if config.detect_existing:
        print(f"  Mode:     Detect existing sandboxes")
    elif config.create_only:
        print(f"  Mode:     Create-only (Phase 0)")
    else:
        print(f"  Mode:     Full workflow")

    print(f"  Total:    {config.total_count} sandboxes")

    # Batch config display
    if config.create_batch_size:
        print(f"  Create Batch: {config.create_batch_count} batches x {config.create_batch_size} (interval {config.create_batch_interval}s)")
    else:
        print(f"  Create Batch: Full concurrent creation")

    if not config.create_only and not config.detect_existing:
        if config.task_batch_size:
            print(f"  Task Batch:   {config.task_batch_count} batches x {config.task_batch_size} (interval {config.task_batch_interval}s)")
        else:
            print(f"  Task Batch:   Full concurrent start")

    print(f"  Duration: {config.test_duration}s")
    print("=" * 80)

    # Stop signal
    stop_event = threading.Event()

    # 2. Create or detect sandboxes
    sandbox_manager = SandboxManager(config, stop_event)

    if config.detect_existing:
        print("\n[Phase 1] Detecting existing sandboxes...")
        sandbox_states = sandbox_manager.detect_existing()
    else:
        print("\n[Phase 1] Creating sandboxes...")
        sandbox_states = sandbox_manager.create_all()

    ready_count = sum(
        1 for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.PORT_READY
    )
    if ready_count == 0:
        print("No sandboxes ready for testing, exiting.")
        return {}

    print(f"\nSandboxes ready: {ready_count}")

    # Create-only mode: exit after creation
    if config.create_only:
        print("\n[Phase 0 Complete] Create-only mode finished.")
        print(f"  Created: {len(sandbox_states)} sandboxes")
        print(f"  Ports Ready: {ready_count}")
        print(f"  Sandboxes left running for later use.")
        return {
            'report': f"Create-only: {ready_count}/{len(sandbox_states)} sandboxes ready",
            'filepath': None
        }

    # 3. Start statistics collection
    print("\n[Phase 2] Starting stats collector...")
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()

    # 4. Start task execution (with batch control)
    print("\n[Phase 3] Starting browser tasks...")
    task_manager = TaskManager(config, sandbox_states, stop_event)
    task_manager.start_all()

    # 5. Run for specified duration
    print(f"\n[Phase 4] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt, stopping...")

    # 6. Stop all components
    print("\n[Phase 5] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()

    # Only kill if we created the sandboxes (not in detect mode)
    if not config.detect_existing:
        sandbox_manager.kill_all()
    else:
        print("Sandboxes left running (detect mode - not killing)")

    time.sleep(0.5)  # Allow daemon threads to complete output

    # 7. Generate and save report
    report = stats_collector.generate_report()
    print("\n" + report)

    filepath = stats_collector.save_report(report)
    print(f"\nReport saved to: {filepath}")

    return {'report': report, 'filepath': filepath}


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(
        description='E2B Sandbox Bench - E2B Sandbox Batch Performance Testing Tool'
    )

    # Configuration file
    parser.add_argument('--config', type=str, default=None,
                        help='YAML configuration file path')

    # E2B environment variables
    parser.add_argument('--e2b-access-token', type=str, help='E2B access token')
    parser.add_argument('--e2b-api-key', type=str, help='E2B API key')
    parser.add_argument('--e2b-domain', type=str, help='E2B domain')
    parser.add_argument('--e2b-api-url', type=str, help='E2B API URL')
    parser.add_argument('--e2b-http-ssl', type=str, help='E2B HTTP SSL setting')

    # Sandbox configuration
    parser.add_argument('--template', type=str, help='E2B template name')
    parser.add_argument('--total', type=int, help='Total sandbox count')
    parser.add_argument('--create-timeout', type=int, help='Sandbox creation timeout')
    parser.add_argument('--detect', action='store_true', help='Detect existing sandboxes instead of creating new ones')
    parser.add_argument('--create-only', action='store_true', help='Create sandboxes only without running tasks (Phase 0)')

    # Create batch control
    parser.add_argument('--create-batch-size', type=int, help='Sandboxes per creation batch (None = full concurrent)')
    parser.add_argument('--create-batch-interval', type=int, help='Creation batch interval seconds')

    # Task batch control
    parser.add_argument('--task-batch-size', type=int, help='Sandboxes to start tasks per batch (None = full concurrent)')
    parser.add_argument('--task-batch-interval', type=int, help='Task batch interval seconds')

    # Browser task
    parser.add_argument('--browser-url', type=str, action='append', help='Browser URL (can specify multiple)')
    parser.add_argument('--browser-timeout', type=int, help='Browser task timeout')
    parser.add_argument('--browser-interval-min', type=float, help='Task interval minimum')
    parser.add_argument('--browser-interval-max', type=float, help='Task interval maximum')

    # Test run
    parser.add_argument('--duration', type=int, help='Test duration seconds')
    parser.add_argument('--stats-interval', type=int, help='Stats snapshot interval')

    # Report
    parser.add_argument('--output-dir', type=str, help='Report output directory')
    parser.add_argument('--filename-prefix', type=str, help='Report filename prefix')

    return parser


def main() -> None:
    """CLI entry point"""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Load configuration
    if args.config:
        config = Config.load_from_yaml(args.config)
        config = Config.merge_with_args(config, args)
    else:
        # Without config file, use CLI arguments
        config = Config.from_args(args)

    # Validate required parameters
    if not config.e2b_access_token and not args.config:
        print("Error: E2B access token is required. Use --e2b-access-token or --config")
        return

    # Run test
    run_benchmark(config)


if __name__ == "__main__":
    main()