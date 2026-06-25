"""
Statistics Collection Module

Responsible for real-time snapshot collection, terminal output and final report generation
Includes detailed container creation time, port wait time, browser task time statistics
Calculates QPS (queries per second) as core performance metric
"""

import time
import threading
import statistics
import os
from datetime import datetime
from typing import List, Dict, Optional

from .config import Config
from .schemas import ContainerState, ContainerStatus, TestSnapshot
from .utils import calc_percentiles, calc_p99


class StatsCollector:
    """Statistics collector - real-time snapshot + final report"""

    def __init__(self, config: Config, container_states: Dict[int, ContainerState]):
        self.config = config
        self.container_states = container_states
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

        # Container status statistics
        active_count = sum(
            1 for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_READY and s.is_alive
        )
        offline_count = sum(
            1 for s in self.container_states.values()
            if not s.is_alive or s.creation_metrics.status in (ContainerStatus.FAILED, ContainerStatus.PORT_FAILED, ContainerStatus.OFFLINE)
        )

        # Creation performance statistics (only port-ready containers)
        create_times = [
            s.creation_metrics.create_elapsed for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_READY and s.creation_metrics.create_elapsed > 0
        ]
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_READY and s.creation_metrics.port_wait_elapsed > 0
        ]
        total_times = [
            s.creation_metrics.total_elapsed for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_READY and s.creation_metrics.total_elapsed > 0
        ]

        creation_stats = {
            "create": calc_percentiles(create_times),
            "port_wait": calc_percentiles(port_wait_times),
            "total": calc_percentiles(total_times),
        }

        # Browser task statistics (queries)
        browser_total = sum(s.browser_metrics.total_tasks for s in self.container_states.values())
        browser_success = sum(s.browser_metrics.success_count for s in self.container_states.values())

        # Collect recent latency data (last 10 per container)
        all_latencies: List[float] = []
        for s in self.container_states.values():
            all_latencies.extend(s.browser_metrics.latencies[-10:])

        browser_avg = statistics.mean(all_latencies) if all_latencies else 0.0
        browser_p99 = calc_p99(all_latencies)

        # Calculate QPS (queries per second)
        if elapsed > 0 and browser_success > 0:
            qps = browser_success / elapsed
        else:
            qps = 0.0

        snapshot = TestSnapshot(
            timestamp=now,
            elapsed=elapsed,
            total_containers=len(self.container_states),
            active_containers=active_count,
            offline_containers=offline_count,
            creation_stats=creation_stats,
            browser_total=browser_total,
            browser_success=browser_success,
            browser_avg_latency=browser_avg,
            browser_p99_latency=browser_p99,
            qps=qps
        )
        self.snapshots.append(snapshot)

        # Real-time terminal output
        self._print_snapshot(snapshot)

    def _print_snapshot(self, snapshot: TestSnapshot) -> None:
        """Print real-time snapshot"""
        print(f"\n{'─'*70}")
        print(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        print(f"{'─'*70}")
        print(f"  Containers: {snapshot.active_containers:3d} ready / {snapshot.offline_containers:2d} offline")

        if snapshot.creation_stats.get("create") and snapshot.creation_stats["create"]["avg"] > 0:
            print(f"  Create:     avg={snapshot.creation_stats['create']['avg']:.1f}s  "
                  f"p99={snapshot.creation_stats['create']['p99']:.1f}s")
        if snapshot.creation_stats.get("port_wait") and snapshot.creation_stats["port_wait"]["avg"] > 0:
            print(f"  PortWait:   avg={snapshot.creation_stats['port_wait']['avg']:.1f}s  "
                  f"p99={snapshot.creation_stats['port_wait']['p99']:.1f}s")

        print(f"  Queries:    {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  "
              f"avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s")
        print(f"  QPS:        {snapshot.qps:.2f} queries/sec")
        print(f"{'─'*70}")

    def generate_report(self) -> str:
        """Generate final TXT report"""
        lines: List[str] = []
        lines.append("=" * 80)
        lines.append("Docker Container Bench - Browser Automation Performance Report")
        lines.append("=" * 80)

        # Configuration info
        lines.append(f"\n[Test Configuration]")
        lines.append(f"  Image:           {self.config.docker_image}")
        lines.append(f"  Container Spec:  {self.config.cpu_limit}vCPU / {self.config.memory_limit}")
        lines.append(f"  Total Containers:{self.config.total_count}")

        # Display mode
        if self.config.detect_existing:
            lines.append(f"  Mode:            Detect existing containers")
        elif self.config.create_only:
            lines.append(f"  Mode:            Create-only (Phase 0)")
        else:
            lines.append(f"  Mode:            Full workflow")

        # Create batch config
        if self.config.create_batch_size:
            lines.append(f"  Create Batch:    {self.config.create_batch_count} batches x {self.config.create_batch_size} containers")
            lines.append(f"  Create Interval: {self.config.create_batch_interval}s")
        else:
            lines.append(f"  Create Batch:    Full concurrent creation")

        # Task batch config
        if not self.config.create_only:
            if self.config.task_batch_size:
                lines.append(f"  Task Batch:      {self.config.task_batch_count} batches x {self.config.task_batch_size} containers")
                lines.append(f"  Task Interval:   {self.config.task_batch_interval}s")
            else:
                lines.append(f"  Task Batch:      Full concurrent start")

        lines.append(f"  Test Duration:   {self.config.test_duration}s")

        # Browser workflow
        lines.append(f"\n[Browser Workflow (5 steps = 1 query)]")
        lines.append(f"  Step 1: openclaw browser open [URL] --label [NAME]")
        lines.append(f"  Step 2: openclaw browser focus [TAB_ID]")
        lines.append(f"  Step 3: openclaw browser snapshot --limit 200")
        lines.append(f"  Step 4: openclaw browser click e218 (retry on fail)")
        lines.append(f"  Step 5: openclaw browser screenshot")

        # Container status statistics
        ready_states = [
            s for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_READY
        ]
        failed_states = [
            s for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.FAILED
        ]
        port_failed_states = [
            s for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_FAILED
        ]
        offline_states = [
            s for s in self.container_states.values() if not s.is_alive
        ]

        lines.append(f"\n[Container Status]")
        lines.append(f"  Created (Docker):   {len([s for s in self.container_states.values() if s.creation_metrics.status not in (ContainerStatus.PENDING, ContainerStatus.CREATING)])} / {len(self.container_states)}")
        lines.append(f"  Ports Ready:        {len(ready_states)} / {len(self.container_states)}")
        lines.append(f"  Create Failed:      {len(failed_states)}")
        lines.append(f"  Port Check Failed:  {len(port_failed_states)}")
        lines.append(f"  Offline (runtime):  {len(offline_states)}")
        if failed_states:
            lines.append(f"  Create Failed IDs:  {[s.container_id for s in failed_states[:10]]}")
        if port_failed_states:
            lines.append(f"  Port Failed IDs:    {[s.container_id for s in port_failed_states[:10]]}")
        if offline_states:
            lines.append(f"  Offline IDs:        {[s.container_id for s in offline_states[:10]]}")

        # Container creation performance statistics
        create_times = [
            s.creation_metrics.create_elapsed for s in self.container_states.values()
            if s.creation_metrics.create_elapsed > 0 and s.creation_metrics.status not in (ContainerStatus.FAILED, ContainerStatus.PENDING, ContainerStatus.CREATING)
        ]
        if create_times:
            stats = calc_percentiles(create_times)
            lines.append(f"\n[Container Creation Performance]")
            lines.append(f"  (docker run --cpus --mem elapsed time)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Port wait performance statistics
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in ready_states
            if s.creation_metrics.port_wait_elapsed > 0
        ]
        if port_wait_times:
            stats = calc_percentiles(port_wait_times)
            lines.append(f"\n[Port Check Wait Performance]")
            lines.append(f"  (Waiting for {self.config.required_ports} ports)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Total startup time (create + port_wait)
        total_times = [
            s.creation_metrics.total_elapsed for s in ready_states
            if s.creation_metrics.total_elapsed > 0
        ]
        if total_times:
            stats = calc_percentiles(total_times)
            lines.append(f"\n[Total Startup Performance]")
            lines.append(f"  (Container creation + port wait)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # Browser task statistics (queries)
        all_latencies: List[float] = []
        for s in self.container_states.values():
            all_latencies.extend(s.browser_metrics.latencies)

        total_queries = sum(s.browser_metrics.total_tasks for s in self.container_states.values())
        total_success = sum(s.browser_metrics.success_count for s in self.container_states.values())
        total_failed = sum(s.browser_metrics.failed_count for s in self.container_states.values())
        total_timeout = sum(s.browser_metrics.timeout_count for s in self.container_states.values())

        lines.append(f"\n[Browser Query Statistics]")
        lines.append(f"  (5-step workflow = 1 query)")
        lines.append(f"  Total Queries: {total_queries}")
        lines.append(f"  Success:       {total_success}")
        lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:  {total_success / max(1, total_queries) * 100:.1f}%")

        if all_latencies:
            avg_ms = statistics.mean(all_latencies) * 1000
            p99_ms = calc_p99(all_latencies) * 1000
            lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
            lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

        # QPS calculation
        elapsed = time.time() - self.start_time
        if elapsed > 0 and total_success > 0:
            qps = total_success / elapsed
            lines.append(f"\n[Overall QPS]")
            lines.append(f"  Total QPS:     {qps:.2f} queries/sec")
            lines.append(f"  (Success queries / Test duration)")

        # Per-step latency analysis
        lines.append(f"\n[Per-Step Latency Analysis]")
        step_names = ['open', 'focus', 'snapshot', 'click', 'screenshot']
        for step_name in step_names:
            all_step_latencies = []
            for s in self.container_states.values():
                if step_name in s.browser_metrics.step_latencies:
                    all_step_latencies.extend(s.browser_metrics.step_latencies[step_name])

            if all_step_latencies:
                avg_ms = statistics.mean(all_step_latencies) * 1000
                lines.append(f"  {step_name.capitalize():12s}: avg={avg_ms:.1f}ms")

        # Collect error details from failed containers
        failed_container_errors = []
        for s in self.container_states.values():
            if s.browser_metrics.failed_count > 0 and s.browser_metrics.last_error:
                failed_container_errors.append(
                    (s.container_id, s.browser_metrics.failed_count, s.browser_metrics.last_error)
                )

        if failed_container_errors:
            # Sort by failed count (descending)
            failed_container_errors.sort(key=lambda x: x[1], reverse=True)
            lines.append(f"\n[Failed Container Error Details]")
            lines.append(f"  Total containers with query failures: {len(failed_container_errors)}")
            lines.append(f"  (Top 10 containers with most failures)")
            for cid, count, error in failed_container_errors[:10]:
                # Truncate error if too long
                error_display = error[:150] if len(error) > 150 else error
                lines.append(f"  Container{cid}: {count} failures - {error_display}")

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