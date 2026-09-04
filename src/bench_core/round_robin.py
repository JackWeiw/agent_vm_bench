"""Round-robin task orchestrator (host-agnostic port of e2b_bench.round_robin).

Rotates sandbox execution across rounds for memory-migration stress testing.
Each round activates a different subset of sandboxes so memory access is evenly
distributed. The manager holds the :class:`EnvironmentProvider` and threads it
into every round runner it constructs; workflow dispatch (browser / coding /
document) selects which round-runner class to build.

The bench-core StatsCollector exposes a polymorphic ``set_round()`` +
``task_metrics`` path, so this port drops most of e2b's per-workflow dispatch:
round baselines and step-timing aggregation both go through ``task_metrics``,
which resolves to the active workflow's metrics object.
"""
from __future__ import annotations

import logging
import math
import threading
import time

from bench_core.admission import Admission
from bench_core.config import KernelConfig
from bench_core.observability.lifecycle_series import LifecycleSeriesWriter
from bench_core.observability.stats_collector import StatsCollector
from bench_core.schemas import BenchSandbox, get_step_order
from env_provider import EnvironmentProvider

logger = logging.getLogger(__name__)


class RoundRobinTaskManager:
    """Round-robin task manager -- rotates sandbox execution across rounds.

    Each round activates a different subset of sandboxes, ensuring:
    1. Even memory access distribution across all sandboxes
    2. No overlap between rounds (each sandbox appears in exactly one round per cycle)
    3. Equal load per round (balanced distribution)
    """

    def __init__(
        self,
        config: KernelConfig,
        sandbox_states: dict[int, BenchSandbox],
        stop_event: threading.Event,
        stats_collector: StatsCollector,
        provider: EnvironmentProvider,
        *,
        series: LifecycleSeriesWriter | None = None,
        admission: Admission | None = None,
        launch_pacer=None,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.stats_collector = stats_collector
        self.provider = provider
        self.series = series
        self.admission = admission
        self.launch_pacer = launch_pacer

        # Sandbox groups for each round
        self.all_ready_states: list[BenchSandbox] = []
        self.sandbox_groups: list[list[BenchSandbox]] = []

        # Current round state
        self.current_round: int = 0
        self._planned_rounds: int = 0  # Total rounds planned to run
        self.active_runners: list[threading.Thread] = []
        self.round_stop_event: threading.Event | None = None

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
        self._prepare_sandbox_groups()

        if not self.sandbox_groups:
            logger.warning("[RoundRobin] No sandbox groups to execute")
            return

        rounds = self._calculate_rounds()
        self._planned_rounds = rounds
        num_groups = len(self.sandbox_groups)

        if self.config.round_count and self.config.round_count > 0:
            logger.info(
                f"\n[RoundRobin] Will run up to {rounds} rounds (whichever ends first: "
                f"round_count={rounds} or duration={self.config.test_duration}s)"
            )
        else:
            logger.info(f"\n[RoundRobin] Will cycle continuously until duration={self.config.test_duration}s")
        logger.info(f"[RoundRobin] Sandbox groups: {num_groups}, {len(self.sandbox_groups[0])} sandboxes per round")

        start_time = time.time()
        for round_id in range(rounds):
            if self.stop_event.is_set():
                logger.info(f"[RoundRobin] Stop event detected, ending at round {round_id}")
                break

            elapsed = time.time() - start_time
            if elapsed >= self.config.test_duration:
                logger.info(
                    f"[RoundRobin] Duration reached ({elapsed:.1f}s >= {self.config.test_duration}s), "
                    f"ending at round {round_id}"
                )
                break

            self._start_round(round_id)

            # Always finalize the round so partial metrics and baselines are
            # preserved even when a runner exceeds its deadline.
            try:
                self._wait_for_active_runners()
            finally:
                self._stop_round()

            # Gap between rounds (after tasks complete).
            if elapsed + self.config.round_interval < self.config.test_duration:
                time.sleep(self.config.round_interval)

        elapsed = time.time() - start_time
        logger.info(f"\n[RoundRobin] Completed {min(self.current_round + 1, rounds)} rounds in {elapsed:.1f}s")

    def _prepare_sandbox_groups(self) -> None:
        """Prepare sandbox groups for round-robin execution.

        Group count determination:
        1. If round_size > 0: group_count = ceil(total / round_size)
        2. Otherwise: use min(total, 10) as default

        round_count does NOT affect group count -- it only controls the max
        number of rounds to execute (termination condition).
        """
        self.all_ready_states = [s for s in self.sandbox_states.values() if s.ready]

        total = len(self.all_ready_states)
        if total == 0:
            logger.warning("[RoundRobin] No ready sandboxes available")
            return

        if self.config.round_size and self.config.round_size > 0:
            group_count = math.ceil(total / self.config.round_size)
            logger.info(f"[RoundRobin] Using round_size={self.config.round_size}, calculated {group_count} groups")
        else:
            group_count = min(total, 10)
            logger.info(f"[RoundRobin] Auto-configured {group_count} sandbox groups (default)")

        base_per_round = total // group_count
        remainder = total % group_count

        logger.info(f"[RoundRobin] Preparing groups: {total} sandboxes ÷ {group_count} groups")
        logger.info(f"[RoundRobin] Base per round: {base_per_round}, remainder: {remainder}")

        self.sandbox_groups = []
        start_idx = 0
        for i in range(group_count):
            per_round = base_per_round + (1 if i < remainder else 0)
            end_idx = start_idx + per_round
            self.sandbox_groups.append(self.all_ready_states[start_idx:end_idx])
            start_idx = end_idx

        group_sizes = [len(g) for g in self.sandbox_groups]
        logger.info(f"[RoundRobin] Group sizes: {group_sizes}")

    def _start_round(self, round_id: int) -> None:
        """Start a specific round.

        Marks the round on the stats collector (records the round's start
        baseline idempotently) and launches one round runner per sandbox in the
        round's group, dispatching by workflow type.
        """
        num_groups = len(self.sandbox_groups)
        group_idx = round_id % num_groups
        current_states = self.sandbox_groups[group_idx]

        if round_id >= num_groups:
            logger.info(
                f"\n[Round {round_id}] (cycle {round_id // num_groups}, group {group_idx}) "
                f"Starting {len(current_states)} sandboxes"
            )
        else:
            logger.info(f"\n[Round {round_id}] Starting {len(current_states)} sandboxes")

        # Mark current round + record start baseline (idempotent). The stats
        # collector's set_round uses task_metrics, so no workflow dispatch here.
        self.stats_collector.set_round(round_id)

        self.round_stop_event = threading.Event()
        self.active_runners = []

        if self.config.workflow_type == "coding":
            from bench_core.task_runner.coding import CodingRoundRunner

            for state in current_states:
                runner = CodingRoundRunner(state, self.config, self.round_stop_event, round_id, self.provider)
                self.active_runners.append(runner)
                runner.start()
        elif self.config.workflow_type == "document":
            from bench_core.task_runner.document import DocumentRoundRunner

            for state in current_states:
                runner = DocumentRoundRunner(state, self.config, self.round_stop_event, round_id, self.provider)
                self.active_runners.append(runner)
                runner.start()
        elif self.config.workflow_type == "browser":
            from bench_core.task_runner.browser import TabOperationRunner

            for state in current_states:
                runner = TabOperationRunner(state, self.config, self.round_stop_event, round_id, self.provider)
                self.active_runners.append(runner)
                runner.start()
        elif self.config.workflow_type == "replay":
            from bench_core.task_runner.replay import ReplayRoundRunner

            for state in current_states:
                runner = ReplayRoundRunner(
                    state,
                    self.config,
                    self.round_stop_event,
                    round_id,
                    self.provider,
                    series=self.series,
                    admission=self.admission,
                    launch_pacer=self.launch_pacer,
                )
                self.active_runners.append(runner)
                runner.start()
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

        self.current_round = round_id

    def _stop_round(self) -> None:
        """Stop the current round, record the end-boundary baseline, and print summary.

        Assumes runners have already completed (joined in the main loop). Forces
        a final snapshot attributed to the current round, then records the
        post-round baseline (the end boundary for this round's delta -- also the
        start baseline for the next round, which set_round keeps idempotent).
        """
        if not self.round_stop_event:
            return

        # Signal stop (in case any runner is still waiting), then force a final
        # snapshot captured under the current round.
        self.round_stop_event.set()
        self.stats_collector._take_snapshot()

        # Record the end-boundary baseline for this round = start baseline for
        # the next. set_round is idempotent, so the next _start_round won't
        # overwrite it. Polymorphic via task_metrics (no workflow dispatch).
        next_round = self.current_round + 1
        self.stats_collector.set_round(next_round)

        # Aggregate step timing from this round's active runners (not all
        # sandboxes). task_metrics resolves to the active workflow's metrics.
        step_totals: dict[str, dict[str, float]] = {}
        for runner in self.active_runners:
            state = runner.state  # type: ignore[attr-defined]
            step_stats = state.task_metrics.get_step_stats()
            for step_name, stats in step_stats.items():
                if step_name not in step_totals:
                    step_totals[step_name] = {"total": 0.0, "count": 0, "min_count": float("inf")}
                step_totals[step_name]["total"] += stats["avg"] * stats["count"]
                step_totals[step_name]["count"] += stats["count"]
                step_totals[step_name]["min_count"] = min(step_totals[step_name]["min_count"], stats["count"])

        runner_count = len(self.active_runners)
        if runner_count > 0 and step_totals:
            avg_parts = []
            step_order = get_step_order(self.config.workflow_type, self.config.document_case_kind)
            for step_name in step_order:
                if step_name in step_totals:
                    avg_ms = (step_totals[step_name]["total"] / max(1, step_totals[step_name]["count"])) * 1000
                    avg_parts.append(f"{step_name}={avg_ms:.0f}ms")
            avg_str = ", ".join(avg_parts) if avg_parts else "no timing data"
            logger.info(f"[Round {self.current_round}] Completed: {runner_count} sandboxes, avg: {avg_str}")
        else:
            logger.info(f"[Round {self.current_round}] Completed: {runner_count} sandboxes")

        # Clear round state; no active round during the inter-round gap.
        self.active_runners.clear()
        self.round_stop_event = None
        self.stats_collector.current_round = None

    def _wait_for_active_runners(self) -> None:
        """Wait for this round's runners; document tasks use one shared deadline."""
        if self.config.workflow_type in {"browser", "coding", "replay"}:
            for runner in self.active_runners:
                runner.join(timeout=120)
            return
        if self.config.workflow_type != "document":
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")
        deadline = time.monotonic() + self.config.document_task_timeout + 5
        for runner in self.active_runners:
            runner.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [runner.name for runner in self.active_runners if runner.is_alive()]
        if alive:
            raise RuntimeError(f"document round runners did not finish before task deadline: {alive}")

    def _calculate_rounds(self) -> int:
        """Calculate max number of rounds to execute.

        round_count controls termination: if specified, the test stops after
        that many rounds OR when duration is reached (whichever comes first).
        If round_count is not specified, the test runs until duration is reached.

        Note: round_size determines group count (via _prepare_sandbox_groups),
        round_count determines the max number of round iterations. They coexist.
        """
        if self.config.round_count and self.config.round_count > 0:
            return self.config.round_count
        # No round_count specified -- rely on duration check in run() to stop.
        # Large enough to cycle until test_duration is reached.
        return 10000
