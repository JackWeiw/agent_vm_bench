"""Trajectory replay workflow task runners (host-agnostic).

Each sandbox gets an independent thread that re-executes a recorded SWE-bench
agent trajectory (ordered shell + ``str_replace_editor`` actions with per-step
``delay_time``) solely through :meth:`EnvironmentProvider.exec`. P1 is
exec-only: the recorded action string is exec'd verbatim -- workdir and env are
passed via the ``exec`` contract's first-class ``cwd`` / ``env`` params, with
no shell wrapping and no ``str_replace_editor`` semantic re-implementation.

The runner is built around ``_run_slice(step)``, whose ``_resume`` / ``_pause``
are overridable **no-op hooks**. P2 (lifecycle mode, e2b only) will override
them with real ``provider.pause`` / ``provider.resume`` calls -- the slice
shape is fixed here so P2 is a pure override, not a refactor. Exec-only mode
also yields the "no lifecycle overhead" baseline against which P2's
pause/resume measurements can be compared.

Classes:
    ReplayBaseRunner   - the slice base (shared by the three runners below)
    ReplayWarmupRunner - load + probe the pool during warmup
    ReplayTaskRunner   - fixed-mode continuous replay loop
    ReplayRoundRunner  - one round-robin round (one trajectory)
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from bench_core.config import KernelConfig
from bench_core.replay_payload import ReplayStep, Trajectory, load_pool
from bench_core.schemas import BenchSandbox
from env_provider import CommandResult, EnvironmentProvider

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StepResult:
    """Internal per-step record produced by :meth:`ReplayBaseRunner._run_slice`.

    ``exec_elapsed_sec`` isolates the exec wall time so P2's lifecycle overhead
    (``slice_total_sec - exec_elapsed_sec`` = ``resume_sec + pause_sec``) is
    computable without re-instrumenting the metrics. P1 sets
    ``slice_total_sec = exec_elapsed_sec``; P2 starts summing the lifecycle
    phases into ``slice_total_sec`` -- ``ReplayMetrics`` needs no schema change.
    """

    step_index: int
    action_type: str
    exit_code: int
    exec_elapsed_sec: float
    slice_total_sec: float
    resume_sec: float
    pause_sec: float
    requested_delay_sec: float


class ReplayBaseRunner(threading.Thread):
    """Slice base shared by the three replay runners.

    Subclasses (warmup / fixed / round) implement ``run()``; they share the
    slice + exec plumbing here. The constructor signature matches the kernel's
    structural protocol for task runners; warmup uses the 3-arg form
    ``(state, config, provider)``.
    """

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        provider: EnvironmentProvider,
    ) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.provider = provider

    # --- the slice (the spine P2 plugs into) ---
    def _run_slice(self, step: ReplayStep) -> StepResult:
        """One resume -> execute -> pause cycle.

        P1: ``_resume`` / ``_pause`` are no-ops. P2 overrides them to call
        ``provider.resume(self.state)`` / ``provider.pause(self.state)``
        (capability-gated, e2b only).
        """
        self._resume()
        exec_start = time.perf_counter()
        result = self._execute(step)
        exec_elapsed = time.perf_counter() - exec_start
        self._pause()
        return StepResult(
            step_index=step.index,
            action_type=step.action_type,
            exit_code=result.exit_code,
            exec_elapsed_sec=exec_elapsed,
            slice_total_sec=exec_elapsed,  # P1: == exec; P2 sums resume+exec+pause
            resume_sec=0.0,
            pause_sec=0.0,
            requested_delay_sec=step.delay_time_sec,
        )

    # --- overridable no-op hooks (P2 replaces with real lifecycle calls) ---
    def _resume(self) -> None:
        """No-op in P1. P2: ``provider.resume(self.state)``."""

    def _pause(self) -> None:
        """No-op in P1. P2: ``provider.pause(self.state)``."""

    def _execute(self, step: ReplayStep) -> CommandResult:
        """Exec the recorded action verbatim -- cwd/env via the exec contract."""
        return self.provider.exec(
            self.state,
            step.action,
            cwd=self.config.replay_workdir or None,
            env=self.config.replay_env or None,
            timeout=self.config.replay_action_timeout,
        )

    # --- shared metric recording ---
    def _record_step(
        self,
        step_result: StepResult,
        *,
        timed_out: bool,
        actual_delay: float,
        trajectory_complete: bool,
    ) -> None:
        """Record one step's metrics into ``self.state.replay_metrics``."""
        success = step_result.exit_code == 0 and not timed_out
        self.state.replay_metrics.add(
            latency=step_result.exec_elapsed_sec,
            success=success,
            timeout=timed_out,
            step_times={step_result.action_type: step_result.exec_elapsed_sec},
            action_type=step_result.action_type,
            requested_delay=step_result.requested_delay_sec,
            actual_delay=actual_delay,
            trajectory_complete=trajectory_complete,
        )
        self.state.update_last_task_time(time.time())

    def _sleep_delay(self, step: ReplayStep) -> None:
        """Honour the recorded think-time gap, scaled. First step's delay applies."""
        gap = step.delay_time_sec * self.config.replay_delay_scale
        if gap > 0 and not self.stop_event.is_set():
            self.stop_event.wait(gap)


