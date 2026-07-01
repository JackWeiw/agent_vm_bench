"""
Task Runner Module

Responsible for QA, Stress, Browser task execution
Each VM has an independent thread
"""

import time
import random
import threading
from typing import Tuple, Dict, Optional

from .config import Config
from .schemas import VMState, VMStatus, OOMType
from .constants import QA_MEMORY_TEXT, QA_QUESTIONS, BROWSER_TASKS, STRESS_TOOL_PATH
from .vm_manager import VMConnection


class QATaskManager:
    """QA Task Manager"""

    def __init__(self, config: Config):
        self.config = config
        self._query_counter = 0

    def _execute_http_query(self, vm: VMConnection, content: str, timeout: int) -> Tuple[bool, float]:
        """Execute QA query via HTTP gateway (curl method)"""
        self._query_counter += 1
        resp_file = f"/tmp/openclaw_resp_{self._query_counter}.json"

        escaped = content.replace('\\', '\\\\').replace('"', '\\"')

        cmd = (
            f"curl -s -o {resp_file} -w '%{{time_total}}' "
            f"-X POST http://127.0.0.1:18789/v1/chat/completions "
            f"-H 'Authorization: Bearer test-token-123' "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"model\":\"openclaw/default\",\"messages\":[{{\"role\":\"user\",\"content\":\"{escaped}\"}}]}}'"
        )

        success, stdout, _, duration, _ = vm.execute(cmd, timeout=timeout + 10, get_exit_code=True)

        latency = 0.0
        if success and stdout.strip():
            parts = stdout.strip().split('\n')
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

        if self.config.qa_mode == "http":
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
        if not state.qa_metrics.memory_init_done:
            success = self.run_memory_init(vm, state)
            if not success:
                state.record_qa_failure()
                return False, 0.0

        idx = state.qa_metrics.current_query_index % len(QA_QUESTIONS)
        question = QA_QUESTIONS[idx]

        state.qa_metrics.current_query_index += 1
        if state.qa_metrics.current_query_index % len(QA_QUESTIONS) == 0:
            state.qa_metrics.query_round += 1

        if self.config.qa_mode == "http":
            success, duration = self._execute_http_query(vm, question, self.config.qa_timeout)
            timeout = duration > self.config.qa_timeout
            state.qa_metrics.add(duration, success, timeout)
            state.last_qa_time = time.time()
            return success, duration

        cmd = f'/usr/local/node-v24.14.1-linux-arm64/bin/openclaw agent --agent main --timeout {self.config.qa_timeout} -m "{question}"'
        success, _, _, duration, code = vm.execute(cmd, timeout=self.config.qa_timeout + 5, get_exit_code=True)

        timeout = (code is not None and code == -1) or duration > self.config.qa_timeout
        state.qa_metrics.add(duration, success, timeout)
        state.last_qa_time = time.time()

        return success, duration


