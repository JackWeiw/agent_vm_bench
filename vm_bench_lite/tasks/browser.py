#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Browser Task Module

Browser task execution with HTTP/CLI/Direct modes and warmup phase.
"""

import time
from typing import Tuple

from ..config import Config
from ..models import VMState, BROWSER_TASKS
from ..connection import VMConnection


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
        cmd1 = 'openclaw config set agents.defaults.memorySearch.chunking.tokens 200'
        success1, _, _, _, _ = vm.execute(cmd1, timeout=30, get_exit_code=True)

        cmd2 = 'openclaw memory index --force'
        success2, _, _, _, _ = vm.execute(cmd2, timeout=120, get_exit_code=True)

        # Mark warmup complete
        state.warmup_done = True
        warmup_success = success1 and success2 and len(failed_urls) == 0

        if not warmup_success:
            if failed_urls:
                state.record_browser_failure()
            print(f"[VM{vm_id}] Warmup failed: {len(failed_urls)} pages, config={success1}, memory={success2}")

        return warmup_success
