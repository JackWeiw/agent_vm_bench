"""
Task Execution Module

Responsible for browser task execution, result collection and exception handling
Each sandbox has an independent thread
Supports task batch control for gradual task execution start
Supports warmup phase for memory preheating
"""

import time
import random
import threading
from typing import Tuple, List, Dict

from .config import Config
from .schemas import SandboxState, SandboxStatus


class WarmupRunner(threading.Thread):
    """Warmup phase runner - executes warmup pages for a single sandbox"""

    def __init__(
        self,
        state: SandboxState,
        config: Config,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config

    def run(self) -> None:
        """Execute warmup phase for this sandbox"""
        # Wait for sandbox ports ready
        while True:
            if self.state.creation_metrics.status == SandboxStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.PORT_FAILED, SandboxStatus.OFFLINE, SandboxStatus.KILLED):
                print(f"[Sandbox{self.state.sandbox_id}] Cannot start warmup: {self.state.creation_metrics.status.value}")
                return
            time.sleep(0.5)

        sbx = self.state.sandbox_obj
        if not sbx:
            print(f"[Sandbox{self.state.sandbox_id}] No sandbox handle for warmup")
            self.state.warmup_done = True
            return

        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, 'sandbox_id') else 'N/A'
        failed_urls = []

        # Loop through warmup pages
        for loop in range(self.config.warmup_loops):
            for url in self.config.warmup_urls:
                if not url.strip():
                    continue

                cmd = f"openclaw browser --browser-profile openclaw open '{url}'"
                try:
                    result = sbx.commands.run(cmd, timeout=60, user="root")
                    if result.exit_code != 0:
                        failed_urls.append(url[:50])
                except Exception as e:
                    failed_urls.append(url[:50])

                # Delay between pages
                time.sleep(self.config.warmup_delay)

        # Execute openclaw config set and memory index (optional, for memory warmup)
        # These commands help bring QEMU memory to target value
        try:
            cmd1 = 'openclaw config set agents.defaults.memorySearch.chunking.tokens 200'
            sbx.commands.run(cmd1, timeout=30, user="root")

            cmd2 = 'openclaw memory index --force'
            sbx.commands.run(cmd2, timeout=120, user="root")
        except Exception:
            pass  # Optional commands, ignore errors

        # Mark warmup complete
        self.state.warmup_done = True

        if failed_urls:
            print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Warmup had {len(failed_urls)} failed pages")
        else:
            print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Warmup completed")


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
    """Task manager - manages all sandbox task execution threads with batch control"""

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
        self.warmup_runners: List[WarmupRunner] = []

    def start_warmup(self) -> None:
        """Start warmup phase for all PORT_READY sandboxes

        Warmup phase runs before benchmark to preheat memory.
        After warmup, sandboxes are ready for actual benchmark.
        """
        ready_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]

        if not ready_states:
            print("No sandboxes ready for warmup")
            return

        if not self.config.warmup_urls:
            print("No warmup URLs configured, skipping warmup")
            for state in ready_states:
                state.warmup_done = True
            return

        print(f"\n{'='*60}")
        print(f"Warmup Phase Starting")
        print(f"  Total: {len(ready_states)} sandboxes")
        print(f"  Warmup pages: {len(self.config.warmup_urls)}")
        print(f"  Loop count: {self.config.warmup_loops}")
        print(f"  Page delay: {self.config.warmup_delay}s")
        print(f"{'='*60}")

        for state in ready_states:
            runner = WarmupRunner(state, self.config)
            self.warmup_runners.append(runner)
            runner.start()

    def wait_warmup(self, timeout: float = 300.0) -> Tuple[int, int]:
        """Wait for all warmup runners to complete

        Returns: (completed_count, failed_count)
        """
        start_time = time.time()
        last_progress_time = start_time

        while time.time() - start_time < timeout:
            if self.stop_event.is_set():
                break

            done_count = sum(1 for s in self.sandbox_states.values() if s.warmup_done)
            total_count = len(self.warmup_runners)

            # Print progress every 5 seconds
            now = time.time()
            if now - last_progress_time >= 5:
                elapsed = now - start_time
                print(f"   Warmup progress: {done_count}/{total_count} completed | elapsed {elapsed:.0f}s")
                last_progress_time = now

            if done_count >= total_count:
                break

            time.sleep(1)

        # Wait for all runners to finish
        for runner in self.warmup_runners:
            runner.join(timeout=2)

        completed = sum(1 for s in self.sandbox_states.values() if s.warmup_done)
        failed = sum(1 for s in self.sandbox_states.values() if s.warmup_done and s.browser_metrics.failed_count > 0)

        return completed, failed

    def start_all(self) -> None:
        """Start task execution threads for all PORT_READY sandboxes

        Strategy based on task_batch config:
        - With task_batch_size: batched start to avoid target server overload
        - Without config: full concurrent start for max load test
        """
        # Filter PORT_READY sandboxes that have completed warmup (or no warmup needed)
        ready_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.warmup_done
        ]

        if not ready_states:
            print("No sandboxes ready for task execution")
            return

        if self.config.task_batch_size and self.config.task_batch_size > 0:
            self._start_batched(ready_states)
        else:
            self._start_concurrent(ready_states)

    def _start_batched(self, ready_states: List[SandboxState]) -> None:
        """Batched task execution start"""
        total = len(ready_states)
        batch_size = self.config.task_batch_size
        batch_count = (total + batch_size - 1) // batch_size

        print(f"\n{'='*60}")
        print(f"Batched Task Execution Start")
        print(f"  Total: {total} sandboxes")
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

            print(f"\n[TaskBatch {batch_id}/{batch_count-1}] Starting tasks for sandboxes {start_idx+1}-{end_idx}")

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

    def _start_concurrent(self, ready_states: List[SandboxState]) -> None:
        """Full concurrent task execution start"""
        print(f"\n{'='*60}")
        print(f"Concurrent Task Execution Start")
        print(f"  Total: {len(ready_states)} sandboxes (full concurrent)")
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