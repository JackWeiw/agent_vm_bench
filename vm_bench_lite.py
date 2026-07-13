#!/usr/bin/env python3
"""
VM Bench Lite - VM Batch Stress Testing Tool

Features:
- Batch VM task startup, stagger execution time to avoid resource surge
- QA task triple polling, Browser task testing, QA and Browser tasks are mutually exclusive
- Optionally start stress_tool on some VMs to consume memory and more CPU
- OpenStack status detection, identify VM shutdown caused by memory overcommit
- Real-time statistics + final report
- Two-phase browser testing: warmup phase (all VMs) then benchmark phase (partial VMs)

Interaction Mode (--mode):
- http: curl call to openclaw gateway, low overhead, suitable for large-scale stress testing
- cli:  Direct call to openclaw command line, higher overhead but complete functionality

Pressure Control Parameters:
- --batch-size N:       Start N VMs per batch, avoid starting all at once
- --batch-interval S:   Batch interval S seconds, stagger startup time
- --browser-interval-min/max: Browser task random interval, avoid VMs making requests simultaneously
- --stress-percent: Percentage of VMs to start stress_tool process (QA+Stress mode)

Browser Two-Phase Testing:
- -wp/--warmup-phase:   Run warmup phase only, all VMs execute warmup tasks then exit
- -bsp/--browser-stress-percent: Percentage of VMs to connect in benchmark phase (default 100%)

Usage:
    # Browser mode - Two-phase testing (warmup then benchmark)
    # Step 1: Warmup phase (all 100 VMs)
    python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
        -wp --warmup-url http://192.168.110.10:8080/page1.html --warmup-url http://192.168.110.10:8080/page2.html

    # Step 2: Benchmark phase (50% VMs, 50 VMs)
    python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
        -bsp 0.5 --browser-url http://192.168.110.10:8080/Weibo.html -t 180

    # Browser mode + use LLM (http curl openclaw gateway call prompt)
    python vm_bench_lite.py -n 80 --start-ip 192.168.110.11 --browser-mode --browser-use-llm --mode http -t 180

    # Browser mode + use LLM + pressure control (20 per batch, 5 second interval)
    python vm_bench_lite.py -n 80 --start-ip 192.168.110.11 --browser-mode --browser-use-llm --mode http \
        --batch-size 20 --batch-interval 5 --browser-interval-min 1 --browser-interval-max 5 -t 180

    # QA+Stress mixed mode
    python vm_bench_lite.py -n 80 --start-ip 192.168.110.11 --stress-percent 0.5 --batch-size 10 -t 180
"""

import argparse
import ipaddress
import json
import os
import random
import re
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

import paramiko

# ==================== Configuration ====================


class OOMType(Enum):
    """OOM Type Classification"""

    NONE = "none"
    START_OOM = "start_oom"  # OOM at startup (memory allocation failed)
    RUNTIME_OOM = "runtime_oom"  # OOM at runtime (killed by OOM Killer)
    CRASH = "crash"  # Program crash (segmentation fault etc.)
    UNKNOWN = "unknown"  # Unknown cause


@dataclass
class Config:
    """Stress test configuration"""

    total_vms: int = 80
    stress_percent: float = 0.5
    batch_size: int = 10
    batch_interval: int = 30

    stress_memory_mb: int = 2048
    stress_duration: int = 300
    stress_keepalive: bool = True  # Stress process keepalive

    qa_interval: float = 0.5
    qa_timeout: int = 600
    qa_init_timeout: int = 600  # Memory entry timeout
    mode: str = "cli"  # Interaction mode: "cli" or "http" (for QA and Browser)
    browser_mode: bool = False  # Browser test mode (mutually exclusive with QA)
    browser_timeout: int = 200  # Single browser task timeout (seconds)
    browser_url: str = ""  # Browser test target URL
    browser_use_llm: bool = False  # Browser task use LLM (True: HTTP/CLI call prompt, False: direct openclaw browser)

    # Browser phase control (two-phase execution: warmup then benchmark)
    is_warmup_phase: bool = False  # True: warmup phase (all VMs), False: benchmark phase (partial VMs)
    browser_stress_percent: float = 1.0  # Percentage of VMs to run browser benchmark (in benchmark phase)

    # Browser warmup configuration
    warmup_urls: List[str] = field(default_factory=list)  # Warmup page URL list
    warmup_loops: int = 2  # Warmup loop count
    warmup_delay: int = 10  # Delay between warmup pages (seconds)

    test_duration: int = 600
    stats_interval: int = 10
    health_check_interval: float = 5.0  # Health check interval
    task_interval: float = 1.0  # VM task interval, stagger different VM task execution times
    browser_task_interval_min: float = 0.5  # Browser task random interval minimum
    browser_task_interval_max: float = 3.0  # Browser task random interval maximum

    # SSH configuration
    start_ip: str = "192.168.110.11"
    port: int = 22
    username: str = "root"
    password: str = "openEuler12#$"

    @property
    def stress_vm_count(self) -> int:
        return int(self.total_vms * self.stress_percent)

    @property
    def browser_benchmark_vm_count(self) -> int:
        """Actual VM count for browser benchmark phase"""
        if self.browser_mode and not self.is_warmup_phase:
            return max(1, int(self.total_vms * self.browser_stress_percent))
        return self.total_vms

    @property
    def batch_count(self) -> int:
        if self.browser_mode:
            # Browser mode: use actual connected VM count
            count = self.browser_benchmark_vm_count
        else:
            count = self.stress_vm_count
        return (count + self.batch_size - 1) // self.batch_size


@dataclass
class QAMetrics:
    """QA performance metrics"""

    total_queries: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    memory_init_done: bool = False
    memory_init_time: float = 0.0
    current_query_index: int = 0
    query_round: int = 0

    def add(self, latency: float, success: bool, timeout: bool = False):
        self.total_queries += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
        else:
            self.failed_count += 1

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(self.latencies) < 100:
            return sorted_lat[-1]
        return sorted_lat[int(len(self.latencies) * 0.99)]


@dataclass
class BrowserMetrics:
    """Browser task metrics"""

    total_tasks: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    task_type_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def add(self, latency: float, success: bool, timeout: bool = False, task_type: str = ""):
        self.total_tasks += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
        else:
            self.failed_count += 1
        if task_type:
            if task_type not in self.task_type_counts:
                self.task_type_counts[task_type] = {"success": 0, "failed": 0}
            if success and not timeout:
                self.task_type_counts[task_type]["success"] += 1
            else:
                self.task_type_counts[task_type]["failed"] += 1

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(self.latencies) < 100:
            return sorted_lat[-1]
        return sorted_lat[int(len(self.latencies) * 0.99)]


