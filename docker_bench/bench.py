#!/usr/bin/env python3
"""
Docker Container Bench - Main Entry Point

Integrates all components, runs test workflow:
Create containers -> Check ports -> Start stats -> Start tasks -> Run duration -> Stop -> Report

Supports multiple modes:
1. Full workflow: create -> port check -> tasks -> stats
2. Create-only: create -> port check -> exit (Phase 0)
3. Detect existing: detect -> tasks -> stats

Browser workflow (5 steps = 1 query):
  Step 1: openclaw browser open [URL] --label [NAME]  → Page open
  Step 2: openclaw browser focus [TAB_ID]             → Tab focus
  Step 3: openclaw browser snapshot --limit 200       → DOM snapshot
  Step 4: openclaw browser click e218                 → Element click (retry)
  Step 5: openclaw browser screenshot                 → Visual screenshot
"""

import argparse
import logging
import threading
import time

from .config import Config
from .container_manager import ContainerManager
from .schemas import ContainerStatus
from .stats_collector import StatsCollector
from .task_runner import TaskManager
from .utils import setup_logging

logger = logging.getLogger(__name__)


def run_benchmark(config: Config) -> dict:
    """Run Docker container browser performance test

    Args:
        config: Test configuration object

    Returns:
        {'report': str, 'filepath': str}
    """
    logger.info("=" * 80)
    logger.info("Docker Container Bench - Browser Automation Performance Test")
    logger.info("=" * 80)
    logger.info(f"  Image:    {config.docker_image}")
    logger.info(f"  Spec:     {config.cpu_limit}vCPU / {config.memory_limit}")

    # Mode display
    if config.detect_existing:
        logger.info("  Mode:     Detect existing containers")
    elif config.create_only:
        logger.info("  Mode:     Create-only (Phase 0)")
    else:
        logger.info("  Mode:     Full workflow")

    logger.info(f"  Total:    {config.total_count} containers")

    # Batch config display
    if config.create_batch_size:
        logger.info(
            f"  Create Batch: {config.create_batch_count} batches x {config.create_batch_size} (interval {config.create_batch_interval}s)"
        )
    else:
        logger.info("  Create Batch: Full concurrent creation")

    if not config.create_only:
        if config.task_batch_size:
            logger.info(
                f"  Task Batch:   {config.task_batch_count} batches x {config.task_batch_size} (interval {config.task_batch_interval}s)"
            )
        else:
            logger.info("  Task Batch:   Full concurrent start")

    logger.info(f"  Duration: {config.test_duration}s")

    # Benchmark percent display
    if config.benchmark_percent < 1.0:
        benchmark_count = config.benchmark_count
        logger.info(
            f"  Benchmark: {benchmark_count}/{config.total_count} containers ({config.benchmark_percent * 100:.0f}%)"
        )

    logger.info("\n  Browser Workflow (5 steps = 1 query):")
    logger.info("    1. open [URL] --label [NAME]")
    logger.info("    2. focus [TAB_ID]")
    logger.info("    3. snapshot --limit 200")
    logger.info("    4. click e218 (retry)")
    logger.info("    5. screenshot")

    logger.info("=" * 80)

    # Stop signal
    stop_event = threading.Event()

    # 1. Create or detect containers
    container_manager = ContainerManager(config, stop_event)

    if config.detect_existing:
        logger.info("\n[Phase 1] Detecting existing containers...")
        creation_start_time = time.time()
        container_states = container_manager.detect_existing()
        creation_end_time = time.time()
    else:
        logger.info("\n[Phase 1] Creating containers...")
        creation_start_time = time.time()
        container_states = container_manager.create_all()
        creation_end_time = time.time()

    ready_count = sum(1 for s in container_states.values() if s.creation_metrics.status == ContainerStatus.PORT_READY)
    if ready_count == 0:
        logger.info("No containers ready for testing, exiting.")
        return {}

    logger.info(f"\nContainers ready: {ready_count}")

    # Create-only mode: exit after creation with detailed timing report
    if config.create_only:
        logger.info("\n[Phase 0 Complete] Create-only mode finished.")
        logger.info(f"  Created: {len(container_states)} containers")
        logger.info(f"  Ports Ready: {ready_count}")
        logger.info("  Containers left running for later use.")

        # Generate creation timing report
        from .utils import calc_percentiles

        # Container status statistics
        ready_states = [s for s in container_states.values() if s.creation_metrics.status == ContainerStatus.PORT_READY]
        failed_states = [s for s in container_states.values() if s.creation_metrics.status == ContainerStatus.FAILED]
        port_failed_states = [
            s for s in container_states.values() if s.creation_metrics.status == ContainerStatus.PORT_FAILED
        ]

        logger.info("\n" + "=" * 70)
        logger.info("Creation Timing Report")
        logger.info("=" * 70)

        # Total elapsed time for all containers
        total_elapsed = creation_end_time - creation_start_time
        logger.info("\n[Overall Creation Time]")
        logger.info(f"  Total Wall Clock Time: {total_elapsed:.1f}s")
        logger.info("  (From first container creation start to last container port ready)")
        logger.info(f"  Throughput: {len(container_states) / total_elapsed:.2f} containers/sec")

        logger.info("\n[Container Status]")
        logger.info(
            f"  Created (Docker):   {len([s for s in container_states.values() if s.creation_metrics.status not in (ContainerStatus.PENDING, ContainerStatus.CREATING)])} / {len(container_states)}"
        )
        logger.info(f"  Ports Ready:        {len(ready_states)} / {len(container_states)}")
        logger.info(f"  Create Failed:      {len(failed_states)}")
        logger.info(f"  Port Check Failed:  {len(port_failed_states)}")
        if failed_states:
            logger.info(f"  Create Failed IDs:  {[s.container_id for s in failed_states[:10]]}")
        if port_failed_states:
            logger.info(f"  Port Failed IDs:    {[s.container_id for s in port_failed_states[:10]]}")

        # Container creation performance statistics
        create_times = [
            s.creation_metrics.create_elapsed
            for s in container_states.values()
            if s.creation_metrics.create_elapsed > 0
            and s.creation_metrics.status
            not in (ContainerStatus.FAILED, ContainerStatus.PENDING, ContainerStatus.CREATING)
        ]
        if create_times:
            stats = calc_percentiles(create_times)
            logger.info("\n[Container Creation Performance]")
            logger.info("  (docker run elapsed time, excluding port wait)")
            logger.info(f"  Min:  {stats['min']:.1f}s")
            logger.info(f"  Max:  {stats['max']:.1f}s")
            logger.info(f"  Avg:  {stats['avg']:.1f}s")
            logger.info(f"  P50:  {stats['p50']:.1f}s")
            logger.info(f"  P95:  {stats['p95']:.1f}s")
            logger.info(f"  P99:  {stats['p99']:.1f}s")

        # Port wait performance statistics
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in ready_states if s.creation_metrics.port_wait_elapsed > 0
        ]
        if port_wait_times:
            stats = calc_percentiles(port_wait_times)
            logger.info("\n[Port Check Wait Performance]")
            logger.info(f"  (Waiting for {config.required_ports} ports)")
            logger.info(f"  Min:  {stats['min']:.1f}s")
            logger.info(f"  Max:  {stats['max']:.1f}s")
            logger.info(f"  Avg:  {stats['avg']:.1f}s")
            logger.info(f"  P50:  {stats['p50']:.1f}s")
            logger.info(f"  P95:  {stats['p95']:.1f}s")
            logger.info(f"  P99:  {stats['p99']:.1f}s")

        # Total startup time (create + port_wait)
        total_times = [s.creation_metrics.total_elapsed for s in ready_states if s.creation_metrics.total_elapsed > 0]
        if total_times:
            stats = calc_percentiles(total_times)
            logger.info("\n[Total Startup Performance]")
            logger.info("  (Container creation + port wait)")
            logger.info(f"  Min:  {stats['min']:.1f}s")
            logger.info(f"  Max:  {stats['max']:.1f}s")
            logger.info(f"  Avg:  {stats['avg']:.1f}s")
            logger.info(f"  P50:  {stats['p50']:.1f}s")
            logger.info(f"  P95:  {stats['p95']:.1f}s")
            logger.info(f"  P99:  {stats['p99']:.1f}s")

        logger.info("\n" + "=" * 70)
        return {"report": f"Create-only: {ready_count}/{len(container_states)} containers ready", "filepath": None}

    # 2. Start statistics collection
    logger.info("\n[Phase 2] Starting stats collector...")
    stats_collector = StatsCollector(config, container_states)
    stats_collector.start()

    # 3. Start task execution (with batch control and benchmark_percent)
    task_manager = TaskManager(config, container_states, stop_event)
    benchmark_count = max(1, int(ready_count * config.benchmark_percent))
    if config.benchmark_percent < 1.0:
        logger.info(
            f"\n[Phase 3] Starting browser tasks on {benchmark_count}/{ready_count} containers ({config.benchmark_percent * 100:.0f}%)..."
        )
    else:
        logger.info("\n[Phase 3] Starting browser tasks...")
    task_manager.start_all()

    # 4. Run for specified duration
    logger.info(f"\n[Phase 4] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        logger.info("\nUser interrupt, stopping...")

    # 5. Stop all components
    logger.info("\n[Phase 5] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()

    # Only remove if we created the containers (not in detect mode)
    if not config.detect_existing:
        container_manager.remove_all()
    else:
        logger.info("Containers left running (detect mode - not removing)")

    time.sleep(0.5)  # Allow daemon threads to complete output

    # 6. Generate and save report
    report = stats_collector.generate_report()
    # The report is the benchmark deliverable (program output), not a log
    # line: print it to stdout so it stays visible regardless of log level.
    print("\n" + report)

    filepath = stats_collector.save_report(report)
    logger.info(f"\nReport saved to: {filepath}")

    return {"report": report, "filepath": filepath}


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(
        description="Docker Container Bench - Browser Automation Batch Performance Testing Tool"
    )

    # Configuration file
    parser.add_argument("-c", "--config", type=str, default=None, help="YAML configuration file path")

    # Docker configuration
    parser.add_argument("--image", type=str, help="Docker image name")
    parser.add_argument("--prefix", type=str, help="Container name prefix")
    parser.add_argument("--cpu", type=float, help="CPU limit per container (--cpus)")
    parser.add_argument("--memory", type=str, help="Memory limit per container (-m)")
    parser.add_argument("--create-timeout", type=int, help="Container creation timeout")

    # Container configuration
    parser.add_argument("-n", "--total", type=int, help="Total container count")
    parser.add_argument(
        "-d", "--detect", action="store_true", help="Detect existing containers instead of creating new ones"
    )
    parser.add_argument(
        "--create-only", action="store_true", help="Create containers only without running tasks (Phase 0)"
    )

    # Create batch control
    parser.add_argument("--create-batch-size", type=int, help="Containers per creation batch (None = full concurrent)")
    parser.add_argument("--create-batch-interval", type=int, help="Creation batch interval seconds")

    # Task batch control
    parser.add_argument(
        "--task-batch-size", type=int, help="Containers to start tasks per batch (None = full concurrent)"
    )
    parser.add_argument("--task-batch-interval", type=int, help="Task batch interval seconds")

    # Browser task
    parser.add_argument("--browser-url", type=str, action="append", help="Browser URL (can specify multiple)")
    parser.add_argument("--browser-timeout", type=int, help="Browser task timeout")
    parser.add_argument("--browser-interval-min", type=float, help="Task interval minimum")
    parser.add_argument("--browser-interval-max", type=float, help="Task interval maximum")

    # Benchmark control
    parser.add_argument(
        "-bp",
        "--benchmark-percent",
        type=float,
        default=None,
        help="Percentage of containers for benchmark (e.g., 0.5 = 50%%)",
    )

    # Test run
    parser.add_argument("--duration", type=int, help="Test duration seconds")
    parser.add_argument("--stats-interval", type=int, help="Stats snapshot interval")

    # Report
    parser.add_argument("-o", "--output-dir", type=str, help="Report output directory")
    parser.add_argument("--filename-prefix", type=str, help="Report filename prefix")

    return parser


def main() -> None:
    """CLI entry point"""
    setup_logging()
    parser = build_arg_parser()
    args = parser.parse_args()

    # Load configuration
    if args.config:
        config = Config.load_from_yaml(args.config)
        config = Config.merge_with_args(config, args)
    else:
        # Without config file, use CLI arguments
        config = Config.from_args(args)

    # Run test
    run_benchmark(config)


if __name__ == "__main__":
    main()
