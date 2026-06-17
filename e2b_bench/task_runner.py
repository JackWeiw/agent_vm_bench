"""
Task Execution Module

Responsible for browser task execution, result collection and exception handling
Each sandbox has an independent thread
"""

import time
import random
import threading
from typing import Tuple, List, Dict

from .config import Config
from .schemas import SandboxState, SandboxStatus


class BrowserTaskRunner(threading.Thread):
    """Browser task runner (one independent thread per sandbox)"""

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.consecutive_errors = 0

    def run(self) -> None:
        """Task execution main loop"""
        # Wait for sandbox ports ready
        while not self.stop_event.is_set():
            if self.state.creation_metrics.status == SandboxStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.PORT_FAILED, SandboxStatus.OFFLINE, SandboxStatus.KILLED):
                print(f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}")
                return
            time.sleep(0.5)

        # Browser task execution loop
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break

            # Execute single browser task
            success, latency = self._run_single_task()

            # Update metrics
            timeout = latency > self.config.browser_timeout
            self.state.browser_metrics.add(latency, success and not timeout, timeout)
            self.state.last_task_time = time.time()

            # Error handling
            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    print(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break

            # Random interval to avoid request spike
            sleep_time = random.uniform(
                self.config.browser_interval_min,
                self.config.browser_interval_max
            )
            time.sleep(sleep_time)

        print(f"[Sandbox{self.state.sandbox_id}] Task runner ended")

    def _run_single_task(self) -> Tuple[bool, float]:
        """Execute single browser task

        Use state.sandbox_obj handle to execute command

        Returns: (success, latency_seconds)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0

        # Get E2B sandbox_id for logging (different from internal sequence number)
        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, 'sandbox_id') else 'N/A'

        # Get current URL (round-robin)
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]

        # Build browser command
        cmd = f"openclaw browser --browser-profile openclaw open '{url}'"

        start_time = time.perf_counter()
        try:
            result = sbx.commands.run(
                cmd,
                timeout=self.config.browser_timeout + 30,
                user="root"
            )
            elapsed = time.perf_counter() - start_time + 10  # simulate llm response time

            success = result.exit_code == 0

            # Log detailed error info on failure
            if not success:
                error_detail = f"exit_code={result.exit_code}"
                if result.stderr:
                    error_detail += f", stderr={result.stderr[:200]}"
                if result.stdout:
                    error_detail += f", stdout={result.stdout[:200]}"
                print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Task failed: {error_detail}")

                # Store last error for debugging
                self.state.browser_metrics.last_error = error_detail

            return success, elapsed
        except Exception as e:
            elapsed = time.perf_counter() - start_time + 10  # simulate llm response time
            error_msg = str(e)
            print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Task exception: {error_msg}")
            # Store last error for debugging
            self.state.browser_metrics.last_error = error_msg
            return False, elapsed


class TaskManager:
    """Task manager - manages all sandbox task execution threads"""

    def __init__(
        self,
        config: Config,
        sandbox_states: Dict[int, SandboxState],
        stop_event: threading.Event,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.runners: List[BrowserTaskRunner] = []

    def start_all(self) -> None:
        """Start task execution threads for all PORT_READY sandboxes"""
        active_count = 0
        for state in self.sandbox_states.values():
            if state.creation_metrics.status == SandboxStatus.PORT_READY:
                runner = BrowserTaskRunner(state, self.config, self.stop_event)
                self.runners.append(runner)
                runner.start()
                active_count += 1

        print(f"\nStarted {active_count} task runners")

    def wait_all(self, timeout: float = 5.0) -> None:
        """Wait for all task threads to end"""
        for runner in self.runners:
            runner.join(timeout=timeout)