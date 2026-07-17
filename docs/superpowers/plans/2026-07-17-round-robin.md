# E2B Bench Round-Robin Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add round-robin sandbox rotation mode to e2b_bench for memory migration stress testing.

**Architecture:** Create RoundRobinTaskManager class that splits sandboxes into equal groups and rotates execution across rounds. Each round activates a different subset of sandboxes, ensuring even memory access distribution.

**Tech Stack:** Python 3.x, threading, dataclasses, argparse

---

## File Structure

| File | Purpose |
|------|---------|
| `e2b_bench/config.py` | Add benchmark_mode, round_count, round_interval fields |
| `e2b_bench/bench.py` | Add round_robin mode branch in run_benchmark() |
| `e2b_bench/stats_collector.py` | Add round tracking for per-round statistics |
| `e2b_bench/round_robin.py` | NEW: RoundRobinTaskManager class |
| `e2b_bench/task_runner.py` | No changes (already supports stop_event) |

---

## Task 1: Add Configuration Fields

**Files:**
- Modify: `e2b_bench/config.py`

- [ ] **Step 1: Add new fields to Config dataclass**

Find the `benchmark_percent` field (around line 52) and add new fields after it:

```python
    # Benchmark stress percent (percentage of sandboxes to run benchmark)
    benchmark_percent: float = 1.0  # Percentage of sandboxes for benchmark (default 100%)

    # Round-robin mode configuration (new fields)
    benchmark_mode: str = "fixed"  # "fixed" (default) or "round_robin"
    round_count: Optional[int] = None  # Round count for round_robin mode (None = auto-calculate)
    round_interval: int = 30  # Round interval in seconds for round_robin mode
```

- [ ] **Step 2: Add parsing in _from_dict method**

Find the `_from_dict` method (around line 99) and add parsing for new fields. Look for the line with `benchmark_percent=test.get("benchmark_percent", 1.0),` and add after it:

```python
            benchmark_percent=test.get("benchmark_percent", 1.0),
            # Round-robin mode configuration
            benchmark_mode=test.get("benchmark_mode", "fixed"),
            round_count=test.get("round_count"),
            round_interval=test.get("round_interval", 30),
```

- [ ] **Step 3: Add CLI argument merging in merge_with_args**

Find the `merge_with_args` method (around line 159) and add merging for new fields. Look for the line with `benchmark_percent=args.benchmark_percent` and add after it:

```python
            benchmark_percent=args.benchmark_percent
            if args.benchmark_percent is not None
            else yaml_config.benchmark_percent,
            # Round-robin mode configuration
            benchmark_mode=args.benchmark_mode if args.benchmark_mode else yaml_config.benchmark_mode,
            round_count=args.round_count if args.round_count else yaml_config.round_count,
            round_interval=args.round_interval if args.round_interval else yaml_config.round_interval,
```

- [ ] **Step 4: Add CLI argument defaults in from_args**

Find the `from_args` method (around line 224) and add defaults for new fields. Look for the line with `benchmark_percent=args.benchmark_percent if args.benchmark_percent is not None else 1.0,` and add after it:

```python
            benchmark_percent=args.benchmark_percent if args.benchmark_percent is not None else 1.0,
            # Round-robin mode configuration
            benchmark_mode=args.benchmark_mode if args.benchmark_mode else "fixed",
            round_count=args.round_count,
            round_interval=args.round_interval if args.round_interval else 30,
```

- [ ] **Step 5: Commit changes**

```bash
git add e2b_bench/config.py
git commit -m "feat(e2b): add round-robin configuration fields to Config class"
```

---

## Task 2: Add CLI Arguments

**Files:**
- Modify: `e2b_bench/bench.py`

- [ ] **Step 1: Add benchmark-mode argument**

Find the `build_arg_parser` function (around line 619). Look for the benchmark-percent argument (around line 668) and add new arguments after it:

```python
    # Benchmark control
    parser.add_argument(
        "-bp",
        "--benchmark-percent",
        type=float,
        default=None,
        help="Percentage of sandboxes for benchmark (e.g., 0.5 = 50%%)",
    )

    # Round-robin mode control (new arguments)
    parser.add_argument(
        "-bm",
        "--benchmark-mode",
        type=str,
        choices=["fixed", "round_robin"],
        default="fixed",
        help="Benchmark mode: 'fixed' (default) or 'round_robin'",
    )
    parser.add_argument(
        "-rc",
        "--round-count",
        type=int,
        default=None,
        help="Round count for round_robin mode",
    )
    parser.add_argument(
        "-ri",
        "--round-interval",
        type=int,
        default=30,
        help="Round interval in seconds for round_robin mode (default: 30)",
    )
```

- [ ] **Step 2: Commit changes**

