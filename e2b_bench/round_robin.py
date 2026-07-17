"""
Round-Robin Task Manager Module

Manages round-robin sandbox rotation for memory migration stress testing.
Each round activates a different subset of sandboxes to ensure even memory access distribution.
"""

import threading
import time
from typing import Dict, List, Optional

from .config import Config
from .schemas import SandboxState, SandboxStatus
from .stats_collector import StatsCollector
from .task_runner import BrowserTaskRunner


class RoundRobinTaskManager:
    """Round-robin task manager - rotates sandbox execution across rounds.

    Each round activates a different subset of sandboxes, ensuring:
    1. Even memory access distribution across all sandboxes
    2. No overlap between rounds (each sandbox appears in exactly one round)
    3. Equal load per round (balanced distribution)
    """

    def __init__(
        self,
        config: Config,
        sandbox_states: Dict[int, SandboxState],
        stop_event: threading.Event,
        stats_collector: StatsCollector,
    ):
        """Initialize the round-robin manager.

        Args:
            config: Test configuration
            sandbox_states: Dictionary of sandbox states
            stop_event: Global stop event for test termination
            stats_collector: Statistics collector for round tracking
        """
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.stats_collector = stats_collector

        # Sandbox groups for each round
        self.all_ready_states: List[SandboxState] = []
        self.sandbox_groups: List[List[SandboxState]] = []

        # Current round state
        self.current_round: int = 0
        self.active_runners: List[BrowserTaskRunner] = []
        self.round_stop_event: Optional[threading.Event] = None

    def run(self) -> None:
        """Execute the round-robin test.

        Main loop:
        1. Prepare sandbox groups (equal distribution)
        2. For each round: start tasks -> wait interval -> stop tasks
        3. Track statistics per round
        """
        # 1. Prepare sandbox groups
        self._prepare_sandbox_groups()

        if not self.sandbox_groups:
            print("[RoundRobin] No sandbox groups to execute")
            return

        # 2. Calculate number of rounds
        rounds = self._calculate_rounds()
        print(f"\n[RoundRobin] Total rounds: {rounds}")
        print(f"[RoundRobin] Sandboxes per round: {len(self.sandbox_groups[0])} (balanced)")

        # 3. Execute each round
        for round_id in range(rounds):
            if self.stop_event.is_set():
                print(f"[RoundRobin] Stop event detected, ending at round {round_id}")
                break

            self._start_round(round_id)
            time.sleep(self.config.round_interval)
            self._stop_round()

        print(f"\n[RoundRobin] Completed {min(self.current_round + 1, rounds)} rounds")

    def _prepare_sandbox_groups(self) -> None:
        """Prepare sandbox groups for round-robin execution.

        Distributes sandboxes evenly across rounds:
        - Base distribution: total // round_count
        - Remainder distributed to first N rounds

        Example: 103 sandboxes ÷ 5 rounds = [21, 21, 21, 20, 20]
        """
        # Get all ready sandboxes
        self.all_ready_states = [
            s
            for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]

        total = len(self.all_ready_states)
        if total == 0:
            print("[RoundRobin] No ready sandboxes available")
            return

        round_count = self.config.round_count
        if not round_count or round_count <= 0:
            print(f"[RoundRobin] Invalid round_count: {round_count}")
            return

        # Calculate base distribution and remainder
        base_per_round = total // round_count
        remainder = total % round_count

        print(f"[RoundRobin] Preparing groups: {total} sandboxes ÷ {round_count} rounds")
        print(f"[RoundRobin] Base per round: {base_per_round}, remainder: {remainder}")

        # Split into groups
        self.sandbox_groups = []
        start_idx = 0

        for i in range(round_count):
            # First N rounds get one extra sandbox (remainder distribution)
            per_round = base_per_round + (1 if i < remainder else 0)
            end_idx = start_idx + per_round
            group = self.all_ready_states[start_idx:end_idx]
            self.sandbox_groups.append(group)
            start_idx = end_idx

        # Log group sizes
        group_sizes = [len(g) for g in self.sandbox_groups]
        print(f"[RoundRobin] Group sizes: {group_sizes}")

    def _start_round(self, round_id: int) -> None:
        """Start a specific round.

        Args:
            round_id: Round index (0-based)
        """
        if round_id >= len(self.sandbox_groups):
            print(f"[RoundRobin] Invalid round_id: {round_id}")
            return

        # Get current round's sandbox group
        current_states = self.sandbox_groups[round_id]
        print(f"\n[Round {round_id}] Starting {len(current_states)} sandboxes")

        # Mark current round for statistics tracking
        self.stats_collector.set_round(round_id)

        # Create round-specific stop event
        self.round_stop_event = threading.Event()

        # Start task runners for current round
        self.active_runners = []
        for state in current_states:
            runner = BrowserTaskRunner(state, self.config, self.round_stop_event)
            self.active_runners.append(runner)
            runner.start()

        self.current_round = round_id

    def _stop_round(self) -> None:
        """Stop the current round."""
        if not self.round_stop_event:
            return

        # Signal all runners to stop
        self.round_stop_event.set()

        # Wait for runners to finish
        for runner in self.active_runners:
            runner.join(timeout=2)

        # Clear round state
        self.active_runners.clear()
        self.round_stop_event = None

        # Clear round marker in stats collector
        self.stats_collector.set_round(None)

        print(f"[Round {self.current_round}] Stopped")

    def _calculate_rounds(self) -> int:
        """Calculate total number of rounds.

        Returns:
            Number of rounds to execute
        """
        return len(self.sandbox_groups)