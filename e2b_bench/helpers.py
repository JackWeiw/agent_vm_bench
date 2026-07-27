"""
Shared utility helpers for e2b_bench.

Provides common patterns used across runner classes to eliminate duplication:
- wait_for_port_ready: sandbox port readiness polling (previously duplicated 4x)
"""

import time
import threading
from typing import Optional

from .schemas import SandboxState, SandboxStatus


def wait_for_port_ready(
    state: SandboxState,
    stop_event: Optional[threading.Event] = None,
    check_interval: float = 0.5,
) -> bool:
    """Wait for a sandbox to reach PORT_READY status.

    Polls the sandbox's creation_metrics.status until PORT_READY or a terminal
    failure state is reached. Used by all runner classes (WarmupRunner,
    BrowserTaskRunner, CodingWarmupRunner, CodingTaskRunner) to avoid
    duplicated polling loops.

    Args:
        state: SandboxState to poll
        stop_event: Optional event to signal early termination
        check_interval: Seconds between status checks (default: 0.5s)

    Returns:
        True if sandbox reached PORT_READY, False if failed/killed/stopped
    """
    terminal_states = (
        SandboxStatus.FAILED,
        SandboxStatus.PORT_FAILED,
        SandboxStatus.OFFLINE,
        SandboxStatus.KILLED,
    )

    while True:
        # Check stop event first (for benchmark termination)
        if stop_event and stop_event.is_set():
            return False

        status = state.creation_metrics.status
        if status == SandboxStatus.PORT_READY:
            return True
        if status in terminal_states:
            print(f"[Sandbox{state.sandbox_id}] Terminal status: {status.value}")
            return False

        time.sleep(check_interval)
