"""Replay runner lifecycle tests: e2e lifecycle, exec_only regression,
_init_lifecycle idempotency, and warmup-stays-exec-only.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bench_core.bench import run_benchmark
from bench_core.config import KernelConfig
from bench_core.lifecycle_series import LifecycleSeriesWriter
from bench_core.schemas import BenchSandbox
from bench_core.task_runner.replay import (
    ReplayBaseRunner,
    ReplayRoundRunner,
    ReplayTaskRunner,
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


def _series_path(tmp_path):
    return tmp_path / "series.jsonl"


class TestRunSliceSeriesRecord:
    def test_run_slice_writes_step_record_with_six_timestamps(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        inst = SandboxInstance(id="x", index=2)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        state.ready = True
        path = _series_path(tmp_path)
        series = LifecycleSeriesWriter(path)
        runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=provider, series=series)
        runner._init_lifecycle()

        # drive one slice via the real _run_slice using a fixture step
        from bench_core.replay_payload import ReplayStep

        step = ReplayStep(index=3, action_type="shell", action="true", delay_time_sec=0.0)
        sr = runner._run_slice(step, trajectory_id="traj-abc")
        series.close()

        lines = path.read_text().splitlines()
        # one initial_pause + one step record
        events = [json.loads(l) for l in lines]
        step_records = [r for r in events if r["event"] == "step"]
        assert len(step_records) == 1
        rec = step_records[0]
        assert rec["sandbox_index"] == 2
        assert rec["trajectory_id"] == "traj-abc"
        assert rec["round_id"] == 0
        assert rec["step_index"] == 3
        for f in ("resume_start", "resume_end", "exec_start", "exec_end", "pause_start", "pause_end"):
            assert f in rec and isinstance(rec[f], float)
        # monotonic boundary invariant
        assert rec["resume_end"] >= rec["resume_start"]
        assert rec["exec_start"] >= rec["resume_end"]
        assert rec["exec_end"] >= rec["exec_start"]
        assert rec["pause_start"] >= rec["exec_end"]
        assert rec["pause_end"] >= rec["pause_start"]
        assert rec["slice_failed"] is False

    def test_init_lifecycle_writes_initial_pause_record(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        inst = SandboxInstance(id="x", index=1)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        path = _series_path(tmp_path)
        series = LifecycleSeriesWriter(path)
        runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=provider, series=series)
        runner._init_lifecycle()
        series.close()

        lines = path.read_text().splitlines()
        recs = [json.loads(l) for l in lines]
        init = [r for r in recs if r["event"] == "initial_pause"]
        assert len(init) == 1
        assert init[0]["sandbox_index"] == 1
        assert init[0]["pause_end"] >= init[0]["pause_start"]
        assert init[0]["initial_pause_sec"] > 0

    def test_synthesized_failure_path_is_in_caller(self, tmp_path):
        # A slice that throws -> _run_slice raises; the caller (_replay_trajectory)
        # synthesizes the zero StepResult. _run_slice itself does NOT catch.
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)

        class _BoomRunner(ReplayBaseRunner):
            def _resume(self):
                raise RuntimeError("resume blew up")

        stop = threading.Event()
        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        path = _series_path(tmp_path)
        series = LifecycleSeriesWriter(path)
        runner = _BoomRunner(state, config, stop, provider, series=series)
        from bench_core.replay_payload import ReplayStep

        step = ReplayStep(index=0, action_type="shell", action="true", delay_time_sec=0.0)
        with pytest.raises(RuntimeError):
            runner._run_slice(step, trajectory_id="t")
        series.close()

    def test_synthesized_failure_emits_slice_failed_record(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)

        class _BoomRunner(ReplayRoundRunner):
            def _resume(self):
                raise RuntimeError("resume blew up")

        stop = threading.Event()
        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        state.ready = True
        path = _series_path(tmp_path)
        series = LifecycleSeriesWriter(path)
        runner = _BoomRunner(state, config, stop, round_id=0, provider=provider, series=series)
        runner.run()
        series.close()

        recs = [json.loads(l) for l in path.read_text().splitlines()]
        failed = [r for r in recs if r.get("event") == "step" and r.get("slice_failed")]
        assert len(failed) >= 1
        assert failed[0]["exit_code"] == 1
        assert failed[0]["slice_failed"] is True


class TestRecordStepPassesDurations:
    def test_record_step_feeds_resume_pause_slice_into_metrics(self, tmp_path):
        from bench_core.task_runner.replay import StepResult

        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=provider)
        sr = StepResult(
            step_index=0,
            action_type="shell",
            exit_code=0,
            exec_elapsed_sec=1.0,
            slice_total_sec=1.5,
            resume_sec=0.2,
            pause_sec=0.3,
            requested_delay_sec=0.0,
        )
        runner._record_step(sr, timed_out=False, actual_delay=0.0, trajectory_complete=False, trajectory_id="t")
        assert state.replay_metrics.resume_secs == [0.2]
        assert state.replay_metrics.pause_secs == [0.3]
        assert state.replay_metrics.slice_total_secs == [1.5]


class TestExecOnlySkipsSeries:
    def test_exec_only_runner_series_arg_is_none(self, tmp_path):
        # exec_only: a runner built without series must not touch any file.
        config = _lifecycle_config(tmp_path, replay_mode="exec_only", filename_prefix="eo")
        provider = FakeLifecycleProvider(count=1)
        stop = threading.Event()
        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        runner = ReplayRoundRunner(state, config, stop, round_id=0, provider=provider)
        assert runner.series is None


class TestLifecycleOverheadReport:
    def test_lifecycle_report_has_overhead_section(self, tmp_path):
        config = _lifecycle_config(tmp_path)
        provider = FakeLifecycleProvider(count=1)
        result = run_benchmark(config, provider)
        report = result["report"]
        assert "[Lifecycle Overhead]" in report
        assert "Resume:" in report
        assert "Pause:" in report
        assert "Overhead per-sample" in report
        assert "Overhead aggregate" in report

    def test_exec_only_report_has_no_overhead_section(self, tmp_path):
        config = _lifecycle_config(tmp_path, replay_mode="exec_only", filename_prefix="eo")
        provider = FakeLifecycleProvider(count=1)
        result = run_benchmark(config, provider)
        assert "[Lifecycle Overhead]" not in result["report"]

    def test_overhead_near_zero_guard_drops_tiny_slices(self, tmp_path):
        # Manually seed ReplayMetrics with one tiny slice (< MIN_SLICE_SEC)
        # and one normal slice; the tiny one must not explode the per-sample
        # overhead (excluded by the guard).
        from bench_core.stats_collector import StatsCollector

        inst = SandboxInstance(id="x", index=0)
        state = BenchSandbox.from_instance(inst, workflow_type="replay")
        config = _lifecycle_config(tmp_path)
        sc = StatsCollector(config, {0: state}, "fake")
        m = state.replay_metrics
        # tiny slice (below MIN_SLICE_SEC=0.001) -- excluded from overhead
        m.add(
            latency=0.0001,
            success=True,
            action_type="shell",
            resume_sec=0.00001,
            pause_sec=0.00001,
            slice_total_sec=0.0001,
        )
        # normal slice
        m.add(
            latency=1.0,
            success=True,
            action_type="shell",
            resume_sec=0.1,
            pause_sec=0.1,
            slice_total_sec=1.2,
        )

        lines = sc.format_replay_stats_section()
        joined = "\n".join(lines)
        assert "[Lifecycle Overhead]" in joined
        # the tiny slice's overhead is (0.00001+0.00001)/0.0001 = 20.0% -- it
        # must be EXCLUDED by the MIN_SLICE_SEC guard (slice_total_sec=0.0001
        # < 0.001), so 20.0% must NOT appear. The normal sample's overhead is
        # 0.2/1.2 = 16.7%, which must appear.
        assert "20.0%" not in joined
        assert "16.7%" in joined
