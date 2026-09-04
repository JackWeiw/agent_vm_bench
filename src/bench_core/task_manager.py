"""Fixed-mode task orchestrator (host-agnostic port of e2b_bench.task_runner.TaskManager).

Manages all sandbox task-execution threads for a fixed-mode benchmark: warmup,
then batched/concurrent task runners. The manager holds the
:class:`EnvironmentProvider` and threads it into every runner it constructs, so
the runners stay host-agnostic (they only call ``provider.exec``). Workflow
dispatch (browser / coding / document) selects which runner class to build; the
provider is the same for all three.
"""
from __future__ import annotations

import logging
import random
import threading
import time

from bench_core.admission import Admission
from bench_core.config import KernelConfig
from bench_core.observability.lifecycle_series import LifecycleSeriesWriter
from bench_core.schemas import BenchSandbox
from env_provider import EnvironmentProvider

logger = logging.getLogger(__name__)


class TaskManager:
    """Task manager -- manages all sandbox task execution threads with batch control."""

    def __init__(
        self,
        config: KernelConfig,
        sandbox_states: dict[int, BenchSandbox],
        stop_event: threading.Event,
        provider: EnvironmentProvider,
        *,
        series: LifecycleSeriesWriter | None = None,
        admission: Admission | None = None,
        launch_pacer=None,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.provider = provider
        self.series = series
        self.admission = admission
        self.launch_pacer = launch_pacer
        self.runners: list[threading.Thread] = []
        self.warmup_runners: list[threading.Thread] = []

    def start_warmup(self) -> None:
        """Start warmup phase for all ready sandboxes.

        Warmup preheats memory before the benchmark. Dispatches based on
        workflow_type:
        - "browser":  WarmupRunner (opens browser tabs)
        - "coding":   CodingWarmupRunner (one initial verify, no resident process)
        - "document": DocumentWarmupRunner (validates and restores the PDF/XLSX seed)
        - "replay":   ReplayWarmupRunner (loads trajectory pool, probe exec)
        """
        ready_states = [s for s in self.sandbox_states.values() if s.ready]

        if not ready_states:
            logger.info("No sandboxes ready for warmup")
            return

        if self.config.workflow_type == "coding":
            from bench_core.task_runner.coding import CodingWarmupRunner

            if not self.config.coding_skip_verify:
                logger.info(f"\n{'=' * 60}")
                logger.info("Coding Warmup Phase Starting")
                logger.info(f"  Total: {len(ready_states)} sandboxes")
                logger.info(f"  Project: {self.config.coding_project_dir}")
                logger.info(f"  Language: {self.config.coding_language}")
                logger.info(f"  Initial verify: {'enabled' if not self.config.coding_skip_verify else 'skipped'}")
                logger.info(f"{'=' * 60}")

                for state in ready_states:
                    runner = CodingWarmupRunner(state, self.config, self.provider)
                    self.warmup_runners.append(runner)
                    runner.start()
            else:
                logger.info("Coding warmup skipped (initial verify disabled)")
                for state in ready_states:
                    state.warmup_done = True
        elif self.config.workflow_type == "document":
            from bench_core.task_runner.document import DocumentWarmupRunner

            logger.info(f"\n{'=' * 60}")
            logger.info("Document Warmup Phase Starting")
            logger.info(f"  Total: {len(ready_states)} sandboxes")
            logger.info(f"  Case kind: {self.config.document_case_kind}")
            logger.info(f"  Seed: {self.config.document_seed_dir}")
            logger.info(f"{'=' * 60}")
            for state in ready_states:
                runner = DocumentWarmupRunner(state, self.config, self.provider)
                self.warmup_runners.append(runner)
                runner.start()
        elif self.config.workflow_type == "browser":
            from bench_core.task_runner.browser import WarmupRunner

            if not self.config.warmup_urls:
                logger.info("No warmup URLs configured, skipping warmup")
                for state in ready_states:
                    state.warmup_done = True
                return

            logger.info(f"\n{'=' * 60}")
            logger.info("Warmup Phase Starting")
            logger.info(f"  Total: {len(ready_states)} sandboxes")
            logger.info(f"  Warmup pages: {len(self.config.warmup_urls)}")
            logger.info(f"  Loop count: {self.config.warmup_loops}")
            logger.info(f"  Page delay: {self.config.warmup_delay}s")
            logger.info(f"{'=' * 60}")

            for state in ready_states:
                runner = WarmupRunner(state, self.config, self.provider)
                self.warmup_runners.append(runner)
                runner.start()
        elif self.config.workflow_type == "replay":
            from bench_core.task_runner.replay import ReplayWarmupRunner

            logger.info(f"\n{'=' * 60}")
            logger.info("Replay Warmup Phase Starting")
            logger.info(f"  Total: {len(ready_states)} sandboxes")
            logger.info(f"  Trajectory dir: {self.config.replay_trajectory_dir}")
            logger.info(f"  Mode: {self.config.replay_mode}")
            logger.info(f"{'=' * 60}")
            for state in ready_states:
                runner = ReplayWarmupRunner(state, self.config, self.provider)
                self.warmup_runners.append(runner)
                runner.start()
        else:
            raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

    def wait_warmup(self, timeout: float = 300.0) -> tuple[int, int]:
        """Wait for all warmup runners to complete.

        Returns: (completed_count, failed_count)
        """
        start_time = time.time()
        last_progress_time = start_time

        while time.time() - start_time < timeout:
            if self.stop_event.is_set():
                break

            done_count = sum(1 for s in self.sandbox_states.values() if s.warmup_done)
            total_count = len(self.warmup_runners)

            now = time.time()
            if now - last_progress_time >= 5:
                elapsed = now - start_time
                logger.info(f"   Warmup progress: {done_count}/{total_count} completed | elapsed {elapsed:.0f}s")
                last_progress_time = now

            if done_count >= total_count:
                break

            time.sleep(1)

        for runner in self.warmup_runners:
            runner.join(timeout=2)

        completed = sum(1 for s in self.sandbox_states.values() if s.warmup_done)
        if self.config.workflow_type == "document":
            failed = sum(1 for s in self.sandbox_states.values() if s.warmup_done and s.document_metrics.last_error)
        else:  # browser, coding, replay -- warmup failures surface in the workflow metrics
            failed = sum(1 for s in self.sandbox_states.values() if s.warmup_done and s.task_metrics.failed_count > 0)

        return completed, failed

    def start_all(self) -> None:
        """Start task execution threads for ready, warmed-up sandboxes.

        Strategy based on task_batch config:
        - With task_batch_size: batched start to avoid target server overload
        - Without config: full concurrent start for max load test

        benchmark_percent controls how many sandboxes to include in benchmark
        (e.g. 0.5 = 50% of ready sandboxes)
        """
        ready_states = [s for s in self.sandbox_states.values() if s.ready and s.warmup_done]

        if not ready_states:
            logger.info("No sandboxes ready for task execution")
            return

        total_ready = len(ready_states)
        benchmark_count = max(1, int(total_ready * self.config.benchmark_percent))

        if benchmark_count < total_ready:
            benchmark_states = random.sample(ready_states, benchmark_count)
            logger.info(
                f"\nBenchmark subset: {benchmark_count}/{total_ready} sandboxes "
                f"({self.config.benchmark_percent * 100:.0f}%)"
            )
        else:
            benchmark_states = ready_states

        if self.config.task_batch_size and self.config.task_batch_size > 0:
            self._start_batched(benchmark_states)
        else:
            self._start_concurrent(benchmark_states)

    def _start_batched(self, ready_states: list[BenchSandbox]) -> None:
        """Batched task execution start."""
        total = len(ready_states)
        batch_size = self.config.task_batch_size or 1
        batch_count = (total + batch_size - 1) // batch_size

        workflow_label = self.config.workflow_type.capitalize()

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Batched {workflow_label} Task Execution Start")
        logger.info(f"  Total: {total} sandboxes")
        logger.info(f"  Batches: {batch_count} x {batch_size}")
        logger.info(f"  Interval: {self.config.task_batch_interval}s")
        logger.info(f"{'=' * 60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                logger.info("Stop event detected, aborting task start")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)
            batch_states = ready_states[start_idx:end_idx]

            logger.info(
                f"\n[TaskBatch {batch_id}/{batch_count - 1}] Starting tasks for sandboxes {start_idx + 1}-{end_idx}"
            )

            for state in batch_states:
                runner = self._create_task_runner(state)
                self.runners.append(runner)
                runner.start()

            if batch_id < batch_count - 1 and self.config.task_batch_interval:
                logger.info(f"Waiting {self.config.task_batch_interval}s before next task batch...")
                time.sleep(self.config.task_batch_interval)

        logger.info(f"\nStarted {len(self.runners)} task runners in {batch_count} batches")

    def _start_concurrent(self, ready_states: list[BenchSandbox]) -> None:
        """Full concurrent task execution start."""
        workflow_label = self.config.workflow_type.capitalize()

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Concurrent {workflow_label} Task Execution Start")
        logger.info(f"  Total: {len(ready_states)} sandboxes (full concurrent)")
        logger.info(f"{'=' * 60}")

        for state in ready_states:
            runner = self._create_task_runner(state)
            self.runners.append(runner)
            runner.start()

        logger.info(f"\nStarted {len(self.runners)} task runners")

    def _create_task_runner(self, state: BenchSandbox) -> threading.Thread:
        """Create the workflow-specific task runner (fixed mode).

        Args:
            state: Sandbox state for the runner.

        Returns:
            Task runner thread (BrowserTaskRunner / CodingTaskRunner / DocumentTaskRunner / ReplayTaskRunner).
        """
        if self.config.workflow_type == "coding":
            from bench_core.task_runner.coding import CodingTaskRunner

            return CodingTaskRunner(state, self.config, self.stop_event, self.provider)
        if self.config.workflow_type == "document":
            from bench_core.task_runner.document import DocumentTaskRunner

            return DocumentTaskRunner(state, self.config, self.stop_event, self.provider)
        if self.config.workflow_type == "browser":
            from bench_core.task_runner.browser import BrowserTaskRunner

            return BrowserTaskRunner(state, self.config, self.stop_event, self.provider)
        if self.config.workflow_type == "replay":
            from bench_core.task_runner.replay import ReplayTaskRunner

            return ReplayTaskRunner(
                state,
                self.config,
                self.stop_event,
                self.provider,
                series=self.series,
                admission=self.admission,
                launch_pacer=self.launch_pacer,
            )
        raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")

    def wait_all(self, timeout: float = 5.0) -> None:
        """Wait for all task threads to end."""
        if self.config.workflow_type == "document":
            deadline = time.monotonic() + self.config.document_task_timeout + 5
            for runner in self.runners:
                remaining = max(0.0, deadline - time.monotonic())
                runner.join(timeout=remaining)
            alive = [runner.name for runner in self.runners if runner.is_alive()]
            if alive:
                raise RuntimeError(f"document runners did not finish before task deadline: {alive}")
            return
        if self.config.workflow_type in {"browser", "coding", "replay"}:
            for runner in self.runners:
                runner.join(timeout=timeout)
            return
        raise ValueError(f"Unsupported workflow_type: {self.config.workflow_type}")
