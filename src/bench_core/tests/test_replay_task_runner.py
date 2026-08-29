"""Tests for the trajectory replay task runners (host-agnostic).

Step 1 of the TDD cycle for Task 8: the slice base (ReplayBaseRunner).
The test is written before the implementation -- it should fail with
``ModuleNotFoundError`` until ``bench_core.task_runner.replay`` exists.
"""
from __future__ import annotations

import threading
import time

from bench_core.config import KernelConfig
from bench_core.replay_payload import ReplayStep
from bench_core.schemas import BenchSandbox
from env_provider import CommandResult
from env_provider.fake import FakeProvider


def _make_state() -> BenchSandbox:
    return BenchSandbox(id="fake-0", index=0, workflow_type="replay", ready=True)


def test_slice_exec_verbatim_with_cwd_and_env():
    from bench_core.task_runner.replay import ReplayBaseRunner

    provider = FakeProvider(count=1)
    captured: list[dict] = []

    class _SpyProvider(FakeProvider):
        def exec(self, inst, command, *, timeout=None, cwd=None, env=None):
            captured.append({"command": command, "cwd": cwd, "env": env})
            return CommandResult(exit_code=0, stdout="ok\n", stderr="")

    spy = _SpyProvider(count=1)
    config = KernelConfig(workflow_type="replay", replay_workdir="/testbed", replay_env={"PAGER": "cat"})
    runner = ReplayBaseRunner(_make_state(), config, threading.Event(), spy)

    step = ReplayStep(index=0, action="find /testbed -name x", delay_time_sec=1.0, action_type="shell")
    result = runner._run_slice(step)

    # exec verbatim — no bash -lc wrap; cwd/env passed as first-class params.
    assert captured[0]["command"] == "find /testbed -name x"
    assert captured[0]["cwd"] == "/testbed"
    assert captured[0]["env"] == {"PAGER": "cat"}
    # P1 floor: resume/pause are no-ops (~0 measured overhead).
    assert result.exit_code == 0
    assert result.resume_sec < 0.01
    assert result.pause_sec < 0.01
    assert result.slice_total_sec >= result.exec_elapsed_sec
    assert result.requested_delay_sec == 1.0


def test_warmup_loads_pool_and_probes_exec():
    from bench_core.task_runner.replay import ReplayWarmupRunner
    from bench_core.replay_payload import reset_pool_cache

    reset_pool_cache()
    config = KernelConfig(
        workflow_type="replay",
        replay_trajectory_dir=str(__import__("pathlib").Path(__file__).parent / "fixtures" / "replay"),
        replay_trajectory_glob="*",
    )
    state = _make_state()
    runner = ReplayWarmupRunner(state, config, FakeProvider(count=1))
    runner.run()
    assert state.warmup_done is True


def test_fixed_runner_replays_pool_and_advances_cursor():
    from bench_core.task_runner.replay import ReplayTaskRunner
    from bench_core.replay_payload import reset_pool_cache

    reset_pool_cache()
    config = KernelConfig(
        workflow_type="replay",
        replay_trajectory_dir=str(__import__("pathlib").Path(__file__).parent / "fixtures" / "replay"),
        replay_trajectory_glob="no_terminal.json",  # one trajectory, 2 steps
        replay_delay_scale=0.0,  # no real sleep in tests
    )
    state = _make_state()
    stop = threading.Event()

    def _stop_after_some_steps():
        import time as _t

        _t.sleep(0.3)
        stop.set()

    threading.Thread(target=_stop_after_some_steps).start()

    runner = ReplayTaskRunner(state, config, stop, FakeProvider(count=1))
    runner.run()

    # 2 steps replayed, both succeeded on FakeProvider (exit 0).
    assert state.replay_metrics.total_tasks >= 2
    assert state.replay_metrics.success_count >= 2
    assert state.replay_metrics.trajectory_completions >= 1


def test_fixed_runner_stop_on_error_advances_to_next_trajectory():
    from bench_core.task_runner.replay import ReplayTaskRunner
    from bench_core.replay_payload import reset_pool_cache
    from env_provider import CommandResult

    reset_pool_cache()

    # Force every exec to fail (exit 1).
    class _FailProvider(FakeProvider):
        def exec(self, inst, command, *, timeout=None, cwd=None, env=None):
            return CommandResult(exit_code=1, stdout="", stderr="boom")

    config = KernelConfig(
        workflow_type="replay",
        replay_trajectory_dir=str(__import__("pathlib").Path(__file__).parent / "fixtures" / "replay"),
        replay_trajectory_glob="no_terminal.json",
        replay_delay_scale=0.0,
        replay_stop_on_error=True,
    )
    state = _make_state()
    stop = threading.Event()

    def _stop_soon():
        import time as _t

        _t.sleep(0.3)
        stop.set()

    threading.Thread(target=_stop_soon).start()

    runner = ReplayTaskRunner(state, config, stop, _FailProvider(count=1))
    runner.run()
    # stop_on_error aborts the trajectory on the first failing step, so no
    # trajectory completes, but the runner kept running (did not crash).
    assert state.replay_metrics.trajectory_completions == 0
    assert state.replay_metrics.failed_count >= 1