@dataclass
class StressMetrics:
    """Stress process metrics"""

    start_count: int = 0
    restart_count: int = 0
    oom_events: Dict[OOMType, int] = field(default_factory=lambda: dict.fromkeys(OOMType, 0))
    last_start_time: float = 0.0
    current_pid: Optional[str] = None


@dataclass
class VMHealth:
    """VM health status"""

    is_connected: bool = True
    last_seen: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    error_history: List[Tuple[float, str]] = field(default_factory=list)

    def mark_failure(self, error: str):
        self.consecutive_failures += 1
        self.last_error = error
        self.error_history.append((time.time(), error))
        if len(self.error_history) > 10:
            self.error_history.pop(0)

    def mark_success(self):
        self.consecutive_failures = 0
        self.last_error = ""

    def check_offline(self, threshold: int = 2) -> bool:
        return self.consecutive_failures >= threshold


@dataclass
class VMState:
    """VM complete state"""

    vm_id: int
    host: str
    is_stress_vm: bool = False
    batch_id: int = 0

    # QA state
    qa_metrics: QAMetrics = field(default_factory=QAMetrics)
    last_qa_time: float = 0.0

    # Stress state
    stress_metrics: StressMetrics = field(default_factory=StressMetrics)
    stress_started: bool = False
    last_stress_check: float = 0.0

    # Health state
    health: VMHealth = field(default_factory=VMHealth)

    qa_failure_count: int = 0
    stress_failure_count: int = 0
    browser_failure_count: int = 0

    # Browser state
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    last_browser_time: float = 0.0
    warmup_done: bool = False  # Warmup completion flag

    @property
    def has_task_failure(self) -> bool:
        return self.qa_failure_count > 0 or self.stress_failure_count > 0 or self.browser_failure_count > 0

    def record_qa_failure(self):
        self.qa_failure_count += 1

    def record_stress_failure(self):
        self.stress_failure_count += 1

    def record_browser_failure(self):
        self.browser_failure_count += 1

    def __post_init__(self):
        self.health.last_seen = time.time()


# ==================== SSH Connection Management ====================


class VMConnection:
    """VM SSH Connection (with Health Detection)"""

    def __init__(self, host: str, port: int, username: str, password: str, vm_id: int):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.vm_id = vm_id
        self.ssh = None
        self.connected = False
        self.lock = threading.Lock()

    def connect(self, timeout: int = 30) -> bool:
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=timeout,
                look_for_keys=False,
            )
            self.connected = True
            return True
        except Exception as e:
            print(f"[VM{self.vm_id}] Connection failed: {e}")
            self.connected = False
            return False

    def execute(
        self, command: str, timeout: int = 300, get_exit_code: bool = False
    ) -> Tuple[bool, str, str, float, Optional[int]]:
        """Execute command, optionally return exit code"""
        start = time.perf_counter()
        with self.lock:
            if not self.connected or not self.ssh:
                duration = time.perf_counter() - start
                return False, "", "Not connected", duration, None

            try:
                stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout, get_pty=True)
                out = stdout.read().decode("utf-8", errors="ignore")
                err = stderr.read().decode("utf-8", errors="ignore")
                code = stdout.channel.recv_exit_status() if get_exit_code else 0
                duration = time.perf_counter() - start
                return code == 0, out, err, duration, code if get_exit_code else None
            except Exception as e:
                duration = time.perf_counter() - start
                self.connected = False
                return False, "", str(e), duration, None

    def is_alive(self) -> bool:
        """Check if connection is alive"""
        if not self.connected or not self.ssh:
            return False
        try:
            transport = self.ssh.get_transport()
            if transport and transport.is_active():
                transport.send_ignore()
                return True
            else:
                self.connected = False
                return False
        except:
            self.connected = False
            return False

    def close(self):
        if self.ssh:
            try:
                self.ssh.close()
            except:
                pass
        self.connected = False


# ==================== QA Task (Memory Input + Round-Robin Query) ====================

QA_MEMORY_TEXT = """Please remember the following information:
Employee Attendance: Work hours are 9:00-18:00, lunch break 12:00-13:00. Late arrivals within 30 minutes are not counted as absenteeism, with 3 late arrival exemptions allowed per month. Field employees do not need to clock in but must submit field work records daily.
Travel Reimbursement: Domestic travel accommodation standards are 500 yuan/day for tier-1 cities, 400 yuan/day for tier-2 cities, and 300 yuan/day for tier-3 and below. Transportation expenses are reimbursed as incurred, taxi fares are limited to urgent official business only. Reimbursements must be submitted within 7 working days after return, late submissions will not be accepted.
Overtime Policy: Weekday overtime pays 1.5x salary, weekends 2x, statutory holidays 3x. Overtime must be pre-approved via the OA system, unapproved overtime will not be counted.
Product Information: Standard edition supports 100 concurrent users, annual fee 9800 yuan; Enterprise edition supports 500 concurrent users, annual fee 29800 yuan."""

QA_QUESTIONS = [
    "What are the work hours for our company? Do field employees need to clock in?",
    "I'm traveling to Shanghai, what is the accommodation standard? How long do I have to submit the reimbursement?",
    "How is overtime pay calculated in our company? Is overtime pay automatically given for any overtime work?",
]

BROWSER_TASKS = [("Page Access", "Please use chromium browser to visit {url} and tell me the page title")]

# BROWSER_TASKS = [
#     ("Page Access", "Please use chromium browser to visit {url} and tell me the page title"),
#     ("Content Extraction", "Please use chromium browser to open {url}, read and extract the main text content from the page"),
#     ("Page Scroll", "Please use chromium browser to open {url}, scroll down the page and see what content is below"),
#     ("Page Navigation", "Please use chromium browser to open {url}, click a link or button on the page to navigate to a sub-page, tell me the new page title"),
#     ("Form Interaction", "Please use chromium browser to open {url}, if there is a search box or input field on the page, enter some text in it"),
#     ("Performance Evaluation", "Please use chromium browser to open {url}, evaluate the page loading speed: how long is the white screen time? Is content rendering smooth? Any lag?"),
# ]


