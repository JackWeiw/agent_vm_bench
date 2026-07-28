"""
Statistics Collection Module

Responsible for real-time snapshot collection, terminal output and final report generation
Includes detailed sandbox creation time, port wait time, browser task time statistics
"""

import os
import statistics
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .schemas import SandboxState, SandboxStatus, TestSnapshot
from .utils import calc_p99, calc_percentiles, calc_tail_ratio, classify_tail_latency


class ErrorClassifier:
    """Error type classification for sandbox failures."""

    # Error type definitions with patterns (order matters - first match wins)
    ERROR_TYPES = [
        ("Open tab failed", ["open_tab failed"]),
        ("Page load failed", ["page_load failed"]),
        ("Snapshot failed", ["snapshot failed"]),
        ("Click failed", ["click failed"]),
        ("Screenshot failed", ["screenshot failed"]),
        ("Chrome start failed", ["failed to start chrome", "chrome_start"]),
        ("D-Bus connection error", ["d-bus", "dbus", "failed to connect to the bus"]),
        ("Gateway connection error", ["gateway", "cdp", "http_unreachable"]),
        ("Timeout", ["timeout", "timed out"]),
    ]

    @classmethod
    def classify(cls, error: str) -> str:
        """Classify error message into error type."""
        error_lower = error.lower()
        for error_type, patterns in cls.ERROR_TYPES:
            if any(pattern in error_lower for pattern in patterns):
                return error_type
        return "Other"

    @classmethod
    def aggregate(cls, errors: List[Tuple[int, int, str]]) -> Tuple[Dict[str, int], Dict[str, List[int]]]:
        """Aggregate errors by type.

        Args:
            errors: List of (sandbox_id, count, error_message)

        Returns:
            Tuple of (error_counts, error_sandbox_ids)
        """
        error_counts: Dict[str, int] = {}
        error_sandbox_ids: Dict[str, List[int]] = {}

        for sid, count, error in errors:
            error_type = cls.classify(error)
            error_counts[error_type] = error_counts.get(error_type, 0) + count
            if error_type not in error_sandbox_ids:
                error_sandbox_ids[error_type] = []
            error_sandbox_ids[error_type].append(sid)

        return error_counts, error_sandbox_ids


class TableFormatter:
    """Simple table formatter for plain text output."""

    @staticmethod
    def format_table(headers: List[str], rows: List[List[str]], title: str = "") -> List[str]:
        """Format a table with aligned columns."""
        if not rows:
            return []

        lines = []
        if title:
            lines.append(title)

        # Calculate column widths
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        # Header row
        header_line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
        lines.append(header_line)

        # Separator
        sep_line = "  ".join("-" * w for w in widths)
        lines.append(sep_line)

        # Data rows
        for row in rows:
            line = "  ".join(cell.ljust(w) for cell, w in zip(row, widths))
            lines.append(line)

        return lines