def test_round_runner_replays_one_trajectory_per_round():
    from bench_core.task_runner.replay import ReplayRoundRunner
    from bench_core.replay_payload import reset_pool_cache

    reset_pool_cache()
    config = KernelConfig(
        workflow_type="replay",
        replay_trajectory_dir=str(__import__("pathlib").Path(__file__).parent / "fixtures" / "replay"),
        replay_trajectory_glob="*",  # 2 valid trajectories in fixtures
        replay_delay_scale=0.0,
    )
    state = _make_state()  # index=0 -> (0 + 0) % 2 = 0
    stop = threading.Event()
    runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=FakeProvider(count=1))
    runner.run()
    # The chosen trajectory has either 2 steps (no_terminal) -> replayed fully.
    assert state.replay_metrics.total_tasks >= 2
    assert state.replay_metrics.trajectory_completions == 1


def test_round_runner_index_rotation_picks_different_trajectory():
    from bench_core.task_runner.replay import ReplayRoundRunner
    from bench_core.replay_payload import reset_pool_cache

    reset_pool_cache()
    config = KernelConfig(
        workflow_type="replay",
        replay_trajectory_dir=str(__import__("pathlib").Path(__file__).parent / "fixtures" / "replay"),
        replay_trajectory_glob="*",
        replay_delay_scale=0.0,
    )
    stop = threading.Event()
    # sandbox index=1, round_id=0 -> (1 + 0) % 2 = 1 -> the other trajectory
    state = BenchSandbox(id="fake-1", index=1, workflow_type="replay", ready=True)
    runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=FakeProvider(count=1))
    runner.run()
    # the with_terminal trajectory has 2 executable steps.
    assert state.replay_metrics.total_tasks >= 2


def test_run_slice_p1_noop_hooks_measure_near_zero():
    """P1 baseline: no-op _resume/_pause add no lifecycle overhead; slice ~= exec."""
    from bench_core.task_runner.replay import ReplayBaseRunner

    config = KernelConfig(workflow_type="replay")
    state = _make_state()
    stop_event = threading.Event()
    provider = FakeProvider(count=1)
    runner = ReplayBaseRunner(state, config, stop_event, provider)
    step = ReplayStep(index=0, action="echo hi", delay_time_sec=0.0, action_type="shell")

    sr = runner._run_slice(step)

    assert sr.exit_code == 0
    assert sr.resume_sec < 0.01  # no-op hook ~0
    assert sr.pause_sec < 0.01
    assert sr.slice_total_sec >= sr.exec_elapsed_sec  # slice includes exec + ~0 hooks


def test_run_slice_p2_override_flows_timing_through_hooks():
    """P2 pluggability: overriding only _resume/_pause (not _run_slice) flows
    lifecycle timings into resume_sec / pause_sec / slice_total_sec."""
    from bench_core.task_runner.replay import ReplayBaseRunner

    class _LifecycleRunner(ReplayBaseRunner):
        RESUME_DUR = 0.02
        PAUSE_DUR = 0.02

        def _resume(self) -> tuple[float, float]:
            t = time.perf_counter()
            time.sleep(self.RESUME_DUR)
            return 0.0, time.perf_counter() - t

        def _pause(self) -> tuple[float, float]:
            t = time.perf_counter()
            time.sleep(self.PAUSE_DUR)
            return 0.0, time.perf_counter() - t

    config = KernelConfig(workflow_type="replay")
    state = _make_state()
    stop_event = threading.Event()
    provider = FakeProvider(count=1)
    runner = _LifecycleRunner(state, config, stop_event, provider)
    step = ReplayStep(index=0, action="echo hi", delay_time_sec=0.0, action_type="shell")

    sr = runner._run_slice(step)

    assert sr.exit_code == 0
    # The override's timing flowed through WITHOUT overriding _run_slice:
    assert sr.resume_sec >= _LifecycleRunner.RESUME_DUR * 0.5  # tolerant lower bound
    assert sr.pause_sec >= _LifecycleRunner.PAUSE_DUR * 0.5
    assert sr.slice_total_sec >= sr.resume_sec + sr.pause_sec
