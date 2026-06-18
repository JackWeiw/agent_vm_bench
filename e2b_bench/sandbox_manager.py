"""
Sandbox Management Module

Responsible for E2B sandbox creation, health check, batch control and termination
Preserves sandbox handle for subsequent task execution
Supports port check (18789 openclaw-gateway + 11436 llama-server)
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple, Optional
from threading import Event

try:
    from e2b import Sandbox
except ImportError:
    # Mock for development/testing without E2B SDK
    class Sandbox:
        @staticmethod
        def create(template, timeout=86400):
            class MockSandbox:
                sandbox_id = "mock_sandbox_id"
                class MockCommands:
                    def run(self, cmd, timeout=60, user="root"):
                        class Result:
                            exit_code = 0
                            stdout = ""
                        return Result()
                commands = MockCommands()

                def kill(self):
                    pass
            return MockSandbox()

        @staticmethod
        def kill(sandbox_id):
            pass

        @staticmethod
        def list():
            """Mock list() for testing - returns iterable"""
            class MockListedSandbox:
                sandbox_id = "mock_sandbox_1"
            return [MockListedSandbox()]  # Return list, not paginator

        @staticmethod
        def connect(sandbox_id):
            """Mock connect() for testing"""
            class MockSandbox:
                sandbox_id = sandbox_id
                class MockCommands:
                    def run(self, cmd, timeout=60, user="root"):
                        class Result:
                            exit_code = 0
                            stdout = ""
                        return Result()
                commands = MockCommands()
            return MockSandbox()

from .config import Config
from .schemas import SandboxState, SandboxStatus

# Required ports to check
REQUIRED_PORTS = [
    (18789, "openclaw-gateway"),
    (11436, "llama-server"),
]

# Port check maximum wait time (seconds)
PORT_CHECK_MAX_WAIT = 300

# Port check interval (seconds)
PORT_CHECK_INTERVAL = 5


class SandboxManager:
    """Sandbox lifecycle management"""

    def __init__(self, config: Config, stop_event: Event):
        self.config = config
        self.stop_event = stop_event
        self.sandbox_states: Dict[int, SandboxState] = {}

    def create_all(self) -> Dict[int, SandboxState]:
        """Batch create sandboxes

        Strategy based on batch config:
        - With batch_size: batched creation to avoid resource spike
        - Without config: full concurrent creation for max performance test

        Returns: {sandbox_id: SandboxState}
        """
        if self.config.batch_size and self.config.batch_size > 0:
            return self._create_batched()
        else:
            return self._create_concurrent()

    def detect_existing(self) -> Dict[int, SandboxState]:
        """Detect existing running sandboxes

        Query E2B API for existing sandboxes, connect to them,
        check port readiness, and prepare for benchmark.

        Sandbox.list() returns a SandboxPaginator, need to iterate it.

        Returns: {sandbox_id: SandboxState}
        """
        print(f"\n{'='*60}")
        print("Detecting Existing Sandboxes")
        print(f"{'='*60}")

        # List all running sandboxes - returns SandboxPaginator
        try:
            paginator = Sandbox.list()
            # Paginator needs to be iterated to get items
            existing_list = list(paginator)
            print(f"  Found {len(existing_list)} running sandboxes")
        except Exception as e:
            print(f"  Failed to list sandboxes: {e}")
            return {}

        if not existing_list:
            print("  No existing sandboxes found")
            return {}

        print(f"  Processing all sandboxes...")

        # Connect to each sandbox and check ports
        for i, listed_sandbox in enumerate(existing_list):
            sandbox_id = i + 1
            e2b_sandbox_id = listed_sandbox.sandbox_id if hasattr(listed_sandbox, 'sandbox_id') else str(listed_sandbox)

            state = SandboxState(sandbox_id=sandbox_id)
            self.sandbox_states[sandbox_id] = state

            print(f"\n[Sandbox{sandbox_id}] Connecting to E2B:{e2b_sandbox_id}...")

            try:
                # Connect to existing sandbox
                sbx = Sandbox.connect(e2b_sandbox_id)
                state.sandbox_obj = sbx
                state.creation_metrics.status = SandboxStatus.CREATED
                print(f"[Sandbox{sandbox_id}] Connected successfully")

                # Check port readiness
                port_result = self._check_ports(state)
                if port_result['success']:
                    state.creation_metrics.status = SandboxStatus.PORT_READY
                    state.creation_metrics.port_wait_elapsed = port_result['wait_elapsed']
                    print(f"[Sandbox{sandbox_id}] Ports ready in {port_result['wait_elapsed']:.1f}s")
                else:
                    state.creation_metrics.status = SandboxStatus.PORT_FAILED
                    state.creation_metrics.port_check_error = port_result['error']
                    print(f"[Sandbox{sandbox_id}] Port check failed: {port_result['error'][:50]}")

            except Exception as e:
                state.creation_metrics.status = SandboxStatus.FAILED
                state.creation_metrics.error_msg = str(e)
                print(f"[Sandbox{sandbox_id}] Connect failed: {str(e)[:80]}")

        return self.sandbox_states

    def _create_batched(self) -> Dict[int, SandboxState]:
        """Batched sandbox creation"""
        total = self.config.total_count
        batch_size = self.config.batch_size
        batch_count = self.config.batch_count

        print(f"\n{'='*60}")
        print(f"Batched Sandbox Creation")
        print(f"  Total: {total} sandboxes")
        print(f"  Batches: {batch_count} x {batch_size}")
        print(f"  Interval: {self.config.batch_interval}s")
        print(f"{'='*60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                print("Stop event detected, aborting creation")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)

            print(f"\n[Batch {batch_id}/{batch_count-1}] Creating sandboxes {start_idx+1}-{end_idx}")

            # Concurrent creation of current batch
            batch_states = self._create_batch_concurrent(batch_id, start_idx, end_idx)
            self.sandbox_states.update(batch_states)

            # Wait between batches (last batch no wait)
            if batch_id < batch_count - 1 and self.config.batch_interval:
                print(f"Waiting {self.config.batch_interval}s before next batch...")
                time.sleep(self.config.batch_interval)

        return self.sandbox_states

    def _create_batch_concurrent(self, batch_id: int, start: int, end: int) -> Dict[int, SandboxState]:
        """Concurrent creation of one batch"""
        states: Dict[int, SandboxState] = {}

        with ThreadPoolExecutor(max_workers=end - start) as executor:
            futures = {}

            for i in range(start, end):
                sandbox_id = i + 1
                state = SandboxState(sandbox_id=sandbox_id, batch_id=batch_id)
                self.sandbox_states[sandbox_id] = state
                future = executor.submit(self._create_single, state)
                futures[future] = sandbox_id

            for future in as_completed(futures):
                sandbox_id = futures[future]
                state = self.sandbox_states[sandbox_id]

                try:
                    result = future.result()
                    if result['success']:
                        # sandbox.create succeeded, start port check
                        print(f"[Sandbox{sandbox_id}] Created in {result['create_elapsed']:.1f}s, checking ports...")

                        # Port check
                        port_result = self._check_ports(state)
                        if port_result['success']:
                            state.creation_metrics.status = SandboxStatus.PORT_READY
                            state.creation_metrics.port_wait_elapsed = port_result['wait_elapsed']
                            state.creation_metrics.total_elapsed = result['create_elapsed'] + port_result['wait_elapsed']
                            print(f"[Sandbox{sandbox_id}] Ports ready in {port_result['wait_elapsed']:.1f}s, total {state.creation_metrics.total_elapsed:.1f}s")
                        else:
                            state.creation_metrics.status = SandboxStatus.PORT_FAILED
                            state.creation_metrics.port_check_error = port_result['error']
                            print(f"[Sandbox{sandbox_id}] Port check failed: {port_result['error'][:50]}")
                    else:
                        state.creation_metrics.status = SandboxStatus.FAILED
                        state.creation_metrics.error_msg = result['error']
                        print(f"[Sandbox{sandbox_id}] Failed: {result['error'][:80]}")
                except Exception as e:
                    state.creation_metrics.status = SandboxStatus.FAILED
                    state.creation_metrics.error_msg = str(e)
                    print(f"[Sandbox{sandbox_id}] Exception: {str(e)[:80]}")

        return {i + 1: self.sandbox_states[i + 1] for i in range(start, end)}

    def _create_concurrent(self) -> Dict[int, SandboxState]:
        """Full concurrent creation of all sandboxes"""
        total = self.config.total_count

        print(f"\n{'='*60}")
        print(f"Concurrent Sandbox Creation")
        print(f"  Total: {total} sandboxes (full concurrent)")
        print(f"{'='*60}")

        return self._create_batch_concurrent(batch_id=0, start=0, end=total)

    def _create_single(self, state: SandboxState) -> Dict[str, any]:
        """Create single sandbox

        Key: Preserve sandbox handle in state.sandbox_obj
        Record time when sandbox.create succeeds, no port waiting

        Returns: {'success': bool, 'create_elapsed': float, 'error': str}
        """
        state.creation_metrics.status = SandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        try:
            sbx = Sandbox.create(
                self.config.template,
                timeout=self.config.create_timeout
            )
            # Preserve sandbox handle
            state.sandbox_obj = sbx
            state.creation_metrics.create_ready_time = time.time()
            state.creation_metrics.create_elapsed = state.creation_metrics.create_ready_time - state.creation_metrics.submit_time
            state.creation_metrics.status = SandboxStatus.CREATED

            return {
                'success': True,
                'create_elapsed': state.creation_metrics.create_elapsed,
                'error': ''
            }
        except Exception as e:
            state.creation_metrics.create_ready_time = time.time()
            return {
                'success': False,
                'create_elapsed': 0.0,
                'error': str(e)
            }

    def _check_ports(self, state: SandboxState) -> Dict[str, any]:
        """Check if sandbox ports are ready

        Check 18789 (openclaw-gateway) and 11436 (llama-server)

        Returns: {'success': bool, 'wait_elapsed': float, 'error': str}
        """
        sbx = state.sandbox_obj
        if not sbx:
            return {'success': False, 'wait_elapsed': 0.0, 'error': 'No sandbox handle'}

        start_time = time.time()
        ready_ports = set()

        while time.time() - start_time < PORT_CHECK_MAX_WAIT:
            if self.stop_event.is_set():
                return {'success': False, 'wait_elapsed': time.time() - start_time, 'error': 'Stop event'}

            for port, name in REQUIRED_PORTS:
                if port in ready_ports:
                    continue

                try:
                    cmd = f"ss -tlnp | grep ':{port}' || netstat -tlnp 2>/dev/null | grep ':{port}' || echo 'PORT_NOT_LISTENING'"
                    result = sbx.commands.run(cmd, timeout=10, user="root")

                    if result.exit_code == 0 and 'PORT_NOT_LISTENING' not in result.stdout:
                        ready_ports.add(port)
                        print(f"[Sandbox{state.sandbox_id}] Port {port} ({name}) is listening")
                except Exception as e:
                    pass  # Continue checking other ports

            if len(ready_ports) == len(REQUIRED_PORTS):
                wait_elapsed = time.time() - start_time
                state.creation_metrics.port_ready_time = time.time()
                return {
                    'success': True,
                    'wait_elapsed': wait_elapsed,
                    'error': ''
                }

            time.sleep(PORT_CHECK_INTERVAL)

        # Timeout, return missing ports info
        missing_ports = [f"{p}:{n}" for p, n in REQUIRED_PORTS if p not in ready_ports]
        wait_elapsed = time.time() - start_time
        return {
            'success': False,
            'wait_elapsed': wait_elapsed,
            'error': f"Timeout waiting for ports: {missing_ports}"
        }

    def check_alive(self, state: SandboxState) -> bool:
        """Check if sandbox is alive"""
        sbx = state.sandbox_obj
        if not sbx or not state.is_alive:
            return False
        try:
            result = sbx.commands.run("echo alive", timeout=10, user="root")
            return result.exit_code == 0
        except Exception:
            return False

    def kill_all(self) -> None:
        """Kill all sandboxes"""
        print("\nKilling all sandboxes...")
        killed_count = 0
        for state in self.sandbox_states.values():
            if state.sandbox_obj:
                try:
                    state.sandbox_obj.kill()
                    # Don't overwrite status - keep original (PORT_READY/FAILED etc) for stats
                    # state.creation_metrics.status = SandboxStatus.KILLED
                    state.is_alive = False
                    killed_count += 1
                except Exception as e:
                    print(f"[Sandbox{state.sandbox_id}] Kill error: {str(e)[:50]}")
        print(f"Killed {killed_count} sandboxes")