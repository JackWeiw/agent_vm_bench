"""Tests for the trajectory replay task runners (host-agnostic).

Step 1 of the TDD cycle for Task 8: the slice base (ReplayBaseRunner).
The test is written before the implementation -- it should fail with
``ModuleNotFoundError`` until ``bench_core.task_runner.replay`` exists.
"""
from __future__ import annotations

import threading

from bench_core.config import KernelConfig
from bench_core.replay_payload import ReplayStep
from bench_core.schemas import BenchSandbox
from env_provider import CommandResult
from env_provider.fake import FakeProvider


def _make_state() -> BenchSandbox:
    return BenchSandbox(id="fake-0", index=0, workflow_type="replay")


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
    # P1 floor: resume/pause are no-ops.
    assert result.exit_code == 0
    assert result.resume_sec == 0.0
    assert result.pause_sec == 0.0
    assert result.slice_total_sec == result.exec_elapsed_sec
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
