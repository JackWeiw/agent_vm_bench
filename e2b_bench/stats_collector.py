"""
统计收集模块

负责实时快照收集、终端输出和最终报告生成
包含详细的沙箱创建时间、端口等待时间、浏览器任务时间统计
"""

import time
import threading
import statistics
import os
from datetime import datetime
from typing import List, Dict, Optional

from .config import Config
from .schemas import SandboxState, SandboxStatus, TestSnapshot
from .utils import calc_percentiles, calc_p99


class StatsCollector:
    """统计收集器 - 实时快照 + 最终报告"""

    def __init__(self, config: Config, sandbox_states: Dict[int, SandboxState]):
        self.config = config
        self.sandbox_states = sandbox_states
        self.snapshots: List[TestSnapshot] = []
        self.start_time: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动后台收集线程"""
        self.start_time = time.time()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止收集"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _collect_loop(self) -> None:
        """定期收集快照"""
        while not self._stop.is_set():
            self._take_snapshot()
            time.sleep(self.config.stats_interval)

    def _take_snapshot(self) -> None:
        """收集当前时刻的统计快照"""
        now = time.time()
        elapsed = now - self.start_time

        # 沙箱状态统计
        active_count = sum(
            1 for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.is_alive
        )
        offline_count = sum(
            1 for s in self.sandbox_states.values()
            if not s.is_alive or s.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.PORT_FAILED, SandboxStatus.OFFLINE)
        )

        # 创建性能统计（仅计算端口就绪的沙箱）
        create_times = [
            s.creation_metrics.create_elapsed for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.creation_metrics.create_elapsed > 0
        ]
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.creation_metrics.port_wait_elapsed > 0
        ]
        total_times = [
            s.creation_metrics.total_elapsed for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.creation_metrics.total_elapsed > 0
        ]

        creation_stats = {
            "create": calc_percentiles(create_times),
            "port_wait": calc_percentiles(port_wait_times),
            "total": calc_percentiles(total_times),
        }

        # 浏览器任务统计
        browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())

        # 收集最近的延迟数据（每个沙箱最近10条）
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
            browser_p99_latency=browser_p99
        )
        self.snapshots.append(snapshot)

        # 实时终端输出
        self._print_snapshot(snapshot)

    def _print_snapshot(self, snapshot: TestSnapshot) -> None:
        """打印实时快照"""
        print(f"\n{'─'*70}")
        print(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        print(f"{'─'*70}")
        print(f"  Sandboxes: {snapshot.active_sandboxes:3d} ready / {snapshot.offline_sandboxes:2d} offline")

        if snapshot.creation_stats.get("create") and snapshot.creation_stats["create"]["avg"] > 0:
            print(f"  Create:    avg={snapshot.creation_stats['create']['avg']:.1f}s  "
                  f"p99={snapshot.creation_stats['create']['p99']:.1f}s")
        if snapshot.creation_stats.get("port_wait") and snapshot.creation_stats["port_wait"]["avg"] > 0:
            print(f"  PortWait:  avg={snapshot.creation_stats['port_wait']['avg']:.1f}s  "
                  f"p99={snapshot.creation_stats['port_wait']['p99']:.1f}s")

        print(f"  Browser:   {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  "
              f"avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s")
        print(f"{'─'*70}")

    def generate_report(self) -> str:
        """生成最终TXT报告"""
        lines: List[str] = []
        lines.append("=" * 80)
        lines.append("E2B Sandbox Bench - Performance Report")
        lines.append("=" * 80)

        # 配置信息
        lines.append(f"\n[Test Configuration]")
        lines.append(f"  Template:        {self.config.template}")
        lines.append(f"  Total Sandboxes: {self.config.total_count}")
        if self.config.batch_size:
            lines.append(f"  Batch Strategy:  {self.config.batch_count} batches x {self.config.batch_size} sandboxes")
            lines.append(f"  Batch Interval:  {self.config.batch_interval}s")
        else:
            lines.append(f"  Batch Strategy:  Full concurrent creation")
        lines.append(f"  Test Duration:   {self.config.test_duration}s")

        # 沙箱状态统计
        ready_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]
        failed_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.FAILED
        ]
        port_failed_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_FAILED
        ]
        offline_states = [
            s for s in self.sandbox_states.values() if not s.is_alive
        ]

        lines.append(f"\n[Sandbox Status]")
        lines.append(f"  Created (API):       {len([s for s in self.sandbox_states.values() if s.creation_metrics.status not in (SandboxStatus.PENDING, SandboxStatus.CREATING)])} / {len(self.sandbox_states)}")
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

        # sandbox.create 性能统计（仅计算创建成功的沙箱）
        create_times = [
            s.creation_metrics.create_elapsed for s in self.sandbox_states.values()
            if s.creation_metrics.create_elapsed > 0 and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
        ]
        if create_times:
            stats = calc_percentiles(create_times)
            lines.append(f"\n[Sandbox.create Performance]")
            lines.append(f"  (sandbox.create API调用时间，不含端口等待)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # 端口等待性能统计
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in ready_states
            if s.creation_metrics.port_wait_elapsed > 0
        ]
        if port_wait_times:
            stats = calc_percentiles(port_wait_times)
            lines.append(f"\n[Port Check Wait Performance]")
            lines.append(f"  (等待18789 openclaw-gateway + 11436 llama-server端口就绪)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # 总启动时间（create + port_wait）
        total_times = [
            s.creation_metrics.total_elapsed for s in ready_states
            if s.creation_metrics.total_elapsed > 0
        ]
        if total_times:
            stats = calc_percentiles(total_times)
            lines.append(f"\n[Total Startup Performance]")
            lines.append(f"  (sandbox.create + 端口等待)")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")

        # 浏览器任务统计
        all_latencies: List[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.browser_metrics.latencies)

        total_tasks = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        total_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.browser_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.browser_metrics.timeout_count for s in self.sandbox_states.values())

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

        lines.append("\n" + "=" * 80)
        return '\n'.join(lines)

    def save_report(self, report: str) -> str:
        """保存报告到文件"""
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.config.filename_prefix}_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)

        return filepath