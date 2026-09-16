"""Statistics collection: real-time snapshots, terminal echo, and the final report.

Host-agnostic port of ``e2b_bench.stats_collector``. The collector reads
:class:`bench_core.schemas.BenchSandbox` state and a :class:`KernelConfig`; it
knows nothing about e2b or docker. The only provider-specific input is the
``provider_label`` string (e.g. ``"e2b"`` / ``"docker"``) shown in the report
header, threaded in by the benchmark spine.

RFC 0002 P3: per-workflow report rendering has collapsed onto the typed
``ReportFormatters`` strategy carried by each ``WorkflowSpec``. This host class
keeps only the workflow-agnostic scaffolding (config / status / percentile /
error / round-comparison sections, snapshot collection, generic ``Snapshot``
projection) and delegates every per-workflow surface
(``format_stats_section`` / ``format_step_timing`` / ``format_throughput_section``
/ ``format_trajectory_summary_section`` / ``format_round_extras`` /
``format_config_extras`` / ``format_snapshot_line``) to
``WORKFLOW_REGISTRY[config.workflow_type].report_formatters`` via a
:class:`ReportContext`. There is no ``workflow_type`` if/elif dispatch left in
the report path. Shared helpers (``ErrorClassifier``, ``TableFormatter``) live
in :mod:`bench_core.observability.report_helpers`; the import cycle into
``task_runner.*`` is dissolved because the per-workflow narrows no longer live
here.
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
import time
from datetime import datetime
from typing import Any

from bench_core.config import KernelConfig
from bench_core.observability.report_helpers import ErrorClassifier, TableFormatter
from bench_core.schemas import BenchSandbox, Snapshot
from bench_core.utils import (
    calc_p99,
    calc_percentiles,
    calc_tail_ratio,
    classify_tail_latency,
)
from bench_core.workflow_registry import WORKFLOW_REGISTRY, ReportContext, ensure_workflow_registered
from env_provider import SandboxStatus

logger = logging.getLogger(__name__)

# Replay lifecycle list accessors snapshotted per round so the per-round
# overhead table can slice each sandbox's lists between round boundaries.
# resume + pause over slice = lifecycle overhead; the three lists are
# appended in lockstep per slice, so they stay index-aligned across a round.
# running_slot_held_secs is sliced the same way for the per-round Slot held
# column (only meaningful under an admission controller).
_LIFECYCLE_ROUND_KEYS: tuple[str, ...] = (
    "resume_secs",
    "pause_secs",
    "slice_total_secs",
    "running_slot_held_secs",
)


class ReportFormatter:
    """Format statistics into a human-readable report.

    Host facade: renders the workflow-agnostic sections itself and delegates
    every per-workflow surface to ``self._fmt`` (the ``ReportFormatters``
    strategy from ``WORKFLOW_REGISTRY``), threading state through a frozen
    :class:`ReportContext` so the strategies never reach back into this class.
    """

    def __init__(
        self,
        config: KernelConfig,
        sandbox_states: dict[int, BenchSandbox],
        provider_label: str = "",
        admission_snapshot: dict | None = None,
        wall_sec: float | None = None,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.provider_label = provider_label
        self.admission_snapshot = admission_snapshot
        self.wall_sec = wall_sec
        # Lazy-register the active workflow's task_runner module so its spec (and
        # ReportFormatters strategy) is in WORKFLOW_REGISTRY before the lookup.
        # No-op for real runs (config.from_raw already registered it); fires the
        # import for tests that construct a config without going through from_raw.
        ensure_workflow_registered(config.workflow_type)
        self._fmt = WORKFLOW_REGISTRY[config.workflow_type].report_formatters

    @property
    def _ctx(self) -> ReportContext:
        """Frozen view of everything a per-workflow strategy needs to render."""
        return ReportContext(
            config=self.config,
            sandbox_states=self.sandbox_states,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self.wall_sec,
        )

    # ---- workflow-agnostic sections ----------------------------------------

    def format_config_section(self) -> list[str]:
        """Format test configuration section."""
        lines = ["=" * 80, "Sandbox Bench - Performance Report", "=" * 80]
        lines.append("\n[Test Configuration]")
        lines.append(f"  Backend:        {self.provider_label}")
        lines.append(f"  Total Sandboxes: {self.config.total_count}")
        # Per-workflow extras (e.g. document emits the Workflow + Document Case
        # lines); non-document formatters return [] so the section is unchanged.
        lines.extend(self._fmt.format_config_extras(self._ctx))

        # Mode
        if self.config.detect_existing:
            lines.append("  Mode:            Detect existing sandboxes")
        elif self.config.create_only:
            lines.append("  Mode:            Create-only (Phase 0)")
        else:
            lines.append("  Mode:            Full workflow")

        # Batch config
        if self.config.create_batch_size:
            lines.append(
                f"  Create Batch:    {self.config.create_batch_count} batches x {self.config.create_batch_size} sandboxes"
            )
            lines.append(f"  Create Interval: {self.config.create_batch_interval}s")
        else:
            lines.append("  Create Batch:    Full concurrent creation")

        if not self.config.create_only:
            if self.config.task_batch_size:
                lines.append(
                    f"  Task Batch:      {self.config.task_batch_count} batches x {self.config.task_batch_size} sandboxes"
                )
                lines.append(f"  Task Interval:   {self.config.task_batch_interval}s")
            else:
                lines.append("  Task Batch:      Full concurrent start")

        lines.append(f"  Test Duration:   {self.config.test_duration}s")
        return lines

    def format_sandbox_status_section(self) -> list[str]:
        """Format sandbox status section."""
        ready_states = [s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.READY]
        failed_states = [s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.FAILED]
        ready_failed_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.READY_FAILED
        ]
        offline_states = [s for s in self.sandbox_states.values() if not s.is_alive and not s.stopped_by_cleanup]

        # command_ready is a per-workflow class attr (browser probes ports,
        # coding/document/replay probe a command). No if/elif.
        command_ready = self._fmt.command_ready
        ready_label = "Command Ready" if command_ready else "Ports Ready"
        check_failed_label = "Ready Check Failed" if command_ready else "Port Check Failed"
        failed_ids_label = "Ready Failed IDs" if command_ready else "Port Failed IDs"

        lines = ["\n[Sandbox Status]"]
        lines.append(
            f"  Created (API):       {len([s for s in self.sandbox_states.values() if s.creation_metrics.status not in (SandboxStatus.PENDING, SandboxStatus.CREATING)])} / {len(self.sandbox_states)}"
        )
        lines.append(f"  {ready_label}:         {len(ready_states)} / {len(self.sandbox_states)}")
        lines.append(f"  Create Failed:       {len(failed_states)}")
        lines.append(f"  {check_failed_label}:   {len(ready_failed_states)}")
        lines.append(f"  Offline (runtime):   {len(offline_states)}")

        if failed_states:
            lines.append(f"  Create Failed IDs:   {[s.index for s in failed_states[:10]]}")
        if ready_failed_states:
            lines.append(f"  {failed_ids_label}:     {[s.index for s in ready_failed_states[:10]]}")
        if offline_states:
            lines.append(f"  Offline IDs:         {[s.index for s in offline_states[:10]]}")

        return lines

    def format_percentile_section(self, title: str, values: list[float], description: str = "") -> list[str]:
        """Format a percentile statistics section."""
        if not values:
            return []

        lines = [f"\n[{title}]"]
        if description:
            lines.append(f"  ({description})")

        stats = calc_percentiles(values)
        lines.append(f"  Min:  {stats['min']:.1f}s")
        lines.append(f"  Max:  {stats['max']:.1f}s")
        lines.append(f"  Avg:  {stats['avg']:.1f}s")
        lines.append(f"  P50:  {stats['p50']:.1f}s")
        lines.append(f"  P95:  {stats['p95']:.1f}s")
        lines.append(f"  P99:  {stats['p99']:.1f}s")

        return lines

    def format_error_section(self) -> list[str]:
        """Format error details and classification section."""
        failed_sandbox_errors: list[tuple[int, int, str]] = []
        for s in self.sandbox_states.values():
            metrics = s.task_metrics
            if metrics.failed_count > 0 and metrics.last_error:
                failed_sandbox_errors.append((s.index, metrics.failed_count, metrics.last_error))

        if not failed_sandbox_errors:
            return []

        failed_sandbox_errors.sort(key=lambda x: x[1], reverse=True)

        lines = ["\n[Failed Sandbox Error Details]"]
        lines.append(f"  Total sandboxes with task failures: {len(failed_sandbox_errors)}")
        lines.append("  (Top 10 sandboxes with most failures)")

        for sid, count, error in failed_sandbox_errors[:10]:
            error_display = error[:150] if len(error) > 150 else error
            lines.append(f"  Sandbox{sid}: {count} failures - {error_display}")

        # Error classification
        lines.append("\n[Error Type Classification]")
        error_counts, error_sandbox_ids = ErrorClassifier.aggregate(failed_sandbox_errors)

        headers = ["Error Type", "Count", "Sandboxes"]
        rows: list[list[str]] = []

        # error_display_order is a per-workflow class attr; the shared classifier
        # may bucket an error into a category this workflow does not display, so
        # fold those into Other instead of silently dropping them from the table.
        error_display_order = self._fmt.error_display_order
        unsupported_types = [error_type for error_type in error_counts if error_type not in error_display_order]
        for error_type in unsupported_types:
            error_counts["Other"] = error_counts.get("Other", 0) + error_counts.pop(error_type)
            error_sandbox_ids.setdefault("Other", []).extend(error_sandbox_ids.pop(error_type, []))

        for error_type in error_display_order:
            if error_type in error_counts:
                count = error_counts[error_type]
                sids = error_sandbox_ids[error_type][:5]
                sids_display = str(sids) + ("..." if len(error_sandbox_ids[error_type]) > 5 else "")
                rows.append([error_type, str(count), sids_display])

        lines.extend(TableFormatter.format_table(headers, rows))
        return lines

    def format_round_comparison_table(self, round_start_totals: dict[int, dict[str, Any]]) -> list[str]:
        """Format round comparison as a table."""
        if not round_start_totals:
            return []

        # Calculate round finals (includes post-last-round sentinel with tasks=0)
        round_finals = self._calculate_round_finals(round_start_totals)

        # Filter out rounds with tasks=0 (post-last-round baseline sentinel)
        active_rounds = {k: v for k, v in round_finals.items() if v["tasks"] > 0}

        # Single-round (or none): the per-round view is pure redundancy with
        # the cumulative task statistics -- suppress it. This is the common
        # replay case (round_count=1, round_size=total -> one all-concurrent
        # pass). Genuine multi-round runs keep the table.
        if len(active_rounds) <= 1:
            return []

        total_tasks = sum(r["tasks"] for r in active_rounds.values())

        lines = ["\n" + "=" * 80, "[Round Comparison]", "=" * 80]
        lines.append(f"\n  Summary: {total_tasks} tasks across {len(active_rounds)} rounds")

        headers = ["Round", "Tasks", "Success%", "Avg(s)", "P50(s)", "P95(s)", "P99(s)", "Tail"]
        rows: list[list[str]] = []

        for round_id in sorted(active_rounds.keys()):
            tasks = active_rounds[round_id]["tasks"]
            success = active_rounds[round_id]["success"]
            latencies = active_rounds[round_id]["latencies"]

            if latencies:
                stats = calc_percentiles(latencies)
                avg = stats["avg"]
                p50 = stats["p50"]
                p95 = stats["p95"]
                p99 = stats["p99"]
                tail_ratio = calc_tail_ratio(latencies)
                severity = classify_tail_latency(tail_ratio)
            else:
                avg = p50 = p95 = p99 = 0.0
                tail_ratio = 1.0
                severity = "N/A"

            rate = success / max(1, tasks) * 100 if tasks > 0 else 0.0
            rows.append(
                [
                    str(round_id),
                    str(tasks),
                    f"{rate:.1f}",
                    f"{avg:.2f}",
                    f"{p50:.2f}",
                    f"{p95:.2f}",
                    f"{p99:.2f}",
                    f"{tail_ratio:.2f}x ({severity})",
                ]
            )

        lines.extend(TableFormatter.format_table(headers, rows))

        # Per-round lifecycle overhead sub-table is a per-workflow strategy
        # surface (replay renders resume/pause/slice; others return []). The
        # active_rounds buckets for non-replay workflows simply lack the
        # resume/pause/slice keys, but the non-replay strategies never reach
        # for them -- the ABC default returns [].
        lines.extend(self._fmt.format_round_extras(self._ctx, active_rounds))

        return lines

    def _calculate_round_finals(self, round_start_totals: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Calculate final statistics for each round."""
        round_finals: dict[int, dict[str, Any]] = {}

        final_task_total = sum(s.task_metrics.total_tasks for s in self.sandbox_states.values())
        final_task_success = sum(s.task_metrics.success_count for s in self.sandbox_states.values())
        final_sandbox_latency_counts = {s.index: len(s.task_metrics.latencies) for s in self.sandbox_states.values()}
        # Final cumulative replay lifecycle list lengths (end boundary for the
        # last round); mirrors the per-round snapshot taken in set_round.
        final_replay_baselines: dict[int, dict[str, int]] = {}
        if self.config.workflow_type == "replay":
            for s in self.sandbox_states.values():
                rm = s.replay_metrics
                final_replay_baselines[s.index] = {k: len(getattr(rm, k)) for k in _LIFECYCLE_ROUND_KEYS}

        for round_id in sorted(round_start_totals.keys()):
            start_total = round_start_totals[round_id]["total"]
            start_success = round_start_totals[round_id]["success"]
            start_sandbox_latency_counts = round_start_totals[round_id]["sandbox_latency_counts"]
            start_replay_baselines = round_start_totals[round_id].get("replay_baselines", {})

            # Determine end values
            if round_id == max(round_start_totals.keys()):
                end_total = final_task_total
                end_success = final_task_success
                end_sandbox_latency_counts = final_sandbox_latency_counts
                end_replay_baselines = final_replay_baselines
            else:
                next_round = round_id + 1
                if next_round in round_start_totals:
                    end_total = round_start_totals[next_round]["total"]
                    end_success = round_start_totals[next_round]["success"]
                    end_sandbox_latency_counts = round_start_totals[next_round]["sandbox_latency_counts"]
                    end_replay_baselines = round_start_totals[next_round].get("replay_baselines", {})
                else:
                    end_total = final_task_total
                    end_success = final_task_success
                    end_sandbox_latency_counts = final_sandbox_latency_counts
                    end_replay_baselines = final_replay_baselines

            round_latencies: list[float] = []
            for s in self.sandbox_states.values():
                sandbox_index = s.index
                start_count = start_sandbox_latency_counts.get(sandbox_index, 0)
                end_count = end_sandbox_latency_counts.get(sandbox_index, len(s.task_metrics.latencies))
                round_latencies.extend(s.task_metrics.get_latencies_since(start_count)[: end_count - start_count])

            # Per-round replay lifecycle slices (resume/pause/slice/slot_held),
            # aligned by index within each sandbox. Empty for non-replay.
            round_resume: list[float] = []
            round_pause: list[float] = []
            round_slice: list[float] = []
            round_slot_held: list[float] = []
            if self.config.workflow_type == "replay":
                for s in self.sandbox_states.values():
                    rm = s.replay_metrics
                    rb_start = start_replay_baselines.get(s.index, {})
                    rb_end = end_replay_baselines.get(s.index, {})
                    for key, acc in (
                        ("resume_secs", round_resume),
                        ("pause_secs", round_pause),
                        ("slice_total_secs", round_slice),
                        ("running_slot_held_secs", round_slot_held),
                    ):
                        sn = rb_start.get(key, 0)
                        en = rb_end.get(key, len(getattr(rm, key)))
                        acc.extend(getattr(rm, key)[sn:en])

            tasks = end_total - start_total
            success = end_success - start_success
            round_finals[round_id] = {
                "tasks": tasks,
                "success": success,
                "latencies": round_latencies,
                "resume": round_resume,
                "pause": round_pause,
                "slice": round_slice,
                "slot_held": round_slot_held,
            }

        return round_finals

    # ---- per-workflow delegating facades -----------------------------------
    # One-liners that thread the frozen ReportContext into the strategy carried
    # by this workflow's WorkflowSpec. Kept on the host so tests and callers
    # that already hold a ReportFormatter keep working through the generic names.

    def format_stats_section(self) -> list[str]:
        return self._fmt.format_stats_section(self._ctx)

    def format_step_timing(self) -> list[str]:
        return self._fmt.format_step_timing(self._ctx)

    def format_throughput_section(self) -> list[str]:
        return self._fmt.format_throughput_section(self._ctx)

    def format_trajectory_summary_section(self) -> list[str]:
        return self._fmt.format_trajectory_summary_section(self._ctx)


