"""
Task Execution Module

Responsible for browser task execution (5-step workflow), result collection and exception handling
Each container has an independent thread
Supports task batch control for gradual task execution start

Browser Workflow:
  Step 1: openclaw browser open [URL] --label [NAME]  → Page open
  Step 2: openclaw browser focus [TAB_ID]             → Tab focus
  Step 3: openclaw browser snapshot --limit 200       → DOM snapshot
  Step 4: openclaw browser click e218                 → Element click (retry on fail)
  Step 5: openclaw browser screenshot                 → Visual screenshot
"""

import time
import random
import threading
import re
from typing import Tuple, List, Dict

from .config import Config
from .schemas import ContainerState, ContainerStatus


class BrowserTaskRunner(threading.Thread):
    """Browser task runner (one independent thread per container)

    Executes complete 5-step workflow as one query
    """

    def __init__(
        self,
        state: ContainerState,
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
        # Wait for container ports ready
        while not self.stop_event.is_set():
            if self.state.creation_metrics.status == ContainerStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (ContainerStatus.FAILED, ContainerStatus.PORT_FAILED, ContainerStatus.OFFLINE, ContainerStatus.KILLED):
                print(f"[Container{self.state.container_id}] Cannot start tasks: {self.state.creation_metrics.status.value}")
                return
            time.sleep(0.5)

        # Start browser backend (hot start)
        if not self.state.browser_started:
            success, error = self._start_browser_backend()
            if not success:
                print(f"[Container{self.state.container_id}] Browser backend start failed: {error}")
                self.state.is_alive = False
                return

        # Browser task execution loop
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Container{self.state.container_id}] Container offline, stopping tasks")
                break

            # Execute single browser task (5-step workflow)
            success, latency, step_times = self._run_single_task()

            # Update metrics
            timeout = latency > self.config.browser_timeout
            self.state.browser_metrics.add(latency, success and not timeout, timeout, step_times)
            self.state.last_task_time = time.time()

            # Error handling
            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    print(f"[Container{self.state.container_id}] Marked offline (3 consecutive failures)")
                    break

            # Random interval to avoid request spike
            sleep_time = random.uniform(
                self.config.browser_interval_min,
                self.config.browser_interval_max
            )
            time.sleep(sleep_time)

        # Clear browser cache after task loop ends
        self._clear_browser_cache()

        print(f"[Container{self.state.container_id}] Task runner ended")

    def _start_browser_backend(self) -> Tuple[bool, str]:
        """Start OpenClaw browser backend (hot start)

        Returns: (success, error_msg)
        """
        container = self.state.docker_container
        if not container:
            return False, "No container handle"

        try:
            # Check status first, then start if needed
            cmd = "openclaw browser status || openclaw browser start"
            result = container.exec_run(cmd, user="root")

            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output

            if result.exit_code == 0:
                self.state.browser_started = True
                print(f"[Container{self.state.container_id}] Browser backend started")
                return True, ""
            else:
                return False, f"exit_code={result.exit_code}, output={output[:200]}"
        except Exception as e:
            return False, str(e)

    def _run_single_task(self) -> Tuple[bool, float, Dict[str, float]]:
        """Execute single browser task (complete 5-step workflow)

        Returns: (success, latency_seconds, step_times)
        """
        container = self.state.docker_container
        if not container:
            return False, 0.0, {}

        # Get current URL (round-robin)
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]

        # Generate label name
        label_name = f"test_{self.state.container_id}_{int(time.time())}"

        step_times = {}
        start_time = time.perf_counter()

        try:
            # Step 1: Open page
            step_success, step_time, tab_id = self._step_open(url, label_name)
            step_times['open'] = step_time
            if not step_success:
                return False, time.perf_counter() - start_time, step_times

            # Step 2: Focus tab
            step_success, step_time = self._step_focus(tab_id)
            step_times['focus'] = step_time
            if not step_success:
                return False, time.perf_counter() - start_time, step_times

            # Step 3: DOM snapshot
            step_success, step_time, elements = self._step_snapshot()
            step_times['snapshot'] = step_time
            if not step_success:
                return False, time.perf_counter() - start_time, step_times

            # Step 4: Element click (with retry)
            step_success, step_time = self._step_click(elements)
            step_times['click'] = step_time
            if not step_success:
                return False, time.perf_counter() - start_time, step_times

            # Step 5: Screenshot
            step_success, step_time = self._step_screenshot()
            step_times['screenshot'] = step_time
            if not step_success:
                return False, time.perf_counter() - start_time, step_times

            elapsed = time.perf_counter() - start_time
            return True, elapsed, step_times

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            print(f"[Container{self.state.container_id}] Task exception: {error_msg}")
            self.state.browser_metrics.last_error = error_msg
            return False, elapsed, step_times

    def _step_open(self, url: str, label: str) -> Tuple[bool, float, str]:
        """Step 1: Open page

        Returns: (success, time_seconds, tab_id)
        """
        container = self.state.docker_container
        cmd = f"openclaw browser open '{url}' --label '{label}'"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, timeout=60, user="root")
            elapsed = time.perf_counter() - start

            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output

            if result.exit_code != 0:
                print(f"[Container{self.state.container_id}] Step 1 (open) failed: {output[:200]}")
                self.state.browser_metrics.last_error = f"open failed: {output[:200]}"
                return False, elapsed, ""

            # Extract tab_id from output (format varies, need parsing)
            # Example output: "Tab ID: abc123" or "tab_id=abc123"
            tab_id = self._extract_tab_id(output)
            return True, elapsed, tab_id

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"open exception: {str(e)}"
            return False, elapsed, ""

    def _step_focus(self, tab_id: str) -> Tuple[bool, float]:
        """Step 2: Focus tab

        Returns: (success, time_seconds)
        """
        if not tab_id:
            # Skip if no tab_id extracted
            return True, 0.0

        container = self.state.docker_container
        cmd = f"openclaw browser focus '{tab_id}'"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, timeout=30, user="root")
            elapsed = time.perf_counter() - start

            if result.exit_code != 0:
                output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output
                print(f"[Container{self.state.container_id}] Step 2 (focus) failed: {output[:100]}")
                # Focus failure is not critical, continue
                return True, elapsed

            return True, elapsed

        except Exception as e:
            # Focus failure is not critical
            return True, time.perf_counter() - start

    def _step_snapshot(self) -> Tuple[bool, float, List[str]]:
        """Step 3: DOM snapshot

        Returns: (success, time_seconds, element_ids)
        """
        container = self.state.docker_container
        cmd = "openclaw browser snapshot --limit 200"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, timeout=60, user="root")
            elapsed = time.perf_counter() - start

            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output

            if result.exit_code != 0:
                print(f"[Container{self.state.container_id}] Step 3 (snapshot) failed: {output[:200]}")
                self.state.browser_metrics.last_error = f"snapshot failed: {output[:200]}"
                return False, elapsed, []

            # Extract element IDs from output
            elements = self._extract_element_ids(output)
            return True, elapsed, elements

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"snapshot exception: {str(e)}"
            return False, elapsed, []

    def _step_click(self, elements: List[str]) -> Tuple[bool, float]:
        """Step 4: Element click (with retry)

        Returns: (success, time_seconds)
        """
        container = self.state.docker_container

        # Pick an element to click (e.g., e218 as mentioned in spec, or first available)
        element_id = "e218"  # Default element as per spec
        if elements and "e218" not in elements:
            element_id = elements[0]  # Use first available if e218 not found

        cmd = f"openclaw browser click {element_id}"

        # First attempt
        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, timeout=30, user="root")
            elapsed = time.perf_counter() - start

            if result.exit_code == 0:
                return True, elapsed

            # Retry on failure
            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output
            print(f"[Container{self.state.container_id}] Step 4 (click) first attempt failed, retrying...")

            result = container.exec_run(cmd, timeout=30, user="root")
            elapsed = time.perf_counter() - start  # Total time including retry

            if result.exit_code == 0:
                return True, elapsed

            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output
            print(f"[Container{self.state.container_id}] Step 4 (click) retry failed: {output[:100]}")
            self.state.browser_metrics.last_error = f"click failed after retry: {output[:200]}"
            return False, elapsed

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"click exception: {str(e)}"
            return False, elapsed

    def _step_screenshot(self) -> Tuple[bool, float]:
        """Step 5: Screenshot

        Returns: (success, time_seconds)
        """
        container = self.state.docker_container
        cmd = "openclaw browser screenshot"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, timeout=30, user="root")
            elapsed = time.perf_counter() - start

            if result.exit_code != 0:
                output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output
                print(f"[Container{self.state.container_id}] Step 5 (screenshot) failed: {output[:100]}")
                self.state.browser_metrics.last_error = f"screenshot failed: {output[:200]}"
                return False, elapsed

            return True, elapsed

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"screenshot exception: {str(e)}"
            return False, elapsed

    def _clear_browser_cache(self) -> bool:
        """Clear browser cache after test

        Returns: success
        """
        container = self.state.docker_container
        if not container:
            return False

        try:
            cmd = "rm -rf /root/.openclaw/browser/openclaw/user-data"
            result = container.exec_run(cmd, user="root")
            return result.exit_code == 0
        except Exception:
            return False

    def _extract_tab_id(self, output: str) -> str:
        """Extract tab ID from browser open output

        Returns: tab_id or empty string
        """
        # Try common patterns
        patterns = [
            r"Tab ID:\s*([a-zA-Z0-9_-]+)",
            r"tab_id[=:\s]+([a-zA-Z0-9_-]+)",
            r"id:\s*([a-zA-Z0-9_-]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                return match.group(1)

        return ""

    def _extract_element_ids(self, output: str) -> List[str]:
        """Extract element IDs from snapshot output

        Returns: list of element IDs
        """
        # Pattern for elements like e123, e456, etc.
        pattern = r"e[0-9]+"
        matches = re.findall(pattern, output)
        return matches[:50]  # Limit to 50 elements


class TaskManager:
    """Task manager - manages all container task execution threads with batch control"""

    def __init__(
        self,
        config: Config,
        container_states: Dict[int, ContainerState],
        stop_event: threading.Event,
    ):
        self.config = config
        self.container_states = container_states
        self.stop_event = stop_event
        self.runners: List[BrowserTaskRunner] = []

    def start_all(self) -> None:
        """Start task execution threads for PORT_READY containers

        Strategy based on task_batch config:
        - With task_batch_size: batched start to avoid target server overload
        - Without config: full concurrent start for max load test

        benchmark_percent controls how many containers to include in benchmark
        (e.g., 0.5 = 50% of ready containers)
        """
        # Filter PORT_READY containers
        ready_states = [
            s for s in self.container_states.values()
            if s.creation_metrics.status == ContainerStatus.PORT_READY
        ]

        if not ready_states:
            print("No containers ready for task execution")
            return

        # Select subset based on benchmark_percent
        total_ready = len(ready_states)
        benchmark_count = max(1, int(total_ready * self.config.benchmark_percent))

        if benchmark_count < total_ready:
            # Select first N containers for benchmark
            benchmark_states = ready_states[:benchmark_count]
            print(f"\nBenchmark subset: {benchmark_count}/{total_ready} containers ({self.config.benchmark_percent*100:.0f}%)")
        else:
            benchmark_states = ready_states

        if self.config.task_batch_size and self.config.task_batch_size > 0:
            self._start_batched(benchmark_states)
        else:
            self._start_concurrent(benchmark_states)

    def _start_batched(self, ready_states: List[ContainerState]) -> None:
        """Batched task execution start"""
        total = len(ready_states)
        batch_size = self.config.task_batch_size
        batch_count = (total + batch_size - 1) // batch_size

        print(f"\n{'='*60}")
        print(f"Batched Task Execution Start")
        print(f"  Total: {total} containers")
        print(f"  Batches: {batch_count} x {batch_size}")
        print(f"  Interval: {self.config.task_batch_interval}s")
        print(f"{'='*60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                print("Stop event detected, aborting task start")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)
            batch_states = ready_states[start_idx:end_idx]

            print(f"\n[TaskBatch {batch_id}/{batch_count-1}] Starting tasks for containers {start_idx+1}-{end_idx}")

            # Start task runners for current batch
            for state in batch_states:
                runner = BrowserTaskRunner(state, self.config, self.stop_event)
                self.runners.append(runner)
                runner.start()

            # Wait between batches (last batch no wait)
            if batch_id < batch_count - 1 and self.config.task_batch_interval:
                print(f"Waiting {self.config.task_batch_interval}s before next task batch...")
                time.sleep(self.config.task_batch_interval)

        print(f"\nStarted {len(self.runners)} task runners in {batch_count} batches")

    def _start_concurrent(self, ready_states: List[ContainerState]) -> None:
        """Full concurrent task execution start"""
        print(f"\n{'='*60}")
        print(f"Concurrent Task Execution Start")
        print(f"  Total: {len(ready_states)} containers (full concurrent)")
        print(f"{'='*60}")

        for state in ready_states:
            runner = BrowserTaskRunner(state, self.config, self.stop_event)
            self.runners.append(runner)
            runner.start()

        print(f"\nStarted {len(self.runners)} task runners")

    def wait_all(self, timeout: float = 5.0) -> None:
        """Wait for all task threads to end"""
        for runner in self.runners:
            runner.join(timeout=timeout)