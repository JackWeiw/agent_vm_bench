"""Host-agnostic benchmark spine (ported from ``e2b_bench.bench.run_benchmark``).

``run_benchmark`` drives any :class:`EnvironmentProvider` through the full
single-test flow: prepare -> create/detect -> (create-only | warmup-only |
benchmark) -> stop -> report. The four e2b-specific seams in the original spine
become provider calls:

  ``setup_e2b_env``           -> ``provider.prepare_env()``
  ``SandboxManager(...)``      -> injected ``provider``
  ``detect_from_file`` / file  -> ``provider.detect_from_ids()`` / ``save_ids()``
  ``kill_all``                 -> ``provider.cleanup_all()``

The wave-based warmup path (e2b's >100-sandbox create-in-waves optimisation) is
out of scope here: it belongs to the batch scheduler, which is a follow-on phase.
The kernel creates all sandboxes in one ``provider.create_all()`` call -- a
provider that needs to batch does so inside that method.

``main()`` is the host-agnostic smoke entry (``--provider fake``); real entries
live in ``e2b_bench`` / ``docker_bench``, which build their provider and call
:func:`run_benchmark` directly.
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Any

from bench_core.config import KernelConfig
from env_provider import EnvironmentProvider, SandboxInstance, SandboxStatus
from bench_core.schemas import BenchSandbox
from bench_core.stats_collector import StatsCollector
from bench_core.task_manager import TaskManager
from bench_core.round_robin import RoundRobinTaskManager
from bench_core.utils import calc_percentiles, setup_logging

logger = logging.getLogger(__name__)


def _promote(instances: dict[int, SandboxInstance], workflow_type: str) -> dict[int, BenchSandbox]:
    """Promote each provider :class:`SandboxInstance` to a kernel state.

    The provider returns lean lifecycle instances; the kernel attaches the
    workflow metrics + runtime counters it reads. ``from_instance`` copies the
    lifecycle fields generically, so the provider stays unaware of ``BenchSandbox``.
    """
    return {i: BenchSandbox.from_instance(s, workflow_type) for i, s in instances.items()}


def _print_header(config: KernelConfig, provider: EnvironmentProvider) -> None:
    """Print the run configuration banner (host-agnostic)."""
    lines = ["=" * 80, "Sandbox Bench - Performance Test", "=" * 80]
    lines.append(f"  Backend:   {provider.name}")
    lines.append(f"  Workflow:  {config.workflow_type}")
    if config.detect_existing:
        lines.append("  Mode:      Detect existing sandboxes")
    elif config.create_only:
        lines.append("  Mode:      Create-only (Phase 0)")
    elif config.warmup_only:
        lines.append("  Mode:      Warmup-only")
    else:
        lines.append("  Mode:      Full workflow")
    lines.append(f"  Total:     {config.total_count} sandboxes")

    if config.workflow_type == "coding":
        lines.append(f"  Project:   {config.coding_project_dir}")
        lines.append(f"  Language:  {config.coding_language}")
        lines.append(f"  Verify:    {'enabled' if not config.coding_skip_verify else 'skipped'}")
    elif config.workflow_type == "document":
        lines.append(f"  Case:      {config.document_case_kind.upper()}")
        lines.append(f"  Workspace: {config.document_workspace_dir}")
    elif config.workflow_type == "replay":
        lines.append(f"  Traj dir:  {config.replay_trajectory_dir}")
        lines.append(f"  Workdir:   {config.replay_workdir}")
        lines.append(f"  Mode:      {config.replay_mode}")
        lines.append(f"  Delay:     {config.replay_delay_scale}x")
    elif config.workflow_type != "browser":
        raise ValueError(f"Unsupported workflow_type: {config.workflow_type}")

    if config.create_batch_size:
        lines.append(
            f"  Create Batch: {config.create_batch_count} x {config.create_batch_size} "
            f"(interval {config.create_batch_interval}s)"
        )
    else:
        lines.append("  Create Batch: Full concurrent creation")
    if not config.create_only:
        if config.task_batch_size:
            lines.append(
                f"  Task Batch:   {config.task_batch_count} x {config.task_batch_size} "
                f"(interval {config.task_batch_interval}s)"
            )
        else:
            lines.append("  Task Batch:   Full concurrent start")
    lines.append(f"  Duration: {config.test_duration}s")

    if config.benchmark_mode == "round_robin":
        rounds_label = f"{config.round_count} rounds" if config.round_count else "unlimited"
        lines.append(f"  Benchmark Mode: round_robin ({rounds_label}, interval {config.round_interval}s)")
    else:
        lines.append("  Benchmark Mode: fixed")
        if config.benchmark_percent < 1.0:
            lines.append(
                f"  Benchmark: {config.benchmark_count}/{config.total_count} "
                f"({config.benchmark_percent * 100:.0f}%)"
            )
    logger.info("\n".join(lines))
    logger.info("=" * 80)


def _percentile_section(title: str, values: list[float], desc: str) -> list[str]:
    """Build a percentile section; empty when there are no values."""
    if not values:
        return []
    stats = calc_percentiles(values)
    return [
        f"\n[{title}]",
        f"  ({desc})" if desc else "",
        f"  Min: {stats['min']:.1f}s  Max: {stats['max']:.1f}s  Avg: {stats['avg']:.1f}s",
        f"  P50: {stats['p50']:.1f}s  P95: {stats['p95']:.1f}s  P99: {stats['p99']:.1f}s",
    ]


def _create_only_report(config: KernelConfig, instances: dict[int, SandboxInstance]) -> str:
    """Build the creation-timing report for create-only mode.

    Create-only exits before the stats collector starts, so it gets its own
    compact report (no snapshots, no task metrics). Uses the same
    :func:`calc_percentiles` helper as the full report.
    """
    ready = [s for s in instances.values() if s.creation_metrics.status == SandboxStatus.READY]
    failed = [s for s in instances.values() if s.creation_metrics.status == SandboxStatus.FAILED]
    ready_failed = [s for s in instances.values() if s.creation_metrics.status == SandboxStatus.READY_FAILED]

    lines = ["=" * 70, "Creation Timing Report", "=" * 70]
    lines.append("\n[Sandbox Status]")
    lines.append(f"  Total:              {len(instances)}")
    lines.append(f"  Ready:              {len(ready)}")
    lines.append(f"  Create Failed:      {len(failed)}")
    lines.append(f"  Ready Check Failed: {len(ready_failed)}")
    if failed:
        lines.append(f"  Create Failed IDs:  {[s.index for s in failed[:10]]}")
    if ready_failed:
        lines.append(f"  Ready Failed IDs:   {[s.index for s in ready_failed[:10]]}")

    create_times = [
        s.creation_metrics.create_elapsed
        for s in instances.values()
        if s.creation_metrics.create_elapsed > 0
        and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
    ]
    ready_check_times = [
        s.creation_metrics.ready_check_elapsed for s in ready if s.creation_metrics.ready_check_elapsed > 0
    ]
    total_times = [s.creation_metrics.total_elapsed for s in ready if s.creation_metrics.total_elapsed > 0]

    lines.extend(
        _percentile_section("Sandbox.create Performance", create_times, "create API call time, excluding ready check")
    )
    lines.extend(
        _percentile_section(
            "Ready Check Wait Performance",
            ready_check_times,
            "waiting for readiness (port probe / command / validate)",
        )
    )
    lines.extend(_percentile_section("Total Startup Performance", total_times, "sandbox.create + ready check"))
    lines.append("\n" + "=" * 70)
    # Drop blank placeholder lines from empty sections.
    return "\n".join(line for line in lines if line != "")


def run_benchmark(config: KernelConfig, provider: EnvironmentProvider) -> dict[str, Any]:
    """Run the host-agnostic single-test benchmark flow.

    Args:
        config: Kernel configuration (host-agnostic fields).
        provider: The sandbox backend, already constructed.

    Returns:
        ``{"report": str, "filepath": str | None}``, or ``{}`` when no sandboxes
        reached ready state.
    """
    config.validate()
    if config.workflow_type == "document":
        from bench_core.task_runner.document import preflight_document

        preflight_document(config)

    # 1. Provider-level setup (e2b sets SDK env vars; docker needs nothing).
    provider.prepare_env()

    # 1b. Cleanup-only: list + kill existing sandboxes, then exit. Tears down
    # what a prior --create-only / --detect run left running. Skips the
    # readiness probe (see provider.cleanup_existing) so a dead/service-down
    # sandbox can't stall teardown.
    if config.cleanup_only:
        logger.info("\n[Cleanup] Tearing down existing sandboxes...")
        killed = provider.cleanup_existing()
        logger.info(f"\nCleanup: tore down {killed} sandbox(es).")
        return {"report": f"Cleanup: tore down {killed} sandbox(es).", "filepath": None}

    _print_header(config, provider)

    stop_event = threading.Event()

    # 2. Create or detect sandboxes. detect mode prefers persisted IDs (when the
    # provider supports them) and falls back to live detection.
    if config.detect_existing:
        instances = provider.detect_from_ids()
        if instances is None:
            instances = provider.detect_existing()
        logger.info("\n[Phase 1] Detected existing sandboxes...")
    else:
        logger.info("\n[Phase 1] Creating sandboxes...")
        instances = provider.create_all()
    instances = dict(instances)

    ready_count = sum(1 for s in instances.values() if s.ready)
    if ready_count == 0:
        logger.info("No sandboxes ready for testing, exiting.")
        return {}
    logger.info(f"\nSandboxes ready: {ready_count}")

    # 3. Create-only: emit the creation timing report and leave sandboxes running.
    if config.create_only:
        logger.info("\n[Phase 0 Complete] Create-only mode finished.")
        report = _create_only_report(config, instances)
        logger.info("\n" + report)
        provider.save_ids(instances)
        return {"report": report, "filepath": None}

    # Promote to the kernel's working state (attaches workflow metrics).
    states = _promote(instances, config.workflow_type)

    # 4. Warmup-only: warm sandboxes and leave them running for a later run.
    if config.warmup_only:
        logger.info("\n[Phase 2] Running warmup phase...")
        warmup_mgr = TaskManager(config, states, stop_event, provider)
        warmup_mgr.start_warmup()
        completed, failed = warmup_mgr.wait_warmup(timeout=300)
        logger.info(f"\nWarmup completed: {completed} ready, {failed} failed")
        provider.save_ids(states)
        return {"report": f"Warmup-only: {completed}/{len(states)} ready, {failed} failed", "filepath": None}

    # 5. Benchmark phase assumes warm sandboxes; mark ready ones warmed-up.
    for state in states.values():
        if state.ready:
            state.warmup_done = True

    # 6. Stats collection (background snapshots + final report).
    logger.info("\n[Phase 3] Starting stats collector...")
    stats_collector = StatsCollector(config, states, provider.name)
    stats_collector.start()

    task_manager: TaskManager | None = None
    try:
        if config.benchmark_mode == "round_robin":
            logger.info("\n[Phase 4] Starting round-robin tasks...")
            round_robin = RoundRobinTaskManager(config, states, stop_event, stats_collector, provider)
            round_robin.run()
        else:
            workflow_label = config.workflow_type.capitalize()
            logger.info(f"\n[Phase 4] Starting {workflow_label} tasks...")
            task_manager = TaskManager(config, states, stop_event, provider)
            task_manager.start_all()
            logger.info(f"\n[Phase 5] Running for {config.test_duration} seconds...")
            try:
                time.sleep(config.test_duration)
            except KeyboardInterrupt:
                logger.info("\nUser interrupt, stopping...")
    except Exception:
        stop_event.set()
        stats_collector.stop()
        if not config.detect_existing:
            provider.cleanup_all()
        raise

    # 7. Stop all components.
    logger.info("\n[Phase 6] Stopping...")
    stop_event.set()
    if task_manager is not None:
        try:
            task_manager.wait_all(timeout=5)
        except Exception:
            stats_collector.stop()
            if not config.detect_existing:
                provider.cleanup_all()
            raise
    # Document workflow: force a final snapshot so the report captures the last
    # task's metrics (document runners are one-shot per round / fixed iteration).
    if config.workflow_type == "document":
        stats_collector._take_snapshot()
    stats_collector.stop()

    # Only tear down sandboxes the kernel created; detect mode leaves them running.
    if not config.detect_existing:
        provider.cleanup_all()
    else:
        logger.info("Sandboxes left running (detect mode - not killing)")

    time.sleep(0.5)  # Let daemon threads finish writing output.

    # 8. Generate and save the report.
    report = stats_collector.generate_report()
    filepath = stats_collector.save_report(report)
    logger.info("\n" + report)
    logger.info(f"\nReport saved to: {filepath}")
    return {"report": report, "filepath": filepath}


def load_config(path: str | Path) -> tuple[KernelConfig, dict[str, Any]]:
    """Load a :class:`KernelConfig` from a YAML file (unified schema).

    Reads the shared stress sections via :meth:`KernelConfig.from_raw`, so the
    kernel picks up ``create_batch`` / ``test`` / ``browser`` / ``sandbox``
    instead of running on defaults. Returns the config plus the raw YAML dict;
    backend blocks (``e2b:`` / ``docker:``) are passed through for the provider
    to read -- the kernel never reads them.
    """
    import yaml

    with open(path, encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}

    return KernelConfig.from_raw(raw), raw


def build_arg_parser() -> argparse.ArgumentParser:
    """Host-agnostic CLI. Provider packages add their own flags on top."""
    parser = argparse.ArgumentParser(description="Host-agnostic benchmark kernel")
    parser.add_argument("--config", help="YAML config path")
    parser.add_argument("--provider", default="fake", choices=["fake", "e2b", "docker"])
    parser.add_argument("-n", "--total-count", type=int)
    parser.add_argument("--workflow-type", choices=["browser", "coding", "document", "replay"])
    parser.add_argument("-bm", "--benchmark-mode", choices=["fixed", "round_robin"])
    parser.add_argument("--round-count", type=int)
    parser.add_argument("--round-size", type=int)
    parser.add_argument("--test-duration", type=int)
    parser.add_argument("--benchmark-percent", type=float)
    parser.add_argument("--create-only", action="store_true")
    parser.add_argument("--detect", action="store_true")
    parser.add_argument("--warmup-only", action="store_true")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="list + kill all existing sandboxes (teardown of a prior "
        "--create-only/--detect run that left them running), then exit",
    )
    parser.add_argument("-o", "--output-dir")
    return parser


def _build_provider(name: str, config: KernelConfig, raw_config: dict[str, Any]) -> EnvironmentProvider:
    """Construct a provider by name.

    All providers live as submodules of ``env_provider`` (``e2b``, ``docker``,
    ``fake``) and are lazy-imported here -- the kernel never imports a backend
    statically, so the layering rule (bench_core must not import e2b_bench /
    docker_bench) holds. ``env_provider``'s contract stays SDK-free; loading a
    provider submodule is what pulls in that backend's SDK.
    """
    if name == "fake":
        from env_provider.fake import FakeProvider

        return FakeProvider(count=config.total_count)
    if name == "e2b":
        from env_provider.e2b import build_provider
    elif name == "docker":
        from env_provider.docker import build_provider
    else:
        raise ValueError(f"Unknown provider: {name}")
    return build_provider(config, raw_config)


def main() -> None:
    """CLI entry point (host-agnostic smoke via ``--provider fake``)."""
    setup_logging()
    args = build_arg_parser().parse_args()

    if args.config:
        config, raw = load_config(args.config)
    else:
        config, raw = KernelConfig(), {}

    # CLI overrides (only when explicitly set).
    if args.total_count is not None:
        config.total_count = args.total_count
    if args.workflow_type:
        config.workflow_type = args.workflow_type
    if args.benchmark_mode:
        config.benchmark_mode = args.benchmark_mode
    if args.round_count is not None:
        config.round_count = args.round_count
    if args.round_size is not None:
        config.round_size = args.round_size
    if args.test_duration is not None:
        config.test_duration = args.test_duration
    if args.benchmark_percent is not None:
        config.benchmark_percent = args.benchmark_percent
    if args.create_only:
        config.create_only = True
    if args.detect:
        config.detect_existing = True
    if args.warmup_only:
        config.warmup_only = True
    if args.cleanup:
        config.cleanup_only = True
    if args.output_dir:
        config.output_dir = args.output_dir

    provider = _build_provider(args.provider, config, raw)
    run_benchmark(config, provider)


if __name__ == "__main__":
    main()