```bash
git add e2b_bench/bench.py
git commit -m "feat(e2b): add CLI arguments for round-robin mode"
```

---

## Task 3: Create RoundRobinTaskManager Class

**Files:**
- Create: `e2b_bench/round_robin.py`

- [ ] **Step 1: Create the new file with the class**

```python
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
```

- [ ] **Step 2: Commit changes**

```bash
git add e2b_bench/round_robin.py
git commit -m "feat(e2b): add RoundRobinTaskManager class for round-robin execution"
```

---

## Task 4: Add Round Tracking to StatsCollector

**Files:**
- Modify: `e2b_bench/stats_collector.py`

- [ ] **Step 1: Add round tracking fields to StatsCollector.__init__**

Find the `__init__` method (around line 23) and add new fields:

```python
    def __init__(self, config: Config, sandbox_states: Dict[int, SandboxState]):
        self.config = config
        self.sandbox_states = sandbox_states
        self.snapshots: List[TestSnapshot] = []
        self.start_time: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Round tracking for round-robin mode (new fields)
        self.current_round: Optional[int] = None
        self.round_snapshots: Dict[int, List[TestSnapshot]] = {}
```

- [ ] **Step 2: Add set_round method**

Add this method after the `stop` method (around line 41):

```python
    def stop(self) -> None:
        """Stop collection"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_round(self, round_id: Optional[int]) -> None:
        """Set current round for statistics tracking.

        Called by RoundRobinTaskManager to mark which round is currently active.
        Snapshots collected during this round will be grouped together.

        Args:
            round_id: Current round index (None to clear)
        """
        self.current_round = round_id
        if round_id is not None and round_id not in self.round_snapshots:
            self.round_snapshots[round_id] = []
```

- [ ] **Step 3: Update _take_snapshot to track rounds**

Find the `_take_snapshot` method (around line 49). Look for the line `self.snapshots.append(snapshot)` and add round tracking after it:

```python
        self.snapshots.append(snapshot)

        # Track round-specific snapshots (new code)
        if self.current_round is not None:
            self.round_snapshots[self.current_round].append(snapshot)
```

- [ ] **Step 4: Add round comparison to generate_report**

Find the `generate_report` method (around line 143). Add round comparison section at the end, before the final `return` statement:

```python
        # ... existing report content ...

            for error_type, count in error_types.items():
                if count > 0:
                    sids = error_type_sandboxes[error_type][:10]
                    lines.append(f"  {error_type}: {count} errors (sandboxes: {sids}...)")

        # Round comparison for round-robin mode (new section)
        if self.round_snapshots:
            lines.append("\n" + "=" * 80)
            lines.append("[Round Comparison]")
            lines.append("=" * 80)
            lines.append(f"{'Round':<8} {'Tasks':<8} {'Success%':<10} {'Avg(s)':<10} {'P99(s)':<10}")
            lines.append("-" * 50)

            for round_id in sorted(self.round_snapshots.keys()):
                snapshots = self.round_snapshots[round_id]
                if snapshots:
                    tasks = sum(s.browser_total for s in snapshots)
                    success = sum(s.browser_success for s in snapshots)
                    avg = statistics.mean(s.browser_avg_latency for s in snapshots if s.browser_avg_latency > 0) if any(s.browser_avg_latency > 0 for s in snapshots) else 0.0
                    p99 = max(s.browser_p99_latency for s in snapshots) if snapshots else 0.0
                    rate = success / max(1, tasks) * 100 if tasks > 0 else 0.0
                    lines.append(f"{round_id:<8} {tasks:<8} {rate:<10.1f} {avg:<10.2f} {p99:<10.2f}")

        lines.append("\n" + "=" * 80)
        return "\n".join(lines)
```

- [ ] **Step 5: Commit changes**

```bash
git add e2b_bench/stats_collector.py
git commit -m "feat(e2b): add round tracking to StatsCollector for round-robin mode"
```

---

## Task 5: Integrate Round-Robin into bench.py

**Files:**
- Modify: `e2b_bench/bench.py`

- [ ] **Step 1: Add import for RoundRobinTaskManager**

Find the imports at the top of the file (around line 25) and add the import:

```python
from .config import Config
from .round_robin import RoundRobinTaskManager
from .sandbox_manager import SandboxManager
from .schemas import SandboxStatus
from .stats_collector import StatsCollector
from .task_runner import TaskManager
```

- [ ] **Step 2: Add round-robin mode branch in run_benchmark**

Find the `run_benchmark` function. Look for the section around line 573-600 where stats_collector and task_manager are started. Replace the entire section with:

