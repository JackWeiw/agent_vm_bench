#!/usr/bin/env python3
"""
E2B Sandbox Bench - Main Entry Point

Integrates all components, runs test workflow:
Create sandboxes -> Warmup -> Start stats -> Start tasks -> Run duration -> Stop -> Report

Supports multiple modes:
1. Full workflow: create -> port check -> warmup -> tasks -> stats
2. Create-only: create -> port check -> exit (Phase 0)
3. Detect existing: detect -> warmup -> tasks -> stats
4. Warmup-only: create/detect -> warmup -> exit
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
    elif config.warmup_only:
        print(f"  Mode:     Warmup-only")
    else:
        print(f"  Mode:     Full workflow")

    print(f"  Total:    {config.total_count} sandboxes")

    # Batch config display
    if config.create_batch_size:
        print(f"  Create Batch: {config.create_batch_count} batches x {config.create_batch_size} (interval {config.create_batch_interval}s)")
    else:
        print(f"  Create Batch: Full concurrent creation")

    if not config.create_only:
        if config.task_batch_size:
            print(f"  Task Batch:   {config.task_batch_count} batches x {config.task_batch_size} (interval {config.task_batch_interval}s)")
        else:
            print(f"  Task Batch:   Full concurrent start")

        # Warmup config display
        if config.warmup_urls:
            print(f"  Warmup:       {len(config.warmup_urls)} pages x {config.warmup_loops} loops (delay {config.warmup_delay}s)")

    print(f"  Duration: {config.test_duration}s")

    # Benchmark percent display
    if config.benchmark_percent < 1.0:
        benchmark_count = config.benchmark_count
        print(f"  Benchmark: {benchmark_count}/{config.total_count} sandboxes ({config.benchmark_percent*100:.0f}%)")

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

    # 3. Warmup phase (only if warmup-only mode)
    # Benchmark phase skips warmup - assumes sandboxes already warmed up
    task_manager = TaskManager(config, sandbox_states, stop_event)

    if config.warmup_only and config.warmup_urls:
        print("\n[Phase 2] Running warmup phase...")
        task_manager.start_warmup()
        warmup_start = time.time()

        completed, failed = task_manager.wait_warmup(timeout=300)
        warmup_duration = time.time() - warmup_start

        print(f"\nWarmup completed: {completed} sandboxes | {failed} failed | duration {warmup_duration:.1f}s")
        print("\n[Phase 2 Complete] Warmup-only mode finished.")
        print(f"  Warmup completed: {completed}/{ready_count}")
        print(f"  Sandboxes left running for later benchmark.")
        return {
            'report': f"Warmup-only: {completed}/{ready_count} sandboxes warmed up",
            'filepath': None
        }

    # 4. Benchmark phase (no warmup, just benchmark)
    # Mark all sandboxes as warmup_done so they can start benchmark immediately
    if not config.warmup_only:
        # If warmup_urls is configured but not warmup-only, assume sandboxes need warmup_done
        # For benchmark phase, mark all ready sandboxes as warmup_done (skip warmup)
        for state in sandbox_states.values():
            if state.creation_metrics.status == SandboxStatus.PORT_READY:
                state.warmup_done = True

    # 5. Start statistics collection
    print("\n[Phase 3] Starting stats collector...")
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()

    # 6. Start task execution (with batch control and benchmark_percent)
    benchmark_count = max(1, int(ready_count * config.benchmark_percent))
    if config.benchmark_percent < 1.0:
        print(f"\n[Phase 4] Starting browser tasks on {benchmark_count}/{ready_count} sandboxes ({config.benchmark_percent*100:.0f}%)...")
    else:
        print(f"\n[Phase 4] Starting browser tasks...")
    task_manager.start_all()

    # 7. Run for specified duration
    print(f"\n[Phase 5] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt, stopping...")

    # 8. Stop all components
    print("\n[Phase 6] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()

    # Only kill if we created the sandboxes (not in detect mode)
    if not config.detect_existing:
        sandbox_manager.kill_all()
    else:
        print("Sandboxes left running (detect mode - not killing)")

    time.sleep(0.5)  # Allow daemon threads to complete output

    # 9. Generate and save report
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
    parser.add_argument('-c', '--config', type=str, default=None,
                        help='YAML configuration file path')

    # E2B environment variables
    parser.add_argument('--e2b-access-token', type=str, help='E2B access token')
    parser.add_argument('--e2b-api-key', type=str, help='E2B API key')
    parser.add_argument('--e2b-domain', type=str, help='E2B domain')
    parser.add_argument('--e2b-api-url', type=str, help='E2B API URL')
    parser.add_argument('--e2b-http-ssl', type=str, help='E2B HTTP SSL setting')

    # Sandbox configuration
    parser.add_argument('-t', '--template', type=str, help='E2B template name')
    parser.add_argument('-n', '--total', type=int, help='Total sandbox count')
    parser.add_argument('--create-timeout', type=int, help='Sandbox creation timeout')
    parser.add_argument('-d', '--detect', action='store_true', help='Detect existing sandboxes instead of creating new ones')
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

    # Warmup configuration
    parser.add_argument('-w', '--warmup-url', type=str, action='append', help='Warmup page URL (can specify multiple)')
    parser.add_argument('--warmup-loops', type=int, default=2, help='Warmup loop count')
    parser.add_argument('--warmup-delay', type=int, default=10, help='Warmup page delay (seconds)')
    parser.add_argument('-wp', '--warmup-only', action='store_true', help='Run warmup phase only, then exit')

    # Benchmark control
    parser.add_argument('-bp', '--benchmark-percent', type=float, default=1.0, help='Percentage of sandboxes for benchmark (e.g., 0.5 = 50%%)')

    # Test run
    parser.add_argument('--duration', type=int, help='Test duration seconds')
    parser.add_argument('--stats-interval', type=int, help='Stats snapshot interval')

    # Report
    parser.add_argument('-o', '--output-dir', type=str, help='Report output directory')
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