class StatsCollector:
    """Statistics collector - real-time snapshot + final report."""

    def __init__(
        self,
        config: KernelConfig,
        sandbox_states: dict[int, BenchSandbox],
        provider_label: str = "",
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.provider_label = provider_label
        # _print_snapshot looks the strategy up directly (no ReportFormatter),
        # so ensure the workflow is registered here too. Idempotent.
        ensure_workflow_registered(config.workflow_type)
        self.admission_snapshot: dict | None = None
        self.snapshots: list[Snapshot] = []
        self.start_time: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Round tracking for round-robin mode
        self.current_round: int | None = None
        self.round_snapshots: dict[int, list[Snapshot]] = {}

        # Round start totals - recorded at round switch to decouple from snapshot timing.
        # Key: round_id, Value: {"total": int, "success": int, "sandbox_latency_counts": dict[int, int]}
        # sandbox_latency_counts: {sandbox_index: latency_count} - how many latencies each
        # sandbox had at round start.
        self._round_start_totals: dict[int, dict[str, Any]] = {}

    def start(self) -> None:
        """Start background collection thread."""
        self.start_time = time.time()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop collection."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_round(self, round_id: int | None) -> None:
        """Set current round for statistics tracking.

        Called by the round-robin task manager to mark which round is active.
        Snapshots collected during this round are grouped together.

        Key design: record cumulative totals at the moment of round switch.
        This decouples round delta calculation from snapshot timing.

        Args:
            round_id: Current round index (None to clear).
        """
        # Get current cumulative totals before switching rounds
        task_total = sum(s.task_metrics.total_tasks for s in self.sandbox_states.values())
        task_success = sum(s.task_metrics.success_count for s in self.sandbox_states.values())
        sandbox_latency_counts = {s.index: len(s.task_metrics.latencies) for s in self.sandbox_states.values()}

        # Replay lifecycle per-round baselines: snapshot cumulative list
        # lengths so the per-round overhead table can slice each sandbox's
        # resume/pause/slice lists between consecutive round boundaries.
        # No-op for non-replay workflows (the lists stay empty).
        replay_baselines: dict[int, dict[str, int]] = {}
        if self.config.workflow_type == "replay":
            for s in self.sandbox_states.values():
                rm = s.replay_metrics
                replay_baselines[s.index] = {k: len(getattr(rm, k)) for k in _LIFECYCLE_ROUND_KEYS}

        # Switch to new round
        self.current_round = round_id

        if round_id is not None:
            # Initialize snapshot list if needed
            if round_id not in self.round_snapshots:
                self.round_snapshots[round_id] = []

            # CRITICAL: Only record baseline if this round doesn't have one yet.
            # This prevents overwriting when cycling (round 0 runs again later)
            if round_id not in self._round_start_totals:
                self._round_start_totals[round_id] = {
                    "total": task_total,
                    "success": task_success,
                    "sandbox_latency_counts": sandbox_latency_counts,
                    "replay_baselines": replay_baselines,
                }

    def _collect_loop(self) -> None:
        """Periodic snapshot collection."""
        while not self._stop.is_set():
            self._take_snapshot()
            time.sleep(self.config.stats_interval)

    def _take_snapshot(self) -> None:
        """Collect current snapshot.

        Generic over workflow: projects cumulative task totals / success /
        recent-latency stats from the polymorphic ``s.task_metrics`` (one of
        Browser/Coding/Document/Replay metrics) onto the slim generic
        :class:`Snapshot`. Per-workflow narrows (browser ports, coding verify,
        replay trajectory) are no longer snapshotted -- the per-workflow
        ``format_snapshot_line`` strategy reads them live from
        ``sandbox_states`` at render time.
        """
        now = time.time()
        elapsed = now - self.start_time

        # Sandbox status statistics
        active_count = sum(
            1 for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.READY and s.is_alive
        )
        offline_count = sum(
            1
            for s in self.sandbox_states.values()
            if not s.is_alive
            or s.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.READY_FAILED, SandboxStatus.OFFLINE)
        )

        # Creation performance statistics (only ready sandboxes)
        create_times = [
            s.creation_metrics.create_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.READY and s.creation_metrics.create_elapsed > 0
        ]
        ready_check_times = [
            s.creation_metrics.ready_check_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.READY and s.creation_metrics.ready_check_elapsed > 0
        ]
        total_times = [
            s.creation_metrics.total_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.READY and s.creation_metrics.total_elapsed > 0
        ]

        creation_stats = {
            "create": calc_percentiles(create_times),
            "ready_check": calc_percentiles(ready_check_times),
            "total": calc_percentiles(total_times),
        }

        # Task statistics (cumulative) -- generic projection from the
        # polymorphic task_metrics. task_total / task_success / the last-10
        # latency window are workflow-agnostic; the per-workflow snapshot line
        # (traj=, ports, etc.) is rendered by the strategy at print time.
        task_total = sum(s.task_metrics.total_tasks for s in self.sandbox_states.values())
        task_success = sum(s.task_metrics.success_count for s in self.sandbox_states.values())

        if self.current_round is not None and self.current_round in self._round_start_totals:
            start_total = self._round_start_totals[self.current_round]["total"]
            start_success = self._round_start_totals[self.current_round]["success"]
            round_total = task_total - start_total
            round_success = task_success - start_success
        else:
            round_total = 0
            round_success = 0

        all_latencies: list[float] = [
            latency for state in self.sandbox_states.values() for latency in state.task_metrics.latencies[-10:]
        ]
        avg_latency = statistics.mean(all_latencies) if all_latencies else 0.0
        p99_latency = calc_p99(all_latencies)

        snapshot = Snapshot(
            timestamp=now,
            elapsed=elapsed,
            total_sandboxes=len(self.sandbox_states),
            active_sandboxes=active_count,
            offline_sandboxes=offline_count,
            creation_stats=creation_stats,
            task_total=task_total,
            task_success=task_success,
            recent_avg_latency=avg_latency,
            recent_p99_latency=p99_latency,
            round_total=round_total,
            round_success=round_success,
        )

        self.snapshots.append(snapshot)

        # Track round-specific snapshots
        if self.current_round is not None:
            self.round_snapshots[self.current_round].append(snapshot)

        # Real-time terminal output
        self._print_snapshot(snapshot)

    def _print_snapshot(self, snapshot: Snapshot) -> None:
        """Emit real-time snapshot to the log stream.

        The per-workflow status line (Coding:/Document:/Browser:/Replay: + the
        ratio + avg/p99) is rendered by ``format_snapshot_line`` on the
        workflow's ReportFormatters strategy, reading recent_avg_latency /
        recent_p99_latency / task_total / task_success off the slim generic
        Snapshot (plus live per-workflow state like trajectory completions).
        """
        fmt = WORKFLOW_REGISTRY[self.config.workflow_type].report_formatters
        logger.info(f"\n{'─' * 70}")
        logger.info(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        logger.info(f"{'─' * 70}")
        logger.info(f"  Sandboxes: {snapshot.active_sandboxes:3d} ready / {snapshot.offline_sandboxes:2d} offline")
        logger.info(fmt.format_snapshot_line(snapshot, self.sandbox_states, self.config))
        logger.info(f"{'─' * 70}")

    def _resolved_wall_sec(self) -> float | None:
        """Measured wall-clock since ``start()``, or None if ``start()`` was never called."""
        if self.start_time:
            return time.time() - self.start_time
        return None

    def format_stats_section(self) -> list[str]:
        """Delegate to ReportFormatter.format_stats_section (per-workflow strategy)."""
        formatter = ReportFormatter(
            self.config,
            self.sandbox_states,
            self.provider_label,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self._resolved_wall_sec(),
        )
        return formatter.format_stats_section()

    def generate_report(self) -> str:
        """Generate final TXT report.

        No per-workflow if/elif: every workflow-variant surface is delegated to
        the ``ReportFormatters`` strategy on ``WORKFLOW_REGISTRY``. Non-replay
        strategies return ``[]`` for throughput / trajectory / round-extras, so
        those calls are unconditional and simply contribute nothing.
        """
        formatter = ReportFormatter(
            self.config,
            self.sandbox_states,
            self.provider_label,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self._resolved_wall_sec(),
        )
        fmt = formatter._fmt

        lines: list[str] = []

        # Configuration + sandbox status sections
        lines.extend(formatter.format_config_section())
        lines.extend(formatter.format_sandbox_status_section())

        # Creation performance sections. The title/description strings are
        # per-workflow class attrs on the strategy (browser = port-wait
        # wording; coding/document/replay = command-ready-check wording).
        ready_states = [s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.READY]

        create_times = [
            s.creation_metrics.create_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.create_elapsed > 0
            and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
        ]
        lines.extend(formatter.format_percentile_section("Sandbox.create Performance", create_times, fmt.create_desc))

        ready_check_times = [
            s.creation_metrics.ready_check_elapsed for s in ready_states if s.creation_metrics.ready_check_elapsed > 0
        ]
        lines.extend(
            formatter.format_percentile_section(fmt.ready_check_title, ready_check_times, fmt.ready_check_desc)
        )

        total_times = [s.creation_metrics.total_elapsed for s in ready_states if s.creation_metrics.total_elapsed > 0]
        lines.extend(formatter.format_percentile_section("Total Startup Performance", total_times, fmt.total_desc))

        # Per-workflow task statistics / step timing / (replay-only) throughput
        # + trajectory summary. All unconditional -- non-replay strategies
        # return [] for the replay-only surfaces.
        lines.extend(formatter.format_stats_section())
        lines.extend(formatter.format_step_timing())
        lines.extend(formatter.format_throughput_section())
        lines.extend(formatter.format_trajectory_summary_section())

        # Error details (per-workflow error_display_order on the strategy)
        lines.extend(formatter.format_error_section())

        # Round comparison (renders the per-round lifecycle sub-table via the
        # strategy's format_round_extras; [] for non-replay)
        lines.extend(formatter.format_round_comparison_table(self._round_start_totals))

        lines.append("\n" + "=" * 80)
        return "\n".join(lines)

    def save_report(self, report: str) -> str:
        """Save report to file."""
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.config.filename_prefix}_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)

        return filepath
