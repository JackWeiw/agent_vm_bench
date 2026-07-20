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
from typing import Dict, List, Optional

from .config import Config
from .schemas import SandboxState, SandboxStatus, TestSnapshot
from .utils import calc_p99, calc_percentiles


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
        sandbox_latency_counts = {
            s.sandbox_id: len(s.browser_metrics.latencies) for s in self.sandbox_states.values()
        }

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

        print(
            f"  Browser:   {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  "
            f"avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s"
        )
        print(f"{'─' * 70}")

    def generate_report(self) -> str:
        """Generate final TXT report"""
        lines: List[str] = []
        lines.append("=" * 80)
        lines.append("E2B Sandbox Bench - Performance Report")
        lines.append("=" * 80)

        # Configuration info
        lines.append("\n[Test Configuration]")
        lines.append(f"  Template:        {self.config.template}")
        lines.append(f"  Total Sandboxes: {self.config.total_count}")

        # Display mode
        if self.config.detect_existing:
            lines.append("  Mode:            Detect existing sandboxes")
        elif self.config.create_only:
            lines.append("  Mode:            Create-only (Phase 0)")
        else:
            lines.append("  Mode:            Full workflow")

        # Create batch config
        if self.config.create_batch_size:
            lines.append(
                f"  Create Batch:    {self.config.create_batch_count} batches x {self.config.create_batch_size} sandboxes"
            )
            lines.append(f"  Create Interval: {self.config.create_batch_interval}s")
        else:
            lines.append("  Create Batch:    Full concurrent creation")

        # Task batch config
        if not self.config.create_only:
            if self.config.task_batch_size:
                lines.append(
                    f"  Task Batch:      {self.config.task_batch_count} batches x {self.config.task_batch_size} sandboxes"
                )
                lines.append(f"  Task Interval:   {self.config.task_batch_interval}s")
            else:
                lines.append("  Task Batch:      Full concurrent start")

        lines.append(f"  Test Duration:   {self.config.test_duration}s")

        # Sandbox status statistics
        ready_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]
        failed_states = [s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.FAILED]
        port_failed_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_FAILED
        ]
        offline_states = [s for s in self.sandbox_states.values() if not s.is_alive]

        lines.append("\n[Sandbox Status]")
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

        # sandbox.create performance statistics
        create_times = [
            s.creation_metrics.create_elapsed
            for s in self.sandbox_states.values()
            if s.creation_metrics.create_elapsed > 0
            and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
        ]
        if create_times:
            stats = calc_percentiles(create_times)
            lines.append("\n[Sandbox.create Performance]")
            lines.append("  (sandbox.create API call time, excluding port wait)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Port wait performance statistics
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in ready_states if s.creation_metrics.port_wait_elapsed > 0
        ]
        if port_wait_times:
            stats = calc_percentiles(port_wait_times)
            lines.append("\n[Port Check Wait Performance]")
            lines.append("  (Waiting for 18789 openclaw-gateway + 11436 llama-server ports)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Total startup time (create + port_wait)
        total_times = [s.creation_metrics.total_elapsed for s in ready_states if s.creation_metrics.total_elapsed > 0]
        if total_times:
            stats = calc_percentiles(total_times)
            lines.append("\n[Total Startup Performance]")
            lines.append("  (sandbox.create + port wait)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Browser task statistics
        all_latencies: List[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.browser_metrics.latencies)

        total_tasks = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        total_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.browser_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.browser_metrics.timeout_count for s in self.sandbox_states.values())

        lines.append("\n[Browser Task Statistics]")
        lines.append(f"  Total Tasks:   {total_tasks}")
        lines.append(f"  Success:       {total_success}")
        lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")

        if all_latencies:
            avg_ms = statistics.mean(all_latencies) * 1000
            p99_ms = calc_p99(all_latencies) * 1000
            lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
            lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

        # Step-level timing statistics for tab-switch mode
        all_step_times: Dict[str, List[float]] = {}
        for s in self.sandbox_states.values():
            step_stats = s.browser_metrics.get_step_stats()
            for step_name, stats in step_stats.items():
                if step_name not in all_step_times:
                    all_step_times[step_name] = []
                # Get raw times from metrics (need to access internal data)
                for _ in range(stats["count"]):
                    all_step_times[step_name].append(stats["avg"])  # Approximate

        # Better approach: aggregate step times directly
        aggregated_steps: Dict[str, Dict[str, float]] = {}
        for s in self.sandbox_states.values():
            step_stats = s.browser_metrics.get_step_stats()
            for step_name, stats in step_stats.items():
                if step_name not in aggregated_steps:
                    aggregated_steps[step_name] = {"total_time": 0.0, "count": 0, "max_p99": 0.0}
                aggregated_steps[step_name]["total_time"] += stats["avg"] * stats["count"]
                aggregated_steps[step_name]["count"] += stats["count"]
                aggregated_steps[step_name]["max_p99"] = max(aggregated_steps[step_name]["max_p99"], stats["p99"])

        if aggregated_steps:
            lines.append("\n[Step-Level Timing (Tab-Switch Mode)]")
            lines.append(f"  {'Step':<15} {'Count':<8} {'Avg(ms)':<12} {'P99(ms)':<12}")
            lines.append("  " + "-" * 50)
            # Use actual step names: open_tab (not tab_switch), snapshot, click, screenshot
            for step_name in ["open_tab", "snapshot", "click", "screenshot"]:
                if step_name in aggregated_steps:
                    stats = aggregated_steps[step_name]
                    avg_ms = (stats["total_time"] / stats["count"]) * 1000 if stats["count"] > 0 else 0
                    p99_ms = stats["max_p99"] * 1000
                    lines.append(f"  {step_name:<15} {stats['count']:<8} {avg_ms:<12.1f} {p99_ms:<12.1f}")

        # Collect error details from failed sandboxes
        failed_sandbox_errors = []
        for s in self.sandbox_states.values():
            if s.browser_metrics.failed_count > 0 and s.browser_metrics.last_error:
                failed_sandbox_errors.append(
                    (s.sandbox_id, s.browser_metrics.failed_count, s.browser_metrics.last_error)
                )

        if failed_sandbox_errors:
            # Sort by failed count (descending)
            failed_sandbox_errors.sort(key=lambda x: x[1], reverse=True)
            lines.append("\n[Failed Sandbox Error Details]")
            lines.append(f"  Total sandboxes with task failures: {len(failed_sandbox_errors)}")
            lines.append("  (Top 10 sandboxes with most failures)")
            for sid, count, error in failed_sandbox_errors[:10]:
                # Truncate error if too long
                error_display = error[:150] if len(error) > 150 else error
                lines.append(f"  Sandbox{sid}: {count} failures - {error_display}")

            # Error type classification
            lines.append("\n[Error Type Classification]")
            error_types = {
                "Chrome start failed": 0,
                "D-Bus connection error": 0,
                "Gateway connection error": 0,
                "Timeout": 0,
                "Open tab failed": 0,
                "Page load failed": 0,
                "Snapshot failed": 0,
                "Click failed": 0,
                "Screenshot failed": 0,
                "Other": 0,
            }
            error_type_sandboxes = {
                "Chrome start failed": [],
                "D-Bus connection error": [],
                "Gateway connection error": [],
                "Timeout": [],
                "Open tab failed": [],
                "Page load failed": [],
                "Snapshot failed": [],
                "Click failed": [],
                "Screenshot failed": [],
                "Other": [],
            }
            for sid, count, error in failed_sandbox_errors:
                error_lower = error.lower()
                # Legacy error types (from old BrowserTaskRunner)
                if "failed to start chrome" in error_lower or "chrome_start" in error_lower:
                    error_types["Chrome start failed"] += count
                    error_type_sandboxes["Chrome start failed"].append(sid)
                elif "d-bus" in error_lower or "dbus" in error_lower or "failed to connect to the bus" in error_lower:
                    error_types["D-Bus connection error"] += count
                    error_type_sandboxes["D-Bus connection error"].append(sid)
                elif "gateway" in error_lower or "cdp" in error_lower or "http_unreachable" in error_lower:
                    error_types["Gateway connection error"] += count
                    error_type_sandboxes["Gateway connection error"].append(sid)
                # New error types (from TabOperationRunner)
                elif "open_tab failed" in error_lower:
                    error_types["Open tab failed"] += count
                    error_type_sandboxes["Open tab failed"].append(sid)
                elif "page_load failed" in error_lower:
                    error_types["Page load failed"] += count
                    error_type_sandboxes["Page load failed"].append(sid)
                elif "snapshot failed" in error_lower:
                    error_types["Snapshot failed"] += count
                    error_type_sandboxes["Snapshot failed"].append(sid)
                elif "click failed" in error_lower:
                    error_types["Click failed"] += count
                    error_type_sandboxes["Click failed"].append(sid)
                elif "screenshot failed" in error_lower:
                    error_types["Screenshot failed"] += count
                    error_type_sandboxes["Screenshot failed"].append(sid)
                elif "timeout" in error_lower or "timed out" in error_lower:
                    error_types["Timeout"] += count
                    error_type_sandboxes["Timeout"].append(sid)
                else:
                    error_types["Other"] += count
                    error_type_sandboxes["Other"].append(sid)

            for error_type, count in error_types.items():
                if count > 0:
                    sids = error_type_sandboxes[error_type][:10]
                    lines.append(f"  {error_type}: {count} errors (sandboxes: {sids}...)")

        # Round comparison for round-robin mode
        if self._round_start_totals:
            lines.append("\n" + "=" * 80)
            lines.append("[Round Comparison]")
            lines.append("=" * 80)

            # Calculate final totals by computing delta for each round
            # Final total for round = last cumulative - round start cumulative
            total_rounds = len(self._round_start_totals)
            total_tasks = 0
            total_success = 0
            round_finals: Dict[int, Dict[str, any]] = {}

            # Get final cumulative values
            final_browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
            final_browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
            final_sandbox_latency_counts = {
                s.sandbox_id: len(s.browser_metrics.latencies) for s in self.sandbox_states.values()
            }

            # Compute each round's delta
            for round_id in sorted(self._round_start_totals.keys()):
                start_total = self._round_start_totals[round_id]["total"]
                start_success = self._round_start_totals[round_id]["success"]
                start_sandbox_latency_counts = self._round_start_totals[round_id]["sandbox_latency_counts"]

                # For the last round, use final cumulative; otherwise use next round's start
                if round_id == max(self._round_start_totals.keys()):
                    end_total = final_browser_total
                    end_success = final_browser_success
                    end_sandbox_latency_counts = final_sandbox_latency_counts
                else:
                    next_round = round_id + 1
                    if next_round in self._round_start_totals:
                        end_total = self._round_start_totals[next_round]["total"]
                        end_success = self._round_start_totals[next_round]["success"]
                        end_sandbox_latency_counts = self._round_start_totals[next_round]["sandbox_latency_counts"]
                    else:
                        end_total = final_browser_total
                        end_success = final_browser_success
                        end_sandbox_latency_counts = final_sandbox_latency_counts

                tasks = end_total - start_total
                success = end_success - start_success
                round_finals[round_id] = {
                    "tasks": tasks,
                    "success": success,
                    "end_sandbox_latency_counts": end_sandbox_latency_counts,
                    "start_sandbox_latency_counts": start_sandbox_latency_counts,
                }
                total_tasks += tasks
                total_success += success

            lines.append(f"  Summary: {total_tasks} tasks across {total_rounds} rounds")
            lines.append("")

            lines.append(f"{'Round':<8} {'Tasks':<8} {'Success%':<10} {'Avg(s)':<10} {'P99(s)':<10}")
            lines.append("-" * 50)

            for round_id in sorted(round_finals.keys()):
                tasks = round_finals[round_id]["tasks"]
                success = round_finals[round_id]["success"]

                # Extract latencies for this round using per-sandbox latency counts
                round_latencies: List[float] = []
                start_counts = round_finals[round_id]["start_sandbox_latency_counts"]
                end_counts = round_finals[round_id]["end_sandbox_latency_counts"]

                for s in self.sandbox_states.values():
                    sandbox_id = s.sandbox_id
                    start_count = start_counts.get(sandbox_id, 0)
                    end_count = end_counts.get(sandbox_id, len(s.browser_metrics.latencies))
                    # Get latencies added during this round
                    round_latencies.extend(s.browser_metrics.get_latencies_since(start_count)[:end_count - start_count])

                if round_latencies:
                    avg = statistics.mean(round_latencies)
                    p99 = calc_p99(round_latencies)
                else:
                    avg = 0.0
                    p99 = 0.0

                rate = success / max(1, tasks) * 100 if tasks > 0 else 0.0
                lines.append(f"{round_id:<8} {tasks:<8} {rate:<10.1f} {avg:<10.2f} {p99:<10.2f}")

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
