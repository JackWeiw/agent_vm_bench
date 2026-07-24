"""
LLM Scenario Runner Module

Executes LLM scenarios by triggering OpenClaw agent in sandbox via Gateway HTTP API.
Each sandbox runs scenarios in a loop with configurable intervals.
"""

import json
import random
import threading
import time
from typing import Tuple

from .config import Config
from .schemas import SandboxState, SandboxStatus


class OpenClawConfig:
    """Configure OpenClaw to use MockLLM provider"""

    @staticmethod
    def set_provider(state: SandboxState, endpoint: str, model: str, timeout: int = 30) -> bool:
        """
        Configure OpenClaw to use MockLLM as LLM provider.

        Uses 'openclaw config set' to update provider settings.
        Gateway hot-reloads after config change.

        Args:
            state: Sandbox state with sandbox object
            endpoint: MockLLM URL (e.g., "http://192.168.1.10:5199/v1")
            model: Scenario name (e.g., "browser-scenario-1")
            timeout: Command timeout in seconds

        Returns:
            True if successful, False otherwise
        """
        sbx = state.sandbox_obj
        if not sbx:
            print(f"[Sandbox{state.sandbox_id}] No sandbox object")
            return False

        try:
            # Write complete provider config in one command
            # OpenClaw validates config atomically - partial updates fail
            # Note: apiKey is required for custom providers, use fixed value
            config_json = json.dumps(
                {
                    "api": "openai-completions",
                    "baseUrl": endpoint,
                    "apiKey": "huawei-kunpeng-ai",
                    "models": [{"id": model, "name": model}],
                }
            )
            cmd = f"openclaw config set models.providers.llm-replay '{config_json}'"
            result = sbx.commands.run(cmd, timeout=timeout, user="root")
            if result.exit_code != 0:
                print(f"[Sandbox{state.sandbox_id}] Failed to set provider config: {result.stderr[:200]}")
                return False

            # Set default model for agent
            cmd = f"openclaw models set llm-replay/{model}"
            result = sbx.commands.run(cmd, timeout=timeout, user="root")
            if result.exit_code != 0:
                print(f"[Sandbox{state.sandbox_id}] Failed to set default model: {result.stderr[:100]}")
                return False

            print(f"[Sandbox{state.sandbox_id}] OpenClaw configured: {endpoint} -> {model}")
            return True

        except Exception as e:
            print(f"[Sandbox{state.sandbox_id}] Config error: {e}")
            return False

    @staticmethod
    def configure_all(states: dict, endpoint: str, model: str, timeout: int = 30) -> int:
        """
        Configure all PORT_READY sandboxes.

        Returns:
            Number of successfully configured sandboxes
        """
        ready = [s for s in states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY and s.sandbox_obj]

        success = 0
        for state in ready:
            if OpenClawConfig.set_provider(state, endpoint, model, timeout):
                success += 1

        print(f"\nConfigured {success}/{len(ready)} sandboxes")
        return success


