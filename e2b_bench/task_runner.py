"""
Task Execution Module

Responsible for task execution, result collection and exception handling.
Each sandbox has an independent thread.
Supports task batch control for gradual task execution start.
Supports warmup phase for memory preheating.
Supports agent-browser operations for browser benchmark mode.
Supports coding operations for coding benchmark mode.

Classes:
- WarmupRunner: Opens multiple tabs during warmup phase (browser)
- BrowserTaskRunner: Executes browser tasks in fixed mode
- TabOperationRunner: Opens new tab and executes operations in round-robin mode
- TaskManager: Manages warmup and task execution threads (workflow-aware dispatch)
"""

import logging
import random
import re
import threading
import time
from typing import Dict, List, Tuple

from .config import Config
from .helpers import wait_for_port_ready
from .schemas import SandboxState, SandboxStatus

logger = logging.getLogger(__name__)


def extract_element_refs(output: str) -> List[str]:
    """Extract element refs from agent-browser snapshot output.

    Args:
        output: stdout from agent-browser snapshot -i command

    Returns:
        List of element refs (e.g., ['e1', 'e2', ...])
    """
    pattern = r"\[ref=(e\d+)\]"
    matches = re.findall(pattern, output)
    return matches[:50]  # Limit to 50 elements


class WarmupRunner(threading.Thread):
    """Warmup phase runner - opens multiple tabs using agent-browser

    Opens each warmup URL as a separate tab, then executes operations
    (snapshot -> click -> screenshot) to allocate memory.

    The warmup_loops parameter is not applicable - each URL is opened once.
    """

    # Class-level flag to ensure warning is printed only once
    _warmup_loops_warned = False
    _warn_lock = threading.Lock()

    def __init__(
        self,
        state: SandboxState,
        config: Config,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config

    def run(self) -> None:
        """Execute warmup phase for this sandbox - open multiple tabs

        Opens each warmup URL as a separate tab.
        Each URL is opened exactly once (warmup_loops is ignored for tabs).
        """
        # Wait for sandbox ports ready
        if not wait_for_port_ready(self.state):
            logger.warning(
                f"[Sandbox{self.state.sandbox_id}] Cannot start warmup: {self.state.creation_metrics.status.value}"
            )
            return

        sbx = self.state.sandbox_obj
        if not sbx:
            logger.info(f"[Sandbox{self.state.sandbox_id}] No sandbox handle for warmup")
            self.state.warmup_done = True
            return

        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, "sandbox_id") else "N/A"
        failed_urls = []

        # Warn if warmup_loops > 1 (not applicable) - only once
        if self.config.warmup_loops > 1:
            with WarmupRunner._warn_lock:
                if not WarmupRunner._warmup_loops_warned:
                    logger.info(
                        f"[Warmup] Note: warmup_loops={self.config.warmup_loops} is ignored (each URL opened once)"
                    )
                    WarmupRunner._warmup_loops_warned = True

        # Check if agent-browser is available
        try:
            result = sbx.commands.run("agent-browser --version", timeout=30, user="root")
            if result.exit_code != 0:
                logger.warning(f"[Sandbox{self.state.sandbox_id}] agent-browser not available, skipping tab warmup")
                self.state.warmup_done = True
                return
        except Exception as e:
            logger.error(f"[Sandbox{self.state.sandbox_id}] Failed to check agent-browser: {e}")
            self.state.warmup_done = True
            return

        # Open tabs with warmup_urls (each URL in a new tab)
        for i, url in enumerate(self.config.warmup_urls):
            if not url.strip():
                continue

            try:
                if i == 0:
                    # First tab: use open (replaces current page)
                    cmd = f'agent-browser open "{url}"'
                else:
                    # Subsequent tabs: use tab new
                    cmd = f'agent-browser tab new "{url}"'

                # Use longer timeout for tab operations (120s instead of 60s)
                result = sbx.commands.run(cmd, timeout=120, user="root")

                if result.exit_code != 0:
                    failed_urls.append(url[:50])
                    continue

                # Wait for page load with longer timeout
                wait_cmd = "agent-browser wait --load domcontentloaded --timeout 120000"
                sbx.commands.run(wait_cmd, timeout=130, user="root")

                # Store tab ID (t1, t2, ...)
                self.state.tab_ids.append(f"t{i+1}")

                # Execute operations on this tab: snapshot -> click -> screenshot
                self._execute_tab_operations(sbx, i + 1)

                # Delay between pages
                time.sleep(self.config.warmup_delay)

            except Exception as e:
                logger.error(f"[Sandbox{self.state.sandbox_id}] Failed to open tab {i+1}: {e}")
                failed_urls.append(url[:50])

        # Mark warmup complete
        self.state.warmup_done = True

        if failed_urls:
            logger.warning(
                f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Warmup had {len(failed_urls)} failed pages"
            )
        else:
            logger.info(
                f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Warmup completed: {len(self.state.tab_ids)} tabs opened"
            )

    def _execute_tab_operations(self, sbx, tab_num: int) -> None:
        """Execute operations on a tab after it's opened.

        Args:
            sbx: Sandbox object
            tab_num: Tab number (1-based, for logging)
        """
        # Step 1: DOM snapshot
        result = sbx.commands.run("agent-browser snapshot -i", timeout=60, user="root")
        if result.exit_code != 0:
            logger.warning(f"[Sandbox{self.state.sandbox_id}] Tab {tab_num}: snapshot failed")
            return

        # Extract element refs
        elements = extract_element_refs(result.stdout)

        # Step 2: Element click (try first valid element)
        if elements:
            click_result = sbx.commands.run(f"agent-browser click {elements[0]}", timeout=30, user="root")
            if click_result.exit_code != 0:
                logger.warning(f"[Sandbox{self.state.sandbox_id}] Tab {tab_num}: click failed on {elements[0]}")

        # Step 3: Screenshot
        screenshot_result = sbx.commands.run("agent-browser screenshot", timeout=30, user="root")
        if screenshot_result.exit_code != 0:
            logger.warning(f"[Sandbox{self.state.sandbox_id}] Tab {tab_num}: screenshot failed")


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
        if not wait_for_port_ready(self.state, self.stop_event):
            logger.warning(
                f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}"
            )
            return

        # Browser task execution loop
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                logger.info(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break

            # Execute single browser task
            success, latency = self._run_single_task()

            # Update metrics
            timeout = latency > self.config.browser_timeout
            self.state.browser_metrics.add(latency, success and not timeout, timeout)
            self.state.update_last_task_time(time.time())  # Thread-safe update

            # Error handling
            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    logger.warning(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break

            # Random interval to avoid request spike
            sleep_time = random.uniform(self.config.browser_interval_min, self.config.browser_interval_max)
            time.sleep(sleep_time)

        logger.info(f"[Sandbox{self.state.sandbox_id}] Task runner ended")

    def _run_single_task(self) -> Tuple[bool, float]:
        """Execute single browser task

        Use state.sandbox_obj handle to execute command

        Returns: (success, latency_seconds)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0

        # Get E2B sandbox_id for logging (different from internal sequence number)
        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, "sandbox_id") else "N/A"

        # Get current URL (round-robin)
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]

        # Build browser command
        cmd = f"openclaw browser --browser-profile openclaw open '{url}'"

        start_time = time.perf_counter()
        try:
            result = sbx.commands.run(cmd, timeout=self.config.browser_timeout + 30, user="root")
            elapsed = time.perf_counter() - start_time + 10  # simulate llm response time

            success = result.exit_code == 0

            # Log detailed error info on failure
            if not success:
                error_detail = f"exit_code={result.exit_code}"
                if result.stderr:
                    error_detail += f", stderr={result.stderr[:200]}"
                if result.stdout:
                    error_detail += f", stdout={result.stdout[:200]}"
                logger.error(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Task failed: {error_detail}")

                # Store last error for debugging
                self.state.browser_metrics.last_error = error_detail

            return success, elapsed
        except Exception as e:
            elapsed = time.perf_counter() - start_time + 10  # simulate llm response time
            error_msg = str(e)
            logger.error(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Task exception: {error_msg}")
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
        self.runners: List[threading.Thread] = []
        self.warmup_runners: List[WarmupRunner] = []

    def start_warmup(self) -> None:
        """Start warmup phase for all PORT_READY sandboxes

        Warmup phase runs before benchmark to preheat memory.
        After warmup, sandboxes are ready for actual benchmark.

        Dispatches based on workflow_type:
        - "browser": uses WarmupRunner (opens browser tabs)
        - "coding": uses CodingWarmupRunner (one initial verify, no resident process)
        - "document": validates and restores the PDF/XLSX seed
        """
        ready_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]

        if not ready_states:
            logger.info("No sandboxes ready for warmup")
            return

        # Select warmup runner based on workflow type
        if self.config.workflow_type == "coding":
            from .coding_task_runner import CodingWarmupRunner

            # Coding warmup: one initial verify (no resident process)
            if not self.config.coding_skip_verify:
                logger.info(f"\n{'=' * 60}")
                logger.info("Coding Warmup Phase Starting")
                logger.info(f"  Total: {len(ready_states)} sandboxes")
                logger.info(f"  Project: {self.config.coding_project_dir}")
                logger.info(f"  Language: {self.config.coding_language}")
                logger.info(f"  Initial verify: {'enabled' if not self.config.coding_skip_verify else 'skipped'}")
                logger.info(f"{'=' * 60}")

                for state in ready_states:
                    runner = CodingWarmupRunner(state, self.config)
                    self.warmup_runners.append(runner)
                    runner.start()
            else:
                logger.info("Coding warmup skipped (initial verify disabled)")
                for state in ready_states:
                    state.warmup_done = True
        elif self.config.workflow_type == "document":
            from .document_task_runner import DocumentWarmupRunner

            logger.info(f"\n{'=' * 60}")
            logger.info("Document Warmup Phase Starting")
            logger.info(f"  Total: {len(ready_states)} sandboxes")
            logger.info(f"  Case kind: {self.config.document_case_kind}")
            logger.info(f"  Seed: {self.config.document_seed_dir}")
            logger.info(f"{'=' * 60}")
            for state in ready_states:
                runner = DocumentWarmupRunner(state, self.config)
                self.warmup_runners.append(runner)
                runner.start()
        elif self.config.workflow_type == "browser":
            # Browser warmup: uses warmup_urls to open tabs
            if not self.config.warmup_urls:
                logger.info("No warmup URLs configured, skipping warmup")
                for state in ready_states:
                    state.warmup_done = True
                return

            logger.info(f"\n{'=' * 60}")
            logger.info("Warmup Phase Starting")
            logger.info(f"  Total: {len(ready_states)} sandboxes")
            logger.info(f"  Warmup pages: {len(self.config.warmup_urls)}")
            logger.info(f"  Loop count: {self.config.warmup_loops}")
            logger.info(f"  Page delay: {self.config.warmup_delay}s")
            logger.info(f"{'=' * 60}")

            for state in ready_states:
                runner = WarmupRunner(state, self.config)
                self.warmup_runners.append(runner)
                runner.start()
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

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
                logger.info(f"   Warmup progress: {done_count}/{total_count} completed | elapsed {elapsed:.0f}s")
                last_progress_time = now

            if done_count >= total_count:
                break

            time.sleep(1)

        # Wait for all runners to finish
        for runner in self.warmup_runners:
            runner.join(timeout=2)

        completed = sum(1 for s in self.sandbox_states.values() if s.warmup_done)
        if self.config.workflow_type == "coding":
            failed = sum(1 for s in self.sandbox_states.values() if s.warmup_done and s.coding_metrics.failed_count > 0)
        elif self.config.workflow_type == "document":
            failed = sum(1 for s in self.sandbox_states.values() if getattr(s, "warmup_error", ""))
        elif self.config.workflow_type == "browser":
            failed = sum(
                1 for s in self.sandbox_states.values() if s.warmup_done and s.browser_metrics.failed_count > 0
            )
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

        return completed, failed

    def start_all(self) -> None:
        """Start task execution threads for PORT_READY sandboxes

        Strategy based on task_batch config:
        - With task_batch_size: batched start to avoid target server overload
        - Without config: full concurrent start for max load test

        benchmark_percent controls how many sandboxes to include in benchmark
        (e.g., 0.5 = 50% of ready sandboxes)
        """
        # Filter PORT_READY sandboxes that have completed warmup (or no warmup needed)
        ready_states = [
            s
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.warmup_done
        ]

        if not ready_states:
            logger.info("No sandboxes ready for task execution")
            return

        # Select subset based on benchmark_percent
        total_ready = len(ready_states)
        benchmark_count = max(1, int(total_ready * self.config.benchmark_percent))

        if benchmark_count < total_ready:
            # Randomly select N sandboxes for benchmark
            benchmark_states = random.sample(ready_states, benchmark_count)
            logger.info(
                f"\nBenchmark subset: {benchmark_count}/{total_ready} sandboxes ({self.config.benchmark_percent * 100:.0f}%)"
            )
        else:
            benchmark_states = ready_states

        if self.config.task_batch_size and self.config.task_batch_size > 0:
            self._start_batched(benchmark_states)
        else:
            self._start_concurrent(benchmark_states)

    def _start_batched(self, ready_states: List[SandboxState]) -> None:
        """Batched task execution start"""
        total = len(ready_states)
        batch_size = self.config.task_batch_size
        batch_count = (total + batch_size - 1) // batch_size

        workflow_label = self.config.workflow_type.capitalize()

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Batched {workflow_label} Task Execution Start")
        logger.info(f"  Total: {total} sandboxes")
        logger.info(f"  Batches: {batch_count} x {batch_size}")
        logger.info(f"  Interval: {self.config.task_batch_interval}s")
        logger.info(f"{'=' * 60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                logger.info("Stop event detected, aborting task start")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)
            batch_states = ready_states[start_idx:end_idx]

            logger.info(
                f"\n[TaskBatch {batch_id}/{batch_count - 1}] Starting tasks for sandboxes {start_idx + 1}-{end_idx}"
            )

            # Select runner based on workflow type
            for state in batch_states:
                runner = self._create_task_runner(state)
                self.runners.append(runner)
                runner.start()

            # Wait between batches (last batch no wait)
            if batch_id < batch_count - 1 and self.config.task_batch_interval:
                logger.info(f"Waiting {self.config.task_batch_interval}s before next task batch...")
                time.sleep(self.config.task_batch_interval)

        logger.info(f"\nStarted {len(self.runners)} task runners in {batch_count} batches")

    def _start_concurrent(self, ready_states: List[SandboxState]) -> None:
        """Full concurrent task execution start"""
        workflow_label = self.config.workflow_type.capitalize()

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Concurrent {workflow_label} Task Execution Start")
        logger.info(f"  Total: {len(ready_states)} sandboxes (full concurrent)")
        logger.info(f"{'=' * 60}")

        for state in ready_states:
            runner = self._create_task_runner(state)
            self.runners.append(runner)
            runner.start()

        logger.info(f"\nStarted {len(self.runners)} task runners")

    def _create_task_runner(self, state: SandboxState) -> threading.Thread:
        """Create task runner based on workflow type

        Args:
            state: Sandbox state for the runner

        Returns:
            Task runner thread (BrowserTaskRunner or CodingTaskRunner)
        """
        if self.config.workflow_type == "coding":
            from .coding_task_runner import CodingTaskRunner

            return CodingTaskRunner(state, self.config, self.stop_event)
        if self.config.workflow_type == "document":
            from .document_task_runner import DocumentTaskRunner

            return DocumentTaskRunner(state, self.config, self.stop_event)
        if self.config.workflow_type == "browser":
            return BrowserTaskRunner(state, self.config, self.stop_event)
        raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

    def wait_all(self, timeout: float = 5.0) -> None:
        """Wait for all task threads to end"""
        if self.config.workflow_type == "document":
            deadline = time.monotonic() + self.config.document_task_timeout + 5
            for runner in self.runners:
                remaining = max(0.0, deadline - time.monotonic())
                runner.join(timeout=remaining)
            alive = [runner.name for runner in self.runners if runner.is_alive()]
            if alive:
                raise RuntimeError(f"document runners did not finish before task deadline: {alive}")
            return
        if self.config.workflow_type in {"browser", "coding"}:
            for runner in self.runners:
                runner.join(timeout=timeout)
            return
        raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")


class TabOperationRunner(threading.Thread):
    """Runner for tab operations in round-robin benchmark mode.

    Each round opens a NEW tab with a URL, then executes:
    new tab -> snapshot -> click -> screenshot

    This allocates new memory per round, triggering swap out events.

    Attributes:
        state: Sandbox state for metrics
        config: Test configuration
        stop_event: Global stop event
        round_id: Current round number
    """

    # Per-step E2B command timeout (seconds). Referenced by both the
    # sbx.commands.run call sites and _classify_exception so the timeout
    # reported in error messages always matches the actual budget.
    OPEN_TAB_TIMEOUT = 60  # `agent-browser tab new`
    SNAPSHOT_TIMEOUT = 60  # `agent-browser snapshot -i`

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
        round_id: int,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.round_id = round_id
        self.consecutive_errors = 0

    def run(self) -> None:
        """Execute tab operations for this round.

        Opens a new tab with browser_url, then executes snapshot -> click -> screenshot.
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            logger.info(f"[Sandbox{self.state.sandbox_id}] No sandbox handle available for tab operations")
            return

        # Get URL for this round (round-robin from browser_urls)
        if not self.config.browser_urls:
            logger.info(f"[Sandbox{self.state.sandbox_id}] No browser_urls configured")
            return

        url_index = self.round_id % len(self.config.browser_urls)
        url = self.config.browser_urls[url_index]

        start_time = time.perf_counter()

        success, step_times, failed_step, error_detail = self._execute_steps(sbx, url)

        elapsed = self._record_metrics(start_time, success, step_times, error_detail)

        if success:
            # Print success summary with step timing breakdown
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            logger.info(f"[Sandbox{self.state.sandbox_id}] New tab completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(url, failed_step, error_detail)

    def _execute_steps(self, sbx, url: str) -> Tuple[bool, Dict[str, float], str, str]:
        """Execute all steps: open new tab -> snapshot -> click -> screenshot.

        Args:
            sbx: Sandbox object
            url: URL to open in new tab

        Returns:
            Tuple of (success, step_times, failed_step, error_detail)
        """
        success = True
        step_times = {}
        failed_step = None
        error_detail = ""
        elements = []

        try:
            # Step 1: Open new tab with URL
            success, error_detail = self._step_open_tab(sbx, url, step_times)
            if not success:
                failed_step = "open_tab"
                return success, step_times, failed_step, error_detail

            # Step 2: DOM snapshot
            success, elements, error_detail = self._step_snapshot(sbx, step_times)
            if not success:
                failed_step = "snapshot"
                return success, step_times, failed_step, error_detail

            # Step 3: Element click (optional, non-fatal)
            _, click_error = self._step_click(sbx, elements, step_times)

            # Step 4: Screenshot (non-fatal)
            _, screenshot_error = self._step_screenshot(sbx, step_times)

            # Log non-fatal errors (click/screenshot failures)
            if click_error:
                logger.warning(f"[Sandbox{self.state.sandbox_id}] Non-fatal: {click_error}")
            if screenshot_error:
                logger.warning(f"[Sandbox{self.state.sandbox_id}] Non-fatal: {screenshot_error}")

            # Combine non-fatal errors for metrics tracking
            non_fatal_errors = []
            if click_error:
                non_fatal_errors.append(click_error)
            if screenshot_error:
                non_fatal_errors.append(screenshot_error)
            if non_fatal_errors:
                error_detail = "; ".join(non_fatal_errors)

        except Exception as e:
            success = False
            failed_step, error_detail = self._classify_exception(e, step_times)

        return success, step_times, failed_step, error_detail

    def _step_open_tab(self, sbx, url: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 1: Open new tab with URL and wait for page load.

        Records two separate timings:
        - open_tab: Time to create new tab
        - page_load: Time to wait for networkidle

        Returns:
            Tuple of (success, error_detail)
        """
        # Step 1a: Create new tab
        tab_start = time.perf_counter()
        result = sbx.commands.run(f'agent-browser tab new "{url}"', timeout=self.OPEN_TAB_TIMEOUT, user="root")
        step_times["open_tab"] = time.perf_counter() - tab_start

        if result.exit_code != 0:
            # Build detailed error message with exit_code, stderr, stdout
            error_parts = [f"open_tab failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:200]}")
            if result.stdout:
                error_parts.append(f"stdout={result.stdout[:200]}")
            error_parts.append(f"url={url[:80]}")
            return False, " | ".join(error_parts)

        # Step 1b: Wait for network idle (page fully loaded)
        wait_start = time.perf_counter()
        wait_result = sbx.commands.run("agent-browser wait --load networkidle --timeout 60000", timeout=70, user="root")
        step_times["page_load"] = time.perf_counter() - wait_start

        if wait_result.exit_code != 0:
            error_parts = [f"page_load failed: exit_code={wait_result.exit_code}"]
            if wait_result.stderr:
                error_parts.append(f"stderr={wait_result.stderr[:200]}")
            error_parts.append(f"url={url[:80]}")
            return False, " | ".join(error_parts)

        return True, ""

    def _step_snapshot(self, sbx, step_times: Dict[str, float]) -> Tuple[bool, List[str], str]:
        """Step 2: DOM snapshot.

        Returns:
            Tuple of (success, elements, error_detail)
        """
        step_start = time.perf_counter()
        result = sbx.commands.run("agent-browser snapshot -i", timeout=self.SNAPSHOT_TIMEOUT, user="root")
        step_times["snapshot"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            # Build detailed error message
            error_parts = [f"snapshot failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:200]}")
            if result.stdout:
                error_parts.append(f"stdout={result.stdout[:200]}")
            return False, [], " | ".join(error_parts)

        elements = extract_element_refs(result.stdout)
        return True, elements, ""

    def _step_click(self, sbx, elements: List[str], step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 3: Element click (non-fatal).

        Args:
            sbx: Sandbox object
            elements: List of element refs
            step_times: Dict to record timing

        Returns:
            Tuple of (success, error_detail) - error_detail is empty string on success
        """
        if not elements:
            return True, ""

        step_start = time.perf_counter()
        result = sbx.commands.run(f"agent-browser click {elements[0]}", timeout=30, user="root")
        step_times["click"] = time.perf_counter() - step_start

        # Click failure is not fatal, but return error for logging
        if result.exit_code != 0:
            error_parts = [f"click failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            error_parts.append(f"element={elements[0]}")
            return True, " | ".join(error_parts)
        return True, ""

    def _step_screenshot(self, sbx, step_times: Dict[str, float]) -> Tuple[bool, str]:
        """Step 4: Screenshot (non-fatal).

        Args:
            sbx: Sandbox object
            step_times: Dict to record timing

        Returns:
            Tuple of (success, error_detail) - error_detail is empty string on success
        """
        step_start = time.perf_counter()
        result = sbx.commands.run("agent-browser screenshot", timeout=30, user="root")
        step_times["screenshot"] = time.perf_counter() - step_start

        # Screenshot failure is not fatal, but return error for logging
        if result.exit_code != 0:
            error_parts = [f"screenshot failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            return True, " | ".join(error_parts)
        return True, ""

    def _classify_exception(self, e: Exception, step_times: Dict[str, float]) -> Tuple[str, str]:
        """Classify an exception to determine which step failed and why.

        Distinguishes:
        - unreachable: the E2B control plane could not route the command to the
          sandbox microVM (e.g. the sandbox was OOM-killed, paused, or reclaimed
          by the host). The command never ran inside the sandbox, so this is an
          infrastructure failure, not a per-step timeout. The dedicated bucket
          keeps it separate from real code exceptions in the aggregated report.
        - open_tab / snapshot: the corresponding step exceeded its E2B command
          timeout (see OPEN_TAB_TIMEOUT / SNAPSHOT_TIMEOUT). The failing step is
          inferred from which step had not yet recorded a timing in step_times.
        - unknown: a timeout occurred on a step without a dedicated classifier.
        - exception: any other non-timeout error.
        """
        error_str = str(e)
        if "Failed to route request to sandbox" in error_str:
            return "unreachable", f"sandbox unreachable: {error_str[:100]}"
        if "context deadline exceeded" in error_str or "timed out" in error_str:
            if "open_tab" not in step_times:
                return "open_tab", f"open_tab timed out after {self.OPEN_TAB_TIMEOUT}s"
            elif "snapshot" not in step_times:
                return "snapshot", f"snapshot timed out after {self.SNAPSHOT_TIMEOUT}s"
            else:
                return "unknown", f"operation timed out: {error_str[:100]}"
        return "exception", f"exception: {error_str[:100]}"

    def _record_metrics(
        self, start_time: float, success: bool, step_times: Dict[str, float], error_detail: str
    ) -> float:
        """Record metrics for this operation.

        Returns:
            Elapsed time in seconds
        """
        elapsed = time.perf_counter() - start_time
        timeout = elapsed > self.config.browser_timeout
        self.state.browser_metrics.add(elapsed, success and not timeout, timeout, step_times=step_times)
        self.state.update_last_task_time(time.time())  # Thread-safe update

        if not success and error_detail:
            self.state.browser_metrics.last_error = error_detail

        return elapsed

    def _handle_failure(self, url: str, failed_step: str, error_detail: str) -> None:
        """Handle failure after metrics are recorded.

        Args:
            url: URL that was being processed when failure occurred
            failed_step: Name of the step that failed
            error_detail: Detailed error message
        """
        logger.error(
            f"[Sandbox{self.state.sandbox_id}] Round {self.round_id} URL '{url[:50]}' "
            f"failed at {failed_step}: {error_detail}"
        )
        self.consecutive_errors += 1
        if self.consecutive_errors >= 3:
            self.state.is_alive = False
