"""
Task Execution Module

Responsible for browser task execution (4-step workflow), result collection and exception handling
Each container has an independent thread
Supports task batch control for gradual task execution start

Browser Workflow (using agent-browser):
  Step 1: agent-browser open [URL]       → Page open
  Step 2: agent-browser snapshot -i      → DOM snapshot (get @eN refs)
  Step 3: agent-browser click @eN        → Element click (retry on fail)
  Step 4: agent-browser screenshot       → Visual screenshot

Note: agent-browser doesn't need explicit focus, open automatically works on current tab
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

    Executes complete 4-step workflow as one query (agent-browser version)
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

        # Start browser backend (agent-browser daemon)
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

            # Execute single browser task (4-step workflow)
            success, latency, step_times, interrupted = self._run_single_task()

            # If task was interrupted by stop_event, don't count as failure
            if interrupted:
                print(f"[Container{self.state.container_id}] Task interrupted by stop signal (not counted as failure)")
                break

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
        """Start agent-browser daemon in clean environment

        Clear proxy environment variables before starting agent-browser
        to ensure it works properly without proxy interference.

        Returns: (success, error_msg)
        """
        container = self.state.docker_container
        if not container:
            return False, "No container handle"

        try:
            # Step 1: Close any existing browser session
            cmd_close = "agent-browser close --all"
            container.exec_run(cmd_close, user="root")
            time.sleep(0.5)

            # Step 2: Clear proxy environment variables
            # Run in a clean shell without proxy
            cmd_clear_proxy = "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY"
            container.exec_run(cmd_clear_proxy, user="root", shell=True)

            # Step 3: Check agent-browser status in clean environment
            # Use sh -c to ensure environment is clean
            cmd = "sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser doctor --offline --quick'"
            result = container.exec_run(cmd, user="root")

            if result.exit_code == 0:
                self.state.browser_started = True
                print(f"[Container{self.state.container_id}] agent-browser ready (clean env)")
                return True, ""
            else:
                # Check if agent-browser is installed at least
                cmd2 = "sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser --version'"
                result2 = container.exec_run(cmd2, user="root")
                if result2.exit_code == 0:
                    self.state.browser_started = True
                    print(f"[Container{self.state.container_id}] agent-browser installed (daemon will auto-start)")
                    return True, ""
                else:
                    output2 = result2.output.decode('utf-8', errors='ignore') if isinstance(result2.output, bytes) else result2.output
                    return False, f"agent-browser not available: {output2[:200]}"
        except Exception as e:
            return False, str(e)

    def _run_single_task(self) -> Tuple[bool, float, Dict[str, float], bool]:
        """Execute single browser task (complete 4-step workflow)

        Returns: (success, latency_seconds, step_times, interrupted)
        - success: whether the task completed successfully
        - latency: total time taken
        - step_times: per-step timing
        - interrupted: whether the task was interrupted by stop_event (not a failure)
        """
        container = self.state.docker_container
        if not container:
            return False, 0.0, {}, False

        # Check stop_event before starting
        if self.stop_event.is_set():
            return False, 0.0, {}, True  # Interrupted, not a failure

        # Get current URL (round-robin)
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]

        step_times = {}
        start_time = time.perf_counter()

        try:
            # Step 1: Open page (agent-browser open)
            step_success, step_time = self._step_open(url)
            step_times['open'] = step_time
            if not step_success:
                if self.stop_event.is_set():
                    return False, time.perf_counter() - start_time, step_times, True
                return False, time.perf_counter() - start_time, step_times, False

            # Step 2: DOM snapshot (agent-browser snapshot -i)
            step_success, step_time, elements = self._step_snapshot()
            step_times['snapshot'] = step_time
            if not step_success:
                if self.stop_event.is_set():
                    return False, time.perf_counter() - start_time, step_times, True
                return False, time.perf_counter() - start_time, step_times, False

            # Step 3: Element click (agent-browser click)
            step_success, step_time = self._step_click(elements)
            step_times['click'] = step_time
            if not step_success:
                if self.stop_event.is_set():
                    return False, time.perf_counter() - start_time, step_times, True
                return False, time.perf_counter() - start_time, step_times, False

            # Step 4: Screenshot (agent-browser screenshot)
            step_success, step_time = self._step_screenshot()
            step_times['screenshot'] = step_time
            if not step_success:
                if self.stop_event.is_set():
                    return False, time.perf_counter() - start_time, step_times, True
                return False, time.perf_counter() - start_time, step_times, False

            elapsed = time.perf_counter() - start_time
            return True, elapsed, step_times, False

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            print(f"[Container{self.state.container_id}] Task exception: {error_msg}")
            self.state.browser_metrics.last_error = error_msg
            interrupted = self.stop_event.is_set()
            return False, elapsed, step_times, interrupted

    def _step_open(self, url: str) -> Tuple[bool, float]:
        """Step 1: Open page using agent-browser in clean environment

        Returns: (success, time_seconds)
        """
        container = self.state.docker_container
        # Run in clean shell without proxy
        cmd = f"sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser open \"{url}\"'"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, user="root")
            elapsed = time.perf_counter() - start

            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output

            if result.exit_code != 0:
                print(f"[Container{self.state.container_id}] Step 1 (open) failed: {output[:200]}")
                self.state.browser_metrics.last_error = f"open failed: {output[:200]}"
                return False, elapsed

            return True, elapsed

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"open exception: {str(e)}"
            return False, elapsed

    def _step_snapshot(self) -> Tuple[bool, float, List[str]]:
        """Step 2: DOM snapshot using agent-browser in clean environment

        Returns: (success, time_seconds, elements)
        """
        container = self.state.docker_container
        cmd = "sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser snapshot -i'"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, user="root")
            elapsed = time.perf_counter() - start

            output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output

            if result.exit_code != 0:
                print(f"[Container{self.state.container_id}] Step 2 (snapshot) failed: {output[:200]}")
                self.state.browser_metrics.last_error = f"snapshot failed: {output[:200]}"
                return False, elapsed, []

            # Extract element refs (@e1, @e2, etc.) from output
            elements = self._extract_element_refs(output)
            return True, elapsed, elements

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"snapshot exception: {str(e)}"
            return False, elapsed, []

    def _step_click(self, elements: List[str]) -> Tuple[bool, float]:
        """Step 3: Element click using agent-browser

        Strategy:
        1. If we have a working element from previous click, try it first
        2. Otherwise try elements from snapshot (prefer middle elements)
        3. On failure, get fresh snapshot and retry

        Returns: (success, time_seconds)
        """
        container = self.state.docker_container

        # Skip click if no elements available
        if not elements:
            print(f"[Container{self.state.container_id}] Step 3 (click) skipped: no elements found")
            return True, 0.0

        start = time.perf_counter()

        # Strategy 1: Try previously successful element first
        if self.state.working_click_element:
            cmd = f"sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser click {self.state.working_click_element}'"
            result = container.exec_run(cmd, user="root")
            elapsed = time.perf_counter() - start

            if result.exit_code == 0:
                return True, elapsed

            # Working element failed (page might have changed), clear it and try new ones
            print(f"[Container{self.state.container_id}] Step 3 (click) previous working element {self.state.working_click_element} failed, trying new...")
            self.state.working_click_element = ""

        # Strategy 2: Try elements from current snapshot (prefer middle elements)
        try_elements = []
        if len(elements) >= 3:
            mid = len(elements) // 2
            try_elements = [elements[mid], elements[0], elements[-1]]
        else:
            try_elements = elements[:3]

        for element_ref in try_elements:
            cmd = f"sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser click {element_ref}'"
            result = container.exec_run(cmd, user="root")
            elapsed = time.perf_counter() - start

            if result.exit_code == 0:
                # Found working element, save it for reuse
                self.state.working_click_element = element_ref
                print(f"[Container{self.state.container_id}] Step 3 (click) succeeded with {element_ref} (saved for reuse)")
                return True, elapsed

        # Strategy 3: Get fresh snapshot and retry
        print(f"[Container{self.state.container_id}] Step 3 (click) initial attempts failed, getting fresh snapshot...")
        time.sleep(0.5)

        snapshot_cmd = "sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser snapshot -i'"
        snapshot_result = container.exec_run(snapshot_cmd, user="root")

        if snapshot_result.exit_code == 0:
            snapshot_output = snapshot_result.output.decode('utf-8', errors='ignore') if isinstance(snapshot_result.output, bytes) else snapshot_result.output
            fresh_elements = self._extract_element_refs(snapshot_output)

            if fresh_elements:
                fresh_try = fresh_elements[:3] if len(fresh_elements) >= 3 else fresh_elements
                for element_ref in fresh_try:
                    cmd = f"sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser click {element_ref}'"
                    result = container.exec_run(cmd, user="root")
                    elapsed = time.perf_counter() - start

                    if result.exit_code == 0:
                        self.state.working_click_element = element_ref
                        print(f"[Container{self.state.container_id}] Step 3 (click) retry succeeded with {element_ref} (saved for reuse)")
                        return True, elapsed

                elapsed = time.perf_counter() - start
                self.state.browser_metrics.last_error = f"click failed after fresh snapshot, tried {len(fresh_try)} elements"
                return False, elapsed
            else:
                elapsed = time.perf_counter() - start
                self.state.browser_metrics.last_error = f"click failed: no elements in fresh snapshot"
                return False, elapsed
        else:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"click failed: fresh snapshot failed"
            return False, elapsed

    def _step_screenshot(self) -> Tuple[bool, float]:
        """Step 4: Screenshot using agent-browser in clean environment

        Returns: (success, time_seconds)
        """
        container = self.state.docker_container
        cmd = "sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser screenshot'"

        start = time.perf_counter()
        try:
            result = container.exec_run(cmd, user="root")
            elapsed = time.perf_counter() - start

            if result.exit_code != 0:
                output = result.output.decode('utf-8', errors='ignore') if isinstance(result.output, bytes) else result.output
                print(f"[Container{self.state.container_id}] Step 4 (screenshot) failed: {output[:100]}")
                self.state.browser_metrics.last_error = f"screenshot failed: {output[:200]}"
                return False, elapsed

            return True, elapsed

        except Exception as e:
            elapsed = time.perf_counter() - start
            self.state.browser_metrics.last_error = f"screenshot exception: {str(e)}"
            return False, elapsed

    def _clear_browser_cache(self) -> bool:
        """Clear browser session after test

        Returns: success
        """
        container = self.state.docker_container
        if not container:
            return False

        try:
            # Close browser session in clean environment
            cmd = "sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY && agent-browser close --all'"
            result = container.exec_run(cmd, user="root")
            return result.exit_code == 0
        except Exception:
            return False

    def _extract_element_refs(self, output: str) -> List[str]:
        """Extract element refs from agent-browser snapshot output

        Returns: list of element refs (@e1, @e2, etc.)
        """
        # Pattern for agent-browser element refs: @eN
        pattern = r"@e[0-9]+"
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