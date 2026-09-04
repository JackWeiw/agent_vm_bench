"""Browser workflow task runners (host-agnostic port of e2b_bench.task_runner).

Each sandbox gets an independent thread that drives the sandbox backend solely
through :meth:`EnvironmentProvider.exec`. The browser backend (agent-browser +
openclaw-gateway) is started by the provider's ``prepare`` hook; the runners
only issue commands, so they are host-agnostic.

Classes:
    WarmupRunner        -- open warmup tabs (snapshot -> click -> screenshot)
    BrowserTaskRunner   -- fixed-mode continuous browser task loop
    TabOperationRunner  -- one round-robin round (new tab -> snapshot -> click -> screenshot)
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time

from bench_core.config import KernelConfig
from bench_core.schemas import BenchSandbox
from env_provider import EnvironmentProvider

logger = logging.getLogger(__name__)


def extract_element_refs(output: str) -> list[str]:
    """Extract element refs from agent-browser snapshot output.

    Args:
        output: stdout from ``agent-browser snapshot -i``.

    Returns:
        List of element refs (e.g. ``['e1', 'e2', ...]``), capped at 50.
    """
    pattern = r"\[ref=(e\d+)\]"
    matches = re.findall(pattern, output)
    return matches[:50]


class WarmupRunner(threading.Thread):
    """Warmup phase runner -- opens multiple tabs using agent-browser.

    Opens each warmup URL as a separate tab, then runs snapshot -> click ->
    screenshot to allocate memory. Each URL is opened exactly once
    (``warmup_loops`` is ignored for tab mode).
    """

    _warmup_loops_warned = False
    _warn_lock = threading.Lock()

    def __init__(self, state: BenchSandbox, config: KernelConfig, provider: EnvironmentProvider):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.provider = provider

    def run(self) -> None:
        """Execute warmup for this sandbox -- open one tab per warmup URL."""
        # Gate on readiness. The provider's create_all runs the readiness check
        # (port probe for browser) before returning, so a non-ready instance
        # never reaches warmup with a live handle.
        if not self.state.ready:
            logger.warning(
                f"[Sandbox{self.state.index}] Cannot start warmup: " f"{self.state.creation_metrics.status.value}"
            )
            return

        sid = self.state.id
        failed_urls: list[str] = []

        # Warn if warmup_loops > 1 (not applicable in tab mode) -- only once.
        if self.config.warmup_loops > 1:
            with WarmupRunner._warn_lock:
                if not WarmupRunner._warmup_loops_warned:
                    logger.info(
                        f"[Warmup] Note: warmup_loops={self.config.warmup_loops} is ignored (each URL opened once)"
                    )
                    WarmupRunner._warmup_loops_warned = True

        # Check that agent-browser is available inside the sandbox.
        try:
            result = self.provider.exec(self.state, "agent-browser --version", timeout=30)
            if result.exit_code != 0:
                logger.warning(f"[Sandbox{self.state.index}] agent-browser not available, skipping tab warmup")
                self.state.warmup_done = True
                return
        except Exception as e:
            logger.error(f"[Sandbox{self.state.index}] Failed to check agent-browser: {e}")
            self.state.warmup_done = True
            return

        for i, url in enumerate(self.config.warmup_urls):
            if not url.strip():
                continue

            try:
                if i == 0:
                    # First tab: open replaces the current page.
                    cmd = f'agent-browser open "{url}"'
                else:
                    cmd = f'agent-browser tab new "{url}"'

                # Tab operations can be slow on a cold sandbox; allow 120s.
                result = self.provider.exec(self.state, cmd, timeout=120)
                if result.exit_code != 0:
                    failed_urls.append(url[:50])
                    continue

                # Wait for the page to reach domcontentloaded.
                self.provider.exec(
                    self.state,
                    "agent-browser wait --load domcontentloaded --timeout 120000",
                    timeout=130,
                )

                self.state.tab_ids.append(f"t{i + 1}")
                self._execute_tab_operations(i + 1)
                time.sleep(self.config.warmup_delay)
            except Exception as e:
                # Non-fatal: the failed URL is recorded in failed_urls and
                # summarized at WARNING below; other tabs still warm up.
                logger.warning(f"[Sandbox{self.state.index}] Failed to open tab {i + 1}: {e}")
                failed_urls.append(url[:50])

        self.state.warmup_done = True

        if failed_urls:
            logger.warning(f"[Sandbox{self.state.index}] (id:{sid}) Warmup had {len(failed_urls)} failed pages")
        else:
            logger.info(
                f"[Sandbox{self.state.index}] (id:{sid}) Warmup completed: " f"{len(self.state.tab_ids)} tabs opened"
            )

    def _execute_tab_operations(self, tab_num: int) -> None:
        """Run snapshot -> click -> screenshot on the current tab.

        Args:
            tab_num: 1-based tab number, for logging only.
        """
        # Step 1: DOM snapshot.
        result = self.provider.exec(self.state, "agent-browser snapshot -i", timeout=60)
        if result.exit_code != 0:
            logger.warning(f"[Sandbox{self.state.index}] Tab {tab_num}: snapshot failed")
            return

        elements = extract_element_refs(result.stdout)

        # Step 2: click the first valid element (non-fatal).
        if elements:
            click_result = self.provider.exec(self.state, f"agent-browser click {elements[0]}", timeout=30)
            if click_result.exit_code != 0:
                logger.warning(f"[Sandbox{self.state.index}] Tab {tab_num}: click failed on {elements[0]}")

        # Step 3: screenshot (non-fatal).
        screenshot_result = self.provider.exec(self.state, "agent-browser screenshot", timeout=30)
        if screenshot_result.exit_code != 0:
            logger.warning(f"[Sandbox{self.state.index}] Tab {tab_num}: screenshot failed")


class BrowserTaskRunner(threading.Thread):
    """Browser task runner (one independent thread per sandbox, fixed mode)."""

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        provider: EnvironmentProvider,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.provider = provider
        self.consecutive_errors = 0

    def run(self) -> None:
        """Task execution main loop."""
        if not self.state.ready:
            logger.warning(
                f"[Sandbox{self.state.index}] Cannot start tasks: " f"{self.state.creation_metrics.status.value}"
            )
            return

        while not self.stop_event.is_set():
            if not self.state.is_alive:
                logger.info(f"[Sandbox{self.state.index}] Sandbox offline, stopping tasks")
                break

            success, latency = self._run_single_task()

            timeout = latency > self.config.browser_timeout
            self.state.browser_metrics.add(latency, success and not timeout, timeout)
            self.state.update_last_task_time(time.time())

            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    logger.warning(f"[Sandbox{self.state.index}] Marked offline (3 consecutive failures)")
                    break

            # Random interval to avoid request spikes against the target server.
            sleep_time = random.uniform(self.config.browser_interval_min, self.config.browser_interval_max)
            time.sleep(sleep_time)

        logger.info(f"[Sandbox{self.state.index}] Task runner ended")

    def _run_single_task(self) -> tuple[bool, float]:
        """Execute a single browser task.

        Returns:
            (success, latency_seconds)
        """
        if not self.state.is_alive:
            return False, 0.0

        sid = self.state.id
        # Round-robin URL selection across the configured set.
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]

        cmd = f"openclaw browser --browser-profile openclaw open '{url}'"

        start_time = time.perf_counter()
        try:
            result = self.provider.exec(self.state, cmd, timeout=self.config.browser_timeout + 30)
            elapsed = time.perf_counter() - start_time + 10  # simulate llm response time

            success = result.exit_code == 0
            if not success:
                error_detail = f"exit_code={result.exit_code}"
                if result.stderr:
                    error_detail += f", stderr={result.stderr[:200]}"
                if result.stdout:
                    error_detail += f", stdout={result.stdout[:200]}"
                logger.error(f"[Sandbox{self.state.index}] (id:{sid}) Task failed: {error_detail}")
                self.state.browser_metrics.last_error = error_detail
            return success, elapsed
        except Exception as e:
            elapsed = time.perf_counter() - start_time + 10  # simulate llm response time
            error_msg = str(e)
            logger.error(f"[Sandbox{self.state.index}] (id:{sid}) Task exception: {error_msg}")
            self.state.browser_metrics.last_error = error_msg
            return False, elapsed


class TabOperationRunner(threading.Thread):
    """One round-robin round: open a NEW tab, then snapshot -> click -> screenshot.

    Each round opens a new tab (round-robin URL from ``browser_urls``), which
    allocates fresh memory and triggers swap-out events -- the load this
    benchmark measures.
    """

    # Per-step command timeout (seconds). Referenced by both the exec call sites
    # and _classify_exception so the timeout reported in error messages matches
    # the actual budget.
    OPEN_TAB_TIMEOUT = 60  # `agent-browser tab new`
    SNAPSHOT_TIMEOUT = 60  # `agent-browser snapshot -i`

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        round_id: int,
        provider: EnvironmentProvider,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.round_id = round_id
        self.provider = provider
        self.consecutive_errors = 0

    def run(self) -> None:
        """Execute tab operations for this round."""
        if not self.state.ready or not self.state.is_alive:
            logger.info(f"[Sandbox{self.state.index}] Not ready/alive for tab operations")
            return

        if not self.config.browser_urls:
            logger.info(f"[Sandbox{self.state.index}] No browser_urls configured")
            return

        url_index = self.round_id % len(self.config.browser_urls)
        url = self.config.browser_urls[url_index]

        start_time = time.perf_counter()
        success, step_times, failed_step, error_detail = self._execute_steps(url)
        elapsed = self._record_metrics(start_time, success, step_times, error_detail)

        if success:
            step_breakdown = ", ".join(f"{k}={v:.2f}s" for k, v in step_times.items() if v > 0)
            logger.info(f"[Sandbox{self.state.index}] New tab completed in {elapsed:.2f}s ({step_breakdown})")
        else:
            self._handle_failure(url, failed_step, error_detail)

    def _execute_steps(self, url: str) -> tuple[bool, dict[str, float], str | None, str]:
        """Run all steps: open new tab -> snapshot -> click -> screenshot.

        Returns:
            (success, step_times, failed_step, error_detail)
        """
        success = True
        step_times: dict[str, float] = {}
        failed_step: str | None = None
        error_detail = ""
        elements: list[str] = []

        try:
            success, error_detail = self._step_open_tab(url, step_times)
            if not success:
                failed_step = "open_tab"
                return success, step_times, failed_step, error_detail

            success, elements, error_detail = self._step_snapshot(step_times)
            if not success:
                failed_step = "snapshot"
                return success, step_times, failed_step, error_detail

            # Step 3 + 4 are non-fatal.
            _, click_error = self._step_click(elements, step_times)
            _, screenshot_error = self._step_screenshot(step_times)

            if click_error:
                logger.warning(f"[Sandbox{self.state.index}] Non-fatal: {click_error}")
            if screenshot_error:
                logger.warning(f"[Sandbox{self.state.index}] Non-fatal: {screenshot_error}")

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

    def _step_open_tab(self, url: str, step_times: dict[str, float]) -> tuple[bool, str]:
        """Step 1: open a new tab with the URL and wait for the page to load.

        Records two timings: ``open_tab`` (create) and ``page_load`` (networkidle).
        """
        # 1a: create the new tab.
        tab_start = time.perf_counter()
        result = self.provider.exec(self.state, f'agent-browser tab new "{url}"', timeout=self.OPEN_TAB_TIMEOUT)
        step_times["open_tab"] = time.perf_counter() - tab_start

        if result.exit_code != 0:
            error_parts = [f"open_tab failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:200]}")
            if result.stdout:
                error_parts.append(f"stdout={result.stdout[:200]}")
            error_parts.append(f"url={url[:80]}")
            return False, " | ".join(error_parts)

        # 1b: wait for network idle (page fully loaded).
        wait_start = time.perf_counter()
        wait_result = self.provider.exec(
            self.state, "agent-browser wait --load networkidle --timeout 60000", timeout=70
        )
        step_times["page_load"] = time.perf_counter() - wait_start

        if wait_result.exit_code != 0:
            error_parts = [f"page_load failed: exit_code={wait_result.exit_code}"]
            if wait_result.stderr:
                error_parts.append(f"stderr={wait_result.stderr[:200]}")
            error_parts.append(f"url={url[:80]}")
            return False, " | ".join(error_parts)

        return True, ""

    def _step_snapshot(self, step_times: dict[str, float]) -> tuple[bool, list[str], str]:
        """Step 2: DOM snapshot."""
        step_start = time.perf_counter()
        result = self.provider.exec(self.state, "agent-browser snapshot -i", timeout=self.SNAPSHOT_TIMEOUT)
        step_times["snapshot"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            error_parts = [f"snapshot failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:200]}")
            if result.stdout:
                error_parts.append(f"stdout={result.stdout[:200]}")
            return False, [], " | ".join(error_parts)

        elements = extract_element_refs(result.stdout)
        return True, elements, ""

    def _step_click(self, elements: list[str], step_times: dict[str, float]) -> tuple[bool, str]:
        """Step 3: element click (non-fatal)."""
        if not elements:
            return True, ""

        step_start = time.perf_counter()
        result = self.provider.exec(self.state, f"agent-browser click {elements[0]}", timeout=30)
        step_times["click"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            error_parts = [f"click failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            error_parts.append(f"element={elements[0]}")
            return True, " | ".join(error_parts)
        return True, ""

    def _step_screenshot(self, step_times: dict[str, float]) -> tuple[bool, str]:
        """Step 4: screenshot (non-fatal)."""
        step_start = time.perf_counter()
        result = self.provider.exec(self.state, "agent-browser screenshot", timeout=30)
        step_times["screenshot"] = time.perf_counter() - step_start

        if result.exit_code != 0:
            error_parts = [f"screenshot failed: exit_code={result.exit_code}"]
            if result.stderr:
                error_parts.append(f"stderr={result.stderr[:100]}")
            return True, " | ".join(error_parts)
        return True, ""

    def _classify_exception(self, e: Exception, step_times: dict[str, float]) -> tuple[str, str]:
        """Classify an exception to infer which step failed and why.

        Distinguishes:
        - unreachable: the provider could not route the command to the sandbox
          (OOM-killed, paused, reclaimed). The command never ran inside the
          sandbox, so this is an infrastructure failure, not a per-step timeout.
        - open_tab / snapshot: the corresponding step exceeded its command
          timeout (see OPEN_TAB_TIMEOUT / SNAPSHOT_TIMEOUT). The failing step is
          inferred from which step had not yet recorded a timing.
        - unknown: a timeout on a step without a dedicated classifier.
        - exception: any other non-timeout error.
        """
        error_str = str(e)
        if "Failed to route request to sandbox" in error_str:
            return "unreachable", f"sandbox unreachable: {error_str[:100]}"
        if "context deadline exceeded" in error_str or "timed out" in error_str:
            if "open_tab" not in step_times:
                return "open_tab", f"open_tab timed out after {self.OPEN_TAB_TIMEOUT}s"
            if "snapshot" not in step_times:
                return "snapshot", f"snapshot timed out after {self.SNAPSHOT_TIMEOUT}s"
            return "unknown", f"operation timed out: {error_str[:100]}"
        return "exception", f"exception: {error_str[:100]}"

    def _record_metrics(
        self, start_time: float, success: bool, step_times: dict[str, float], error_detail: str
    ) -> float:
        """Record metrics for this operation; return elapsed seconds."""
        elapsed = time.perf_counter() - start_time
        timeout = elapsed > self.config.browser_timeout
        self.state.browser_metrics.add(elapsed, success and not timeout, timeout, step_times=step_times)
        self.state.update_last_task_time(time.time())
        if not success and error_detail:
            self.state.browser_metrics.last_error = error_detail
        return elapsed

    def _handle_failure(self, url: str, failed_step: str | None, error_detail: str) -> None:
        """Handle failure after metrics are recorded."""
        logger.error(
            f"[Sandbox{self.state.index}] Round {self.round_id} URL '{url[:50]}' "
            f"failed at {failed_step}: {error_detail}"
        )
        self.consecutive_errors += 1
        if self.consecutive_errors >= 3:
            self.state.is_alive = False