class QATaskManager:
    """QA Task Manager"""

    def __init__(self, config: Config):
        self.config = config
        self._query_counter = 0  # Used to generate unique temporary filenames

    def _execute_http_query(self, vm: VMConnection, content: str, timeout: int) -> Tuple[bool, float]:
        """Execute QA query via HTTP gateway (curl method)"""
        self._query_counter += 1
        resp_file = f"/tmp/openclaw_resp_{self._query_counter}.json"

        # Escape double quotes and backslashes in content
        escaped = content.replace("\\", "\\\\").replace('"', '\\"')

        cmd = (
            f"curl -s -o {resp_file} -w '%{{time_total}}' "
            f"-X POST http://127.0.0.1:18789/v1/chat/completions "
            f"-H 'Authorization: Bearer test-token-123' "
            f"-H 'Content-Type: application/json' "
            f'-d \'{{"model":"openclaw/default","messages":[{{"role":"user","content":"{escaped}"}}]}}\''
        )

        success, stdout, _, duration, _ = vm.execute(cmd, timeout=timeout + 10, get_exit_code=True)

        # curl's time_total is in the last line of stdout
        latency = 0.0
        if success and stdout.strip():
            parts = stdout.strip().split("\n")
            try:
                latency = float(parts[-1])
            except (ValueError, IndexError):
                latency = duration

        return success, latency

    def run_memory_init(self, vm: VMConnection, state: VMState) -> bool:
        """Execute memory input"""
        if state.qa_metrics.memory_init_done:
            return True

        print(f"[VM{vm.vm_id}] Starting memory input...")

        if self.config.mode == "http":
            success, duration = self._execute_http_query(vm, QA_MEMORY_TEXT, self.config.qa_init_timeout)
            if success:
                state.qa_metrics.memory_init_done = True
                state.qa_metrics.memory_init_time = time.time()
                print(f"[VM{vm.vm_id}] Memory input completed ({duration:.1f}s)")
                return True
            else:
                print(f"[VM{vm.vm_id}] Memory input failed (HTTP)")
                return False

        cmd = f'/usr/local/node-v24.14.1-linux-arm64/bin/openclaw agent --agent main --timeout {self.config.qa_init_timeout} -m "{QA_MEMORY_TEXT}"'

        success, _, stderr, duration, _ = vm.execute(cmd, timeout=self.config.qa_init_timeout + 10, get_exit_code=True)

        if success:
            state.qa_metrics.memory_init_done = True
            state.qa_metrics.memory_init_time = time.time()
            print(f"[VM{vm.vm_id}] Memory input completed ({duration:.1f}s)")
            return True
        else:
            print(f"[VM{vm.vm_id}] Memory input failed: {stderr[:100]}")
            return False

    def run_qa_query(self, vm: VMConnection, state: VMState) -> Tuple[bool, float]:
        """Execute QA query (round-robin)"""
        # Must complete memory input first
        if not state.qa_metrics.memory_init_done:
            success = self.run_memory_init(vm, state)
            if not success:
                state.record_qa_failure()
                return False, 0.0

        # Get current question
        idx = state.qa_metrics.current_query_index % len(QA_QUESTIONS)
        question = QA_QUESTIONS[idx]

        # Update index
        state.qa_metrics.current_query_index += 1
        if state.qa_metrics.current_query_index % len(QA_QUESTIONS) == 0:
            state.qa_metrics.query_round += 1

        if self.config.mode == "http":
            success, duration = self._execute_http_query(vm, question, self.config.qa_timeout)
            timeout = duration > self.config.qa_timeout
            state.qa_metrics.add(duration, success, timeout)

        # Build command (CLI mode)
        cmd = f'/usr/local/node-v24.14.1-linux-arm64/bin/openclaw agent --agent main --timeout {self.config.qa_timeout} -m "{question}"'

        success, _, _, duration, code = vm.execute(cmd, timeout=self.config.qa_timeout + 5, get_exit_code=True)

        # Determine timeout
        timeout = (code is not None and code == -1) or duration > self.config.qa_timeout

        state.qa_metrics.add(duration, success, timeout)
        state.last_qa_time = time.time()

        return success, duration


# ==================== Stress Task ====================