class ReplayWarmupRunner(threading.Thread):
    """Warmup phase runner for replay -- load + validate the shared pool, probe exec.

    Lightweight: loads the trajectory pool (cached, shared across sandboxes),
    runs one trivial command to confirm the sandbox is command-responsive,
    and marks the sandbox warmed up. No resident process (none needed).
    """

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        provider: EnvironmentProvider,
    ) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.provider = provider

    def run(self) -> None:
        if not self.state.ready:
            logger.warning(f"[Sandbox{self.state.index}] Cannot start replay warmup: not ready")
            self.state.warmup_done = True
            return

        pool = load_pool(self.config)
        if not pool:
            logger.warning(f"[Sandbox{self.state.index}] Replay pool empty; nothing to replay")
        else:
            logger.info(f"[Sandbox{self.state.index}] Replay pool: {len(pool)} trajectories")

        try:
            self.provider.exec(self.state, "true", timeout=10)
        except Exception as e:
            logger.warning(f"[Sandbox{self.state.index}] Replay warmup exec probe failed: {e}")

        self.state.warmup_done = True
        logger.info(f"[Sandbox{self.state.index}] Replay warmup completed")


class ReplayTaskRunner(ReplayBaseRunner):
    """Fixed-mode replay runner -- continuously cycles the pool until stop_event.

    Each iteration: pick the next trajectory (cursor advances per trajectory,
    wrapping at pool end), replay its steps as slices, record metrics. On
    ``stop_on_error``, a failing step aborts the current trajectory and
    advances to the next -- it does NOT stop the sandbox or the benchmark.
    """

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        provider: EnvironmentProvider,
    ) -> None:
        super().__init__(state, config, stop_event, provider)
        self.consecutive_errors = 0

    def run(self) -> None:
        if not self.state.ready:
            logger.warning(f"[Sandbox{self.state.index}] Cannot start replay: not ready")
            return

        pool = load_pool(self.config)
        if not pool:
            logger.info(f"[Sandbox{self.state.index}] Replay pool empty; exiting")
            return

        cursor = self.state.index % len(pool)
        logger.info(f"[Sandbox{self.state.index}] Replay task runner started ({len(pool)} trajectories)")

        while not self.stop_event.is_set() and self.state.is_alive:
            traj = pool[cursor]
            self._replay_trajectory(traj)
            cursor = (cursor + 1) % len(pool)

        logger.info(f"[Sandbox{self.state.index}] Replay task runner ended")

    def _replay_trajectory(self, traj: Trajectory) -> None:
        prev_slice_end = time.perf_counter()
        aborted = False
        for step in traj.steps:
            if self.stop_event.is_set() or not self.state.is_alive:
                return
            self._sleep_delay(step)
            if self.stop_event.is_set():
                return
            slice_start = time.perf_counter()
            actual_delay = slice_start - prev_slice_end
            timed_out = False
            try:
                sr = self._run_slice(step)
            except Exception as e:
                msg = str(e).lower()
                timed_out = "timed out" in msg or "context deadline exceeded" in msg
                sr = StepResult(
                    step_index=step.index,
                    action_type=step.action_type,
                    exit_code=1,
                    exec_elapsed_sec=0.0,
                    slice_total_sec=0.0,
                    resume_sec=0.0,
                    pause_sec=0.0,
                    requested_delay_sec=step.delay_time_sec,
                )
                logger.error(f"[Sandbox{self.state.index}] Replay step {step.index} exception: {msg[:120]}")
            prev_slice_end = time.perf_counter()
            self._record_step(sr, timed_out=timed_out, actual_delay=actual_delay, trajectory_complete=False)

            if sr.exit_code != 0 or timed_out:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    logger.warning(f"[Sandbox{self.state.index}] Marked offline (3 consecutive replay failures)")
                    return
                if self.config.replay_stop_on_error:
                    aborted = True
                    break
            else:
                self.consecutive_errors = 0

        if not aborted and not self.stop_event.is_set():
            # Ran every step to the end -> mark one completion.
            self.state.replay_metrics._mark_completion()

        if aborted:
            logger.info(f"[Sandbox{self.state.index}] Trajectory {traj.instance_id} aborted (stop_on_error); advancing")


