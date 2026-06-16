#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QA Task Module

QA task execution with HTTP/CLI modes.
"""

import time
from typing import Tuple

from ..config import Config
from ..models import QAMetrics, VMState, QA_MEMORY_TEXT, QA_QUESTIONS
from ..connection import VMConnection


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
        escaped = content.replace('\\', '\\\\').replace('"', '\\"')

        cmd = (
            f"curl -s -o {resp_file} -w '%{{time_total}}' "
            f"-X POST http://127.0.0.1:18789/v1/chat/completions "
            f"-H 'Authorization: Bearer test-token-123' "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"model\":\"openclaw/default\",\"messages\":[{{\"role\":\"user\",\"content\":\"{escaped}\"}}]}}'"
        )

        success, stdout, _, duration, _ = vm.execute(cmd, timeout=timeout + 10, get_exit_code=True)

        # curl's time_total is in the last line of stdout
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
