#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stress Task Module

Stress task execution with process management and OOM diagnosis.
"""

import time
from typing import Tuple

from ..config import Config
from ..models import VMState, OOMType
from ..connection import VMConnection


class StressTaskManager:
    """Stress Task Manager (with Keepalive and OOM Detection)"""

    def __init__(self, config: Config):
        self.config = config

    def start_stress(self, vm: VMConnection, state: VMState) -> Tuple[bool, str]:
        """Start stress_tool, with retry checks"""
        log_id = f"stress_vm{state.vm_id}"

        # Clean up old processes first
        cleanup_cmd = 'pkill -9 -f "stress_tool" 2>/dev/null; sleep 0.5; rm -f /tmp/{}.log /tmp/{}.pid'.format(log_id, log_id)
        vm.execute(cleanup_cmd, timeout=5)

        start_cmd = (
            f'nohup /root/stress_tool '
            f'-c 2 -m {self.config.stress_memory_mb} -i 5 -d {self.config.stress_duration} '
            f'> /tmp/{log_id}.log 2>&1 & '
            f'echo $! > /tmp/{log_id}.pid; sync; '
            f'sleep 2; cat /tmp/{log_id}.pid'
        )

        success, stdout, stderr, _, _ = vm.execute(start_cmd, timeout=15, get_exit_code=True)

        if success and stdout.strip():
            pid = stdout.strip().split()[0]
            state.stress_metrics.current_pid = pid
            state.stress_metrics.start_count += 1
            state.stress_metrics.last_start_time = time.time()

            # Check process multiple times (max 5 seconds, every 0.5 seconds)
            for _ in range(10):
                time.sleep(0.5)
                if self._check_process(vm, pid):
                    # Additional verification: check if "Started" appears in log
                    check_cmd = f'grep -q "Stress Tool Started" /tmp/{log_id}.log 2>/dev/null && echo "READY"'
                    _, out, _, _, _ = vm.execute(check_cmd, timeout=3)
                    if "READY" in out:
                        return True, f"PID={pid}, verified"

            # If not detected within 5 seconds, mark as failed
            oom_type = self._diagnose_failure(vm, log_id, "start")
            state.record_stress_failure()
            return False, f"Process check failed after start, diagnosis: {oom_type.value}"
        else:
            state.record_stress_failure()
            return False, f"Start command execution failed: {stderr[:50]}"

    def check_and_restart(self, vm: VMConnection, state: VMState) -> Tuple[bool, str]:
        """Check stress status, restart if needed, return (is_running, status_info)"""
        if not state.health.is_connected:
            return False, "VM offline, skip restart"

        if not state.stress_metrics.current_pid:
            return False, "Not started"

        # Check if process exists
        if self._check_process(vm, state.stress_metrics.current_pid):
            return True, f"Running PID={state.stress_metrics.current_pid}"

        # Process disappeared, diagnose reason
        log_id = f"stress_vm{state.vm_id}"
        oom_type = self._diagnose_failure(vm, log_id, "runtime")
        state.stress_metrics.oom_events[oom_type] += 1

        # Keepalive: if not permanent failure, try restart
        if self.config.stress_keepalive and oom_type != OOMType.START_OOM:
            print(f"[VM{vm.vm_id}] Stress process disappeared ({oom_type.value}), attempting restart...")
            state.stress_metrics.restart_count += 1
            success, msg = self.start_stress(vm, state)
            if success:
                return True, f"Restarted {msg}"
            else:
                state.record_stress_failure()
                return False, f"Restart failed: {msg}"
        else:
            state.record_stress_failure()
            return False, f"Process disappeared: {oom_type.value}"

    def _check_process(self, vm: VMConnection, pid: str) -> bool:
        """Check if process exists"""
        cmd = f'ps -p {pid} -o pid= 2>/dev/null || echo "DEAD"'
        success, stdout, _, _, _ = vm.execute(cmd, timeout=30, get_exit_code=True)
        return success and pid in stdout and "DEAD" not in stdout

    def _diagnose_failure(self, vm: VMConnection, log_id: str, phase: str) -> OOMType:
        """Diagnose failure reason"""
        # 1. Check OOM keywords in log
        log_cmd = f'cat /tmp/{log_id}.log 2>/dev/null | head -20'
        success, stdout, _, _, _ = vm.execute(log_cmd, timeout=50, get_exit_code=True)

        if stdout:
            log_lower = stdout.lower()

            # Check OOM keywords
            if any(kw in log_lower for kw in ['cannot allocate', 'out of memory', 'oom', 'killed']):
                if phase == "start":
                    return OOMType.START_OOM
                else:
                    return OOMType.RUNTIME_OOM

            # Check crash keywords
            if any(kw in log_lower for kw in ['segmentation fault', 'sigsegv', 'crash', 'aborted']):
                return OOMType.CRASH

            # Check normal completion
            if 'finished' in log_lower or 'completed' in log_lower:
                return OOMType.NONE

        # 2. Check dmesg (runtime OOM)
        if phase == "runtime":
            dmesg_cmd = f'dmesg | grep -i "killed process" | tail -3'
            success, stdout, _, _, _ = vm.execute(dmesg_cmd, timeout=50, get_exit_code=True)
            if success and stdout and 'stress_tool' in stdout.lower():
                return OOMType.RUNTIME_OOM

        return OOMType.UNKNOWN