class ReplayRoundRunner(ReplayBaseRunner):
    """Round-robin replay runner -- replays one trajectory per round.

    Trajectory selection is deterministic: index ``(state.index + round_id) %
    len(pool)``. So round N, sandbox S always maps to the same trajectory, and
    successive rounds rotate each sandbox through the pool. No cursor state
    is held across rounds (round_id encodes the position).
    """

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        round_id: int,
        provider: EnvironmentProvider,
    ) -> None:
        super().__init__(state, config, stop_event, provider)
        self.round_id = round_id

    def run(self) -> None:
        if not self.state.ready or not self.state.is_alive:
            logger.info(f"[Sandbox{self.state.index}] Not ready/alive for replay round")
            return

        pool = load_pool(self.config)
        if not pool:
            logger.info(f"[Sandbox{self.state.index}] Replay pool empty; skipping round {self.round_id}")
            return

        idx = (self.state.index + self.round_id) % len(pool)
        traj = pool[idx]
        logger.info(f"[Sandbox{self.state.index}] Replay round {self.round_id}: trajectory {traj.instance_id}")

        prev_slice_end = time.perf_counter()
        aborted = False
        for step in traj.steps:
            if self.stop_event.is_set():
                return
            self._sleep_delay(step)
            if self.stop_event.is_set():
                return
            slice_start = time.perf_counter()
            actual_delay = slice_start - prev_slice_end
            timed_out = False
            try:
                sr = self._run_slice(step)
            except Exception as e:
                msg = str(e).lower()
                timed_out = "timed out" in msg or "context deadline exceeded" in msg
                sr = StepResult(
                    step_index=step.index,
                    action_type=step.action_type,
                    exit_code=1,
                    exec_elapsed_sec=0.0,
                    slice_total_sec=0.0,
                    resume_sec=0.0,
                    pause_sec=0.0,
                    requested_delay_sec=step.delay_time_sec,
                )
                logger.error(f"[Sandbox{self.state.index}] Replay step {step.index} exception: {msg[:120]}")
            prev_slice_end = time.perf_counter()
            self._record_step(sr, timed_out=timed_out, actual_delay=actual_delay, trajectory_complete=False)
            if (sr.exit_code != 0 or timed_out) and self.config.replay_stop_on_error:
                aborted = True
                break

        if not aborted and not self.stop_event.is_set():
            self.state.replay_metrics._mark_completion()

        logger.info(f"[Sandbox{self.state.index}] Replay round {self.round_id} done (aborted={aborted})")
