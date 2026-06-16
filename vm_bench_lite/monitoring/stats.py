#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Statistics Collection Module

Collect and aggregate metrics, generate reports.
"""

import time
import threading
import statistics
from dataclasses import dataclass, field
from typing import List, Dict

from ..config import Config
from ..models import VMState, OOMType


@dataclass
class TestSnapshot:
    """Test Snapshot"""
    timestamp: float
    elapsed: float
    stress_vm_count: int
    normal_vm_count: int
    offline_vm_count: int
    total_failure_vm_count: int
    browser_total: int = 0
    browser_success: int = 0
    browser_avg_latency: float = 0.0
    browser_p99_latency: float = 0.0
    stress_restart_count: int = 0
    oom_events: Dict[OOMType, int] = field(default_factory=dict)
    browser_type_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)


class StatsCollector:
    """Stats Collector"""

    def __init__(self, config: Config, vm_states: Dict[int, VMState]):
        self.config = config
        self.vm_states = vm_states
        self.snapshots: List[TestSnapshot] = []
        self.start_time = time.time()
        self._stop = threading.Event()

    def start(self):
        thread = threading.Thread(target=self._collect_loop, daemon=True)
        thread.start()

    def stop(self):
        self._stop.set()

    def _collect_loop(self):
        while not self._stop.is_set():
            self._take_snapshot()
            time.sleep(self.config.stats_interval)

    def _take_snapshot(self):
        now = time.time()
        elapsed = now - self.start_time

        # Group statistics
        stress_vms = [s for s in self.vm_states.values() if s.is_stress_vm]
        normal_vms = [s for s in self.vm_states.values() if not s.is_stress_vm]
        offline_vms = [s for s in self.vm_states.values() if not s.health.is_connected]

        # Task failure statistics
        total_failure_vms = [s for s in self.vm_states.values() if s.has_task_failure]

        # QA statistics
        def calc_qa_stats(vms):
            total = sum(s.qa_metrics.total_queries for s in vms)
            success = sum(s.qa_metrics.success_count for s in vms)
            all_lat = []
            for s in vms:
                all_lat.extend(s.qa_metrics.latencies[-10:])
            avg = statistics.mean(all_lat) if all_lat else 0
            p99 = 0.0
            if all_lat:
                sorted_lat = sorted(all_lat)
                p99 = sorted_lat[int(len(all_lat)*0.99)] if len(all_lat)>=100 else sorted_lat[-1]
            return total, success, avg, p99

        s_total, s_success, s_avg, s_p99 = calc_qa_stats(stress_vms)
        n_total, n_success, n_avg, _ = calc_qa_stats(normal_vms)

        # Browser statistics
        def calc_browser_stats(vms):
            total = sum(s.browser_metrics.total_tasks for s in vms)
            success = sum(s.browser_metrics.success_count for s in vms)
            all_lat = []
            for s in vms:
                all_lat.extend(s.browser_metrics.latencies[-10:])
            avg = statistics.mean(all_lat) if all_lat else 0
            p99 = 0.0
            if all_lat:
                sorted_lat = sorted(all_lat)
                p99 = sorted_lat[int(len(all_lat)*0.99)] if len(all_lat)>=100 else sorted_lat[-1]

            type_stats: Dict[str, Dict[str, int]] = {}
            for s in vms:
                for tname, tcounts in s.browser_metrics.task_type_counts.items():
                    if tname not in type_stats:
                        type_stats[tname] = {"success": 0, "failed": 0}
                    type_stats[tname]["success"] += tcounts.get("success", 0)
                    type_stats[tname]["failed"] += tcounts.get("failed", 0)
            return total, success, avg, p99, type_stats

        b_total, b_success, b_avg, b_p99, b_type_stats = calc_browser_stats(self.vm_states.values())

        # Stress statistics
        restart_count = sum(s.stress_metrics.restart_count for s in stress_vms)
        oom_events = {t: sum(s.stress_metrics.oom_events.get(t, 0) for s in stress_vms) for t in OOMType}

        snapshot = TestSnapshot(
            timestamp=now, elapsed=elapsed,
            stress_vm_count=len(stress_vms), normal_vm_count=len(normal_vms),
            offline_vm_count=len(offline_vms), total_failure_vm_count=len(total_failure_vms),
            browser_total=b_total, browser_success=b_success,
            browser_avg_latency=b_avg, browser_p99_latency=b_p99,
            stress_restart_count=restart_count, oom_events=oom_events,
            browser_type_stats=b_type_stats
        )

        self.snapshots.append(snapshot)

        # Real-time output
        print(f"\n{'─'*70}")
        print(f"T+{elapsed:6.1f}s  Status Snapshot")
        print(f"{'─'*70}")
        if self.config.browser_mode:
            fail_vm_ids = sorted([s.vm_id for s in total_failure_vms])
            print(f"  VM: {len(stress_vms)+len(normal_vms):3d} online / {len(offline_vms):2d} offline / {len(total_failure_vms):2d} task failures")
            print(f"  Browser:  {b_success:3d}/{b_total:3d}  avg={b_avg:.2f}s  p99={b_p99:.2f}s")
            if b_type_stats:
                for tname, tcounts in sorted(b_type_stats.items()):
                    print(f"    [{tname}] success={tcounts['success']} failed={tcounts['failed']}")
            if fail_vm_ids:
                print(f"  Failed VMs:  {fail_vm_ids}")
        else:
            s_offline = len([s for s in stress_vms if not s.health.is_connected])
            n_task_fail = len([s for s in normal_vms if s.has_task_failure])
            s_task_fail = len([s for s in stress_vms if s.has_task_failure])
            print(f"  StressVM: {len(stress_vms):3d}  offline:{s_offline:2d}  task_fail:{s_task_fail:2d}  QA:{s_success:3d}/{s_total:3d}")
            print(f"  NormalVM: {len(normal_vms):3d}  task_fail:{n_task_fail:2d}  QA:{n_success:3d}/{n_total:3d}")
            print(f"  Total:    Failed VM {len(total_failure_vms):2d} | Offline VM {len(offline_vms):2d} | Restarts {restart_count} | OOM {sum(v for v in oom_events.values())}")
            if total_failure_vms:
                print(f"  Failed VMs:  {sorted([s.vm_id for s in total_failure_vms])}")
        print(f"{'─'*70}")

    def generate_report(self) -> str:
        """Generate complete report"""
        lines = []
        lines.append("=" * 80)
        if self.config.browser_mode:
            lines.append("VM Bench Lite v2 - Browser Benchmark Report")
        else:
            lines.append("VM Bench Lite v2 - Mixed QA+Stress Test Report")
        lines.append("=" * 80)

        # Configuration info
        lines.append(f"\n[Test Configuration]")
        lines.append(f"  Total VMs:       {self.config.total_vms}")
        if self.config.browser_mode:
            # Browser benchmark phase shows actual connected VMs
            actual_vm_count = int(self.config.total_vms * self.config.browser_stress_percent)
            lines.append(f"  Connected VMs:   {actual_vm_count} ({self.config.browser_stress_percent*100:.0f}%)")
        lines.append(f"  Batches:         {self.config.batch_count} batches x {self.config.batch_size} VMs/batch")
        lines.append(f"  Batch Interval:  {self.config.batch_interval}s")
        if self.config.browser_mode:
            lines.append(f"  Browser Task:    Page Access")
            lines.append(f"  Target URL:      {self.config.browser_url}")
            lines.append(f"  Task Interval:   {self.config.browser_task_interval_min}~{self.config.browser_task_interval_max}s (random)")
            lines.append(f"  Test Duration:   {self.config.test_duration}s")
        else:
            lines.append(f"  Stress VM:       {self.config.stress_vm_count}")
            lines.append(f"  Stress Memory:   {self.config.stress_memory_mb}MB/VM")
            lines.append(f"  Stress Keepalive: {'Enabled' if self.config.stress_keepalive else 'Disabled'}")

        # Final statistics
        stress_vms = [s for s in self.vm_states.values() if s.is_stress_vm and s.health.is_connected]
        normal_vms = [s for s in self.vm_states.values() if not s.is_stress_vm and s.health.is_connected]
        offline_vms = [s for s in self.vm_states.values() if not s.health.is_connected]

        if self.config.browser_mode:
            # Browser mode report
            all_online = stress_vms + normal_vms
            lines.append(f"\n[VM Status]")
            lines.append(f"  Online VMs:  {len(all_online)}")
            lines.append(f"  Offline VMs: {len(offline_vms)}")
            if offline_vms:
                lines.append(f"  Offline List: {[s.vm_id for s in offline_vms]}")

            # Browser task summary
            total_tasks = sum(s.browser_metrics.total_tasks for s in all_online)
            total_success = sum(s.browser_metrics.success_count for s in all_online)
            all_lat = []
            for s in all_online:
                all_lat.extend(s.browser_metrics.latencies)
            avg_ms = statistics.mean(all_lat) * 1000 if all_lat else 0
            p99_ms = 0
            if all_lat:
                sl = sorted(all_lat)
                p99_ms = (sl[int(len(sl)*0.99)] if len(sl)>=100 else sl[-1]) * 1000
            total_timeout = sum(s.browser_metrics.timeout_count for s in all_online)
            total_fail = sum(s.browser_metrics.failed_count for s in all_online)

            lines.append(f"\n[Browser Task Statistics]")
            lines.append(f"  Total Tasks:   {total_tasks}")
            lines.append(f"  Success:       {total_success}")
            lines.append(f"  Failed:        {total_fail} (timeout {total_timeout})")
            lines.append(f"  Success Rate:  {total_success/max(1,total_tasks)*100:.1f}%")
            lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
            lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")

            # By task type
            type_stats = {}
            for s in all_online:
                for tn, tc in s.browser_metrics.task_type_counts.items():
                    if tn not in type_stats:
                        type_stats[tn] = {"success": 0, "failed": 0}
                    type_stats[tn]["success"] += tc.get("success", 0)
                    type_stats[tn]["failed"] += tc.get("failed", 0)
            if type_stats:
                lines.append(f"\n[By Task Type]")
                for tn, tc in sorted(type_stats.items()):
                    lines.append(f"  {tn}:  success={tc['success']}  failed={tc['failed']}")
        else:
            # QA/Stress mode report
            def agg(vms):
                tq = sum(s.qa_metrics.total_queries for s in vms)
                sq = sum(s.qa_metrics.success_count for s in vms)
                lat = []
                for s in vms:
                    lat.extend(s.qa_metrics.latencies)
                mi = sum(1 for s in vms if s.qa_metrics.memory_init_done)
                avg = statistics.mean(lat)*1000 if lat else 0
                p99 = 0
                if lat:
                    sl = sorted(lat)
                    p99 = (sl[int(len(sl)*0.99)] if len(sl)>=100 else sl[-1])*1000
                return {'vm': len(vms), 'init': mi, 'tq': tq, 'sq': sq, 'avg': avg, 'p99': p99, 'rate': sq/max(1,tq)*100}

            sa = agg(stress_vms)
            na = agg(normal_vms)
            restart_count = sum(s.stress_metrics.restart_count for s in self.vm_states.values())
            oom_events = {t: sum(s.stress_metrics.oom_events.get(t, 0) for s in self.vm_states.values()) for t in OOMType}

            lines.append(f"\n[VM Status]")
            lines.append(f"  Online Stress VM: {sa['vm']} (memory init completed: {sa['init']})")
            lines.append(f"  Online Normal VM: {na['vm']} (memory init completed: {na['init']})")
            lines.append(f"  Offline VM:       {len(offline_vms)}")
            if offline_vms:
                lines.append(f"  Offline List: {[s.vm_id for s in offline_vms]}")

            lines.append(f"\n[QA Task Statistics - Stress VM]")
            lines.append(f"  Total Queries: {sa['tq']}")
            lines.append(f"  Success:       {sa['sq']}")
            lines.append(f"  Success Rate:  {sa['rate']:.1f}%")
            lines.append(f"  Avg Latency:   {sa['avg']:.1f}ms")
            lines.append(f"  P99 Latency:   {sa['p99']:.1f}ms")

            lines.append(f"\n[QA Task Statistics - Normal VM]")
            lines.append(f"  Total Queries: {na['tq']}")
            lines.append(f"  Success:       {na['sq']}")
            lines.append(f"  Success Rate:  {na['rate']:.1f}%")
            lines.append(f"  Avg Latency:   {na['avg']:.1f}ms")
            lines.append(f"  P99 Latency:   {na['p99']:.1f}ms")

            lines.append(f"\n[Stress Process Statistics]")
            lines.append(f"  Total Restarts: {restart_count}")
            if any(c > 0 for c in oom_events.values()):
                for t, c in oom_events.items():
                    if c > 0:
                        lines.append(f"  {t.value}: {c}")
            else:
                lines.append(f"  OOM Events: 0")

        lines.append("\n" + "=" * 80)
        return '\n'.join(lines)
