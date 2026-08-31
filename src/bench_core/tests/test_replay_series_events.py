"""Phase 2: structured retry/admission/trajectory events written to the series."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bench_core.admission import Admission, QpsRateLimiter, RunningSlotScheduler, ShutdownInterrupted
from bench_core.config import KernelConfig
from bench_core.lifecycle_series import LifecycleSeriesWriter
from bench_core.replay_payload import ReplayStep, Trajectory
from bench_core.schemas import BenchSandbox
from bench_core.task_runner.replay import ReplayBaseRunner
from env_provider import SandboxInstance
from env_provider.tests.lifecycle_fake import FakeLifecycleProvider


def _series_events(tmp_path: Path) -> list[dict]:
    return [json.loads(l) for l in (tmp_path / "s.jsonl").read_text().splitlines()]


class TestRetryEvents:
    def _runner(self, tmp_path, *, retries=2, qps=100.0, stop=None):
        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="lifecycle",
            replay_lifecycle_retries=retries,
            replay_ready_probe=False,
        )
        provider = FakeLifecycleProvider(count=1)
        provider.create_all()
        state = BenchSandbox.from_instance(provider._instances[0], "replay")
        stop = stop or threading.Event()
        series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
        adm = Admission(
            slots=RunningSlotScheduler(maximum=1, stop_event=stop),
            qps=QpsRateLimiter(qps=qps, inflight_cap=4, stop_event=stop),
        )
        runner = ReplayBaseRunner(state, cfg, stop, provider, admission=adm, series=series)
        return runner, series, provider, state

    def test_retry_queued_and_recovered_on_transient_then_success(self, tmp_path):
        class _Flaky(FakeLifecycleProvider):
            def __init__(self):
                super().__init__(count=1)
                self._n = 0

            def resume(self, inst):
                self._n += 1
                if self._n == 1:
                    raise RuntimeError("503 gateway timeout")
                super().resume(inst)

        runner, series, provider, state = self._runner(tmp_path)
        flaky = _Flaky()
        runner.provider = flaky  # inject flaky
        runner._lifecycle_call_with_retry("resume", lambda: flaky.resume(state))
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"].startswith("retry_")]
        types = [e["event"] for e in events]
        assert "retry_queued" in types
        assert "retry_recovered" in types
        queued = next(e for e in events if e["event"] == "retry_queued")
        assert queued["operation"] == "resume"
        assert queued["attempt"] == 1
        assert queued["error_type"] == "RuntimeError"
        assert "503" in queued["error"]
        # ReplayMetrics accumulator advanced
        assert state.replay_metrics.retry_queued_count == 1
        recovered = next(e for e in events if e["event"] == "retry_recovered")
        assert recovered["attempt"] == 2  # recovered on the 2nd (1-indexed) attempt

    def test_retry_exhausted_on_non_transient(self, tmp_path):
        class _Hard(FakeLifecycleProvider):
            def resume(self, inst):
                raise RuntimeError("permanent boom")

        runner, series, provider, state = self._runner(tmp_path)
        hard = _Hard()
        runner.provider = hard
        with pytest.raises(RuntimeError, match="permanent boom"):
            runner._lifecycle_call_with_retry("resume", lambda: hard.resume(state))
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"] == "retry_exhausted"]
        assert len(events) == 1
        assert events[0]["retryable"] is False

    def test_shutdown_mid_retry_emits_no_retry_exhausted(self, tmp_path):
        """Negative: ShutdownInterrupted bypasses except Exception -> no retry_exhausted."""

        class _AlwaysTransient(FakeLifecycleProvider):
            def resume(self, inst):
                raise RuntimeError("503 gateway timeout")

        stop = threading.Event()
        runner, series, provider, state = self._runner(tmp_path, qps=2.0, stop=stop)
        always = _AlwaysTransient()
        runner.provider = always
        stop.set()  # shutdown before call
        with pytest.raises(ShutdownInterrupted):
            runner._lifecycle_call_with_retry("resume", lambda: always.resume(state))
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"] == "retry_exhausted"]
        assert events == []

    def test_first_attempt_success_emits_no_retry_event(self, tmp_path):
        runner, series, provider, state = self._runner(tmp_path)
        runner._lifecycle_call_with_retry("resume", lambda: provider.resume(state))
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"].startswith("retry_")]
        assert events == []


class TestAdmissionEvents:
    def _runner(self, tmp_path, *, mode="lifecycle", admission=True):
        cfg = KernelConfig(workflow_type="replay", replay_mode=mode, replay_ready_probe=False)
        provider = FakeLifecycleProvider(count=1)
        provider.create_all()
        state = BenchSandbox.from_instance(provider._instances[0], "replay")
        stop = threading.Event()
        series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
        adm = None
        if admission:
            adm = Admission(
                slots=RunningSlotScheduler(maximum=1, stop_event=stop),
                qps=QpsRateLimiter(qps=1000.0, inflight_cap=4, stop_event=stop),
            )
        runner = ReplayBaseRunner(state, cfg, stop, provider, admission=adm, series=series)
        return runner, series, provider, state

    def test_slot_acquire_and_release_emitted(self, tmp_path):
        runner, series, provider, state = self._runner(tmp_path)
        step = ReplayStep(index=0, action_type="shell", action="echo hi", delay_time_sec=0.0)
        runner._run_slice(step, trajectory_id="t1")
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"] in ("slot_acquire", "slot_release")]
        assert [e["event"] for e in events] == ["slot_acquire", "slot_release"]
        acq = events[0]
        assert acq["sandbox_index"] == state.index
        assert "lease_id" in acq and "queue_wait_sec" in acq and "active_after" in acq

    def test_no_admission_events_in_exec_only(self, tmp_path):
        runner, series, provider, state = self._runner(tmp_path, mode="exec_only", admission=False)
        step = ReplayStep(index=0, action_type="shell", action="echo hi", delay_time_sec=0.0)
        runner._run_slice(step)
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"].startswith("slot_")]
        assert events == []


class TestTrajectoryEvents:
    def test_create_and_kill_events_emitted(self, tmp_path):
        class _Provider(FakeLifecycleProvider):
            def __init__(self):
                super().__init__(count=0)
                self.killed = []

            def kill_one(self, inst):
                self.killed.append(inst.index)
                super().kill_one(inst)

        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="trajectory",
            total_count=1,
            replay_running_concurrency=1,
            replay_control_plane_qps=100.0,
            replay_ready_probe=False,
        )
        provider = _Provider()
        stop = threading.Event()
        state = BenchSandbox.from_instance(SandboxInstance(id="shell", index=1), "replay")
        state.ready = True
        state.is_alive = True
        series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
        adm = Admission(
            slots=RunningSlotScheduler(maximum=1, stop_event=stop),
            qps=QpsRateLimiter(qps=100.0, inflight_cap=4, stop_event=stop),
        )
        runner = ReplayBaseRunner(state, cfg, stop, provider, series=series, admission=adm)
        traj = Trajectory(
            path=Path("tr-1"),
            instance_id="tr-1",
            environment="env",
            steps=(ReplayStep(index=0, action="echo hi", delay_time_sec=0.0, action_type="shell"),),
        )
        runner._run_trajectory(traj)
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"] in ("trajectory_create", "trajectory_kill")]
        assert [e["event"] for e in events] == ["trajectory_create", "trajectory_kill"]
        assert events[0]["trajectory_id"] == "tr-1"
        assert "create_sec" in events[0] and events[0]["success"] is True
        assert "kill_sec" in events[1]
        assert provider.killed == [1]  # kill_one ran with the sandbox index

    def test_create_failure_emits_event_with_success_false(self, tmp_path):
        class _FailCreate(FakeLifecycleProvider):
            def __init__(self):
                super().__init__(count=0)

            def create_one(self, index, *, template=None, metadata=None):
                raise RuntimeError("create boom")

        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="trajectory",
            total_count=1,
            replay_running_concurrency=1,
            replay_control_plane_qps=100.0,
            replay_ready_probe=False,
        )
        provider = _FailCreate()
        stop = threading.Event()
        state = BenchSandbox.from_instance(SandboxInstance(id="shell", index=1), "replay")
        state.ready = True
        state.is_alive = True
        series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
        adm = Admission(
            slots=RunningSlotScheduler(maximum=1, stop_event=stop),
            qps=QpsRateLimiter(qps=100.0, inflight_cap=4, stop_event=stop),
        )
        runner = ReplayBaseRunner(state, cfg, stop, provider, series=series, admission=adm)
        traj = Trajectory(
            path=Path("tr-1"),
            instance_id="tr-1",
            environment="env",
            steps=(ReplayStep(index=0, action="echo hi", delay_time_sec=0.0, action_type="shell"),),
        )
        runner._run_trajectory(traj)
        series.close()
        events = [e for e in _series_events(tmp_path) if e["event"] == "trajectory_create"]
        assert len(events) == 1
        assert events[0]["success"] is False
        assert events[0]["error_type"] == "RuntimeError"
        assert "create boom" in events[0]["error"]
        # No kill event since create failed and we returned early
        kill_events = [e for e in _series_events(tmp_path) if e["event"] == "trajectory_kill"]
        assert kill_events == []

    def test_kill_failure_emits_event_with_success_false(self, tmp_path):
        class _FailKill(FakeLifecycleProvider):
            def __init__(self):
                super().__init__(count=0)

            def kill_one(self, inst):
                raise RuntimeError("kill boom")

        cfg = KernelConfig(
            workflow_type="replay",
            replay_mode="trajectory",
            total_count=1,
            replay_running_concurrency=1,
            replay_control_plane_qps=100.0,
            replay_ready_probe=False,
        )
        provider = _FailKill()
        stop = threading.Event()
        state = BenchSandbox.from_instance(SandboxInstance(id="shell", index=1), "replay")
        state.ready = True
        state.is_alive = True
        series = LifecycleSeriesWriter(tmp_path / "s.jsonl")
        adm = Admission(
            slots=RunningSlotScheduler(maximum=1, stop_event=stop),
            qps=QpsRateLimiter(qps=100.0, inflight_cap=4, stop_event=stop),
        )
        runner = ReplayBaseRunner(state, cfg, stop, provider, series=series, admission=adm)
        traj = Trajectory(
            path=Path("tr-1"),
            instance_id="tr-1",
            environment="env",
            steps=(ReplayStep(index=0, action="echo hi", delay_time_sec=0.0, action_type="shell"),),
        )
        runner._run_trajectory(traj)
        series.close()
        create_events = [e for e in _series_events(tmp_path) if e["event"] == "trajectory_create"]
        assert len(create_events) == 1 and create_events[0]["success"] is True
        kill_events = [e for e in _series_events(tmp_path) if e["event"] == "trajectory_kill"]
        assert len(kill_events) == 1
        assert kill_events[0]["success"] is False
        assert kill_events[0]["error_type"] == "RuntimeError"
        assert "kill boom" in kill_events[0]["error"]