class ReportFormatter:
    """Format statistics into human-readable reports."""

    def __init__(self, config: Config, sandbox_states: Dict[int, SandboxState]):
        self.config = config
        self.sandbox_states = sandbox_states

    def format_config_section(self) -> List[str]:
        """Format test configuration section."""
        lines = ["=" * 80, "E2B Sandbox Bench - Performance Report", "=" * 80]
        lines.append("\n[Test Configuration]")
        lines.append(f"  Template:        {self.config.template}")
        lines.append(f"  Total Sandboxes: {self.config.total_count}")

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

    def format_sandbox_status_section(self) -> List[str]:
        """Format sandbox status section."""
        ready_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]
        failed_states = [s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.FAILED]
        port_failed_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_FAILED
        ]
        offline_states = [s for s in self.sandbox_states.values() if not s.is_alive]

        lines = ["\n[Sandbox Status]"]
        lines.append(
            f"  Created (API):       {len([s for s in self.sandbox_states.values() if s.creation_metrics.status not in (SandboxStatus.PENDING, SandboxStatus.CREATING)])} / {len(self.sandbox_states)}"
        )
        lines.append(f"  Ports Ready:         {len(ready_states)} / {len(self.sandbox_states)}")
        lines.append(f"  Create Failed:       {len(failed_states)}")
        lines.append(f"  Port Check Failed:   {len(port_failed_states)}")
        lines.append(f"  Offline (runtime):   {len(offline_states)}")

        if failed_states:
            lines.append(f"  Create Failed IDs:   {[s.sandbox_id for s in failed_states[:10]]}")
        if port_failed_states:
            lines.append(f"  Port Failed IDs:     {[s.sandbox_id for s in port_failed_states[:10]]}")
        if offline_states:
            lines.append(f"  Offline IDs:         {[s.sandbox_id for s in offline_states[:10]]}")

        return lines

    def format_percentile_section(self, title: str, values: List[float], description: str = "") -> List[str]:
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

    def format_browser_stats_section(self) -> List[str]:
        """Format browser task statistics section."""
        all_latencies: List[float] = []
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

    def format_step_timing_table(self) -> List[str]:
        """Format step-level timing as a table."""
        # Collect all step times
        all_step_times: Dict[str, List[float]] = {}
        for s in self.sandbox_states.values():
            step_times_copy = s.browser_metrics.get_step_times_copy()
            for step_name, times in step_times_copy.items():
                if step_name not in all_step_times:
                    all_step_times[step_name] = []
                all_step_times[step_name].extend(times)

        if not all_step_times:
            return []

        lines = ["\n[Step-Level Timing (Tab-Switch Mode)]"]

        # Build table
        headers = ["Step", "Count", "Avg(ms)", "P50(ms)", "P95(ms)", "P99(ms)", "Tail"]
        rows = []

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

    def format_error_section(self) -> List[str]:
        """Format error details and classification section."""
        # Collect errors
        failed_sandbox_errors = []
        for s in self.sandbox_states.values():
            if s.browser_metrics.failed_count > 0 and s.browser_metrics.last_error:
                failed_sandbox_errors.append(
                    (s.sandbox_id, s.browser_metrics.failed_count, s.browser_metrics.last_error)
                )

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

        # Build table
        headers = ["Error Type", "Count", "Sandboxes"]
        rows = []

        for error_type in [
            "Open tab failed",
            "Page load failed",
            "Snapshot failed",
            "Click failed",
            "Screenshot failed",
            "Chrome start failed",
            "D-Bus connection error",
            "Gateway connection error",
            "Timeout",
            "Other",
        ]:
            if error_type in error_counts:
                count = error_counts[error_type]
                sids = error_sandbox_ids[error_type][:5]
                sids_display = str(sids) + ("..." if len(error_sandbox_ids[error_type]) > 5 else "")
                rows.append([error_type, str(count), sids_display])

        lines.extend(TableFormatter.format_table(headers, rows))
        return lines

    def format_llm_stats_section(self) -> List[str]:
        """Format LLM scenario statistics section."""
        all_latencies: List[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.llm_metrics.latencies)

        total_scenarios = sum(s.llm_metrics.total_scenarios for s in self.sandbox_states.values())
        total_success = sum(s.llm_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.llm_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.llm_metrics.timeout_count for s in self.sandbox_states.values())

        lines = ["\n[LLM Scenario Statistics]"]
        lines.append(f"  Total Scenarios: {total_scenarios}")
        lines.append(f"  Success:         {total_success}")
        lines.append(f"  Failed:          {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:    {total_success / max(1, total_scenarios) * 100:.1f}%")

        if all_latencies:
            avg_s = statistics.mean(all_latencies)
            p99_s = calc_p99(all_latencies)
            lines.append(f"  Avg Latency:     {avg_s:.2f}s")
            lines.append(f"  P99 Latency:     {p99_s:.2f}s")

        # Collect error details from failed sandboxes
        failed_sandbox_errors = []
        for s in self.sandbox_states.values():
            if s.llm_metrics.failed_count > 0 and s.llm_metrics.last_error:
                failed_sandbox_errors.append((s.sandbox_id, s.llm_metrics.failed_count, s.llm_metrics.last_error))

        if failed_sandbox_errors:
            failed_sandbox_errors.sort(key=lambda x: x[1], reverse=True)
            lines.append("\n[Failed Sandbox Error Details]")
            lines.append(f"  Total sandboxes with scenario failures: {len(failed_sandbox_errors)}")
            lines.append("  (Top 10 sandboxes with most failures)")
            for sid, count, error in failed_sandbox_errors[:10]:
                error_display = error[:150] if len(error) > 150 else error
                lines.append(f"  Sandbox{sid}: {count} failures - {error_display}")

            # Error classification for LLM errors
            lines.append("\n[Error Type Classification]")
            error_counts, error_sandbox_ids = ErrorClassifier.aggregate(failed_sandbox_errors)

            headers = ["Error Type", "Count", "Sandboxes"]
            rows = []
            for error_type, count in sorted(error_counts.items(), key=lambda x: -x[1]):
                if count > 0:
                    sids = error_sandbox_ids[error_type][:5]
                    sids_display = str(sids) + ("..." if len(error_sandbox_ids[error_type]) > 5 else "")
                    rows.append([error_type, str(count), sids_display])

            lines.extend(TableFormatter.format_table(headers, rows))

        return lines

    def format_round_comparison_table(self, round_start_totals: Dict[int, Dict[str, Any]]) -> List[str]:
        """Format round comparison as a table."""
        if not round_start_totals:
            return []

        lines = ["\n" + "=" * 80, "[Round Comparison]", "=" * 80]

        # Calculate round finals (includes post-last-round sentinel with tasks=0)
        round_finals = self._calculate_round_finals(round_start_totals)

        # Filter out rounds with tasks=0 (post-last-round baseline sentinel)
        active_rounds = {k: v for k, v in round_finals.items() if v["tasks"] > 0}
        total_tasks = sum(r["tasks"] for r in active_rounds.values())

        lines.append(f"\n  Summary: {total_tasks} tasks across {len(active_rounds)} rounds")

        # Build table
        headers = ["Round", "Tasks", "Success%", "Avg(s)", "P50(s)", "P95(s)", "P99(s)", "Tail"]
        rows = []

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
        return lines

    def _calculate_round_finals(self, round_start_totals: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Calculate final statistics for each round."""
        round_finals: Dict[int, Dict[str, Any]] = {}

        # Get final cumulative values
        final_browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        final_browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        final_sandbox_latency_counts = {
            s.sandbox_id: len(s.browser_metrics.latencies) for s in self.sandbox_states.values()
        }

        for round_id in sorted(round_start_totals.keys()):
            start_total = round_start_totals[round_id]["total"]
            start_success = round_start_totals[round_id]["success"]
            start_sandbox_latency_counts = round_start_totals[round_id]["sandbox_latency_counts"]

            # Determine end values
            if round_id == max(round_start_totals.keys()):
                end_total = final_browser_total
                end_success = final_browser_success
                end_sandbox_latency_counts = final_sandbox_latency_counts
            else:
                next_round = round_id + 1
                if next_round in round_start_totals:
                    end_total = round_start_totals[next_round]["total"]
                    end_success = round_start_totals[next_round]["success"]
                    end_sandbox_latency_counts = round_start_totals[next_round]["sandbox_latency_counts"]
                else:
                    end_total = final_browser_total
                    end_success = final_browser_success
                    end_sandbox_latency_counts = final_sandbox_latency_counts

            # Extract latencies for this round
            round_latencies: List[float] = []
            for s in self.sandbox_states.values():
                sandbox_id = s.sandbox_id
                start_count = start_sandbox_latency_counts.get(sandbox_id, 0)
                end_count = end_sandbox_latency_counts.get(sandbox_id, len(s.browser_metrics.latencies))
                round_latencies.extend(s.browser_metrics.get_latencies_since(start_count)[: end_count - start_count])

            tasks = end_total - start_total
            success = end_success - start_success
            round_finals[round_id] = {
                "tasks": tasks,
                "success": success,
                "latencies": round_latencies,
            }

        return round_finals


class StatsCollector:
    """Statistics collector - real-time snapshot + final report"""

    def __init__(self, config: Config, sandbox_states: Dict[int, SandboxState]):
        self.config = config
        self.sandbox_states = sandbox_states
        self.snapshots: List[TestSnapshot] = []
        self.start_time: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Round tracking for round-robin mode
        self.current_round: Optional[int] = None
        self.round_snapshots: Dict[int, List[TestSnapshot]] = {}

        # Round start totals - recorded at round switch to decouple from snapshot timing
        # Key: round_id, Value: {"total": int, "success": int, "sandbox_latency_counts": Dict[int, int]}
        # sandbox_latency_counts: {sandbox_id: latency_count} - tracks how many latencies each sandbox has at round start
        self._round_start_totals: Dict[int, Dict[str, any]] = {}

    def start(self) -> None:
        """Start background collection thread"""
        self.start_time = time.time()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop collection"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_round(self, round_id: Optional[int]) -> None:
        """Set current round for statistics tracking.

        Called by RoundRobinTaskManager to mark which round is currently active.
        Snapshots collected during this round will be grouped together.

        Key design: Record cumulative totals at the moment of round switch.
        This decouples round delta calculation from snapshot timing.

        Args:
            round_id: Current round index (None to clear)
        """
        # Get current cumulative totals before switching rounds
        browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        # Track latency count per sandbox for accurate round latency extraction
        sandbox_latency_counts = {s.sandbox_id: len(s.browser_metrics.latencies) for s in self.sandbox_states.values()}

        # Switch to new round
        self.current_round = round_id

        if round_id is not None:
            # Initialize snapshot list if needed
            if round_id not in self.round_snapshots:
                self.round_snapshots[round_id] = []

            # CRITICAL: Only record baseline if this round doesn't have one yet
            # This prevents overwriting when cycling (round 0 runs again later)
            if round_id not in self._round_start_totals:
                self._round_start_totals[round_id] = {
                    "total": browser_total,
                    "success": browser_success,
                    "sandbox_latency_counts": sandbox_latency_counts,
                }

    def _collect_loop(self) -> None:
        """Periodic snapshot collection"""
        while not self._stop.is_set():
            self._take_snapshot()
            time.sleep(self.config.stats_interval)

    def _take_snapshot(self) -> None:
        """Collect current snapshot"""
        now = time.time()
        elapsed = now - self.start_time

        # Sandbox status statistics
        active_count = sum(
            1
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.is_alive
        )
        offline_count = sum(
            1
            for s in self.sandbox_states.values()
            if not s.is_alive
            or s.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.PORT_FAILED, SandboxStatus.OFFLINE)
        )

        # Creation performance statistics (only port-ready sandboxes)
        create_times = [
            s.creation_metrics.create_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.creation_metrics.create_elapsed > 0
        ]
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.creation_metrics.port_wait_elapsed > 0
        ]
        total_times = [
            s.creation_metrics.total_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.creation_metrics.total_elapsed > 0
        ]

        creation_stats = {
            "create": calc_percentiles(create_times),
            "port_wait": calc_percentiles(port_wait_times),
            "total": calc_percentiles(total_times),
        }

        # Build snapshot based on task mode
        if self.config.task_mode == "llm":
            # LLM scenario statistics
            llm_total = sum(s.llm_metrics.total_scenarios for s in self.sandbox_states.values())
            llm_success = sum(s.llm_metrics.success_count for s in self.sandbox_states.values())
            llm_failed = sum(s.llm_metrics.failed_count for s in self.sandbox_states.values())

            # Collect latency data
            all_latencies: List[float] = []
            for s in self.sandbox_states.values():
                all_latencies.extend(s.llm_metrics.latencies)

            llm_avg = statistics.mean(all_latencies) if all_latencies else 0.0
            llm_p99 = calc_p99(all_latencies)

            snapshot = TestSnapshot(
                timestamp=now,
                elapsed=elapsed,
                total_sandboxes=len(self.sandbox_states),
                active_sandboxes=active_count,
                offline_sandboxes=offline_count,
                creation_stats=creation_stats,
                # LLM metrics
                llm_total=llm_total,
                llm_success=llm_success,
                llm_failed=llm_failed,
                llm_avg_latency=llm_avg,
                llm_p99_latency=llm_p99,
            )
        else:
            # Browser task statistics (cumulative)
            browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
            browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())

            # Calculate round delta: current cumulative - round start cumulative
            # This is decoupled from snapshot timing
            if self.current_round is not None and self.current_round in self._round_start_totals:
                start_total = self._round_start_totals[self.current_round]["total"]
                start_success = self._round_start_totals[self.current_round]["success"]
                round_total = browser_total - start_total
                round_success = browser_success - start_success
            else:
                round_total = 0
                round_success = 0

            # Collect recent latency data (last 10 per sandbox)
            all_latencies: List[float] = []
            for s in self.sandbox_states.values():
                all_latencies.extend(s.browser_metrics.latencies[-10:])

            browser_avg = statistics.mean(all_latencies) if all_latencies else 0.0
            browser_p99 = calc_p99(all_latencies)

            snapshot = TestSnapshot(
                timestamp=now,
                elapsed=elapsed,
                total_sandboxes=len(self.sandbox_states),
                active_sandboxes=active_count,
                offline_sandboxes=offline_count,
                creation_stats=creation_stats,
                browser_total=browser_total,
                browser_success=browser_success,
                browser_avg_latency=browser_avg,
                browser_p99_latency=browser_p99,
            )

            # Add per-round fields for round comparison
            snapshot.round_total = round_total
            snapshot.round_success = round_success

        self.snapshots.append(snapshot)

        # Track round-specific snapshots
        if self.current_round is not None:
            self.round_snapshots[self.current_round].append(snapshot)

        # Real-time terminal output
        self._print_snapshot(snapshot)

    def _print_snapshot(self, snapshot: TestSnapshot) -> None:
        """Print real-time snapshot"""
        print(f"\n{'─' * 70}")
        print(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        print(f"{'─' * 70}")
        print(f"  Sandboxes: {snapshot.active_sandboxes:3d} ready / {snapshot.offline_sandboxes:2d} offline")

        if snapshot.creation_stats.get("create") and snapshot.creation_stats["create"]["avg"] > 0:
            print(
                f"  Create:    avg={snapshot.creation_stats['create']['avg']:.1f}s  "
                f"p99={snapshot.creation_stats['create']['p99']:.1f}s"
            )
        if snapshot.creation_stats.get("port_wait") and snapshot.creation_stats["port_wait"]["avg"] > 0:
            print(
                f"  PortWait:  avg={snapshot.creation_stats['port_wait']['avg']:.1f}s  "
                f"p99={snapshot.creation_stats['port_wait']['p99']:.1f}s"
            )

        # Print metrics based on task mode
        if self.config.task_mode == "llm":
            print(
                f"  LLM:       {snapshot.llm_success:3d}/{snapshot.llm_total:3d} success  "
                f"avg={snapshot.llm_avg_latency:.2f}s  p99={snapshot.llm_p99_latency:.2f}s"
            )
        else:
            print(
                f"  Browser:   {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  "
                f"avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s"
            )
        print(f"{'─' * 70}")

    def generate_report(self) -> str:
        """Generate final TXT report using ReportFormatter."""
        formatter = ReportFormatter(self.config, self.sandbox_states)

        lines: List[str] = []

        # Configuration section
        lines.extend(formatter.format_config_section())

        # Display task mode info
        if self.config.task_mode == "llm":
            lines.append("  Task Mode:       LLM Scenario")
            lines.append(f"  LLM Endpoint:    {self.config.llm.endpoint}")
            lines.append(f"  Scenario:        {self.config.llm.model}")

        # Sandbox status section
        lines.extend(formatter.format_sandbox_status_section())

        # Creation performance sections
        ready_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]

        create_times = [
            s.creation_metrics.create_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.create_elapsed > 0
            and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
        ]
        lines.extend(
            formatter.format_percentile_section(
                "Sandbox.create Performance", create_times, "sandbox.create API call time, excluding port wait"
            )
        )

        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in ready_states if s.creation_metrics.port_wait_elapsed > 0
        ]
        lines.extend(
            formatter.format_percentile_section(
                "Port Check Wait Performance",
                port_wait_times,
                "Waiting for 18789 openclaw-gateway + 11436 llama-server ports",
            )
        )

        total_times = [s.creation_metrics.total_elapsed for s in ready_states if s.creation_metrics.total_elapsed > 0]
        lines.extend(
            formatter.format_percentile_section("Total Startup Performance", total_times, "sandbox.create + port wait")
        )

        # Task statistics based on mode
        if self.config.task_mode == "llm":
            lines.extend(formatter.format_llm_stats_section())
        else:
            # Browser statistics
            lines.extend(formatter.format_browser_stats_section())

            # Step-level timing
            lines.extend(formatter.format_step_timing_table())

            # Error details
            lines.extend(formatter.format_error_section())

            # Round comparison
            lines.extend(formatter.format_round_comparison_table(self._round_start_totals))

        lines.append("\n" + "=" * 80)
        return "\n".join(lines)

    def save_report(self, report: str) -> str:
        """Save report to file"""
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.config.filename_prefix}_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)

        return filepath
