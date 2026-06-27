"""
Statistics Collector Module

Real-time snapshot collection, terminal output and final report generation
"""

import time
import threading
import statistics
import os
from datetime import datetime
from typing import List, Dict

from .config import Config
from .schemas import VMState, VMStatus, TestSnapshot, OOMType
from .utils import calc_percentiles, calc_p99


class StatsCollector:
    """Statistics collector - real-time snapshot + final report"""

    def __init__(self, config: Config, vm_states: Dict[int, VMState]):
        self.config = config
        self.vm_states = vm_states
        self.snapshots: List[TestSnapshot] = []
        self.start_time: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

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

    def _collect_loop(self) -> None:
        """Periodic snapshot collection"""
        while not self._stop.is_set():
            self._take_snapshot()
            time.sleep(self.config.stats_interval)

    def _take_snapshot(self) -> None:
        """Collect current snapshot"""
        now = time.time()
        elapsed = now - self.start_time

        # VM status counts
        active_count = sum(
            1 for s in self.vm_states.values()
            if s.connection_metrics.status == VMStatus.CONNECTED and s.health.is_connected
        )
        offline_count = sum(
            1 for s in self.vm_states.values()
            if not s.health.is_connected or s.connection_metrics.status in (VMStatus.OFFLINE, VMStatus.SHUTOFF)
        )
        failure_count = sum(1 for s in self.vm_states.values() if s.has_task_failure)

        # Creation stats (Phase 0)
        create_times = [
            s.creation_metrics.elapsed for s in self.vm_states.values()
            if s.creation_metrics.status == VMStatus.ACTIVE and s.creation_metrics.elapsed > 0
        ]
        creation_stats = calc_percentiles(create_times)

        # Connection stats (Phase 1)
        connect_times = [
            s.connection_metrics.connect_elapsed for s in self.vm_states.values()
            if s.connection_metrics.status == VMStatus.CONNECTED and s.connection_metrics.connect_elapsed > 0
        ]
        connection_stats = calc_percentiles(connect_times)

        # Task stats
        if self.config.task_mode == "browser":
            browser_total = sum(s.browser_metrics.total_tasks for s in self.vm_states.values())
            browser_success = sum(s.browser_metrics.success_count for s in self.vm_states.values())

            all_latencies = []
            for s in self.vm_states.values():
                all_latencies.extend(s.browser_metrics.latencies[-10:])

            browser_avg = statistics.mean(all_latencies) if all_latencies else 0.0
            browser_p99 = calc_p99(all_latencies)

            # Task type breakdown
            browser_type_stats: Dict[str, Dict[str, int]] = {}
            for s in self.vm_states.values():
                for tname, tcounts in s.browser_metrics.task_type_counts.items():
                    if tname not in browser_type_stats:
                        browser_type_stats[tname] = {"success": 0, "failed": 0}
                    browser_type_stats[tname]["success"] += tcounts.get("success", 0)
                    browser_type_stats[tname]["failed"] += tcounts.get("failed", 0)

            snapshot = TestSnapshot(
                timestamp=now,
                elapsed=elapsed,
                total_vms=len(self.vm_states),
                active_vms=active_count,
                offline_vms=offline_count,
                total_failure_vms=failure_count,
                creation_stats=creation_stats,
                connection_stats=connection_stats,
                browser_total=browser_total,
                browser_success=browser_success,
                browser_avg_latency=browser_avg,
                browser_p99_latency=browser_p99,
                browser_type_stats=browser_type_stats,
            )

        elif self.config.task_mode == "qa":
            qa_total = sum(s.qa_metrics.total_queries for s in self.vm_states.values())
            qa_success = sum(s.qa_metrics.success_count for s in self.vm_states.values())

            all_latencies = []
            for s in self.vm_states.values():
                all_latencies.extend(s.qa_metrics.latencies[-10:])

            qa_avg = statistics.mean(all_latencies) if all_latencies else 0.0
            qa_p99 = calc_p99(all_latencies)

            snapshot = TestSnapshot(
                timestamp=now,
                elapsed=elapsed,
                total_vms=len(self.vm_states),
                active_vms=active_count,
                offline_vms=offline_count,
                total_failure_vms=failure_count,
                creation_stats=creation_stats,
                connection_stats=connection_stats,
                qa_total=qa_total,
                qa_success=qa_success,
                qa_avg_latency=qa_avg,
                qa_p99_latency=qa_p99,
            )

        else:
            snapshot = TestSnapshot(
                timestamp=now,
                elapsed=elapsed,
                total_vms=len(self.vm_states),
                active_vms=active_count,
                offline_vms=offline_count,
                total_failure_vms=failure_count,
                creation_stats=creation_stats,
                connection_stats=connection_stats,
            )

        self.snapshots.append(snapshot)
        self._print_snapshot(snapshot)

    def _print_snapshot(self, snapshot: TestSnapshot) -> None:
        """Print real-time snapshot"""
        print(f"\n{'─'*70}")
        print(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        print(f"{'─'*70}")
        print(f"  VMs: {snapshot.active_vms:3d} active / {snapshot.offline_vms:2d} offline / {snapshot.total_failure_vms:2d} task failures")

        if snapshot.creation_stats and snapshot.creation_stats.get("avg", 0) > 0:
            print(f"  Create:  avg={snapshot.creation_stats['avg']:.1f}s  p99={snapshot.creation_stats['p99']:.1f}s")

        if snapshot.connection_stats and snapshot.connection_stats.get("avg", 0) > 0:
            print(f"  Connect: avg={snapshot.connection_stats['avg']:.1f}s  p99={snapshot.connection_stats['p99']:.1f}s")

        if self.config.task_mode == "browser":
            print(f"  Browser: {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s")
            if snapshot.browser_type_stats:
                for tname, tcounts in sorted(snapshot.browser_type_stats.items()):
                    print(f"    [{tname}] success={tcounts['success']} failed={tcounts['failed']}")

        elif self.config.task_mode == "qa":
            print(f"  QA:      {snapshot.qa_success:3d}/{snapshot.qa_total:3d}  avg={snapshot.qa_avg_latency:.2f}s  p99={snapshot.qa_p99_latency:.2f}s")

        print(f"{'─'*70}")

    def generate_report(self) -> str:
        """Generate final TXT report"""
        lines: List[str] = []
        lines.append("=" * 80)
        lines.append("VM Bench - Performance Report")
        lines.append("=" * 80)

        # Configuration
        lines.append(f"\n[Test Configuration]")
        lines.append(f"  Total VMs:      {self.config.total_count}")
        lines.append(f"  Task Mode:      {self.config.task_mode}")
        lines.append(f"  Test Duration:  {self.config.test_duration}s")

        if self.config.create_batch_size:
            lines.append(f"  Create Batch:   {self.config.create_batch_count} x {self.config.create_batch_size}")
        if self.config.connect_batch_size:
            lines.append(f"  Connect Batch:  {self.config.connect_batch_count} x {self.config.connect_batch_size}")

        # VM Status
        created_vms = [s for s in self.vm_states.values() if s.creation_metrics.status == VMStatus.ACTIVE]
        connected_vms = [s for s in self.vm_states.values() if s.connection_metrics.status == VMStatus.CONNECTED]
        offline_vms = [s for s in self.vm_states.values() if not s.health.is_connected]
        failed_creation = [s for s in self.vm_states.values() if s.creation_metrics.status == VMStatus.CREATE_FAILED]

        lines.append(f"\n[VM Status]")
        lines.append(f"  Created (ACTIVE):  {len(created_vms)}")
        lines.append(f"  Connected:         {len(connected_vms)}")
        lines.append(f"  Creation Failed:   {len(failed_creation)}")
        lines.append(f"  Offline:           {len(offline_vms)}")
        if failed_creation:
            lines.append(f"  Failed IDs:        {[s.vm_id for s in failed_creation[:10]]}")
        if offline_vms:
            lines.append(f"  Offline IDs:       {[s.vm_id for s in offline_vms[:10]]}")

        # Creation Performance
        create_times = [s.creation_metrics.elapsed for s in created_vms if s.creation_metrics.elapsed > 0]
        if create_times:
            stats = calc_percentiles(create_times)
            lines.append(f"\n[VM Creation Performance]")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Connection Performance
        connect_times = [s.connection_metrics.connect_elapsed for s in connected_vms if s.connection_metrics.connect_elapsed > 0]
        if connect_times:
            stats = calc_percentiles(connect_times)
            lines.append(f"\n[SSH Connection Performance]")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Task Performance
        if self.config.task_mode == "browser":
            all_latencies = []
            for s in self.vm_states.values():
                all_latencies.extend(s.browser_metrics.latencies)

            total_tasks = sum(s.browser_metrics.total_tasks for s in self.vm_states.values())
            total_success = sum(s.browser_metrics.success_count for s in self.vm_states.values())
            total_failed = sum(s.browser_metrics.failed_count for s in self.vm_states.values())
            total_timeout = sum(s.browser_metrics.timeout_count for s in self.vm_states.values())

            lines.append(f"\n[Browser Task Statistics]")
            lines.append(f"  Total Tasks:   {total_tasks}")
            lines.append(f"  Success:       {total_success}")
            lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
            lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")

            if all_latencies:
                avg_ms = statistics.mean(all_latencies) * 1000
                p99_ms = calc_p99(all_latencies) * 1000
                lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
                lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

        elif self.config.task_mode == "qa":
            all_latencies = []
            for s in self.vm_states.values():
                all_latencies.extend(s.qa_metrics.latencies)

            total_queries = sum(s.qa_metrics.total_queries for s in self.vm_states.values())
            total_success = sum(s.qa_metrics.success_count for s in self.vm_states.values())
            total_failed = sum(s.qa_metrics.failed_count for s in self.vm_states.values())

            lines.append(f"\n[QA Task Statistics]")
            lines.append(f"  Total Queries: {total_queries}")
            lines.append(f"  Success:       {total_success}")
            lines.append(f"  Failed:        {total_failed}")
            lines.append(f"  Success Rate:  {total_success / max(1, total_queries) * 100:.1f}%")

            if all_latencies:
                avg_ms = statistics.mean(all_latencies) * 1000
                p99_ms = calc_p99(all_latencies) * 1000
                lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
                lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

        lines.append("\n" + "=" * 80)
        return '\n'.join(lines)

    def save_report(self, report: str) -> str:
        """Save report to file"""
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.config.filename_prefix}_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)

        return filepath