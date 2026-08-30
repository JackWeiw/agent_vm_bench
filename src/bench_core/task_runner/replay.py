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

from bench_core.admission import Admission, LaunchPacer, ShutdownInterrupted
from bench_core.config import KernelConfig
from bench_core.lifecycle_series import LifecycleSeriesWriter
from bench_core.replay_payload import ReplayStep, Trajectory, load_pool
from bench_core.schemas import BenchSandbox
from bench_core.transients import is_transient_sandbox_error
from env_provider import CommandResult, EnvironmentProvider, EphemeralCapable

logger = logging.getLogger(__name__)

# Ready-probe kernel constants (not YAML knobs -- provider-transparent, like
# ``_ready.py``'s ``READY_MAX_WAIT``). The probe runs ``true`` via the exec
# contract until the sandbox command plane is ready after resume.
READY_PROBE_MAX_ATTEMPTS = 5
READY_PROBE_TIMEOUT = 10  # seconds per attempt


class SandboxInfrastructureError(RuntimeError):
    """Sandbox transport/readiness failure that must stop the current slice."""


@dataclass(slots=True)
class StepResult:
    """Internal per-step record produced by :meth:`ReplayBaseRunner._run_slice`.

    ``exec_elapsed_sec`` isolates the exec wall time so P2's lifecycle overhead
    (``slice_total_sec - exec_elapsed_sec`` = ``resume_sec + pause_sec``) is
    computable without re-instrumenting the metrics. ``_run_slice`` times the
    resume / pause hooks and sums all three phases into ``slice_total_sec``; in
    P1 the hooks are no-ops so ``resume_sec`` / ``pause_sec`` are ~0 and
    ``slice_total_sec`` ~= ``exec_elapsed_sec``. P2 overrides the hooks with
    real lifecycle calls and the timings flow through -- no schema change.
    """

    step_index: int
    action_type: str
    exit_code: int
    exec_elapsed_sec: float
    slice_total_sec: float
    resume_sec: float
    pause_sec: float
    requested_delay_sec: float
    # P2.6 segment decomposition (sum to resume_sec / pause_sec respectively)
    resume_api_sec: float = 0.0
    resume_ready_wait_sec: float = 0.0
    slot_contention_wait_sec: float = 0.0
    resume_queue_wait_sec: float = 0.0
    pause_queue_wait_sec: float = 0.0
    pause_api_sec: float = 0.0
    # L7 decomposition (Phase 1): slot hold + full interaction budget.
    running_slot_held_sec: float = 0.0
    interaction_total_sec: float = 0.0


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
        *,
        series: LifecycleSeriesWriter | None = None,
        admission: Admission | None = None,
        launch_pacer: LaunchPacer | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.provider = provider
        self.series = series
        self.admission = admission
        self._prev_pause_end_monotonic: float | None = None
        # G5: shared no-catch-up launch pacer (trajectory mode). One LaunchPacer
        # instance is shared across the whole fleet by the spine so the
        # next-launch deadline is visible to every worker.
        self._launch_pacer = launch_pacer

    # --- the slice (the spine P2 plugs into) ---
    def _run_slice(self, step: ReplayStep, *, trajectory_id: str = "", lease_already_held: bool = False) -> StepResult:
        """One resume -> execute -> pause cycle.

        P2.6: slot.acquire -> resume(QPS) -> ready_probe -> exec -> pause(QPS) ->
        slot.release. Six segment durations (slot_contention / resume_queue /
        resume_api / resume_ready_wait / pause_queue / pause_api) written to the
        series + fed into ReplayMetrics. resume_sec/pause_sec totals preserved
        (sum of their segments). Lease release in finally so mid-slice exceptions
        don't leak the running slot.
        """
        # Acquire running slot (if admission configured AND no trajectory-level
        # lease already held). In trajectory mode _run_trajectory acquires the
        # lease once for the whole trajectory and passes lease_already_held=True
        # so _run_slice does NOT double-acquire.
        lease = None
        slot_contention_wait_sec = 0.0
        natural_delay_sec = 0.0
        capacity_wait_sec = 0.0
        slot_acquired_at = 0.0
        if self.admission is not None and not lease_already_held:
            ready_at = self._compute_ready_at(step)
            lease = self.admission.slots.acquire(f"sbx{self.state.index}_step{step.index}", ready_at=ready_at)
            natural_delay_sec = lease.natural_delay_sec
            capacity_wait_sec = lease.queue_wait_sec
            slot_contention_wait_sec = natural_delay_sec + capacity_wait_sec
            slot_acquired_at = lease.acquired_at

        try:
            # Resume phase (QPS-gated inside _resume with G3 retry). P1-9 un-folds
            # the queue wait from the API duration: _lifecycle_call_with_retry
            # returns (queue_wait, api) so resume_queue_wait_sec carries the real
            # QPS time-wait and resume_api_sec the pure call. The sum invariant
            # (resume_sec = queue + api + ready_wait) holds exactly.
            resume_start_ts = time.time()
            resume_queue_wait_sec, resume_api_sec = self._resume()

            # Ready probe (post-resume, not QPS-gated; config-gated + lifecycle/trajectory)
            resume_ready_wait_sec = 0.0
            if getattr(self.config, "replay_ready_probe", True) and self.config.replay_mode in (
                "lifecycle",
                "trajectory",
            ):
                resume_ready_wait_sec = self._probe_ready()

            # resume_sec = sum of segments (exact, no double-counting)
            resume_sec = resume_queue_wait_sec + resume_api_sec + resume_ready_wait_sec
            resume_end_ts = time.time()

            # Exec phase
            exec_start_ts = time.time()
            exec_start = time.perf_counter()
            result = self._execute(step)
            exec_end = time.perf_counter()
            exec_end_ts = time.time()
            exec_elapsed = exec_end - exec_start

            # Pause phase (QPS-gated inside _pause with G3 retry). P1-9 un-folds
            # the queue wait from the API duration, mirroring resume.
            pause_start_ts = time.time()
            pause_queue_wait_sec, pause_api_sec = self._pause()

            # pause_sec = sum of segments (exact, no double-counting)
            pause_sec = pause_queue_wait_sec + pause_api_sec
            pause_end_ts = time.time()

            sr = StepResult(
                step_index=step.index,
                action_type=step.action_type,
                exit_code=result.exit_code,
                exec_elapsed_sec=exec_elapsed,
                slice_total_sec=resume_sec + exec_elapsed + pause_sec,
                resume_sec=resume_sec,
                pause_sec=pause_sec,
                requested_delay_sec=step.delay_time_sec,
                resume_api_sec=resume_api_sec,
                resume_ready_wait_sec=resume_ready_wait_sec,
                slot_contention_wait_sec=slot_contention_wait_sec,
                resume_queue_wait_sec=resume_queue_wait_sec,
                pause_queue_wait_sec=pause_queue_wait_sec,
                pause_api_sec=pause_api_sec,
                running_slot_held_sec=((time.perf_counter() - slot_acquired_at) if slot_acquired_at else 0.0),
                interaction_total_sec=(
                    resume_sec
                    + exec_elapsed
                    + pause_sec
                    + step.delay_time_sec * self.config.replay_delay_scale
                    + natural_delay_sec
                    + capacity_wait_sec
                ),
            )
            # Success-path record. On exception _run_slice raises (no record here);
            # the caller's except block emits a slice_failed=True record instead.
            if self.series is not None:
                self.series.write(
                    {
                        "event": "step",
                        "sandbox_index": self.state.index,
                        "trajectory_id": trajectory_id,
                        "round_id": getattr(self, "round_id", None),
                        "step_index": step.index,
                        "action_type": step.action_type,
                        "resume_start": resume_start_ts,
                        "resume_end": resume_end_ts,
                        "exec_start": exec_start_ts,
                        "exec_end": exec_end_ts,
                        "pause_start": pause_start_ts,
                        "pause_end": pause_end_ts,
                        "resume_sec": resume_sec,
                        "exec_sec": exec_elapsed,
                        "pause_sec": pause_sec,
                        "slice_total_sec": sr.slice_total_sec,
                        "exit_code": result.exit_code,
                        "timed_out": False,
                        "slice_failed": False,
                        "slot_contention_wait_sec": slot_contention_wait_sec,
                        "resume_queue_wait_sec": resume_queue_wait_sec,
                        "resume_api_sec": resume_api_sec,
                        "resume_ready_wait_sec": resume_ready_wait_sec,
                        "pause_queue_wait_sec": pause_queue_wait_sec,
                        "pause_api_sec": pause_api_sec,
                        "running_slot_held_sec": sr.running_slot_held_sec,
                        "interaction_total_sec": sr.interaction_total_sec,
                    }
                )
            # Track pause-end for the next step's ready_at (G2).
            self._prev_pause_end_monotonic = time.perf_counter()
            return sr
        finally:
            # Release lease in finally so mid-slice exceptions don't leak the slot.
            if lease is not None:
                self.admission.slots.release(lease)

    def _compute_ready_at(self, step: ReplayStep) -> float | None:
        """G2: the monotonic timestamp this step becomes admissible.

        ``prev_pause_end_monotonic + step.delay_time_sec + replay_pause_duration_sec``.
        The first step in a worker has no preceding pause -> ``None`` (immediate
        admission, no pre-delay park). Splits natural think-time delay from
        capacity contention on the lease.
        """
        prev = self._prev_pause_end_monotonic
        if prev is None:
            return None
        extra = self.config.replay_pause_duration_sec or 0.0
        return prev + step.delay_time_sec * self.config.replay_delay_scale + extra

    # --- lifecycle hooks (lifecycle/trajectory: real call + G3 retry; exec_only: no-op) ---
    def _resume(self) -> tuple[float, float]:
        """Resume the sandbox (restore from snapshot) before exec.

        lifecycle/trajectory: real call with G3 transient retry. Each retry
        attempt is QPS-gated (re-enters the limiter). Returns
        ``(queue_wait_sec, api_sec)`` -- the QPS time-wait split from the pure
        API call. exec_only: no-op so the baseline (resume ~= 0) stays
        comparable.
        """
        if self.config.replay_mode not in ("lifecycle", "trajectory"):
            return 0.0, 0.0
        return self._lifecycle_call_with_retry("resume", lambda: self.provider.resume(self.state))

    def _pause(self) -> tuple[float, float]:
        """Pause the sandbox (memory-snapshot) after exec.

        lifecycle/trajectory: real call with G3 transient retry. Returns
        ``(queue_wait_sec, api_sec)``. exec_only: no-op.
        """
        if self.config.replay_mode not in ("lifecycle", "trajectory"):
            return 0.0, 0.0
        return self._lifecycle_call_with_retry("pause", lambda: self.provider.pause(self.state))

    def _series_write(self, record: dict) -> None:
        """Best-effort series write; a logging failure must never change control flow."""
        if self.series is None:
            return
        try:
            self.series.write(record)
        except Exception as e:  # noqa: BLE001 - logging best-effort
            logger.warning(f"[Sandbox{self.state.index}] series write failed: {str(e)[:80]}")

    def _lifecycle_call_with_retry(self, operation: str, fn) -> tuple[float, float]:
        """Run a lifecycle call with transient-error retry (G3) + structured
        retry events (Phase 2).

        Returns ``(queue_wait_sec, api_sec)``. Emits ``retry_queued`` /
        ``retry_recovered`` / ``retry_exhausted`` events to the series and
        advances the ReplayMetrics retry accumulators. Series writes are
        best-effort (never mask the lifecycle exception). ``ShutdownInterrupted``
        (BaseException) bypasses the ``except Exception`` retry path, so no
        ``retry_exhausted`` is emitted for a shutdown.
        """
        retries = getattr(self.config, "replay_lifecycle_retries", 0)
        total_queue_wait = 0.0
        total_api = 0.0
        had_transient = False
        last_exc: BaseException | None = None
        for attempt in range(retries + 1):
            try:
                if self.admission is not None and self.admission.qps is not None:
                    t_qps_start = time.perf_counter()
                    with self.admission.qps.slot(operation):
                        t_api_start = time.perf_counter()
                        total_queue_wait += t_api_start - t_qps_start
                        try:
                            fn()
                        finally:
                            total_api += time.perf_counter() - t_api_start
                else:
                    t_api_start = time.perf_counter()
                    try:
                        fn()
                    finally:
                        total_api += time.perf_counter() - t_api_start
                if had_transient:
                    self._series_write(
                        {
                            "event": "retry_recovered",
                            "sandbox_index": self.state.index,
                            "timestamp": time.time(),
                            "operation": operation,
                            "attempt": attempt + 1,
                            "total_queue_wait_sec": total_queue_wait,
                            "total_api_sec": total_api,
                        }
                    )
                return total_queue_wait, total_api
            except Exception as e:  # noqa: BLE001 - classify + retry/re-raise
                last_exc = e
                transient = is_transient_sandbox_error(e)
                if attempt >= retries or not transient:
                    self._series_write(
                        {
                            "event": "retry_exhausted",
                            "sandbox_index": self.state.index,
                            "timestamp": time.time(),
                            "operation": operation,
                            "attempt": attempt + 1,
                            "retryable": transient,
                            "error_type": type(e).__name__,
                            "error": str(e)[:120],
                        }
                    )
                    raise
                had_transient = True
                self._series_write(
                    {
                        "event": "retry_queued",
                        "sandbox_index": self.state.index,
                        "timestamp": time.time(),
                        "operation": operation,
                        "attempt": attempt + 1,
                        "max_attempts": retries + 1,
                        "error_type": type(e).__name__,
                        "error": str(e)[:120],
                        "accumulated_queue_wait_sec": total_queue_wait,
                        "accumulated_api_sec": total_api,
                    }
                )
                # Advance the report accumulator; time_lost = accumulated time across all attempts so far.
                self.state.replay_metrics.record_retry_event(
                    "retry_queued",
                    operation=operation,
                    time_lost_sec=total_queue_wait + total_api,
                )
                logger.warning(
                    f"[Sandbox{self.state.index}] {operation} transient error "
                    f"(attempt {attempt + 1}/{retries + 1}): {str(e)[:80]}; retrying"
                )
        if last_exc is not None:
            raise last_exc
        return 0.0, 0.0  # pragma: no cover - unreachable

    def _init_lifecycle(self) -> None:
        """One-time transition into the paused state before the first slice.

        Pre-pauses a running sandbox so step 1's resume is a true snapshot
        restore (not a reattach). Idempotent via ``state.lifecycle_paused`` so
        a fresh ReplayRoundRunner per round / multiple trajectories don't
        double-pause. exec_only: no-op. The initial-pause cost is recorded
        separately (``initial_pause_sec``), never folded into a per-step
        ``resume_sec``.

        trajectory mode intentionally skips the initial pause (guard is
        ``== "lifecycle"``): each trajectory ``create_one`` yields a fresh
        sandbox, so the first ``_resume`` is whatever the provider defines for
        a freshly-created sandbox (the contract is that ``create_one`` returns
        a sandbox ready for ``resume``; whether resume is a no-op or a real
        restore is provider-defined and out of scope for the runner).
        """
        if self.config.replay_mode == "lifecycle" and not self.state.lifecycle_paused:
            pause_start_ts = time.time()
            t0 = time.perf_counter()
            self.provider.pause(self.state)
            pause_elapsed = time.perf_counter() - t0
            pause_end_ts = time.time()
            self.state.replay_metrics.initial_pause_sec = pause_elapsed
            self.state.lifecycle_paused = True
            if self.series is not None:
                self.series.write(
                    {
                        "event": "initial_pause",
                        "sandbox_index": self.state.index,
                        "pause_start": pause_start_ts,
                        "pause_end": pause_end_ts,
                        "initial_pause_sec": pause_elapsed,
                    }
                )
            logger.info(
                f"[Sandbox{self.state.index}] lifecycle initial pause "
                f"{self.state.replay_metrics.initial_pause_sec:.3f}s"
            )

    def _probe_ready(self) -> float:
        """Run ``true`` until the sandbox command plane is ready. Not QPS-gated.

        Returns total wall time of the probe loop. Exhaustion raises
        SandboxInfrastructureError -> the caller synthesizes a failed slice.
        """
        started = time.perf_counter()
        for _ in range(READY_PROBE_MAX_ATTEMPTS):
            try:
                result = self.provider.exec(self.state, "true", timeout=READY_PROBE_TIMEOUT)
                if result.exit_code == 0:
                    return time.perf_counter() - started
            except Exception as e:
                logger.debug(f"[Sandbox{self.state.index}] ready-probe attempt failed: {e}")
        raise SandboxInfrastructureError(
            f"Sandbox {self.state.index} command service did not become ready "
            f"after {READY_PROBE_MAX_ATTEMPTS} attempts"
        )

    def _execute(self, step: ReplayStep) -> CommandResult:
        """Exec the recorded action verbatim -- cwd/env via the exec contract.

        G1: in lifecycle/trajectory mode the exec dispatch is QPS-gated under the
        ``"command"`` bucket but with ``hold_inflight=False`` -- the QPS time-wait
        smooths the burst of command dispatches, but no inflight permit is held
        during the body. The reference's ``command_start_slot`` rate-limits only
        stream establishment and releases before the command body runs; bench_core
        has no stream handle (``provider.exec`` is a monolithic blocking RPC), so
        holding an inflight permit for the whole body would serialize long
        commands behind the cap. exec_only is ungated (no admission controller).
        """
        if self.admission is not None and self.admission.qps is not None:
            with self.admission.qps.slot("command", hold_inflight=False):
                return self.provider.exec(
                    self.state,
                    step.action,
                    cwd=self.config.replay_workdir or None,
                    env=self.config.replay_env or None,
                    timeout=self.config.replay_action_timeout,
                )
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
        trajectory_id: str = "",
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
            resume_sec=step_result.resume_sec,
            pause_sec=step_result.pause_sec,
            slice_total_sec=step_result.slice_total_sec,
            resume_api_sec=step_result.resume_api_sec,
            resume_ready_wait_sec=step_result.resume_ready_wait_sec,
            slot_contention_wait_sec=step_result.slot_contention_wait_sec,
            pause_api_sec=step_result.pause_api_sec,
            resume_queue_wait_sec=step_result.resume_queue_wait_sec,
            running_slot_held_sec=step_result.running_slot_held_sec,
            interaction_total_sec=step_result.interaction_total_sec,
            create_sec=0.0,  # populated by _run_trajectory (trajectory mode)
            kill_sec=0.0,
        )
        self.state.update_last_task_time(time.time())

    def _sleep_delay(self, step: ReplayStep) -> None:
        """Honour the recorded think-time gap, scaled. First step's delay applies."""
        gap = step.delay_time_sec * self.config.replay_delay_scale
        if gap > 0 and not self.stop_event.is_set():
            self.stop_event.wait(gap)

    # --- trajectory mode (G8): per-trajectory create -> steps -> kill ---
    def _wait_for_launch_turn(self) -> None:
        """G5: no-catch-up pacing of trajectory starts.

        ``next_launch_at = max(now, next_launch_at) + interval`` -- never let
        multiple workers start in the same instant to "catch up". The shared
        :class:`LaunchPacer` owns the lock + deadline cell so every worker in
        the fleet sees the same advancing deadline. No-op when the interval is
        0 or no pacer is wired (the spine wires one for trajectory mode).
        """
        if self._launch_pacer is None:
            return
        interval = getattr(self.config, "replay_launch_interval_sec", 0.0) or 0.0
        if interval <= 0:
            return
        wait_until = self._launch_pacer.claim_turn(interval)
        delay = wait_until - time.monotonic()
        if delay > 0 and not self.stop_event.is_set():
            self.stop_event.wait(delay)

    def _run_trajectory(self, traj: Trajectory) -> None:
        """Per-trajectory create -> (resume->exec->pause steps) -> kill loop (G8).

        One running-slot lease spans the whole trajectory: acquired before
        ``create_one``, held through every step, released in ``finally`` after
        ``kill_one``. ``_run_slice`` is called with ``lease_already_held=True``
        so it does NOT double-acquire. ``running_slot_held_sec`` is the
        trajectory-level lease hold (release_monotonic - acquired_at), overlaid
        on the last recorded step in ``finally`` -- ``_run_slice``'s per-step
        slot_held is 0 in trajectory mode (``lease_already_held`` ->
        ``slot_acquired_at=0``). exec_only/lifecycle (non-EphemeralCapable)
        skip create/kill + the per-trajectory lease.
        """
        is_ephemeral = isinstance(self.provider, EphemeralCapable)
        lease = None
        create_sec = 0.0
        kill_sec = 0.0
        lease_acquired_at = 0.0
        self._prev_pause_end_monotonic = None  # first step: ready_at=None (G2)
        if is_ephemeral:
            self._wait_for_launch_turn()
        # The outer try/finally owns the trajectory-level lease release so a
        # BaseException (ShutdownInterrupted from the create/step/cleanup QPS
        # slots, or KeyboardInterrupt) still releases the lease -- the lease is
        # acquired inside this try, so the finally always sees the current state.
        try:
            if is_ephemeral:
                if self.admission is not None:
                    lease = self.admission.slots.acquire(f"sbx{self.state.index}_{traj.instance_id}")
                    lease_acquired_at = lease.acquired_at
                try:
                    # G1: create under the 'create' QPS bucket; G3: single call, no retry.
                    t0 = time.perf_counter()
                    if self.admission is not None and self.admission.qps is not None:
                        with self.admission.qps.slot("create"):
                            self.provider.create_one(self.state.index, metadata={"trajectory_id": traj.instance_id})
                    else:
                        self.provider.create_one(self.state.index, metadata={"trajectory_id": traj.instance_id})
                    create_sec = time.perf_counter() - t0
                    # Post-create ready probe (lifecycle concern; not QPS-gated).
                    if getattr(self.config, "replay_ready_probe", True) and self.config.replay_mode in (
                        "lifecycle",
                        "trajectory",
                    ):
                        self._probe_ready()
                except Exception as e:
                    logger.error(
                        f"[Sandbox{self.state.index}] trajectory {traj.instance_id} create failed: {str(e)[:120]}"
                    )
                    self._record_trajectory_failure(traj, create_sec=create_sec, kill_sec=0.0)
                    return  # lease released by the outer finally
            self._init_lifecycle()
            prev_slice_end = time.perf_counter()
            completed = True
            for step in traj.steps:
                if self.stop_event.is_set() or not self.state.is_alive:
                    completed = False
                    break
                self._sleep_delay(step)
                if self.stop_event.is_set():
                    completed = False
                    break
                actual_delay = time.perf_counter() - prev_slice_end
                timed_out = False
                try:
                    sr = self._run_slice(step, trajectory_id=traj.instance_id, lease_already_held=lease is not None)
                except Exception as e:
                    msg = str(e).lower()
                    timed_out = "timed out" in msg or "context deadline exceeded" in msg
                    sr = self._failed_step_result(step)
                    if self.series is not None:
                        self.series.write(self._failed_series_record(step, traj.instance_id, timed_out))
                    logger.error(f"[Sandbox{self.state.index}] step {step.index} exception: {msg[:120]}")
                prev_slice_end = time.perf_counter()
                self._record_step(
                    sr,
                    timed_out=timed_out,
                    actual_delay=actual_delay,
                    trajectory_complete=False,
                    trajectory_id=traj.instance_id,
                )
                if sr.exit_code != 0 or timed_out:
                    if self.config.replay_stop_on_error:
                        completed = False
                        break
            # Mirror the non-trajectory paths: a trajectory whose every step
            # ran to the end (no stop_event / offline / stop_on_error abort)
            # counts as one completion, regardless of kill outcome.
            if completed and not self.stop_event.is_set():
                self.state.replay_metrics._mark_completion()
            if is_ephemeral:
                # G1: kill under the 'cleanup' QPS bucket.
                t0 = time.perf_counter()
                try:
                    if self.admission is not None and self.admission.qps is not None:
                        with self.admission.qps.slot("cleanup"):
                            self.provider.kill_one(self.state)
                    else:
                        self.provider.kill_one(self.state)
                    kill_sec = time.perf_counter() - t0
                except Exception as e:
                    logger.warning(f"[Sandbox{self.state.index}] kill failed: {str(e)[:80]}; lease still released")
                    kill_sec = time.perf_counter() - t0
        finally:
            if lease is not None:
                self.admission.slots.release(lease)
            # Overlay trajectory-level durations (create/kill/slot-held) on the
            # last recorded step. These span the whole trajectory, not a single
            # step, so they can't be measured per-step; attach them to the last
            # successful step's entries (lists are length-aligned to slice_total>0
            # steps). Skipped when no step recorded (all-failed trajectory -- the
            # create cost is lost on the failure-exclusion path; acceptable in
            # Phase 1).
            if lease_acquired_at:
                held = time.perf_counter() - lease_acquired_at
                m = self.state.replay_metrics
                with m._lock:
                    if m._create_secs:
                        m._create_secs[-1] = create_sec
                        m._kill_secs[-1] = kill_sec
                        m._running_slot_held_secs[-1] = held

    def _failed_step_result(self, step: ReplayStep) -> StepResult:
        """Synthesize a zero-duration failed StepResult for an exception path."""
        return StepResult(
            step_index=step.index,
            action_type=step.action_type,
            exit_code=1,
            exec_elapsed_sec=0.0,
            slice_total_sec=0.0,
            resume_sec=0.0,
            pause_sec=0.0,
            requested_delay_sec=step.delay_time_sec,
        )

    def _failed_series_record(self, step: ReplayStep, trajectory_id: str, timed_out: bool) -> dict:
        """Series record for a step that threw (slice_failed=True, honestly zeroed)."""
        return {
            "event": "step",
            "sandbox_index": self.state.index,
            "trajectory_id": trajectory_id,
            "round_id": getattr(self, "round_id", None),
            "step_index": step.index,
            "action_type": step.action_type,
            "resume_start": 0.0,
            "resume_end": 0.0,
            "exec_start": 0.0,
            "exec_end": 0.0,
            "pause_start": 0.0,
            "pause_end": 0.0,
            "resume_sec": 0.0,
            "exec_sec": 0.0,
            "pause_sec": 0.0,
            "slice_total_sec": 0.0,
            "exit_code": 1,
            "timed_out": timed_out,
            "slice_failed": True,
            "slot_contention_wait_sec": 0.0,
            "resume_queue_wait_sec": 0.0,
            "resume_api_sec": 0.0,
            "resume_ready_wait_sec": 0.0,
            "pause_queue_wait_sec": 0.0,
            "pause_api_sec": 0.0,
            "running_slot_held_sec": 0.0,
            "interaction_total_sec": 0.0,
        }

    def _record_trajectory_failure(self, traj: Trajectory, *, create_sec: float, kill_sec: float) -> None:
        """Record a failed trajectory (create failed before any step)."""
        if self.series is not None:
            self.series.write(
                {
                    "event": "trajectory_failed",
                    "sandbox_index": self.state.index,
                    "trajectory_id": traj.instance_id,
                    "create_sec": create_sec,
                    "kill_sec": kill_sec,
                }
            )
        self.state.replay_metrics.last_error = "trajectory create failed"
        self.state.consecutive_failures += 1
        if self.state.consecutive_failures >= 3:
            self.state.is_alive = False


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
        *,
        series: LifecycleSeriesWriter | None = None,
        admission: Admission | None = None,
        launch_pacer: LaunchPacer | None = None,
    ) -> None:
        super().__init__(
            state, config, stop_event, provider, series=series, admission=admission, launch_pacer=launch_pacer
        )
        self.consecutive_errors = 0

    def run(self) -> None:
        if not self.state.ready:
            logger.warning(f"[Sandbox{self.state.index}] Cannot start replay: not ready")
            return

        pool = load_pool(self.config)
        if not pool:
            logger.info(f"[Sandbox{self.state.index}] Replay pool empty; exiting")
            return

        logger.info(f"[Sandbox{self.state.index}] Replay task runner started ({len(pool)} trajectories)")

        if self.config.replay_mode == "trajectory":
            # Shared trajectory queue: cycle the pool to total_count trajectories.
            # _run_trajectory owns per-trajectory lifecycle setup (create/kill);
            # _init_lifecycle is a no-op in trajectory mode (skips initial pause),
            # so it is only called in the non-trajectory branch below.
            target = self.config.total_count
            run = 0
            cursor = self.state.index % len(pool)
            while not self.stop_event.is_set() and self.state.is_alive and run < target:
                traj = pool[cursor]
                try:
                    self._run_trajectory(traj)
                except ShutdownInterrupted:
                    break
                cursor = (cursor + 1) % len(pool)
                run += 1
        else:
            self._init_lifecycle()
            cursor = self.state.index % len(pool)
            while not self.stop_event.is_set() and self.state.is_alive:
                traj = pool[cursor]
                try:
                    self._replay_trajectory(traj)
                except ShutdownInterrupted:
                    break
                cursor = (cursor + 1) % len(pool)

        logger.info(f"[Sandbox{self.state.index}] Replay task runner ended")

    def _replay_trajectory(self, traj: Trajectory) -> None:
        """Fixed-mode (non-trajectory) per-step loop. Trajectory mode uses _run_trajectory."""
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
                sr = self._run_slice(step, trajectory_id=traj.instance_id)
            except Exception as e:
                msg = str(e).lower()
                timed_out = "timed out" in msg or "context deadline exceeded" in msg
                sr = self._failed_step_result(step)
                if self.series is not None:
                    self.series.write(self._failed_series_record(step, traj.instance_id, timed_out))
                logger.error(f"[Sandbox{self.state.index}] Replay step {step.index} exception: {msg[:120]}")
            prev_slice_end = time.perf_counter()
            self._record_step(
                sr,
                timed_out=timed_out,
                actual_delay=actual_delay,
                trajectory_complete=False,
                trajectory_id=traj.instance_id,
            )

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
        *,
        series: LifecycleSeriesWriter | None = None,
        admission: Admission | None = None,
        launch_pacer: LaunchPacer | None = None,
    ) -> None:
        super().__init__(
            state, config, stop_event, provider, series=series, admission=admission, launch_pacer=launch_pacer
        )
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

        try:
            if self.config.replay_mode == "trajectory":
                self._run_trajectory(traj)
            else:
                self._init_lifecycle()
                self._replay_round_loop(traj)
        except ShutdownInterrupted:
            pass  # benchmark shutdown mid-slice; round ends cleanly
        logger.info(f"[Sandbox{self.state.index}] Replay round {self.round_id} done")

    def _replay_round_loop(self, traj: Trajectory) -> None:
        """Per-step loop for non-trajectory round-robin mode (one trajectory)."""
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
                sr = self._run_slice(step, trajectory_id=traj.instance_id)
            except Exception as e:
                msg = str(e).lower()
                timed_out = "timed out" in msg or "context deadline exceeded" in msg
                sr = self._failed_step_result(step)
                if self.series is not None:
                    self.series.write(self._failed_series_record(step, traj.instance_id, timed_out))
                logger.error(f"[Sandbox{self.state.index}] Replay step {step.index} exception: {msg[:120]}")
            prev_slice_end = time.perf_counter()
            self._record_step(
                sr,
                timed_out=timed_out,
                actual_delay=actual_delay,
                trajectory_complete=False,
                trajectory_id=traj.instance_id,
            )
            if (sr.exit_code != 0 or timed_out) and self.config.replay_stop_on_error:
                aborted = True
                break

        if not aborted and not self.stop_event.is_set():
            self.state.replay_metrics._mark_completion()
