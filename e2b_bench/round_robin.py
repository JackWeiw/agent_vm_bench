"""
Round-Robin Task Manager Module

Manages round-robin sandbox rotation for memory migration stress testing.
Each round activates a different subset of sandboxes to ensure even memory access distribution.

Benchmark mode:
- Warmup phase: Opens multiple tabs per sandbox via WarmupRunner (memory allocation)
- Benchmark phase: Each round opens a NEW tab and executes operations
- Operations: new tab -> snapshot -> click -> screenshot
- Creates continuous memory allocation during benchmark
"""

import threading
import time
from typing import Dict, List, Optional

from .config import Config
from .schemas import SandboxState, SandboxStatus
from .stats_collector import StatsCollector
from .task_runner import TabOperationRunner


class RoundRobinTaskManager:
    """Round-robin task manager - rotates sandbox execution across rounds.

    Each round activates a different subset of sandboxes, ensuring:
    1. Even memory access distribution across all sandboxes
    2. No overlap between rounds (each sandbox appears in exactly one round)
    3. Equal load per round (balanced distribution)

    Benchmark mode:
    - Each round opens a NEW tab with URL from browser_urls (round-robin)
    - Executes operations: snapshot -> click -> screenshot
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
        self._planned_rounds: int = 0  # Total rounds planned to run
        self.active_runners: List[TabOperationRunner] = []
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

        # 1. Prepare sandbox groups
        self._prepare_sandbox_groups()

        if not self.sandbox_groups:
            print("[RoundRobin] No sandbox groups to execute")
            return

        # 2. Calculate number of rounds (auto or from config)
        rounds = self._calculate_rounds()
        self._planned_rounds = rounds  # Store for _stop_round to check
        num_groups = len(self.sandbox_groups)

        # Print cycling info
        if self.config.round_count and self.config.round_count > 0:
            print(
                f"\n[RoundRobin] Will run up to {rounds} rounds (whichever ends first: round_count={rounds} or duration={self.config.test_duration}s)"
            )
        else:
            print(f"\n[RoundRobin] Will cycle continuously until duration={self.config.test_duration}s")
        print(f"[RoundRobin] Sandbox groups: {num_groups}, {len(self.sandbox_groups[0])} sandboxes per round")

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

            # Wait for all runners to complete
            # Each runner executes one task (tab operations) and finishes naturally
            for runner in self.active_runners:
                runner.join(timeout=120)

            # Now stop the round (records metrics and baseline)
            self._stop_round()

            # Wait for round_interval before starting next round
            # This is the gap between rounds (after tasks complete)
            if elapsed + self.config.round_interval < self.config.test_duration:
                time.sleep(self.config.round_interval)

        elapsed = time.time() - start_time
        print(f"\n[RoundRobin] Completed {min(self.current_round + 1, rounds)} rounds in {elapsed:.1f}s")

    def _prepare_sandbox_groups(self) -> None:
        """Prepare sandbox groups for round-robin execution.

        Group count determination:
        1. If round_size > 0: group_count = ceil(total / round_size)
        2. Otherwise: use min(total, 10) as default

        Note: round_count does NOT affect group count — it only controls
        the max number of rounds to execute (termination condition).

        Distributes sandboxes evenly across groups:
        - Base distribution: total // group_count
        - Remainder distributed to first N groups

        Example: 103 sandboxes ÷ round_size=5 = 21 groups, [5,5,...,5,3]
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

        # Determine number of groups based on round_size (determines group granularity)
        if self.config.round_size and self.config.round_size > 0:
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

        Each runner will open a NEW tab and execute operations.

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

        # Mark current round for statistics tracking (for snapshot grouping)
        # Note: Baseline is recorded in _stop_round() after tasks complete
        self.stats_collector.current_round = round_id
        if round_id not in self.stats_collector.round_snapshots:
            self.stats_collector.round_snapshots[round_id] = []

        # Initialize Round 0 baseline if this is the first round
        if round_id == 0 and 0 not in self.stats_collector._round_start_totals:
            sandbox_latency_counts = {
                s.sandbox_id: len(s.browser_metrics.latencies) for s in self.sandbox_states.values()
            }
            self.stats_collector._round_start_totals[0] = {
                "total": 0,
                "success": 0,
                "sandbox_latency_counts": sandbox_latency_counts,
            }

        # Create round-specific stop event
        self.round_stop_event = threading.Event()

        # Start runners for current round
        self.active_runners = []
        for state in current_states:
            runner = TabOperationRunner(state, self.config, self.round_stop_event, round_id)
            self.active_runners.append(runner)
            runner.start()

        self.current_round = round_id

    def _stop_round(self) -> None:
        """Stop the current round and print summary.

        Note: This method assumes runners have already completed (joined in main loop).
        It only records metrics and prints the round summary.
        """
        if not self.round_stop_event:
            return

        # Signal stop event (in case any runner is still waiting)
        self.round_stop_event.set()

        # Force a final snapshot to capture this round's final metrics
        self.stats_collector._take_snapshot()

        # Record baseline for NEXT round AFTER current round's tasks are complete.
        # This includes the last round — we record a "post-last" baseline so that
        # _calculate_round_finals can use it as the end boundary for the final round,
        # avoiding dependency on final_browser_total which may be stale.
        next_round = self.current_round + 1
        if next_round not in self.stats_collector._round_start_totals:
            browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
            browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
            sandbox_latency_counts = {
                s.sandbox_id: len(s.browser_metrics.latencies) for s in self.sandbox_states.values()
            }
            self.stats_collector._round_start_totals[next_round] = {
                "total": browser_total,
                "success": browser_success,
                "sandbox_latency_counts": sandbox_latency_counts,
            }

        # Aggregate step timing from active runners' sandbox states (not all sandboxes)
        step_totals = {}
        for runner in self.active_runners:
            state = runner.state
            metrics = state.browser_metrics
            step_stats = metrics.get_step_stats()
            for step_name, stats in step_stats.items():
                if step_name not in step_totals:
                    step_totals[step_name] = {"total": 0.0, "count": 0, "min_count": float("inf")}
                step_totals[step_name]["total"] += stats["avg"] * stats["count"]
                step_totals[step_name]["count"] += stats["count"]
                # Track min count to detect steps with different sample counts
                step_totals[step_name]["min_count"] = min(step_totals[step_name]["min_count"], stats["count"])

        # Print round summary with step timing
        runner_count = len(self.active_runners)
        if runner_count > 0 and step_totals:
            avg_parts = []
            for step_name in ["open_tab", "page_load", "snapshot", "click", "screenshot"]:
                if step_name in step_totals:
                    avg_ms = (step_totals[step_name]["total"] / max(1, step_totals[step_name]["count"])) * 1000
                    avg_parts.append(f"{step_name}={avg_ms:.0f}ms")

            avg_str = ", ".join(avg_parts) if avg_parts else "no timing data"
            print(f"[Round {self.current_round}] Completed: {runner_count} sandboxes, avg: {avg_str}")
        else:
            print(f"[Round {self.current_round}] Completed: {runner_count} sandboxes")

        # Clear round state
        self.active_runners.clear()
        self.round_stop_event = None

        # Clear round marker in stats collector (but keep baseline)
        self.stats_collector.current_round = None

    def _calculate_rounds(self) -> int:
        """Calculate max number of rounds to execute.

        round_count controls termination: if specified, the test stops after
        that many rounds OR when duration is reached (whichever comes first).
        If round_count is not specified, the test runs until duration is reached.

        Note: round_size determines group count (via _prepare_sandbox_groups),
        round_count determines the max number of round iterations. They coexist.

        Returns:
            Max rounds to execute (or a large number if relying on duration only)
        """
        # If round_count is specified, use it as the max round limit
        if self.config.round_count and self.config.round_count > 0:
            return self.config.round_count

        # No round_count specified — rely on duration check in run() to stop
        # This allows continuous cycling until test_duration is reached
        return 10000  # Large enough to cycle until duration ends
