"""
Round-Robin Task Manager Module

Manages round-robin sandbox rotation for memory migration stress testing.
Each round activates a different subset of sandboxes to ensure even memory access distribution.

Tab-switch mode:
- Warmup phase opens multiple tabs per sandbox (via WarmupRunner in task_runner.py)
- Each round operates on a specific tab (round_id % len(tab_ids))
- TabSwitchRunner (from task_runner.py) executes per-tab operations
- Creates swap in events when accessing migrated memory
"""

import re
import threading
import time
from typing import Dict, List, Optional

from .config import Config
from .schemas import SandboxState, SandboxStatus
from .stats_collector import StatsCollector
from .task_runner import TabSwitchRunner


class RoundRobinTaskManager:
    """Round-robin task manager - rotates sandbox execution across rounds.

    Each round activates a different subset of sandboxes, ensuring:
    1. Even memory access distribution across all sandboxes
    2. No overlap between rounds (each sandbox appears in exactly one round)
    3. Equal load per round (balanced distribution)

    Tab-switch mode:
    - Each round operates on a specific tab (round_id % len(tab_ids))
    - Detects existing tabs at startup (cross-process state)
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
        self.active_runners: List[TabSwitchRunner] = []
        self.round_stop_event: Optional[threading.Event] = None

    def _detect_tabs(self) -> None:
        """Detect existing tabs for each sandbox (cross-process state).

        Since warmup and benchmark may run in different processes,
        we need to detect tabs at benchmark start using agent-browser tab command.
        """
        print("\n[RoundRobin] Detecting existing tabs...")

        for state in self.all_ready_states:
            sbx = state.sandbox_obj
            if not sbx:
                continue

            try:
                # List existing tabs
                result = sbx.commands.run("agent-browser tab", timeout=30, user="root")

                if result.exit_code == 0:
                    # Parse tab IDs from output (format: t1, t2, t3, ...)
                    output = result.stdout if result.stdout else ""
                    tab_ids = self._parse_tab_list(output)

                    if tab_ids:
                        state.tab_ids = tab_ids
                        print(f"[Sandbox{state.sandbox_id}] Detected {len(tab_ids)} tabs: {tab_ids}")
                    else:
                        # No tabs detected, mark as warning
                        print(f"[Sandbox{state.sandbox_id}] Warning: No tabs detected")
                else:
                    print(f"[Sandbox{state.sandbox_id}] Failed to list tabs")

            except Exception as e:
                print(f"[Sandbox{state.sandbox_id}] Tab detection error: {e}")

    def _parse_tab_list(self, output: str) -> List[str]:
        """Parse tab IDs from agent-browser tab output.

        Output format:
          [t1] url1 - http://...
          [t2] url2 - http://...

        The regex matches t1, t2, etc. from [t1], [t2] patterns.
        """
        tab_ids = []

        # Match tab IDs (t1, t2, t3, etc.) from [tN] patterns
        pattern = r"\[(t\d+)\]|\b(t\d+)\b"
        matches = re.findall(pattern, output)

        # re.findall returns tuples for groups, flatten and filter
        for match in matches:
            tab_id = match[0] if match[0] else match[1]
            if tab_id:
                tab_ids.append(tab_id)

        return tab_ids

    def run(self) -> None:
        """Execute the round-robin test.

        Main loop:
        1. Prepare sandbox groups (equal distribution)
        2. Detect existing tabs (cross-process state)
        3. Calculate number of rounds (auto or from config)
        4. For each round: start tasks -> wait interval -> stop tasks
        5. Loop back to first group if rounds exceed groups (cycling)
        6. Track statistics per round
        7. Stop when duration is reached or all rounds completed
        """
        import math
        import time

        # 1. Prepare sandbox groups
        self._prepare_sandbox_groups()

        if not self.sandbox_groups:
            print("[RoundRobin] No sandbox groups to execute")
            return

        # 2. Detect existing tabs (cross-process state)
        self._detect_tabs()

        # Check if any tabs were detected
        sandboxes_with_tabs = sum(1 for s in self.all_ready_states if s.tab_ids)
        if sandboxes_with_tabs == 0:
            print("[RoundRobin] Warning: No tabs detected in any sandbox")
            print("[RoundRobin] Make sure to run warmup phase first to open tabs")
            return

        print(f"[RoundRobin] Sandboxes with tabs: {sandboxes_with_tabs}/{len(self.all_ready_states)}")

        # 3. Calculate number of rounds (auto or from config)
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

        # 4. Execute each round (with cycling) until duration is reached
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

        # Determine which tab this round operates on
        # All sandboxes should have the same number of tabs
        sample_state = current_states[0] if current_states else None
        tab_count = len(sample_state.tab_ids) if sample_state and sample_state.tab_ids else 0
        tab_index = round_id % tab_count if tab_count > 0 else 0

        # Show cycle info if this is a repeated group
        if round_id >= num_groups:
            print(
                f"\n[Round {round_id}] (cycle {round_id // num_groups}, group {group_idx}) Starting {len(current_states)} sandboxes, tab t{tab_index + 1}"
            )
        else:
            print(f"\n[Round {round_id}] Starting {len(current_states)} sandboxes, tab t{tab_index + 1}")

        # Mark current round for statistics tracking
        self.stats_collector.set_round(round_id)

        # Create round-specific stop event
        self.round_stop_event = threading.Event()

        # Start tab-switch runners for current round
        self.active_runners = []
        for state in current_states:
            # Skip sandboxes without tabs
            if not state.tab_ids:
                print(f"[Sandbox{state.sandbox_id}] Skipping - no tabs available")
                continue

            runner = TabSwitchRunner(state, self.config, self.round_stop_event, round_id)
            self.active_runners.append(runner)
            runner.start()

        self.current_round = round_id

    def _stop_round(self) -> None:
        """Stop the current round and print summary."""
        if not self.round_stop_event:
            return

        # Signal all runners to stop
        self.round_stop_event.set()

        # Wait for runners to finish
        for runner in self.active_runners:
            runner.join(timeout=2)

        # Aggregate step timing from sandbox states
        step_totals = {}
        for state in self.all_ready_states:
            if state.tab_ids:
                metrics = state.browser_metrics
                step_stats = metrics.get_step_stats()
                for step_name, stats in step_stats.items():
                    if step_name not in step_totals:
                        step_totals[step_name] = {"total": 0.0, "count": 0}
                    step_totals[step_name]["total"] += stats["avg"] * stats["count"]
                    step_totals[step_name]["count"] += stats["count"]

        # Print round summary with step timing
        runner_count = len(self.active_runners)
        if runner_count > 0 and step_totals:
            avg_tab_switch = (
                step_totals.get("tab_switch", {}).get("total", 0)
                / max(1, step_totals.get("tab_switch", {}).get("count", 1))
            ) * 1000
            avg_snapshot = (
                step_totals.get("snapshot", {}).get("total", 0)
                / max(1, step_totals.get("snapshot", {}).get("count", 1))
            ) * 1000

            tab_index = (
                self.current_round % len(self.all_ready_states[0].tab_ids)
                if self.all_ready_states and self.all_ready_states[0].tab_ids
                else 0
            )
            print(
                f"[Round {self.current_round}] Completed: {runner_count} sandboxes, tab t{tab_index + 1}, avg: tab_switch={avg_tab_switch:.0f}ms, snapshot={avg_snapshot:.0f}ms"
            )
        else:
            print(f"[Round {self.current_round}] Completed: {runner_count} sandboxes")

        # Clear round state
        self.active_runners.clear()
        self.round_stop_event = None

        # Clear round marker in stats collector
        self.stats_collector.set_round(None)

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
