"""Replay runner lifecycle tests: e2e lifecycle, exec_only regression,
_init_lifecycle idempotency, and warmup-stays-exec-only.
"""
from __future__ import annotations

import threading
from pathlib import Path

from bench_core.bench import run_benchmark
from bench_core.config import KernelConfig
from bench_core.schemas import BenchSandbox
from bench_core.task_runner.replay import (
    ReplayRoundRunner,
    ReplayWarmupRunner,
)
from env_provider import SandboxInstance
from env_provider.tests.lifecycle_fake import FakeLifecycleProvider

REPLAY_FIXTURES = Path(__file__).parent / "fixtures" / "replay"


def _lifecycle_config(tmp_path, **kw):
    base = dict(
        workflow_type="replay",
        total_count=1,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(REPLAY_FIXTURES),
        replay_mode="lifecycle",
        replay_delay_scale=0.0,
        output_dir=str(tmp_path),
        filename_prefix="lc",
    )
    base.update(kw)
    return KernelConfig(**base)


class TestLifecycleE2E:
    def test_lifecycle_e2e_records_resume_pause_and_initial_pause(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        result = run_benchmark(config, provider)

        assert "Replay Task Statistics" in result["report"]
        assert "Initial Pause" in result["report"]
        # lifecycle hooks fired: resume + pause per step, plus one initial pause.
        assert provider.pause_calls >= 1
        assert provider.resume_calls >= 1


class TestExecOnlyRegression:
    def test_exec_only_does_not_call_lifecycle_hooks(self, tmp_path):
        config = _lifecycle_config(tmp_path, replay_mode="exec_only", filename_prefix="eo")
        provider = FakeLifecycleProvider(count=1)
        result = run_benchmark(config, provider)

        assert "Replay Task Statistics" in result["report"]
        # exec_only: no pause/resume at all (not even the initial pause).
        assert provider.pause_calls == 0
        assert provider.resume_calls == 0


class TestInitLifecycleIdempotency:
    def test_init_lifecycle_pauses_once_across_calls(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=provider)

        runner._init_lifecycle()
        first_pause = provider.pause_calls
        first_initial = state.replay_metrics.initial_pause_sec
        assert first_pause == 1
        assert first_initial > 0
        assert state.lifecycle_paused is True

        # Second call no-ops (guard on lifecycle_paused).
        runner._init_lifecycle()
        assert provider.pause_calls == 1
        # initial_pause_sec not overwritten (still the first value).
        assert state.replay_metrics.initial_pause_sec == first_initial


class TestWarmupStaysExecOnly:
    def test_warmup_runner_does_not_pause_or_resume(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        state.ready = True
        runner = ReplayWarmupRunner(state, config, provider)
        runner.run()

        # Warmup probes exec directly; lifecycle hooks must NOT fire.
        assert provider.pause_calls == 0
        assert provider.resume_calls == 0