```python
    # 5. Start statistics collection
    print("\n[Phase 3] Starting stats collector...")
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()

    # 6. Start task execution (with batch control and benchmark_percent)
    if config.benchmark_mode == "round_robin":
        # Round-robin mode: rotate sandbox groups across rounds
        benchmark_count = max(1, int(ready_count * config.benchmark_percent))
        print(f"\n[Phase 4] Starting round-robin browser tasks...")
        print(f"  Mode: round_robin")
        print(f"  Rounds: {config.round_count}")
        print(f"  Interval: {config.round_interval}s per round")
        print(f"  Total sandboxes: {ready_count}")
        print(f"  Per round (balanced): ~{ready_count // config.round_count if config.round_count else 0}")

        round_robin_manager = RoundRobinTaskManager(
            config, sandbox_states, stop_event, stats_collector
        )
        round_robin_manager.run()
    else:
        # Fixed mode (original behavior)
        benchmark_count = max(1, int(ready_count * config.benchmark_percent))
        if config.benchmark_percent < 1.0:
            print(
                f"\n[Phase 4] Starting browser tasks on {benchmark_count}/{ready_count} sandboxes ({config.benchmark_percent * 100:.0f}%)..."
            )
        else:
            print("\n[Phase 4] Starting browser tasks...")
        task_manager.start_all()

        # 7. Run for specified duration
        print(f"\n[Phase 5] Running for {config.test_duration} seconds...")
        try:
            time.sleep(config.test_duration)
        except KeyboardInterrupt:
            print("\nUser interrupt, stopping...")

    # 8. Stop all components
    print("\n[Phase 6] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()
```

- [ ] **Step 3: Update benchmark mode display**

Find the configuration display section (around line 380-387) and add benchmark mode display:

```python
    # Benchmark percent display
    if config.benchmark_percent < 1.0:
        benchmark_count = config.benchmark_count
        print(f"  Benchmark: {benchmark_count}/{config.total_count} sandboxes ({config.benchmark_percent * 100:.0f}%)")

    # Benchmark mode display (new)
    if config.benchmark_mode == "round_robin":
        print(f"  Benchmark Mode: round_robin ({config.round_count} rounds x {config.round_interval}s)")
    else:
        print(f"  Benchmark Mode: fixed")
```

- [ ] **Step 4: Commit changes**

```bash
git add e2b_bench/bench.py
git commit -m "feat(e2b): integrate round-robin mode into run_benchmark"
```

---

## Task 6: Update YAML Configuration Example

**Files:**
- Modify: `config/e2b_bench.yaml`

- [ ] **Step 1: Add round-robin configuration section**

Find the `test:` section (around line 56) and update it:

```yaml
# Test run configuration
test:
  duration: 160
  stats_interval: 10
  benchmark_percent: 1.0    # Percentage of sandboxes for benchmark (1.0 = 100%, 0.5 = 50%)

  # Round-robin mode configuration (optional)
  benchmark_mode: "fixed"   # "fixed" (default) or "round_robin"
  # round_count: 5          # Round count for round_robin mode
  # round_interval: 30      # Round interval in seconds for round_robin mode
```

- [ ] **Step 2: Commit changes**

```bash
git add config/e2b_bench.yaml
git commit -m "docs: add round-robin configuration example to e2b_bench.yaml"
```

---

## Task 7: Verify Implementation

**Files:**
- Test: Manual verification

- [ ] **Step 1: Test fixed mode still works**

Run a quick test with default settings to ensure backward compatibility:

```bash
cd /path/to/agent_vm_bench
python -m e2b_bench --help
```

Expected: Help output shows new `-bm`, `-rc`, `-ri` arguments.

- [ ] **Step 2: Test round-robin mode with dry-run**

Create a minimal test config and verify the logic works:

```bash
# This would require actual E2B setup, so just verify syntax
python -c "from e2b_bench.round_robin import RoundRobinTaskManager; print('Import OK')"
```

Expected: `Import OK`

- [ ] **Step 3: Final commit if needed**

```bash
git status
# If any uncommitted changes:
git add -A
git commit -m "feat(e2b): complete round-robin mode implementation"
```

---

## Spec Coverage Check

| Spec Requirement | Task |
|------------------|------|
| Add benchmark_mode, round_count, round_interval fields | Task 1 |
| Add CLI arguments with short versions (-bm, -rc, -ri) | Task 2 |
| Create RoundRobinTaskManager class | Task 3 |
| Add round tracking to StatsCollector | Task 4 |
| Integrate round-robin into bench.py | Task 5 |
| Update YAML config example | Task 6 |
| Backward compatible (default fixed mode) | Task 1, 5 |
| Round comparison in report | Task 4 |

---

## Notes

- All code and comments are in English as requested
- No unit tests in this plan (existing codebase has no tests)
- Manual verification recommended before production use
- smap_tool integration is NOT included (user handles externally)