class StressTaskManager:
    """Stress Task Manager (with Keepalive and OOM Detection)"""

    def __init__(self, config: Config):
        self.config = config

    def start_stress(self, vm: VMConnection, state: VMState) -> Tuple[bool, str]:
        """Start stress_tool, with retry checks"""
        log_id = f"stress_vm{state.vm_id}"

        # Clean up old processes first
        cleanup_cmd = f'pkill -9 -f "stress_tool" 2>/dev/null; sleep 0.5; rm -f /tmp/{log_id}.log /tmp/{log_id}.pid'
        vm.execute(cleanup_cmd, timeout=5)

        start_cmd = (
            f"nohup /root/stress_tool "
            f"-c 2 -m {self.config.stress_memory_mb} -i 5 -d {self.config.stress_duration} "
            f"> /tmp/{log_id}.log 2>&1 & "
            f"echo $! > /tmp/{log_id}.pid; sync; "
            f"sleep 2; cat /tmp/{log_id}.pid"
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
        log_cmd = f"cat /tmp/{log_id}.log 2>/dev/null | head -20"
        success, stdout, _, _, _ = vm.execute(log_cmd, timeout=50, get_exit_code=True)

        if stdout:
            log_lower = stdout.lower()

            # Check OOM keywords
            if any(kw in log_lower for kw in ["cannot allocate", "out of memory", "oom", "killed"]):
                if phase == "start":
                    return OOMType.START_OOM
                else:
                    return OOMType.RUNTIME_OOM

            # Check crash keywords
            if any(kw in log_lower for kw in ["segmentation fault", "sigsegv", "crash", "aborted"]):
                return OOMType.CRASH

            # Check normal completion
            if "finished" in log_lower or "completed" in log_lower:
                return OOMType.NONE

        # 2. Check dmesg (runtime OOM)
        if phase == "runtime":
            dmesg_cmd = 'dmesg | grep -i "killed process" | tail -3'
            success, stdout, _, _, _ = vm.execute(dmesg_cmd, timeout=50, get_exit_code=True)
            if success and stdout and "stress_tool" in stdout.lower():
                return OOMType.RUNTIME_OOM

        return OOMType.UNKNOWN


# ==================== Browser Task (HTTP gateway + CLI) ====================


class BrowserTaskManager:
    """Browser Task Manager (supports both HTTP gateway and CLI methods)"""

    def __init__(self, config: Config):
        self.config = config
        self._task_counter = 0

    def _execute_http_browser(self, vm: VMConnection, prompt: str, timeout: int) -> Tuple[bool, float]:
        """Execute browser task via HTTP gateway (curl method, low CPU overhead)

        Send prompt via /v1/chat/completions, agent will automatically call browser tool.
        Requires plugins.entries.browser.enabled = true in openclaw.json (already configured)
        """
        self._task_counter += 1
        resp_file = f"/tmp/browser_resp_{self._task_counter}.json"

        escaped = prompt.replace("\\", "\\\\").replace('"', '\\"')
        cmd = (
            f"curl -s -o {resp_file} -w '%{{time_total}}' "
            f"-X POST http://127.0.0.1:18789/v1/chat/completions "
            f"-H 'Authorization: Bearer test-token-123' "
            f"-H 'Content-Type: application/json' "
            f'-d \'{{"model":"openclaw/default","stream":false,"messages":[{{"role":"user","content":"{escaped}"}}]}}\''
        )

        success, stdout, stderr, duration, _ = vm.execute(cmd, timeout=timeout + 30, get_exit_code=True)

        latency = 0.0
        if success and stdout.strip():
            parts = stdout.strip().split("\n")
            try:
                latency = float(parts[-1])
            except (ValueError, IndexError):
                latency = duration
        return success, latency

    def _execute_cli_browser(self, vm: VMConnection, prompt: str, timeout: int) -> Tuple[bool, float]:
        """Execute browser task via CLI (openclaw agent)"""
        cmd = f'/usr/local/node-v24.14.1-linux-arm64/bin/openclaw agent --agent main --timeout {timeout} -m "{prompt}"'
        success, stdout, stderr, duration, code = vm.execute(cmd, timeout=timeout + 30, get_exit_code=True)
        return success, duration

    def _execute_direct_browser(self, vm: VMConnection, timeout: int) -> Tuple[bool, float]:
        """Execute browser task directly (without LLM)

        Adds 10s to latency to simulate LLM response delay for realistic benchmarking.
        """
        cmd = f'openclaw browser --browser-profile openclaw open "{self.config.browser_url}"'
        success, _, _, duration, _ = vm.execute(cmd, timeout=timeout + 30, get_exit_code=True)
        latency = duration + 10.0  # Simulate LLM delay
        return success, latency

    def run_browser_task(self, vm: VMConnection, state: VMState) -> Tuple[bool, float, str]:
        """Execute single browser task, return (success, latency, task_type)"""
        idx = state.browser_metrics.total_tasks % len(BROWSER_TASKS)
        task_type, task_template = BROWSER_TASKS[idx]
        prompt = task_template.format(url=self.config.browser_url)

        if self.config.browser_use_llm:
            # Use LLM: HTTP or CLI call with prompt
            if self.config.mode == "http":
                success, duration = self._execute_http_browser(vm, prompt, self.config.browser_timeout)
            else:
                success, duration = self._execute_cli_browser(vm, prompt, self.config.browser_timeout)
        else:
            # Don't use LLM: execute openclaw browser directly
            success, duration = self._execute_direct_browser(vm, self.config.browser_timeout)

        timeout = duration > self.config.browser_timeout
        state.browser_metrics.add(duration, success and not timeout, timeout, task_type)
        state.last_browser_time = time.time()

        return success and not timeout, duration, task_type

    def warmup_phase(self, vm: VMConnection, state: VMState) -> bool:
        """Browser warmup phase

        Loop through warmup pages warmup_loops times to bring QEMU process memory to target value.
        Then execute openclaw config set and memory index commands.

        Returns:
            bool: Whether warmup succeeded
        """
        if not self.config.warmup_urls:
            state.warmup_done = True
            return True

        vm_id = vm.vm_id
        failed_urls = []

        # Loop through warmup pages (reduce log output)
        for loop in range(self.config.warmup_loops):
            for url in self.config.warmup_urls:
                if not url.strip():
                    continue

                cmd = f'openclaw browser --browser-profile openclaw open "{url}"'
                success, _, _, _, _ = vm.execute(cmd, timeout=60, get_exit_code=True)

                if not success:
                    failed_urls.append(url[:50])

                # Delay between pages, wait for memory increase
                time.sleep(self.config.warmup_delay)

        # Execute openclaw config set and memory index
        cmd1 = "openclaw config set agents.defaults.memorySearch.chunking.tokens 200"
        success1, _, _, _, _ = vm.execute(cmd1, timeout=30, get_exit_code=True)

        cmd2 = "openclaw memory index --force"
        success2, _, _, _, _ = vm.execute(cmd2, timeout=120, get_exit_code=True)

        # Mark warmup complete
        state.warmup_done = True
        warmup_success = success1 and success2 and len(failed_urls) == 0

        if not warmup_success:
            if failed_urls:
                state.record_browser_failure()
            print(f"[VM{vm_id}] Warmup failed: {len(failed_urls)} pages, config={success1}, memory={success2}")

        return warmup_success


# ==================== OpenStack VM Status Detection ====================


class OpenStackVMChecker:
    """Query VM status via OpenStack CLI, used to detect SHUTOFF due to memory overcommit"""

    def __init__(self, vm_ips: Dict[int, str]):
        self.ip_name_map: Dict[str, str] = {}  # ip -> name
        self.os_env = self._load_os_env()
        self._available = self.os_env is not None
        self._build_ip_name_map(vm_ips)

    def _load_os_env(self) -> Optional[dict]:
        """Load openrc environment variables"""
        openrc_path = os.path.expanduser("~/.admin-openrc")
        if not os.path.exists(openrc_path):
            print(f"[OpenStack] {openrc_path} not found, skipping OpenStack status detection")
            return None
        try:
            env = os.environ.copy()
            with open(openrc_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        m = re.match(r"export\s+(\w+)=(.*)", line)
                        if m:
                            key, val = m.group(1), m.group(2).strip("\"'")
                            env[key] = val
            env.pop("http_proxy", None)
            env.pop("https_proxy", None)
            env.pop("HTTP_PROXY", None)
            env.pop("HTTPS_PROXY", None)
            print("[OpenStack] Loaded openrc environment variables")
            return env
        except Exception as e:
            print(f"[OpenStack] Failed to load openrc: {e}")
            return None

    def _build_ip_name_map(self, vm_ips: Dict[int, str]):
        """Get IP -> name mapping for all VMs at once"""
        if not self._available:
            return
        try:
            result = subprocess.run(
                ["openstack", "server", "list", "-f", "json", "-c", "Name", "-c", "Networks"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.os_env,
                timeout=60,
            )
            if result.returncode != 0:
                print(f"[OpenStack] server list failed: {result.stderr.strip()}")
                return
            servers = json.loads(result.stdout)
            for srv in servers:
                name = srv.get("Name", "")
                networks = srv.get("Networks", "")
                # Parse "netname=ip1,ip2" format
                if "=" in networks:
                    ips = networks.split("=", 1)[1]
                    for ip in ips.split(","):
                        ip = ip.strip()
                        if ip:
                            self.ip_name_map[ip] = name
            print(f"[OpenStack] IP->Name mapping established: {len(self.ip_name_map)} entries")
        except Exception as e:
            print(f"[OpenStack] Failed to build IP mapping: {e}")

    def get_vm_status(self, vm_name: str) -> Optional[str]:
        """Query VM current status (ACTIVE/SHUTOFF/ERROR/...)"""
        if not self._available or not vm_name:
            return None
        try:
            result = subprocess.run(
                ["openstack", "server", "show", vm_name, "-f", "value", "-c", "status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.os_env,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def check_vm_offline(self, ip: str) -> Tuple[bool, str]:
        """Check if VM has been shut down by hypervisor. Return (is_offline, reason)"""
        if not self._available:
            return False, ""
        vm_name = self.ip_name_map.get(ip)
        if not vm_name:
            return False, ""
        status = self.get_vm_status(vm_name)
        if status in ("SHUTOFF", "ERROR"):
            return True, f"OpenStack status: {status}"
        return False, ""


# ==================== VM Health Checker ====================


class HealthChecker:
    """VM Health Checker"""

    def __init__(
        self,
        config: Config,
        vm_states: Dict[int, VMState],
        vm_conns: Dict[int, VMConnection],
        os_checker: Optional[OpenStackVMChecker] = None,
    ):
        self.config = config
        self.vm_states = vm_states
        self.vm_conns = vm_conns
        self.os_checker = os_checker
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.offline_vms: set = set()

    def start(self):
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _check_loop(self):
        while not self.stop_event.is_set():
            for vm_id, state in self.vm_states.items():
                if vm_id in self.offline_vms:
                    continue

                conn = self.vm_conns.get(vm_id)
                if not conn:
                    continue

                # Check if connection is alive
                if not conn.is_alive():
                    state.health.mark_failure("Connection lost")

                    # Check OpenStack status after 1 consecutive failure, detect if shut down due to memory overcommit
                    if state.health.consecutive_failures >= 1 and self.os_checker:
                        shutoff, reason = self.os_checker.check_vm_offline(conn.host)
                        if shutoff:
                            self.offline_vms.add(vm_id)
                            state.health.is_connected = False
                            print(f"[VM{vm_id}] OpenStack detected VM is shut down ({reason})")
                            continue

                    if state.health.check_offline():
                        self.offline_vms.add(vm_id)
                        state.health.is_connected = False
                        print(
                            f"[VM{vm_id}] Marked as offline (consecutive failures: {state.health.consecutive_failures})"
                        )
                else:
                    state.health.mark_success()
                    state.health.last_seen = time.time()

            time.sleep(self.config.health_check_interval)


# ==================== Batch Controller ====================


class BatchController:
    """Batch Startup Controller"""

    def __init__(self, config: Config, vm_ids: List[int]):
        self.config = config
        self.vm_ids = sorted(vm_ids)

        # Batch status
        self.batch_ready: Dict[int, bool] = {}
        self.batch_started_count: Dict[int, int] = {}
        self.vm_batch_map: Dict[int, int] = {}

        # Calculate batch allocation
        for i, vm_id in enumerate(self.vm_ids):
            batch_id = i // config.batch_size
            self.vm_batch_map[vm_id] = batch_id
            if batch_id not in self.batch_ready:
                self.batch_ready[batch_id] = False
                self.batch_started_count[batch_id] = 0

    def start(self):
        thread = threading.Thread(target=self._control_loop, daemon=True)
        thread.start()

    def _control_loop(self):
        max_batch = max(self.batch_ready.keys()) if self.batch_ready else 0

        for batch_id in range(max_batch + 1):
            vm_list = [vm_id for vm_id, bid in self.vm_batch_map.items() if bid == batch_id]

            print(f"\n{'=' * 60}")
            print(f"Preparing to start batch {batch_id} / {max_batch}")
            print(f"   VM: {vm_list} (consecutive IP segment)")
            print(f"{'=' * 60}")

            self.batch_ready[batch_id] = True

            if batch_id < max_batch:
                print(f"\nWaiting {self.config.batch_interval} seconds before starting next batch...")
                time.sleep(self.config.batch_interval)

        print(f"\nAll {max_batch + 1} batches are ready")

    def is_batch_ready(self, batch_id: int) -> bool:
        return self.batch_ready.get(batch_id, False)

    def notify_stress_started(self, vm_id: int):
        batch_id = self.vm_batch_map.get(vm_id)
        if batch_id is not None:
            self.batch_started_count[batch_id] += 1
            expected = sum(1 for vid, b in self.vm_batch_map.items() if b == batch_id)
            if self.batch_started_count[batch_id] >= expected:
                print(f"   Batch {batch_id} startup complete")


# ==================== Stats Collector ====================


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
                p99 = sorted_lat[int(len(all_lat) * 0.99)] if len(all_lat) >= 100 else sorted_lat[-1]
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
                p99 = sorted_lat[int(len(all_lat) * 0.99)] if len(all_lat) >= 100 else sorted_lat[-1]

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
            timestamp=now,
            elapsed=elapsed,
            stress_vm_count=len(stress_vms),
            normal_vm_count=len(normal_vms),
            offline_vm_count=len(offline_vms),
            total_failure_vm_count=len(total_failure_vms),
            browser_total=b_total,
            browser_success=b_success,
            browser_avg_latency=b_avg,
            browser_p99_latency=b_p99,
            stress_restart_count=restart_count,
            oom_events=oom_events,
            browser_type_stats=b_type_stats,
        )

        self.snapshots.append(snapshot)

        # Real-time output
        print(f"\n{'─' * 70}")
        print(f"T+{elapsed:6.1f}s  Status Snapshot")
        print(f"{'─' * 70}")
        if self.config.browser_mode:
            fail_vm_ids = sorted([s.vm_id for s in total_failure_vms])
            print(
                f"  VM: {len(stress_vms) + len(normal_vms):3d} online / {len(offline_vms):2d} offline / {len(total_failure_vms):2d} task failures"
            )
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
            print(
                f"  StressVM: {len(stress_vms):3d}  offline:{s_offline:2d}  task_fail:{s_task_fail:2d}  QA:{s_success:3d}/{s_total:3d}"
            )
            print(f"  NormalVM: {len(normal_vms):3d}  task_fail:{n_task_fail:2d}  QA:{n_success:3d}/{n_total:3d}")
            print(
                f"  Total:    Failed VM {len(total_failure_vms):2d} | Offline VM {len(offline_vms):2d} | Restarts {restart_count} | OOM {sum(v for v in oom_events.values())}"
            )
            if total_failure_vms:
                print(f"  Failed VMs:  {sorted([s.vm_id for s in total_failure_vms])}")
        print(f"{'─' * 70}")

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
        lines.append("\n[Test Configuration]")
        lines.append(f"  Total VMs:       {self.config.total_vms}")
        if self.config.browser_mode:
            # Browser benchmark phase shows actual connected VMs
            actual_vm_count = int(self.config.total_vms * self.config.browser_stress_percent)
            lines.append(f"  Connected VMs:   {actual_vm_count} ({self.config.browser_stress_percent * 100:.0f}%)")
        lines.append(f"  Batches:         {self.config.batch_count} batches x {self.config.batch_size} VMs/batch")
        lines.append(f"  Batch Interval:  {self.config.batch_interval}s")
        if self.config.browser_mode:
            lines.append("  Browser Task:    Page Access")
            lines.append(f"  Target URL:      {self.config.browser_url}")
            lines.append(
                f"  Task Interval:   {self.config.browser_task_interval_min}~{self.config.browser_task_interval_max}s (random)"
            )
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
            lines.append("\n[VM Status]")
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
                p99_ms = (sl[int(len(sl) * 0.99)] if len(sl) >= 100 else sl[-1]) * 1000
            total_timeout = sum(s.browser_metrics.timeout_count for s in all_online)
            total_fail = sum(s.browser_metrics.failed_count for s in all_online)

            lines.append("\n[Browser Task Statistics]")
            lines.append(f"  Total Tasks:   {total_tasks}")
            lines.append(f"  Success:       {total_success}")
            lines.append(f"  Failed:        {total_fail} (timeout {total_timeout})")
            lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")
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
                lines.append("\n[By Task Type]")
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
                avg = statistics.mean(lat) * 1000 if lat else 0
                p99 = 0
                if lat:
                    sl = sorted(lat)
                    p99 = (sl[int(len(sl) * 0.99)] if len(sl) >= 100 else sl[-1]) * 1000
                return {
                    "vm": len(vms),
                    "init": mi,
                    "tq": tq,
                    "sq": sq,
                    "avg": avg,
                    "p99": p99,
                    "rate": sq / max(1, tq) * 100,
                }

            sa = agg(stress_vms)
            na = agg(normal_vms)
            restart_count = sum(s.stress_metrics.restart_count for s in self.vm_states.values())
            oom_events = {
                t: sum(s.stress_metrics.oom_events.get(t, 0) for s in self.vm_states.values()) for t in OOMType
            }

            lines.append("\n[VM Status]")
            lines.append(f"  Online Stress VM: {sa['vm']} (memory init completed: {sa['init']})")
            lines.append(f"  Online Normal VM: {na['vm']} (memory init completed: {na['init']})")
            lines.append(f"  Offline VM:       {len(offline_vms)}")
            if offline_vms:
                lines.append(f"  Offline List: {[s.vm_id for s in offline_vms]}")

            lines.append("\n[QA Task Statistics - Stress VM]")
            lines.append(f"  Total Queries: {sa['tq']}")
            lines.append(f"  Success:       {sa['sq']}")
            lines.append(f"  Success Rate:  {sa['rate']:.1f}%")
            lines.append(f"  Avg Latency:   {sa['avg']:.1f}ms")
            lines.append(f"  P99 Latency:   {sa['p99']:.1f}ms")

            lines.append("\n[QA Task Statistics - Normal VM]")
            lines.append(f"  Total Queries: {na['tq']}")
            lines.append(f"  Success:       {na['sq']}")
            lines.append(f"  Success Rate:  {na['rate']:.1f}%")
            lines.append(f"  Avg Latency:   {na['avg']:.1f}ms")
            lines.append(f"  P99 Latency:   {na['p99']:.1f}ms")

            lines.append("\n[Stress Process Statistics]")
            lines.append(f"  Total Restarts: {restart_count}")
            if any(c > 0 for c in oom_events.values()):
                for t, c in oom_events.items():
                    if c > 0:
                        lines.append(f"  {t.value}: {c}")
            else:
                lines.append("  OOM Events: 0")

        lines.append("\n" + "=" * 80)
        return "\n".join(lines)


# ==================== VM Task Runner ====================


class VMTaskRunner(threading.Thread):
    """VM Task Runner"""

    def __init__(
        self,
        vm: VMConnection,
        state: VMState,
        config: Config,
        stop_event: threading.Event,
        batch_controller: BatchController,
        qa_manager: QATaskManager,
        stress_manager: StressTaskManager,
        health_checker: HealthChecker,
        browser_manager: Optional[BrowserTaskManager] = None,
    ):
        super().__init__(daemon=True)
        self.vm = vm
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.batch_controller = batch_controller
        self.qa_manager = qa_manager
        self.stress_manager = stress_manager
        self.health_checker = health_checker
        self.browser_manager = browser_manager

    def run(self):
        consecutive_errors = 0

        # Wait for batch ready
        if self.config.browser_mode or self.state.is_stress_vm:
            batch_id = self.state.batch_id
            if batch_id >= 0:
                while not self.stop_event.is_set() and not self.batch_controller.is_batch_ready(batch_id):
                    time.sleep(0.5)

        # Warmup phase: execute warmup tasks then exit
        if self.config.browser_mode and self.config.is_warmup_phase and self.browser_manager:
            self.browser_manager.warmup_phase(self.vm, self.state)
            print(f"[VM{self.vm.vm_id}] Warmup phase completed")
            return

        # Benchmark phase: execute browser benchmark tasks
        while not self.stop_event.is_set():
            try:
                if not self.state.health.is_connected:
                    print(f"[VM{self.vm.vm_id}] VM offline, stopping tasks")
                    break

                # Execute Browser/QA tasks
                if self.config.browser_mode:
                    success, duration, task_type = self.browser_manager.run_browser_task(self.vm, self.state)
                    consecutive_errors = 0 if success else consecutive_errors + 1
                    if not success:
                        self.state.record_browser_failure()
                        self.state.health.mark_failure("Browser failed")
                else:
                    success, duration = self.qa_manager.run_qa_query(self.vm, self.state)
                    consecutive_errors = 0 if success else consecutive_errors + 1
                    if not success:
                        self.state.health.mark_failure("QA failed")

                # Stress task handling
                if self.state.is_stress_vm:
                    self._handle_stress()

                # Error handling
                if consecutive_errors >= 3:
                    if not self.vm.is_alive():
                        self.state.health.is_connected = False
                        self.health_checker.offline_vms.add(self.vm.vm_id)
                        break
                    consecutive_errors = 0

                # Task interval
                if self.config.browser_mode:
                    # Browser mode: random interval per task round, stagger VM task execution to avoid sudden memory pressure
                    sleep_time = random.uniform(
                        self.config.browser_task_interval_min, self.config.browser_task_interval_max
                    )
                else:
                    # QA/Stress mode: stagger VMs within batch by position
                    batch_start_id = self.state.batch_id * self.config.batch_size + 1
                    vm_offset = (self.vm.vm_id - batch_start_id) * self.config.task_interval
                    sleep_time = max(0.5, self.config.qa_interval + vm_offset)
                time.sleep(sleep_time)

            except Exception as e:
                consecutive_errors += 1
                self.state.health.mark_failure(str(e)[:50])
                if "connection" in str(e).lower():
                    # Try reconnect to avoid misjudging offline due to brief network jitter
                    if self.vm.connect(timeout=10):
                        print(f"[VM{self.vm.vm_id}] SSH reconnect successful")
                        self.state.health.mark_success()
                        self.state.health.is_connected = True
                        if self.vm.vm_id in self.health_checker.offline_vms:
                            self.health_checker.offline_vms.discard(self.vm.vm_id)
                        continue
                    # Reconnect failed, check OpenStack status to confirm if shut down
                    if self.health_checker.os_checker:
                        shutoff, reason = self.health_checker.os_checker.check_vm_offline(self.vm.host)
                        if shutoff:
                            self.state.health.is_connected = False
                            self.health_checker.offline_vms.add(self.vm.vm_id)
                            print(f"[VM{self.vm.vm_id}] OpenStack confirmed VM is shut down ({reason})")
                            break
                    # OpenStack not responding or VM status unknown, mark offline
                    self.state.health.is_connected = False
                    self.health_checker.offline_vms.add(self.vm.vm_id)
                    break
                time.sleep(3)

        print(f"[VM{self.vm.vm_id}] runner ended")

    def _handle_stress(self):
        """Handle Stress task"""
        if not self.state.stress_started:
            batch_id = self.state.batch_id
            while not self.stop_event.is_set():
                if self.batch_controller.is_batch_ready(batch_id):
                    break
                time.sleep(0.5)

            print(f"[VM{self.vm.vm_id}] Batch {batch_id} starting stress_tool")
            success, msg = self.stress_manager.start_stress(self.vm, self.state)
            if success:
                self.state.stress_started = True
                self.batch_controller.notify_stress_started(self.vm.vm_id)
            else:
                print(f"[VM{self.vm.vm_id}] stress startup failed: {msg}")

        elif self.config.stress_keepalive and time.time() - self.state.last_stress_check >= 5:
            self.state.last_stress_check = time.time()
            running, msg = self.stress_manager.check_and_restart(self.vm, self.state)
            if not running:
                print(f"[VM{self.vm.vm_id}] {msg}")


# ==================== Main Function ====================


def run_benchmark(config: Config) -> dict:
    """Run benchmark"""

    print("=" * 80)
    if config.browser_mode and config.is_warmup_phase:
        print("VM Bench Lite - Browser Warmup Phase")
    elif config.browser_mode:
        print("VM Bench Lite - Browser Benchmark Phase")
    else:
        print("VM Bench Lite - QA+Stress Test")
    print("=" * 80)

    def ip_range(start_ip, count):
        start = ipaddress.IPv4Address(start_ip)
        return [str(start + i) for i in range(count)]

    # Calculate actual VM count to connect
    if config.browser_mode and not config.is_warmup_phase:
        # Benchmark phase: only connect browser_stress_percent of VMs
        actual_vm_count = int(config.total_vms * config.browser_stress_percent)
        actual_vm_count = max(1, actual_vm_count)  # At least 1 VM
        print(
            f"  Browser benchmark phase: connecting {actual_vm_count}/{config.total_vms} VMs ({config.browser_stress_percent * 100:.0f}%)"
        )
    else:
        # Warmup phase or QA/Stress mode: connect all VMs
        actual_vm_count = config.total_vms

    vm_ips = ip_range(config.start_ip, actual_vm_count)
    stress_vm_ids = list(range(1, config.stress_vm_count + 1))
    stress_vm_set = set(stress_vm_ids)

    # Establish SSH connections
    vm_connections = {}
    for vm_id in range(1, actual_vm_count + 1):
        ip = vm_ips[vm_id - 1]
        vm = VMConnection(ip, config.port, config.username, config.password, vm_id)
        if vm.connect():
            vm_connections[vm_id] = vm

    if not vm_connections:
        print("No connectable VMs")
        return {}

    print(f"Successfully connected: {len(vm_connections)}/{actual_vm_count} VMs")

    # Create OpenStack VM status checker (for detecting shutdowns due to memory overcommit)
    os_vm_ips = {vm_id: vm.host for vm_id, vm in vm_connections.items()}
    os_checker = OpenStackVMChecker(os_vm_ips)

    # Create VM states
    vm_states = {}
    for vm_id, vm in vm_connections.items():
        # Browser mode: no stress VMs, all VMs only run browser tasks
        # QA/Stress mode: mark VMs based on stress_vm_set
        is_stress = False if config.browser_mode else (vm_id in stress_vm_set)
        batch_id = (vm_id - 1) // config.batch_size if (config.browser_mode or is_stress) else -1
        vm_states[vm_id] = VMState(vm_id=vm_id, host=vm.host, is_stress_vm=is_stress, batch_id=batch_id)

    # Initialize managers
    qa_manager = QATaskManager(config)
    stress_manager = StressTaskManager(config)
    browser_manager = BrowserTaskManager(config) if config.browser_mode else None

    # Start components
    health_checker = HealthChecker(config, vm_states, vm_connections, os_checker)
    health_checker.start()
    batch_vm_ids = list(range(1, actual_vm_count + 1)) if config.browser_mode else stress_vm_ids
    batch_controller = BatchController(config, batch_vm_ids)
    batch_controller.start()

    # Stats collector
    stats_collector = StatsCollector(config, vm_states)
    if config.browser_mode and config.is_warmup_phase:
        # Warmup phase: don't start stats collector, no benchmark statistics needed
        pass
    else:
        stats_collector.start()

    # Start task threads
    stop_event = threading.Event()
    runners = []
    for vm_id, vm in vm_connections.items():
        runner = VMTaskRunner(
            vm,
            vm_states[vm_id],
            config,
            stop_event,
            batch_controller,
            qa_manager,
            stress_manager,
            health_checker,
            browser_manager,
        )
        runners.append(runner)
        runner.start()

    # Warmup phase: wait for all VMs to complete warmup then exit
    if config.browser_mode and config.is_warmup_phase:
        print("\nWarmup phase starting...")
        print(f"   Total VMs: {actual_vm_count}")
        print(f"   Warmup pages: {len(config.warmup_urls)}")
        print(f"   Loop count: {config.warmup_loops}")
        print(f"   Page delay: {config.warmup_delay} seconds")
        warmup_start = time.time()
        last_progress_time = warmup_start

        while not stop_event.is_set():
            done_count = sum(1 for s in vm_states.values() if s.warmup_done)
            total_count = len(vm_states)
            fail_count = sum(1 for s in vm_states.values() if s.warmup_done and s.browser_failure_count > 0)

            # Print progress every 5 seconds
            now = time.time()
            if now - last_progress_time >= 5:
                elapsed = now - warmup_start
                print(
                    f"   Warmup progress: {done_count}/{total_count} completed | {fail_count} failed | elapsed {elapsed:.0f}s"
                )
                last_progress_time = now

            if done_count >= total_count:
                warmup_duration = time.time() - warmup_start
                print(f"\nWarmup completed: {done_count} VM | {fail_count} failed | total time {warmup_duration:.1f}s")
                break
            time.sleep(1)

        # Warmup phase complete, exit directly
        print("\nWarmup phase finished, exiting...")
        stop_event.set()
        health_checker.stop()
        for runner in runners:
            runner.join(timeout=2)
        stats_collector.stop()  # Stop stats collector (even though not started in warmup)
        for vm in vm_connections.values():
            vm.close()

        # Small delay to let daemon threads finish their last output
        time.sleep(0.5)

        # Save warmup summary
        warmup_summary = f"Warmup Phase Summary\n{'=' * 40}\nTotal VMs: {actual_vm_count}\nCompleted: {done_count}\nFailed: {fail_count}\nDuration: {warmup_duration:.1f}s\n"
        os.makedirs("results", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(f"results/warmup_summary_{timestamp}.txt", "w") as f:
            f.write(warmup_summary)
        print("\nWarmup summary saved")

        return {"warmup_summary": warmup_summary}

    # Benchmark phase: run for specified duration
    print(f"\nBenchmark running... ({config.test_duration} seconds)")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt")

    # Graceful stop
    print("\nStopping all components...")
    stop_event.set()
    health_checker.stop()
    for runner in runners:
        runner.join(timeout=2)
    stats_collector.stop()
    for vm in vm_connections.values():
        vm.close()

    # Small delay to let daemon threads finish their last output
    time.sleep(0.5)

    # Generate report
    report = stats_collector.generate_report()
    print("\n" + report)

    # Save report
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"results/bench_report_{timestamp}.txt", "w") as f:
        f.write(report)
    print("\nReport saved")

    return {"report": report}


def main():
    import warnings

    warnings.warn(
        "vm_bench_lite.py is deprecated. Use 'python -m vm_bench' instead. See docs/vm_bench-usage-guide.md",
        DeprecationWarning,
        stacklevel=2,
    )
    parser = argparse.ArgumentParser(description="VM Bench Lite v2")
    parser.add_argument("-n", "--vms", type=int, default=80, help="Total VM count")
    parser.add_argument("--stress-percent", type=float, default=0.5, help="Percentage of VMs to run stress_tool")
    parser.add_argument("--stress-memory", type=int, default=2048, help="Stress memory MB")
    parser.add_argument("--no-keepalive", action="store_true", help="Disable Stress keepalive")
    parser.add_argument("--batch-size", type=int, default=10, help="VMs per batch")
    parser.add_argument("--batch-interval", type=int, default=30, help="Batch interval seconds")
    parser.add_argument(
        "--task-interval", type=float, default=1.0, help="Task interval within batch, stagger VM task execution times"
    )
    parser.add_argument("--browser-interval-min", type=float, default=0.5, help="Browser task random interval minimum")
    parser.add_argument("--browser-interval-max", type=float, default=3.0, help="Browser task random interval maximum")
    parser.add_argument(
        "--mode", choices=["cli", "http"], default="cli", help="Interaction mode (http low overhead, cli full features)"
    )
    parser.add_argument("--browser-mode", action="store_true", help="Enable browser testing")
    parser.add_argument(
        "--browser-url", type=str, default="http://192.168.110.10:8080/Weibo.html", help="Browser test URL"
    )
    parser.add_argument(
        "--browser-use-llm",
        action="store_true",
        help="Browser task uses LLM (HTTP/CLI call prompt), otherwise direct openclaw browser",
    )
    # Browser phase control (two-phase execution: warmup then benchmark)
    parser.add_argument(
        "-wp",
        "--warmup-phase",
        action="store_true",
        help="Run warmup phase only (all VMs execute warmup tasks then exit)",
    )
    parser.add_argument(
        "-bsp",
        "--browser-stress-percent",
        type=float,
        default=1.0,
        help="Percentage of VMs to run browser benchmark (default 100%%, only for benchmark phase)",
    )
    # Warmup parameters
    parser.add_argument(
        "--warmup-url", type=str, action="append", help="Warmup page URL (can be specified multiple times)"
    )
    parser.add_argument("--warmup-loops", type=int, default=1, help="Warmup loop count")
    parser.add_argument("--warmup-delay", type=int, default=2, help="Warmup page delay (seconds)")
    parser.add_argument(
        "-t", "--duration", type=int, default=600, help="Total test duration (only for benchmark phase)"
    )
    parser.add_argument("--start-ip", default="192.168.110.11", help="Starting IP")
    parser.add_argument("-u", "--username", default="root", help="SSH username")
    parser.add_argument("-p", "--password", default="openEuler12#$", help="SSH password")

    args = parser.parse_args()

    config = Config(
        total_vms=args.vms,
        stress_percent=args.stress_percent,
        batch_size=args.batch_size,
        batch_interval=args.batch_interval,
        stress_memory_mb=args.stress_memory,
        stress_keepalive=not args.no_keepalive,
        mode=args.mode,
        browser_mode=args.browser_mode,
        browser_url=args.browser_url,
        browser_use_llm=args.browser_use_llm,
        is_warmup_phase=args.warmup_phase,
        browser_stress_percent=args.browser_stress_percent,
        warmup_urls=args.warmup_url or [],
        warmup_loops=args.warmup_loops,
        warmup_delay=args.warmup_delay,
        test_duration=args.duration,
        start_ip=args.start_ip,
        username=args.username,
        password=args.password,
        task_interval=args.task_interval,
        browser_task_interval_min=args.browser_interval_min,
        browser_task_interval_max=args.browser_interval_max,
    )

    run_benchmark(config)


if __name__ == "__main__":
    main()
