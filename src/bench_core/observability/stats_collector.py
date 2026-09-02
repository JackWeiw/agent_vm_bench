"""Statistics collection: real-time snapshots, terminal echo, and the final report.

Host-agnostic port of ``e2b_bench.stats_collector``. The collector reads
:class:`bench_core.schemas.BenchSandbox` state and a :class:`KernelConfig`; it
knows nothing about e2b or docker. The only provider-specific input is the
``provider_label`` string (e.g. ``"e2b"`` / ``"docker"``) shown in the report
header, threaded in by the benchmark spine.
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
from bench_core.observability.replay_obs import ReplayObservability
from env_provider import SandboxStatus
from bench_core.schemas import (
    CODING_STEP_ORDER,
    BenchSandbox,
    Snapshot,
    get_step_order,
)
from bench_core.utils import (
    calc_p99,
    calc_percentiles,
    calc_tail_ratio,
    classify_tail_latency,
)

logger = logging.getLogger(__name__)

# P2.5 lifecycle overhead: slices below this are excluded from per-sample
# overhead (prevents 1.0 / 0.0001-style explosion). The duration percentile
# lists already exclude == 0 (synthesized failures); this additionally drops
# pathologically-fast real slices.
MIN_SLICE_SEC = 0.001

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

# Error display order, selected by workflow_type. The shared ErrorClassifier
# may bucket an error into a category this workflow does not display; such
# categories fold into "Other" so the report schema stays consistent.
BROWSER_ERROR_DISPLAY = [
    "Open tab failed",
    "Page load failed",
    "Snapshot failed",
    "Click failed",
    "Screenshot failed",
    "Chrome start failed",
    "D-Bus connection error",
    "Gateway connection error",
    "Sandbox unreachable",
    "Timeout",
    "Other",
]

CODING_ERROR_DISPLAY = [
    "Checkout failed",
    "Edit failed",
    "Verify failed",
    "OOM",
    "Sandbox unreachable",
    "Timeout",
    "Other",
]
DOCUMENT_ERROR_DISPLAY = ["Read failed", "Write failed", "Verifier failed", "Timeout", "Other"]


class ErrorClassifier:
    """Error type classification for sandbox failures."""

    # Error type definitions with patterns (order matters - first match wins).
    ERROR_TYPES: list[tuple[str, list[str]]] = [
        # Browser errors
        ("Open tab failed", ["open_tab failed"]),
        ("Page load failed", ["page_load failed"]),
        ("Snapshot failed", ["snapshot failed"]),
        ("Click failed", ["click failed"]),
        ("Screenshot failed", ["screenshot failed"]),
        ("Chrome start failed", ["failed to start chrome", "chrome_start"]),
        ("D-Bus connection error", ["d-bus", "dbus", "failed to connect to the bus"]),
        ("Gateway connection error", ["gateway", "cdp", "http_unreachable"]),
        ("Sandbox unreachable", ["failed to route", "sandbox unreachable"]),
        # Coding errors
        ("Find failed", ["find failed", "git checkout", "locate failed"]),
        ("Read failed", ["read failed", "head failed"]),
        ("Edit failed", ["edit failed", "sed failed"]),
        ("Verify failed", ["verify failed", "npx tsx", "go run", "exit code"]),
        ("Diff failed", ["diff failed", "git diff"]),
        ("Write failed", ["write failed", "create write directory"]),
        ("Verifier failed", ["verification", "verifier", "business_verification"]),
        ("OOM", ["oom", "out of memory", "cannot allocate"]),
        ("Timeout", ["timeout", "timed out"]),
    ]

    @classmethod
    def classify(cls, error: str) -> str:
        """Classify an error message into an error type."""
        error_lower = error.lower()
        for error_type, patterns in cls.ERROR_TYPES:
            if any(pattern in error_lower for pattern in patterns):
                return error_type
        return "Other"

    @classmethod
    def aggregate(cls, errors: list[tuple[int, int, str]]) -> tuple[dict[str, int], dict[str, list[int]]]:
        """Aggregate errors by type.

        Args:
            errors: List of (sandbox_index, count, error_message).

        Returns:
            Tuple of (error_counts, error_sandbox_ids).
        """
        error_counts: dict[str, int] = {}
        error_sandbox_ids: dict[str, list[int]] = {}

        for sid, count, error in errors:
            error_type = cls.classify(error)
            error_counts[error_type] = error_counts.get(error_type, 0) + count
            error_sandbox_ids.setdefault(error_type, []).append(sid)

        return error_counts, error_sandbox_ids


class TableFormatter:
    """Simple table formatter for plain text output."""

    @staticmethod
    def format_table(headers: list[str], rows: list[list[str]], title: str = "") -> list[str]:
        """Format a table with aligned columns."""
        if not rows:
            return []

        lines: list[str] = []
        if title:
            lines.append(title)

        # Calculate column widths
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        # Header row
        lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        # Separator
        lines.append("  ".join("-" * w for w in widths))
        # Data rows
        for row in rows:
            lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))

        return lines


class ReportFormatter:
    """Format statistics into a human-readable report."""

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

    def format_config_section(self) -> list[str]:
        """Format test configuration section."""
        lines = ["=" * 80, "Sandbox Bench - Performance Report", "=" * 80]
        lines.append("\n[Test Configuration]")
        lines.append(f"  Backend:        {self.provider_label}")
        lines.append(f"  Total Sandboxes: {self.config.total_count}")
        if self.config.workflow_type == "document":
            lines.append(f"  Workflow:        {self.config.workflow_type}")
            lines.append(f"  Document Case:   {self.config.document_case_kind}")

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

        # Use workflow-specific labels
        if self.config.workflow_type in {"coding", "document", "replay"}:
            command_ready = True
        elif self.config.workflow_type == "browser":
            command_ready = False
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")
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

    def format_browser_stats_section(self) -> list[str]:
        """Format browser task statistics section."""
        all_latencies: list[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.browser_metrics.latencies)

        total_tasks = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        total_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.browser_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.browser_metrics.timeout_count for s in self.sandbox_states.values())

        lines = ["\n[Browser Task Statistics]"]
        lines.append(f"  Total Tasks:   {total_tasks}")
        lines.append(f"  Success:       {total_success}")
        lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")

        if all_latencies:
            avg_ms = statistics.mean(all_latencies) * 1000
            p99_ms = calc_p99(all_latencies) * 1000
            lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
            lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

        return lines

    def format_step_timing_table(self) -> list[str]:
        """Format step-level timing as a table."""
        all_step_times: dict[str, list[float]] = {}
        for s in self.sandbox_states.values():
            step_times_copy = s.browser_metrics.get_step_times_copy()
            for step_name, times in step_times_copy.items():
                all_step_times.setdefault(step_name, []).extend(times)

        if not all_step_times:
            return []

        lines = ["\n[Step-Level Timing (Tab-Switch Mode)]"]
        headers = ["Step", "Count", "Avg(ms)", "P50(ms)", "P95(ms)", "P99(ms)", "Tail"]
        rows: list[list[str]] = []

        for step_name in ["open_tab", "page_load", "snapshot", "click", "screenshot"]:
            if step_name in all_step_times and all_step_times[step_name]:
                times = all_step_times[step_name]
                stats = calc_percentiles(times)
                tail_ratio = calc_tail_ratio(times)
                severity = classify_tail_latency(tail_ratio)
                rows.append(
                    [
                        step_name,
                        str(len(times)),
                        f"{stats['avg'] * 1000:.1f}",
                        f"{stats['p50'] * 1000:.1f}",
                        f"{stats['p95'] * 1000:.1f}",
                        f"{stats['p99'] * 1000:.1f}",
                        f"{tail_ratio:.2f}x ({severity})",
                    ]
                )

        lines.extend(TableFormatter.format_table(headers, rows))
        lines.append("\n  Tail Ratio: P99/P50 - indicates long-tail latency severity")
        lines.append("  < 1.2x: minimal | 1.2-1.5x: moderate | > 1.5x: significant")
        return lines

    def format_coding_stats_section(self) -> list[str]:
        """Format coding task statistics section."""
        all_latencies: list[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.coding_metrics.latencies)

        total_tasks = sum(s.coding_metrics.total_tasks for s in self.sandbox_states.values())
        total_success = sum(s.coding_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.coding_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.coding_metrics.timeout_count for s in self.sandbox_states.values())
        verify_success = sum(s.coding_metrics.verify_success_count for s in self.sandbox_states.values())
        compile_only = sum(s.coding_metrics.compile_only_count for s in self.sandbox_states.values())

        lines = ["\n[Coding Task Statistics]"]
        lines.append(f"  Total Tasks:   {total_tasks}")
        lines.append(f"  Success:       {total_success}")
        lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")
        # Verify is split: real-assertion passes vs compile-only passes (no
        # assertion). Kept separate so a compile-only pass is never read as an
        # assertion pass - the distinction a strong reviewer checks.
        lines.append(
            f"  Verify Success: {verify_success}/{total_tasks} "
            f"({verify_success / max(1, total_tasks) * 100:.1f}%) "
            f"[assert: {verify_success}, compile-only: {compile_only}]"
        )

        if all_latencies:
            avg_ms = statistics.mean(all_latencies) * 1000
            p99_ms = calc_p99(all_latencies) * 1000
            lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
            lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

        return lines

    def format_coding_step_timing_table(self) -> list[str]:
        """Format coding step-level timing as a table."""
        all_step_times: dict[str, list[float]] = {}
        for s in self.sandbox_states.values():
            step_times_copy = s.coding_metrics.get_step_times_copy()
            for step_name, times in step_times_copy.items():
                all_step_times.setdefault(step_name, []).extend(times)

        if not all_step_times:
            return []

        lines = ["\n[Step-Level Timing (Coding Mode)]"]
        headers = ["Step", "Count", "Avg(ms)", "P50(ms)", "P95(ms)", "P99(ms)", "Tail"]
        rows: list[list[str]] = []

        for step_name in CODING_STEP_ORDER:
            if step_name in all_step_times and all_step_times[step_name]:
                times = all_step_times[step_name]
                stats = calc_percentiles(times)
                tail_ratio = calc_tail_ratio(times)
                severity = classify_tail_latency(tail_ratio)
                rows.append(
                    [
                        step_name,
                        str(len(times)),
                        f"{stats['avg'] * 1000:.1f}",
                        f"{stats['p50'] * 1000:.1f}",
                        f"{stats['p95'] * 1000:.1f}",
                        f"{stats['p99'] * 1000:.1f}",
                        f"{tail_ratio:.2f}x ({severity})",
                    ]
                )

        lines.extend(TableFormatter.format_table(headers, rows))
        lines.append("\n  Tail Ratio: P99/P50 - indicates long-tail latency severity")
        lines.append("  < 1.2x: minimal | 1.2-1.5x: moderate | > 1.5x: significant")
        return lines

    def format_document_stats_section(self) -> list[str]:
        """Format document task statistics section."""
        metrics = [state.document_metrics for state in self.sandbox_states.values()]
        all_latencies = [latency for metric in metrics for latency in metric.latencies]
        total_tasks = sum(metric.total_tasks for metric in metrics)
        total_success = sum(metric.success_count for metric in metrics)
        total_failed = sum(metric.failed_count for metric in metrics)
        total_timeout = sum(metric.timeout_count for metric in metrics)
        lines = ["\n[Document Task Statistics]"]
        lines.append(f"  Case Kind:     {self.config.document_case_kind}")
        lines.append(f"  Total Tasks:   {total_tasks}")
        lines.append(f"  Success:       {total_success}")
        lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")
        if all_latencies:
            lines.append(f"  Avg Latency:   {statistics.mean(all_latencies) * 1000:.1f}ms")
            lines.append(f"  P99 Latency:   {calc_p99(all_latencies) * 1000:.1f}ms")
        return lines

    def format_document_step_timing_table(self) -> list[str]:
        """Format document step-level timing as a table."""
        all_step_times: dict[str, list[float]] = {}
        for state in self.sandbox_states.values():
            for step_name, times in state.document_metrics.get_step_times_copy().items():
                all_step_times.setdefault(step_name, []).extend(times)
        if not all_step_times:
            return []
        lines = [f"\n[Step-Level Timing (Document {self.config.document_case_kind.upper()} Mode)]"]
        headers = ["Step", "Count", "Avg(ms)", "P50(ms)", "P95(ms)", "P99(ms)", "Tail"]
        rows: list[list[str]] = []
        for step_name in get_step_order("document", self.config.document_case_kind):
            times = all_step_times.get(step_name, [])
            if not times:
                continue
            stats = calc_percentiles(times)
            tail_ratio = calc_tail_ratio(times)
            rows.append(
                [
                    step_name,
                    str(len(times)),
                    f"{stats['avg'] * 1000:.1f}",
                    f"{stats['p50'] * 1000:.1f}",
                    f"{stats['p95'] * 1000:.1f}",
                    f"{stats['p99'] * 1000:.1f}",
                    f"{tail_ratio:.2f}x ({classify_tail_latency(tail_ratio)})",
                ]
            )
        lines.extend(TableFormatter.format_table(headers, rows))
        return lines

    def format_replay_stats_section(self) -> list[str]:
        """Format trajectory-replay task statistics section."""
        all_latencies: list[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.replay_metrics.latencies)

        total_tasks = sum(s.replay_metrics.total_tasks for s in self.sandbox_states.values())
        total_success = sum(s.replay_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.replay_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.replay_metrics.timeout_count for s in self.sandbox_states.values())
        completions = sum(s.replay_metrics.trajectory_completions for s in self.sandbox_states.values())
        # Delay fidelity is per-sandbox (actual/requested delay); average across sandboxes.
        fidelity_values = [s.replay_metrics.delay_fidelity for s in self.sandbox_states.values()]
        delay_fidelity = statistics.mean(fidelity_values) if fidelity_values else 0.0

        lines = ["\n[Replay Task Statistics]"]
        # Label+colon padded to the widest ("Trajectory Completions:") so every
        # value starts in the same column; the colon stays contiguous with the
        # label so ``"Label:"`` substring asserts keep working.
        lines.append(f"  {'Total Steps:':<24}{total_tasks}")
        lines.append(f"  {'Success:':<24}{total_success}")
        lines.append(f"  {'Failed:':<24}{total_failed} (timeout: {total_timeout})")
        lines.append(f"  {'Success Rate:':<24}{total_success / max(1, total_tasks) * 100:.1f}%")
        lines.append(f"  {'Trajectory Completions:':<24}{completions}")
        orphan_skipped = sum(s.replay_metrics.orphan_skip_count for s in self.sandbox_states.values())
        if orphan_skipped:
            lines.append(f"  {'Orphan Skipped:':<24}{orphan_skipped}")
        # P2 lifecycle: one-time snapshot-creation pause (separate from per-step resume_sec).
        initial_pauses = [
            s.replay_metrics.initial_pause_sec
            for s in self.sandbox_states.values()
            if s.replay_metrics.initial_pause_sec > 0
        ]
        if initial_pauses:
            noun = "sandbox" if len(initial_pauses) == 1 else "sandboxes"
            lines.append(
                f"  {'Initial Pause:':<24}{statistics.mean(initial_pauses):.3f}s (over {len(initial_pauses)} {noun})"
            )
        lines.append(f"  {'Delay Fidelity:':<24}{delay_fidelity:.2f}")

        if all_latencies:
            avg = statistics.mean(all_latencies)
            p99 = calc_p99(all_latencies)
            lines.append(f"  {'Avg Latency:':<24}{avg:.3f}s")
            lines.append(f"  {'P99 Latency:':<24}{p99:.3f}s")

        # P2.5 [Lifecycle Overhead] -- lifecycle + trajectory mode. Per-sample
        # overhead_i = (resume_sec_i + pause_sec_i) / slice_total_sec_i
        # (mean/P50/P95), plus an aggregate ratio. Near-zero slices
        # (slice_total_sec < MIN_SLICE_SEC) are excluded from the per-sample
        # overhead list; the duration percentile lists already exclude == 0.
        if self.config.replay_mode in ("lifecycle", "trajectory"):
            all_resume: list[float] = []
            all_pause: list[float] = []
            all_slice: list[float] = []
            all_slot_held: list[float] = []
            all_interaction: list[float] = []
            for s in self.sandbox_states.values():
                all_resume.extend(s.replay_metrics.resume_secs)
                all_pause.extend(s.replay_metrics.pause_secs)
                all_slice.extend(s.replay_metrics.slice_total_secs)
                all_slot_held.extend(s.replay_metrics.running_slot_held_secs)
                all_interaction.extend(s.replay_metrics.interaction_total_secs)
            if all_slice:
                lines.append("[Lifecycle Overhead]")
                resume_stats = calc_percentiles(all_resume)
                pause_stats = calc_percentiles(all_pause)
                slice_stats = calc_percentiles(all_slice)
                n = len(all_slice)
                # Percentile lines aligned to the widest label in this group
                # ("Interaction:") so the P50= column starts in one place.
                lines.append(
                    f"  {'Resume:':<13}P50={resume_stats['p50']:.3f}s "
                    f"P95={resume_stats['p95']:.3f}s P99={resume_stats['p99']:.3f}s  (n={n})"
                )
                lines.append(
                    f"  {'Pause:':<13}P50={pause_stats['p50']:.3f}s "
                    f"P95={pause_stats['p95']:.3f}s P99={pause_stats['p99']:.3f}s  (n={n})"
                )
                lines.append(
                    f"  {'Slice:':<13}P50={slice_stats['p50']:.3f}s "
                    f"P95={slice_stats['p95']:.3f}s P99={slice_stats['p99']:.3f}s  (n={n})"
                )
                # L7: Slot held (overcommit-efficiency = lease release - acquire).
                # slot_held ≡ slice by construction (the lease spans resume->pause;
                # slot_contention_wait happens before acquire, so it is never in the
                # lease). Only render when it materially diverges from slice -- e.g.
                # trajectory-mode capacity_wait -- otherwise it restates Slice.
                if all_slot_held and self.admission_snapshot is not None:
                    held_diverges = any(abs(h - s) >= 0.001 for h, s in zip(all_slot_held, all_slice))
                    if held_diverges:
                        held_stats = calc_percentiles(all_slot_held)
                        lines.append(
                            f"  {'Slot held:':<13}P50={held_stats['p50']:.3f}s "
                            f"P95={held_stats['p95']:.3f}s  (n={len(all_slot_held)})"
                        )
                if all_interaction:
                    inter_stats = calc_percentiles(all_interaction)
                    lines.append(
                        f"  {'Interaction:':<13}P50={inter_stats['p50']:.3f}s "
                        f"P95={inter_stats['p95']:.3f}s  (n={len(all_interaction)})"
                    )
                # per-sample overhead, near-zero guarded
                overheads = [(r + p) / s for r, p, s in zip(all_resume, all_pause, all_slice) if s >= MIN_SLICE_SEC]
                if overheads:
                    oh_stats = calc_percentiles(overheads)
                    lines.append(
                        f"  Overhead per-sample: mean={oh_stats['avg'] * 100:.1f}% "
                        f"P50={oh_stats['p50'] * 100:.1f}% P95={oh_stats['p95'] * 100:.1f}% "
                        f"(n={len(overheads)})"
                    )
                # aggregate ratio (robust to per-sample outliers)
                agg_slice = [s for s in all_slice if s >= MIN_SLICE_SEC]
                agg_resume = [r for r, s in zip(all_resume, all_slice) if s >= MIN_SLICE_SEC]
                agg_pause = [p for p, s in zip(all_pause, all_slice) if s >= MIN_SLICE_SEC]
                if sum(agg_slice) > 0:
                    agg = (sum(agg_resume) + sum(agg_pause)) / sum(agg_slice)
                    lines.append(f"  Overhead aggregate:  {agg * 100:.1f}%")
                # known caveat footer (resume_sec includes the post-resume ready-wait;
                # the Resume decomp line below breaks resume_sec into api + ready_wait + qps_wait)
                lines.append("  (resume_sec includes post-resume ready-wait; see Resume decomp)")

                # Phase 3.3: retry-impact sub-block. Reads the ReplayMetrics
                # accumulators (populated by the runner alongside retry_* series
                # events), NOT the series JSONL -- ReplayMetrics is the report's
                # consistent data source. retries/time lost always render; the
                # per-slice P95 line only when retries actually occurred.
                retry_count = sum(s.replay_metrics.retry_queued_count for s in self.sandbox_states.values())
                retry_time_lost = sum(s.replay_metrics.time_lost_to_retry_sec for s in self.sandbox_states.values())
                if retry_count > 0:
                    all_retries_per_slice: list[int] = []
                    for s in self.sandbox_states.values():
                        all_retries_per_slice.extend(s.replay_metrics.retries_per_slice)
                    rp95 = calc_percentiles(all_retries_per_slice)["p95"]
                    lines.append(
                        f"  Retry impact: retries={retry_count} "
                        f"(time lost={retry_time_lost:.3f}s) "
                        f"retries/slice P95={rp95:.2f}"
                    )
                else:
                    lines.append(f"  Retry impact: retries={retry_count} " f"(time lost={retry_time_lost:.3f}s)")

                # P2.6 decomposition sub-block: column-stable breakdown of
                # resume/pause into their segment components. Columns are NEVER
                # conditionally dropped (0.000s when inactive); only the Slot
                # contention and Admission WHOLE LINES are conditional on an
                # admission controller being present.
                all_resume_api: list[float] = []
                all_resume_ready_wait: list[float] = []
                all_resume_queue_wait: list[float] = []
                all_slot_contention: list[float] = []
                all_pause_api: list[float] = []
                for s in self.sandbox_states.values():
                    all_resume_api.extend(s.replay_metrics.resume_api_secs)
                    all_resume_ready_wait.extend(s.replay_metrics.resume_ready_wait_secs)
                    all_resume_queue_wait.extend(s.replay_metrics.resume_queue_wait_secs)
                    all_slot_contention.extend(s.replay_metrics.slot_contention_wait_secs)
                    all_pause_api.extend(s.replay_metrics.pause_api_secs)

                # Resume decomp line (always rendered when all_slice non-empty)
                resume_api_stats = calc_percentiles(all_resume_api)
                resume_ready_wait_stats = calc_percentiles(all_resume_ready_wait)
                resume_queue_wait_stats = calc_percentiles(all_resume_queue_wait)
                lines.append(
                    f"  Resume decomp: api P50={resume_api_stats['p50']:.3f}s "
                    f"P95={resume_api_stats['p95']:.3f}s | "
                    f"ready_wait P50={resume_ready_wait_stats['p50']:.3f}s | "
                    f"qps_wait P50={resume_queue_wait_stats['p50']:.3f}s  (n={n})"
                )

                # Pause decomp line (qps_wait shared from resume_queue_wait_secs
                # since the limiter is shared; pause has no ready_wait)
                pause_api_stats = calc_percentiles(all_pause_api)
                lines.append(
                    f"  Pause decomp:  api P50={pause_api_stats['p50']:.3f}s | "
                    f"qps_wait P50={resume_queue_wait_stats['p50']:.3f}s  (n={n})"
                )

                # Conditional lines (only when admission controller was built)
                if self.admission_snapshot is not None:
                    slot_contention_stats = calc_percentiles(all_slot_contention)
                    lines.append(
                        f"  Slot contention: P50={slot_contention_stats['p50']:.3f}s "
                        f"P95={slot_contention_stats['p95']:.3f}s  (n={len(all_slot_contention)})"
                    )
                    a = self.admission_snapshot
                    lines.append("  Admission:")
                    rs = a.get("running_slots") or {}
                    if rs:
                        lines.append(
                            f"    Running slots: maximum={rs.get('maximum', 0)} "
                            f"active={rs.get('active', 0)} peak_active={rs.get('peak_active', 0)} "
                            f"granted={rs.get('granted', 0)} waiting={rs.get('waiting', 0)} "
                            f"avg_queue_wait={rs.get('average_queue_wait_sec', 0.0):.3f}s"
                        )
                    ql = a.get("qps_limiter")
                    if ql and a.get("qps") != "off":
                        lines.append(
                            f"    QPS limiter:   qps={ql.get('qps')} "
                            f"inflight={ql.get('in_flight', 0)}/{ql.get('inflight_cap', 0)} "
                            f"dispatched={ql.get('dispatched', 0)} "
                            f"avg_wait={ql.get('average_wait_sec', 0.0):.1f}s "
                            f"max_wait={ql.get('max_wait_sec', 0.0):.1f}s"
                        )
                        dbo = ql.get("dispatched_by_operation", {})
                        lines.append(
                            "    Dispatched by operation: "
                            + " ".join(
                                f"{op}={dbo.get(op, 0)}" for op in ("resume", "pause", "cleanup", "create", "command")
                            )
                        )
                        wbo = ql.get("waiting_by_operation", {})
                        # Suppress an all-zero waiting line -- it is pure noise
                        # (no op is ever queued) and never adds information.
                        if any(wbo.get(op, 0) for op in ("resume", "pause", "cleanup", "create", "command")):
                            lines.append(
                                "    Waiting by operation:    "
                                + " ".join(
                                    f"{op}={wbo.get(op, 0)}"
                                    for op in ("resume", "pause", "cleanup", "create", "command")
                                )
                            )

        return lines

    def format_throughput_section(self) -> list[str]:
        """[Throughput & Overcommit] -- throughput/efficiency from ReplayObservability.

        Rendered for replay workflows. Wall-gated metrics (steps_per_sec,
        effective_parallelism, exec_wall_utilization) render ``n/a (zero
        wall-clock time)`` when wall_sec is None/<=0; overcommit_ratio is
        always rendered (it does not depend on wall-clock).
        """
        obs = ReplayObservability(
            self.config,
            self.sandbox_states,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self.wall_sec,
        )
        lines = ["\n[Throughput & Overcommit]"]
        na = "n/a (zero wall-clock time)"
        if self.wall_sec is not None and self.wall_sec > 0:
            lines.append(f"  {'wall_sec:':<24}{self.wall_sec:.1f}")
        sps = obs.steps_per_sec
        lines.append(f"  {'steps_per_sec:':<24}{f'{sps:.2f}' if sps is not None else na}")
        ep = obs.effective_parallelism
        lines.append(f"  {'effective_parallelism:':<24}{f'{ep:.2f}' if ep is not None else na}")
        eu = obs.exec_wall_utilization
        lines.append(f"  {'exec_wall_utilization:':<24}{f'{eu * 100:.1f}%' if eu is not None else na}")
        lines.append(f"  {'overcommit_ratio:':<24}{obs.overcommit_ratio:.1f}x")
        return lines

    def format_trajectory_summary_section(self) -> list[str]:
        """[Trajectory Summary] -- create/kill/slot-held percentiles for trajectory mode.

        Rendered only in trajectory mode AND when create_sec measurements exist
        (create_secs is populated only in trajectory mode; empty otherwise). The
        slot_held line reuses the same running_slot_held_secs list as
        [Lifecycle Overhead] but shows P99 here too.
        """
        if self.config.replay_mode != "trajectory":
            return []
        obs = ReplayObservability(
            self.config,
            self.sandbox_states,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self.wall_sec,
        )
        cs = obs.create_sec_stats
        # Gate on non-empty create_secs (calc_percentiles returns all-0.0 for an empty list).
        if cs["p99"] == 0.0 and cs["max"] == 0.0:
            return []
        ks = obs.kill_sec_stats
        sh = obs.slot_held_stats
        n_create = sum(len(s.replay_metrics.create_secs) for s in self.sandbox_states.values())
        n_kill = sum(len(s.replay_metrics.kill_secs) for s in self.sandbox_states.values())
        lines = ["\n[Trajectory Summary]"]
        lines.append(f"  Create sec: P50={cs['p50']:.3f}s P95={cs['p95']:.3f}s P99={cs['p99']:.3f}s  (n={n_create})")
        lines.append(f"  Kill sec:   P50={ks['p50']:.3f}s P95={ks['p95']:.3f}s P99={ks['p99']:.3f}s  (n={n_kill})")
        if sh["max"] > 0.0:
            lines.append(f"  Slot held:  P50={sh['p50']:.3f}s P95={sh['p95']:.3f}s P99={sh['p99']:.3f}s")
        return lines

    def format_replay_step_timing_table(self) -> list[str]:
        """Format replay per-action-type timing as a table.

        Replay's "step" axis is the recorded action's type (shell /
        str_replace_editor / bash / other), bucketed by ``classify_action``.
        """
        all_step_times: dict[str, list[float]] = {}
        for s in self.sandbox_states.values():
            step_times_copy = s.replay_metrics.get_step_times_copy()
            for step_name, times in step_times_copy.items():
                all_step_times.setdefault(step_name, []).extend(times)

        if not all_step_times:
            return []

        lines = ["\n[Step-Level Timing (Replay Mode)]"]
        headers = ["Action", "Count", "Avg(s)", "P50(s)", "P95(s)", "P99(s)", "Tail"]
        rows: list[list[str]] = []

        for step_name in get_step_order("replay"):
            if step_name in all_step_times and all_step_times[step_name]:
                times = all_step_times[step_name]
                stats = calc_percentiles(times)
                tail_ratio = calc_tail_ratio(times)
                severity = classify_tail_latency(tail_ratio)
                rows.append(
                    [
                        step_name,
                        str(len(times)),
                        f"{stats['avg']:.3f}",
                        f"{stats['p50']:.3f}",
                        f"{stats['p95']:.3f}",
                        f"{stats['p99']:.3f}",
                        f"{tail_ratio:.2f}x ({severity})",
                    ]
                )

        lines.extend(TableFormatter.format_table(headers, rows))
        lines.append("\n  Tail Ratio: P99/P50 - indicates long-tail latency severity")
        lines.append("  < 1.2x: minimal | 1.2-1.5x: moderate | > 1.5x: significant")
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

        if self.config.workflow_type == "coding":
            error_display_order = CODING_ERROR_DISPLAY
        elif self.config.workflow_type == "document":
            error_display_order = DOCUMENT_ERROR_DISPLAY
        elif self.config.workflow_type == "browser":
            error_display_order = BROWSER_ERROR_DISPLAY
        elif self.config.workflow_type == "replay":
            error_display_order = CODING_ERROR_DISPLAY
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

        # The classifier is shared by all workflows, so a Document-specific
        # pattern can also match text emitted by Browser/Coding.  Preserve the
        # current workflow's report schema by folding categories it does not
        # display into Other instead of silently dropping them from the table.
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

        # Per-round lifecycle overhead: only replay lifecycle/trajectory have
        # resume/pause data (exec_only has none -> lists empty -> skip). This
        # is the per-round view the cumulative [Lifecycle Overhead] section
        # cannot show -- e.g. whether the 2nd trajectory pass resumes slower.
        if self.config.workflow_type == "replay" and self.config.replay_mode in ("lifecycle", "trajectory"):
            lines.extend(self._format_lifecycle_overhead_by_round(active_rounds))

        return lines

    def _format_lifecycle_overhead_by_round(self, active_rounds: dict[int, dict[str, Any]]) -> list[str]:
        """Per-round resume/pause/slice P50+P95 + aggregate overhead sub-table.

        The lifecycle lists are index-aligned within each round (appended in
        lockstep per slice), so the aggregate overhead ratio = (resume+pause)
        /slice is computed over matching samples, with near-zero slices
        excluded (same guard as the cumulative section).

        Carries only the per-round comparison dimension (P50/P95 + overhead +
        slot-held); the depth metrics (P99, per-sample overhead, decomp,
        interaction) stay in the cumulative [Lifecycle Overhead] section to
        avoid restating them per round. Slot held is meaningful only under an
        admission controller, so its column is conditional on admission_snapshot.
        """
        has_admission = self.admission_snapshot is not None
        lines = ["", "[Lifecycle Overhead by Round]"]
        headers = [
            "Round",
            "n",
            "Resume P50(s)",
            "Resume P95(s)",
            "Pause P50(s)",
            "Pause P95(s)",
            "Slice P50(s)",
            "Slice P95(s)",
        ]
        if has_admission:
            headers.append("Slot held P50(s)")
        headers.append("Overhead%")
        rows: list[list[str]] = []
        for round_id in sorted(active_rounds.keys()):
            resumes = active_rounds[round_id]["resume"]
            pauses = active_rounds[round_id]["pause"]
            slices = active_rounds[round_id]["slice"]
            if not slices:
                continue
            r_stats = calc_percentiles(resumes)
            p_stats = calc_percentiles(pauses)
            s_stats = calc_percentiles(slices)
            agg_slice = [sv for sv in slices if sv >= MIN_SLICE_SEC]
            agg_resume = [rv for rv, sv in zip(resumes, slices) if sv >= MIN_SLICE_SEC]
            agg_pause = [pv for pv, sv in zip(pauses, slices) if sv >= MIN_SLICE_SEC]
            overhead = (sum(agg_resume) + sum(agg_pause)) / sum(agg_slice) if sum(agg_slice) > 0 else 0.0
            row = [
                str(round_id),
                str(len(slices)),
                f"{r_stats['p50']:.3f}",
                f"{r_stats['p95']:.3f}",
                f"{p_stats['p50']:.3f}",
                f"{p_stats['p95']:.3f}",
                f"{s_stats['p50']:.3f}",
                f"{s_stats['p95']:.3f}",
            ]
            if has_admission:
                slot_held = active_rounds[round_id].get("slot_held", [])
                held_stats = calc_percentiles(slot_held) if slot_held else calc_percentiles([0.0])
                row.append(f"{held_stats['p50']:.3f}")
            row.append(f"{overhead * 100:.1f}")
            rows.append(row)
        if not rows:
            return []
        lines.extend(TableFormatter.format_table(headers, rows))
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
        """Collect current snapshot."""
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

        # Task statistics (cumulative) -- only for the active workflow
        if self.config.workflow_type == "browser":
            task_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
            task_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())

            if self.current_round is not None and self.current_round in self._round_start_totals:
                start_total = self._round_start_totals[self.current_round]["total"]
                start_success = self._round_start_totals[self.current_round]["success"]
                round_total = task_total - start_total
                round_success = task_success - start_success
            else:
                round_total = 0
                round_success = 0

            all_latencies: list[float] = []
            for s in self.sandbox_states.values():
                all_latencies.extend(s.browser_metrics.latencies[-10:])

            avg_latency = statistics.mean(all_latencies) if all_latencies else 0.0
            p99_latency = calc_p99(all_latencies)

            snapshot = Snapshot(
                timestamp=now,
                elapsed=elapsed,
                total_sandboxes=len(self.sandbox_states),
                active_sandboxes=active_count,
                offline_sandboxes=offline_count,
                creation_stats=creation_stats,
                browser_total=task_total,
                browser_success=task_success,
                browser_avg_latency=avg_latency,
                browser_p99_latency=p99_latency,
                round_total=round_total,
                round_success=round_success,
            )
        elif self.config.workflow_type == "coding":
            task_total = sum(s.coding_metrics.total_tasks for s in self.sandbox_states.values())
            task_success = sum(s.coding_metrics.success_count for s in self.sandbox_states.values())
            coding_verify_success = sum(s.coding_metrics.verify_success_count for s in self.sandbox_states.values())
            coding_compile_only = sum(s.coding_metrics.compile_only_count for s in self.sandbox_states.values())

            if self.current_round is not None and self.current_round in self._round_start_totals:
                start_total = self._round_start_totals[self.current_round]["total"]
                start_success = self._round_start_totals[self.current_round]["success"]
                round_total = task_total - start_total
                round_success = task_success - start_success
            else:
                round_total = 0
                round_success = 0

            all_latencies = []
            for s in self.sandbox_states.values():
                all_latencies.extend(s.coding_metrics.latencies[-10:])

            avg_latency = statistics.mean(all_latencies) if all_latencies else 0.0
            p99_latency = calc_p99(all_latencies)

            snapshot = Snapshot(
                timestamp=now,
                elapsed=elapsed,
                total_sandboxes=len(self.sandbox_states),
                active_sandboxes=active_count,
                offline_sandboxes=offline_count,
                creation_stats=creation_stats,
                coding_total=task_total,
                coding_success=task_success,
                coding_verify_success=coding_verify_success,
                coding_compile_only=coding_compile_only,
                coding_avg_latency=avg_latency,
                coding_p99_latency=p99_latency,
                round_total=round_total,
                round_success=round_success,
            )
        elif self.config.workflow_type == "document":
            task_total = sum(s.document_metrics.total_tasks for s in self.sandbox_states.values())
            task_success = sum(s.document_metrics.success_count for s in self.sandbox_states.values())
            if self.current_round is not None and self.current_round in self._round_start_totals:
                start_total = self._round_start_totals[self.current_round]["total"]
                start_success = self._round_start_totals[self.current_round]["success"]
                round_total = task_total - start_total
                round_success = task_success - start_success
            else:
                round_total = 0
                round_success = 0
            all_latencies = [
                latency for state in self.sandbox_states.values() for latency in state.document_metrics.latencies[-10:]
            ]
            snapshot = Snapshot(
                timestamp=now,
                elapsed=elapsed,
                total_sandboxes=len(self.sandbox_states),
                active_sandboxes=active_count,
                offline_sandboxes=offline_count,
                creation_stats=creation_stats,
                document_total=task_total,
                document_success=task_success,
                document_avg_latency=statistics.mean(all_latencies) if all_latencies else 0.0,
                document_p99_latency=calc_p99(all_latencies),
                round_total=round_total,
                round_success=round_success,
            )
        elif self.config.workflow_type == "replay":
            task_total = sum(s.replay_metrics.total_tasks for s in self.sandbox_states.values())
            task_success = sum(s.replay_metrics.success_count for s in self.sandbox_states.values())
            if self.current_round is not None and self.current_round in self._round_start_totals:
                start_total = self._round_start_totals[self.current_round]["total"]
                start_success = self._round_start_totals[self.current_round]["success"]
                round_total = task_total - start_total
                round_success = task_success - start_success
            else:
                round_total = 0
                round_success = 0
            all_latencies = [
                latency for state in self.sandbox_states.values() for latency in state.replay_metrics.latencies[-10:]
            ]
            snapshot = Snapshot(
                timestamp=now,
                elapsed=elapsed,
                total_sandboxes=len(self.sandbox_states),
                active_sandboxes=active_count,
                offline_sandboxes=offline_count,
                creation_stats=creation_stats,
                replay_total=task_total,
                replay_success=task_success,
                replay_avg_latency=statistics.mean(all_latencies) if all_latencies else 0.0,
                replay_p99_latency=calc_p99(all_latencies),
                round_total=round_total,
                round_success=round_success,
            )
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

        self.snapshots.append(snapshot)

        # Track round-specific snapshots
        if self.current_round is not None:
            self.round_snapshots[self.current_round].append(snapshot)

        # Real-time terminal output
        self._print_snapshot(snapshot)

    def _print_snapshot(self, snapshot: Snapshot) -> None:
        """Emit real-time snapshot to the log stream."""
        logger.info(f"\n{'─' * 70}")
        logger.info(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        logger.info(f"{'─' * 70}")
        logger.info(f"  Sandboxes: {snapshot.active_sandboxes:3d} ready / {snapshot.offline_sandboxes:2d} offline")

        if self.config.workflow_type == "coding":
            logger.info(
                f"  Coding:    {snapshot.coding_success:3d}/{snapshot.coding_total:3d}  "
                f"avg={snapshot.coding_avg_latency:.2f}s  p99={snapshot.coding_p99_latency:.2f}s"
            )
        elif self.config.workflow_type == "document":
            logger.info(
                f"  Document:  {snapshot.document_success:3d}/{snapshot.document_total:3d}  "
                f"avg={snapshot.document_avg_latency:.2f}s  p99={snapshot.document_p99_latency:.2f}s"
            )
        elif self.config.workflow_type == "browser":
            logger.info(
                f"  Browser:   {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  "
                f"avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s"
            )
        elif self.config.workflow_type == "replay":
            logger.info(
                f"  Replay:    {snapshot.replay_success:3d}/{snapshot.replay_total:3d}  "
                f"avg={snapshot.replay_avg_latency:.2f}s  p99={snapshot.replay_p99_latency:.2f}s"
            )
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")
        logger.info(f"{'─' * 70}")

    def _resolved_wall_sec(self) -> float | None:
        """Measured wall-clock since ``start()``, or None if ``start()`` was never called."""
        if self.start_time:
            return time.time() - self.start_time
        return None

    def format_replay_stats_section(self) -> list[str]:
        """Delegate to ReportFormatter.format_replay_stats_section."""
        formatter = ReportFormatter(
            self.config,
            self.sandbox_states,
            self.provider_label,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self._resolved_wall_sec(),
        )
        return formatter.format_replay_stats_section()

    def generate_report(self) -> str:
        """Generate final TXT report using ReportFormatter."""
        formatter = ReportFormatter(
            self.config,
            self.sandbox_states,
            self.provider_label,
            admission_snapshot=self.admission_snapshot,
            wall_sec=self._resolved_wall_sec(),
        )

        lines: list[str] = []

        # Configuration section
        lines.extend(formatter.format_config_section())

        # Sandbox status section
        lines.extend(formatter.format_sandbox_status_section())

        # Creation performance sections
        ready_states = [s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.READY]

        create_times = [
            s.creation_metrics.create_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.create_elapsed > 0
            and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
        ]
        create_desc = (
            "sandbox.create API call time, excluding ready check"
            if self.config.workflow_type in {"coding", "document", "replay"}
            else "sandbox.create API call time, excluding port wait"
        )
        lines.extend(formatter.format_percentile_section("Sandbox.create Performance", create_times, create_desc))

        ready_check_times = [
            s.creation_metrics.ready_check_elapsed for s in ready_states if s.creation_metrics.ready_check_elapsed > 0
        ]

        # Use workflow-specific labels for ready-check performance
        if self.config.workflow_type == "coding":
            ready_check_title = "Ready Check Wait Performance"
            ready_check_desc = "Waiting for 'uname -a' command response"
        elif self.config.workflow_type == "document":
            ready_check_title = "Document Asset Check Performance"
            ready_check_desc = "Running document-bench-validate inside the sandbox"
        elif self.config.workflow_type == "browser":
            ready_check_title = "Port Check Wait Performance"
            ready_check_desc = "Waiting for 18789 openclaw-gateway + 11436 llama-server ports"
        elif self.config.workflow_type == "replay":
            ready_check_title = "Ready Check Wait Performance"
            ready_check_desc = "Waiting for 'uname -a' command response"
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

        lines.extend(formatter.format_percentile_section(ready_check_title, ready_check_times, ready_check_desc))

        total_times = [s.creation_metrics.total_elapsed for s in ready_states if s.creation_metrics.total_elapsed > 0]
        total_desc = (
            "sandbox.create + ready check"
            if self.config.workflow_type in {"coding", "document", "replay"}
            else "sandbox.create + port wait"
        )
        lines.extend(formatter.format_percentile_section("Total Startup Performance", total_times, total_desc))

        # Task statistics -- dispatch based on workflow type
        if self.config.workflow_type == "coding":
            lines.extend(formatter.format_coding_stats_section())
            lines.extend(formatter.format_coding_step_timing_table())
        elif self.config.workflow_type == "document":
            lines.extend(formatter.format_document_stats_section())
            lines.extend(formatter.format_document_step_timing_table())
        elif self.config.workflow_type == "browser":
            lines.extend(formatter.format_browser_stats_section())
            lines.extend(formatter.format_step_timing_table())
        elif self.config.workflow_type == "replay":
            lines.extend(formatter.format_replay_stats_section())
            lines.extend(formatter.format_throughput_section())
            lines.extend(formatter.format_trajectory_summary_section())
            lines.extend(formatter.format_replay_step_timing_table())
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

        # Error details
        lines.extend(formatter.format_error_section())

        # Round comparison
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