class StressTaskManager:
    """Stress Task Manager (with Keepalive and OOM Detection)"""

    def __init__(self, config: Config):
        self.config = config

    def start_stress(self, vm: VMConnection, state: VMState) -> Tuple[bool, str]:
        """Start stress_tool"""
        log_id = f"stress_vm{state.vm_id}"

        cleanup_cmd = f'pkill -9 -f "stress_tool" 2>/dev/null; sleep 0.5; rm -f /tmp/{log_id}.log /tmp/{log_id}.pid'
        vm.execute(cleanup_cmd, timeout=5)

        start_cmd = (
            f'nohup {STRESS_TOOL_PATH} '
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

            for _ in range(10):
                time.sleep(0.5)
                if self._check_process(vm, pid):
                    check_cmd = f'grep -q "Stress Tool Started" /tmp/{log_id}.log 2>/dev/null && echo "READY"'
                    _, out, _, _, _ = vm.execute(check_cmd, timeout=3)
                    if "READY" in out:
                        return True, f"PID={pid}, verified"

            oom_type = self._diagnose_failure(vm, log_id, "start")
            state.record_stress_failure()
            return False, f"Process check failed, diagnosis: {oom_type.value}"
        else:
            state.record_stress_failure()
            return False, f"Start command failed: {stderr[:50]}"

    def check_and_restart(self, vm: VMConnection, state: VMState) -> Tuple[bool, str]:
        """Check stress status, restart if needed"""
        if not state.health.is_connected:
            return False, "VM offline"

        if not state.stress_metrics.current_pid:
            return False, "Not started"

        if self._check_process(vm, state.stress_metrics.current_pid):
            return True, f"Running PID={state.stress_metrics.current_pid}"

        log_id = f"stress_vm{state.vm_id}"
        oom_type = self._diagnose_failure(vm, log_id, "runtime")
        state.stress_metrics.oom_events[oom_type] += 1

        if self.config.stress_keepalive and oom_type != OOMType.START_OOM:
            print(f"[VM{vm.vm_id}] Stress disappeared ({oom_type.value}), restarting...")
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
        cmd = f'ps -p {pid} -o pid= 2>/dev/null || echo "DEAD"'
        success, stdout, _, _, _ = vm.execute(cmd, timeout=30, get_exit_code=True)
        return success and pid in stdout and "DEAD" not in stdout

    def _diagnose_failure(self, vm: VMConnection, log_id: str, phase: str) -> OOMType:
        log_cmd = f'cat /tmp/{log_id}.log 2>/dev/null | head -20'
        success, stdout, _, _, _ = vm.execute(log_cmd, timeout=50, get_exit_code=True)

        if stdout:
            log_lower = stdout.lower()
            if any(kw in log_lower for kw in ['cannot allocate', 'out of memory', 'oom', 'killed']):
                return OOMType.START_OOM if phase == "start" else OOMType.RUNTIME_OOM
            if any(kw in log_lower for kw in ['segmentation fault', 'sigsegv', 'crash', 'aborted']):
                return OOMType.CRASH
            if 'finished' in log_lower or 'completed' in log_lower:
                return OOMType.NONE

        if phase == "runtime":
            dmesg_cmd = f'dmesg | grep -i "killed process" | tail -3'
            success, stdout, _, _, _ = vm.execute(dmesg_cmd, timeout=50, get_exit_code=True)
            if success and stdout and 'stress_tool' in stdout.lower():
                return OOMType.RUNTIME_OOM

        return OOMType.UNKNOWN


class BrowserTaskManager:
    """Browser Task Manager"""

    def __init__(self, config: Config):
        self.config = config
        self._task_counter = 0

    def _execute_http_browser(self, vm: VMConnection, prompt: str, timeout: int) -> Tuple[bool, float]:
        """Execute browser task via HTTP gateway"""
        self._task_counter += 1
        resp_file = f"/tmp/browser_resp_{self._task_counter}.json"

        escaped = prompt.replace('\\', '\\\\').replace('"', '\\"')
        cmd = (
            f"curl -s -o {resp_file} -w '%{{time_total}}' "
            f"-X POST http://127.0.0.1:18789/v1/chat/completions "
            f"-H 'Authorization: Bearer test-token-123' "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"model\":\"openclaw/default\",\"stream\":false,\"messages\":[{{\"role\":\"user\",\"content\":\"{escaped}\"}}]}}'"
        )

        success, stdout, stderr, duration, _ = vm.execute(cmd, timeout=timeout + 30, get_exit_code=True)

        latency = 0.0
        if success and stdout.strip():
            parts = stdout.strip().split('\n')
            try:
                latency = float(parts[-1])
            except (ValueError, IndexError):
                latency = duration
        return success, latency

    def _execute_cli_browser(self, vm: VMConnection, prompt: str, timeout: int) -> Tuple[bool, float]:
        """Execute browser task via CLI"""
        cmd = f'/usr/local/node-v24.14.1-linux-arm64/bin/openclaw agent --agent main --timeout {timeout} -m "{prompt}"'
        success, stdout, stderr, duration, code = vm.execute(cmd, timeout=timeout + 30, get_exit_code=True)
        return success, duration

    def _execute_direct_browser(self, vm: VMConnection, url: str, timeout: int) -> Tuple[bool, float]:
        """Execute browser task directly"""
        cmd = f'openclaw browser --browser-profile openclaw open "{url}"'
        success, _, _, duration, _ = vm.execute(cmd, timeout=timeout + 30, get_exit_code=True)
        latency = duration + 10.0
        return success, latency

    def run_browser_task(self, vm: VMConnection, state: VMState) -> Tuple[bool, float, str]:
        """Execute single browser task"""
        idx = state.browser_metrics.total_tasks % len(BROWSER_TASKS)
        task_type, task_template = BROWSER_TASKS[idx]
        url = self.config.browser_urls[state.browser_metrics.total_tasks % len(self.config.browser_urls)]
        prompt = task_template.format(url=url)

        if self.config.browser_use_llm:
            if self.config.qa_mode == "http":
                success, duration = self._execute_http_browser(vm, prompt, self.config.browser_timeout)
            else:
                success, duration = self._execute_cli_browser(vm, prompt, self.config.browser_timeout)
        else:
            success, duration = self._execute_direct_browser(vm, url, self.config.browser_timeout)

        timeout = duration > self.config.browser_timeout
        state.browser_metrics.add(duration, success and not timeout, timeout, task_type)
        state.last_browser_time = time.time()

        return success and not timeout, duration, task_type

    def warmup_phase(self, vm: VMConnection, state: VMState) -> bool:
        """Browser warmup phase"""
        if not self.config.warmup_urls:
            state.warmup_done = True
            return True

        vm_id = vm.vm_id
        failed_urls = []

        for loop in range(self.config.warmup_loops):
            for url in self.config.warmup_urls:
                if not url.strip():
                    continue

                cmd = f'openclaw browser --browser-profile openclaw open "{url}"'
                success, _, _, _, _ = vm.execute(cmd, timeout=60, get_exit_code=True)

                if not success:
                    failed_urls.append(url[:50])

                time.sleep(self.config.warmup_delay)

        cmd1 = 'openclaw config set agents.defaults.memorySearch.chunking.tokens 200'
        success1, _, _, _, _ = vm.execute(cmd1, timeout=30, get_exit_code=True)

        cmd2 = 'openclaw memory index --force'
        success2, _, _, _, _ = vm.execute(cmd2, timeout=120, get_exit_code=True)

        state.warmup_done = True
        warmup_success = success1 and success2 and len(failed_urls) == 0

        if not warmup_success:
            state.record_browser_failure()
            print(f"[VM{vm_id}] Warmup failed: {len(failed_urls)} pages")

        return warmup_success


class VMTaskRunner(threading.Thread):
    """VM Task Runner - one thread per VM"""

    def __init__(
        self,
        vm: VMConnection,
        state: VMState,
        config: Config,
        stop_event: threading.Event,
        qa_manager: Optional[QATaskManager] = None,
        stress_manager: Optional[StressTaskManager] = None,
        browser_manager: Optional[BrowserTaskManager] = None,
        batch_controller: Optional[object] = None,
        health_checker: Optional[object] = None,
    ):
        super().__init__(daemon=True)
        self.vm = vm
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.qa_manager = qa_manager
        self.stress_manager = stress_manager
        self.browser_manager = browser_manager
        self.batch_controller = batch_controller
        self.health_checker = health_checker
        self.consecutive_errors = 0

    def run(self) -> None:
        """Task execution main loop"""
        # Wait for batch ready if applicable
        if self.batch_controller and (self.config.task_mode == "browser" or self.state.is_stress_vm):
            batch_id = self.state.batch_id
            if batch_id >= 0:
                while not self.stop_event.is_set() and not self.batch_controller.is_batch_ready(batch_id):
                    time.sleep(0.5)

        # Warmup phase
        if self.config.task_mode == "browser" and self.config.warmup_only and self.browser_manager:
            self.browser_manager.warmup_phase(self.vm, self.state)
            print(f"[VM{self.vm.vm_id}] Warmup completed")
            return

        # Benchmark loop
        while not self.stop_event.is_set():
            try:
                if not self.state.health.is_connected:
                    print(f"[VM{self.vm.vm_id}] VM offline, stopping")
                    break

                # Execute task based on mode
                if self.config.task_mode == "browser":
                    success, duration, task_type = self.browser_manager.run_browser_task(self.vm, self.state)
                    self.consecutive_errors = 0 if success else self.consecutive_errors + 1
                    if not success:
                        self.state.record_browser_failure()
                        self.state.health.mark_failure("Browser failed")

                elif self.config.task_mode == "qa":
                    success, duration = self.qa_manager.run_qa_query(self.vm, self.state)
                    self.consecutive_errors = 0 if success else self.consecutive_errors + 1
                    if not success:
                        self.state.health.mark_failure("QA failed")

                # Stress handling
                if self.state.is_stress_vm and self.stress_manager:
                    self._handle_stress()

                # Error threshold
                if self.consecutive_errors >= 3:
                    if not self.vm.is_alive():
                        self.state.health.is_connected = False
                        if self.health_checker:
                            self.health_checker.offline_vms.add(self.vm.vm_id)
                        break
                    self.consecutive_errors = 0

                # Task interval
                if self.config.task_mode == "browser":
                    sleep_time = random.uniform(
                        self.config.browser_interval_min,
                        self.config.browser_interval_max
                    )
                else:
                    sleep_time = self.config.qa_interval
                time.sleep(sleep_time)

            except Exception as e:
                self.consecutive_errors += 1
                self.state.health.mark_failure(str(e)[:50])
                time.sleep(3)

        print(f"[VM{self.vm.vm_id}] runner ended")

    def _handle_stress(self):
        """Handle stress task"""
        if not self.state.stress_started:
            batch_id = self.state.batch_id
            if self.batch_controller:
                while not self.stop_event.is_set():
                    if self.batch_controller.is_batch_ready(batch_id):
                        break
                    time.sleep(0.5)

            print(f"[VM{self.vm.vm_id}] Batch {batch_id} starting stress_tool")
            success, msg = self.stress_manager.start_stress(self.vm, self.state)
            if success:
                self.state.stress_started = True
                if self.batch_controller:
                    self.batch_controller.notify_stress_started(self.vm.vm_id)
            else:
                print(f"[VM{self.vm.vm_id}] stress startup failed: {msg}")

        elif self.config.stress_keepalive and time.time() - self.state.last_stress_check >= 5:
            self.state.last_stress_check = time.time()
            running, msg = self.stress_manager.check_and_restart(self.vm, self.state)
            if not running:
                print(f"[VM{self.vm.vm_id}] {msg}")