class LLMScenarioRunner(threading.Thread):
    """LLM scenario runner (one independent thread per sandbox)"""

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        prompt: str,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.prompt = prompt
        self.stop_event = stop_event
        self.consecutive_errors = 0

    def run(self) -> None:
        """Scenario execution main loop"""
        # Wait for sandbox PORT_READY
        while not self.stop_event.is_set():
            if self.state.creation_metrics.status == SandboxStatus.PORT_READY:
                break
            if self.state.creation_metrics.status in (
                SandboxStatus.FAILED,
                SandboxStatus.PORT_FAILED,
                SandboxStatus.OFFLINE,
                SandboxStatus.KILLED,
            ):
                print(
                    f"[Sandbox{self.state.sandbox_id}] Cannot start LLM scenarios: "
                    f"{self.state.creation_metrics.status.value}"
                )
                return
            time.sleep(0.5)

        # Scenario execution loop
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping LLM scenarios")
                break

            # Execute single scenario
            success, latency = self._execute_scenario()

            # Update metrics
            timeout = latency > self.config.llm.timeout
            self.state.llm_metrics.add_result(latency, success and not timeout, timeout)
            self.state.last_task_time = time.time()

            # Error handling: mark offline after 3 consecutive failures
            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    print(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break

            # Random interval between scenarios
            sleep_time = random.uniform(
                self.config.llm.interval_min,
                self.config.llm.interval_max,
            )
            time.sleep(sleep_time)

        print(f"[Sandbox{self.state.sandbox_id}] LLM scenario runner ended")

    def _execute_scenario(self) -> Tuple[bool, float]:
        """
        Execute complete scenario via Gateway HTTP API.

        Sends initial prompt and waits for agent to complete all turns.
        Agent handles multi-turn conversation internally with MockLLM.

        Returns: (success, latency_seconds)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0

        e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, "sandbox_id") else "N/A"

        start_time = time.perf_counter()
        try:
            # Build payload JSON file to avoid shell escaping issues
            # Use streaming mode to keep connection alive during long agent execution
            payload = {
                "model": "openclaw",
                "messages": [{"role": "user", "content": self.prompt}],
                "stream": True,  # Enable streaming to prevent timeout during long execution
            }
            payload_json = json.dumps(payload, ensure_ascii=False)

            # Write payload to temp file in sandbox using files.write API
            # This avoids shell escaping issues
            # Use /home/user directory which has write permissions
            payload_path = "/home/user/llm_payload.json"
            try:
                sbx.files.write(payload_path, payload_json)
            except Exception as write_error:
                print(f"[Sandbox{self.state.sandbox_id}] Failed to write payload file: {write_error}")
                return False, 0.0

            # Execute Gateway HTTP request with payload file
            # Use streaming mode (stream: true) to keep connection alive during long execution
            # This prevents timeout issues when agent takes a long time
            cmd = (
                f"curl -s -X POST http://127.0.0.1:18789/v1/chat/completions "
                f"-H 'Authorization: Bearer test-token-123' "
                f"-H 'Content-Type: application/json' "
                f"-d @{payload_path} "
                f"--max-time {self.config.llm.timeout} "
                f"-o /dev/null -w '%{{time_total}}'"
            )
            result = sbx.commands.run(cmd, timeout=self.config.llm.timeout + 30, user="root")
            elapsed = time.perf_counter() - start_time

            success = result.exit_code == 0

            if not success:
                error_detail = f"exit_code={result.exit_code}"
                if result.stderr:
                    error_detail += f", stderr={result.stderr[:200]}"
                if result.stdout:
                    error_detail += f", stdout={result.stdout[:200]}"
                print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Scenario failed: {error_detail}")
                self.state.llm_metrics.last_error = error_detail
            else:
                # Log completion time for successful scenarios
                print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Scenario completed in {elapsed:.1f}s")

            return success, elapsed

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            error_msg = str(e)
            print(f"[Sandbox{self.state.sandbox_id}] (E2B:{e2b_sandbox_id}) Scenario exception: {error_msg}")
            self.state.llm_metrics.last_error = error_msg
            return False, elapsed


class LLMTaskManager:
    """LLM task manager - manages all sandbox scenario execution threads"""

    def __init__(
        self,
        config: Config,
        sandbox_states: dict,
        prompt: str,
        stop_event: threading.Event,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.prompt = prompt
        self.stop_event = stop_event
        self.runners: list = []

    def start_all(self) -> None:
        """Start scenario execution threads for PORT_READY sandboxes"""
        # Filter PORT_READY sandboxes that have completed warmup (or no warmup needed)
        ready_states = [
            s
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY and s.warmup_done
        ]

        if not ready_states:
            print("No sandboxes ready for LLM scenario execution")
            return

        # Select subset based on benchmark_percent
        total_ready = len(ready_states)
        benchmark_count = max(1, int(total_ready * self.config.benchmark_percent))

        if benchmark_count < total_ready:
            benchmark_states = random.sample(ready_states, benchmark_count)
            print(
                f"\nBenchmark subset: {benchmark_count}/{total_ready} sandboxes "
                f"({self.config.benchmark_percent * 100:.0f}%)"
            )
        else:
            benchmark_states = ready_states

        # Start scenario runners
        print(f"\n{'=' * 60}")
        print("LLM Scenario Execution Start")
        print(f"  Total: {len(benchmark_states)} sandboxes")
        print(f"  Scenario: {self.config.llm.model}")
        print(f"{'=' * 60}")

        for state in benchmark_states:
            runner = LLMScenarioRunner(state, self.config, self.prompt, self.stop_event)
            self.runners.append(runner)
            runner.start()

        print(f"\nStarted {len(self.runners)} LLM scenario runners")

    def wait_all(self, timeout: float = 5.0) -> None:
        """Wait for all scenario threads to end"""
        for runner in self.runners:
            runner.join(timeout=timeout)


def check_mockllm_health(endpoint: str, timeout: int = 5) -> bool:
    """
    Check if MockLLM service is healthy.

    Args:
        endpoint: MockLLM service base URL
        timeout: Request timeout in seconds

    Returns:
        True if service is healthy, False otherwise
    """
    import requests

    try:
        url = f"{endpoint.rstrip('/')}/health"
        resp = requests.get(url, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False
