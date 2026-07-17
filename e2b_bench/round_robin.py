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
        2. Calculate number of rounds (auto or from config)
        3. For each round: start tasks -> wait interval -> stop tasks
        4. Loop back to first group if rounds exceed groups (cycling)
        5. Track statistics per round
        6. Stop when duration is reached or all rounds completed
        """
        import math
        import time

        # 1. Prepare sandbox groups
        self._prepare_sandbox_groups()

        if not self.sandbox_groups:
            print("[RoundRobin] No sandbox groups to execute")
            return

        # 2. Calculate number of rounds (auto or from config)
        rounds = self._calculate_rounds()
        num_groups = len(self.sandbox_groups)

        # Check if cycling will occur
        will_cycle = rounds > num_groups
        if will_cycle:
            print(f"\n[RoundRobin] Total rounds: {rounds} (cycling enabled)")
            print(
                f"[RoundRobin] Sandbox groups: {num_groups}, cycles: {rounds // num_groups}x + {rounds % num_groups} rounds"
            )
        else:
            print(f"\n[RoundRobin] Total rounds: {rounds}")
        print(f"[RoundRobin] Sandboxes per round: {len(self.sandbox_groups[0])} (balanced)")

        # 3. Execute each round (with cycling) until duration is reached
        start_time = time.time()
        for round_id in range(rounds):
            # Check stop conditions
            if self.stop_event.is_set():
                print(f"[RoundRobin] Stop event detected, ending at round {round_id}")
                break

            # Check if duration is reached
            elapsed = time.time() - start_time
            if elapsed >= self.config.test_duration:
                print(
                    f"[RoundRobin] Duration reached ({elapsed:.1f}s >= {self.config.test_duration}s), ending at round {round_id}"
                )
                break

            # Cycle back to first group if needed
            self._start_round(round_id)
            time.sleep(self.config.round_interval)
            self._stop_round()

        elapsed = time.time() - start_time
        print(f"\n[RoundRobin] Completed {min(self.current_round + 1, rounds)} rounds in {elapsed:.1f}s")

    def _prepare_sandbox_groups(self) -> None:
        """Prepare sandbox groups for round-robin execution.

        Group count determination (priority order):
        1. If round_count is specified: use it
        2. If round_size is specified: group_count = ceil(total / round_size)
        3. Otherwise: use min(total, 10) as default

        Distributes sandboxes evenly across groups:
        - Base distribution: total // group_count
        - Remainder distributed to first N groups

        Example: 103 sandboxes ÷ 5 groups = [21, 21, 21, 20, 20]
        """
        import math

        # Get all ready sandboxes
        self.all_ready_states = [
            s for s in self.sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]

        total = len(self.all_ready_states)
        if total == 0:
            print("[RoundRobin] No ready sandboxes available")
            return

        # Determine number of groups (priority: round_count > round_size > default)
        if self.config.round_count and self.config.round_count > 0:
            group_count = self.config.round_count
            print(f"[RoundRobin] Using round_count={group_count} groups")
        elif self.config.round_size and self.config.round_size > 0:
            group_count = math.ceil(total / self.config.round_size)
            print(f"[RoundRobin] Using round_size={self.config.round_size}, calculated {group_count} groups")
        else:
            # Default: use min(total, 10) groups
            group_count = min(total, 10)
            print(f"[RoundRobin] Auto-configured {group_count} sandbox groups (default)")

        # Calculate base distribution and remainder
        base_per_round = total // group_count
        remainder = total % group_count

        print(f"[RoundRobin] Preparing groups: {total} sandboxes ÷ {group_count} groups")
        print(f"[RoundRobin] Base per round: {base_per_round}, remainder: {remainder}")

        # Split into groups
        self.sandbox_groups = []
        start_idx = 0

        for i in range(group_count):
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
            round_id: Round index (0-based, can exceed num_groups for cycling)
        """
        # Cycle back to first group if round_id exceeds number of groups
        num_groups = len(self.sandbox_groups)
        group_idx = round_id % num_groups

        # Get current round's sandbox group (with cycling)
        current_states = self.sandbox_groups[group_idx]

        # Show cycle info if this is a repeated group
        if round_id >= num_groups:
            print(
                f"\n[Round {round_id}] (cycle {round_id // num_groups}, group {group_idx}) Starting {len(current_states)} sandboxes"
            )
        else:
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

        If round_count is specified, use it.
        Otherwise, auto-calculate based on duration and interval.

        Returns:
            Number of rounds to execute
        """
        import math

        # If round_count is specified, use it
        if self.config.round_count and self.config.round_count > 0:
            return self.config.round_count

        # Auto-calculate based on duration and interval
        if self.config.round_interval > 0:
            rounds = math.ceil(self.config.test_duration / self.config.round_interval)
            print(
                f"[RoundRobin] Auto-calculated {rounds} rounds from duration={self.config.test_duration}s, interval={self.config.round_interval}s"
            )
            return rounds

        # Fallback: number of groups (no cycling)
        return len(self.sandbox_groups)
