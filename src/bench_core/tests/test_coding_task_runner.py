"""Tests for the coding workflow runners (host-agnostic).

Runners are driven through a :class:`FakeProvider`, so these tests exercise the
exec-only code path with no real sandbox. Thread bodies are invoked directly
(``runner.run()``) rather than via ``start()`` to keep timing deterministic.
"""
from __future__ import annotations

import base64
import threading

from bench_core.coding_payload import DEFAULT_CODING_SOURCE_FILES
from bench_core.task_runner.coding import (
    CodingRoundRunner,
    CodingTaskRunner,
    CodingWarmupRunner,
    _build_edit_command,
    _run_verify,
)
from bench_core.config import KernelConfig
from bench_core.provider import CommandResult
from bench_core.schemas import BenchSandbox
from bench_core.tests.fake_provider import FakeProvider


def _ready_sandbox(index: int = 0) -> BenchSandbox:
    return BenchSandbox(id=f"fake-{index}", index=index, ready=True, is_alive=True)


class _VerifyFailingProvider(FakeProvider):
    """FakeProvider whose verify runs (npx tsx / go run / python3 verify) fail."""

    def exec(self, inst, command, **kw):  # type: ignore[override]
        if "npx tsx" in command or "go run" in command or "python3 /tmp/bench_verify" in command:
            return CommandResult(1, "", "verify boom")
        return super().exec(inst, command, **kw)


class TestBuildEditCommand:
    def test_builds_literal_replace_heredoc(self):
        find = "export const NOOP = (): void => {}"
        replace = "export const NOOP = (): void => undefined"
        target = "packages/shared/src/general.ts"
        cmd = _build_edit_command("/opt/coding-bench", target, find, replace)

        # find/replace are base64-carried so no quoting can break them.
        assert base64.b64encode(find.encode()).decode() in cmd
        assert base64.b64encode(replace.encode()).decode() in cmd
        # target file + python3 stdin + quoted heredoc terminator.
        assert target in cmd
        assert "python3 -" in cmd
        assert "<<'PYEOF'" in cmd
        # the literal-edit script body is present verbatim.
        assert "s.replace(f, r, 1)" in cmd
        assert "sys.exit(2)" in cmd  # absent find -> explicit failure


class TestRunVerify:
    def test_ts_success_records_verify_timing(self):
        config = KernelConfig(coding_language="ts", coding_verify_repeat=2)
        pair = DEFAULT_CODING_SOURCE_FILES[0]
        provider = FakeProvider()
        state = _ready_sandbox()
        step_times: dict[str, float] = {}

        ok, err, compile_only = _run_verify(
            provider, state, "/opt/coding-bench", config, pair, step_times=step_times, round_id=0
        )

        assert ok is True
        assert err == ""
        # pair has no "verify" key -> compile_only stays False.
        assert compile_only is False
        assert "verify" in step_times
        # ts profile has no pre_verify_cmd -> no verify_clean timing.
        assert "verify_clean" not in step_times

    def test_go_records_verify_clean_then_verify(self):
        config = KernelConfig(coding_language="go")
        pair = config.coding_source_files[0]  # hugo pair with its own verify_script
        provider = FakeProvider()
        state = _ready_sandbox()
        step_times: dict[str, float] = {}

        ok, _err, compile_only = _run_verify(
            provider, state, "/opt/coding-bench", config, pair, step_times=step_times, round_id=0
        )

        assert ok is True
        assert compile_only is False
        # go profile has pre_verify_cmd="go clean -cache" -> timed separately.
        assert "verify_clean" in step_times
        assert "verify" in step_times

    def test_verify_failure_returns_error(self):
        config = KernelConfig(coding_language="ts", coding_verify_repeat=1)
        pair = DEFAULT_CODING_SOURCE_FILES[0]
        provider = _VerifyFailingProvider()
        state = _ready_sandbox()

        ok, err, _compile_only = _run_verify(provider, state, "/opt/coding-bench", config, pair, round_id=0)

        assert ok is False
        assert "verify failed" in err
        assert "exit_code=1" in err


class TestCodingWarmupRunner:
    def test_runs_initial_verify_and_marks_done(self):
        config = KernelConfig(coding_language="ts")
        provider = FakeProvider()
        state = _ready_sandbox()
        CodingWarmupRunner(state, config, provider).run()

        assert state.warmup_done is True

    def test_skips_when_not_ready(self):
        # A non-ready instance is skipped before the command-issuing body; it is
        # NOT marked warmup_done -- matching the readiness-gate early-return.
        config = KernelConfig(coding_language="ts")
        provider = FakeProvider()
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        CodingWarmupRunner(state, config, provider).run()
        assert state.warmup_done is False

    def test_skips_when_project_marker_missing(self):
        config = KernelConfig(coding_language="ts")
        provider = FakeProvider(
            exec_results={"ls /opt/coding-bench/package.json": CommandResult(1, "", "no such file")}
        )
        state = _ready_sandbox()
        CodingWarmupRunner(state, config, provider).run()
        # Project missing -> warmup bails out, but still marks itself done.
        assert state.warmup_done is True

    def test_skip_verify_still_marks_done(self):
        config = KernelConfig(coding_language="ts", coding_skip_verify=True)
        provider = _VerifyFailingProvider()  # verify would fail, but it's skipped
        state = _ready_sandbox()
        CodingWarmupRunner(state, config, provider).run()
        assert state.warmup_done is True


class TestCodingTaskRunner:
    def test_single_task_success(self):
        config = KernelConfig(coding_language="ts")
        provider = FakeProvider()
        state = _ready_sandbox()
        runner = CodingTaskRunner(state, config, threading.Event(), provider)

        success, latency, verify_success, compile_only, timed_out = runner._run_single_task()

        assert success is True
        assert verify_success is True
        assert compile_only is False
        assert timed_out is False
        assert latency > 0.0

    def test_edit_failure_records_error(self):
        config = KernelConfig(coding_language="ts")
        pair = config.coding_source_files[0]
        edit_cmd = _build_edit_command(config.coding_project_dir, pair["file"], pair["find"], pair["replace"])
        provider = FakeProvider(exec_results={edit_cmd: CommandResult(1, "", "edit boom")})
        state = _ready_sandbox()
        runner = CodingTaskRunner(state, config, threading.Event(), provider)

        success, _latency, verify_success, _compile_only, _timed_out = runner._run_single_task()

        assert success is False
        assert verify_success is False  # verify never ran (edit failed first)
        assert "edit failed" in state.coding_metrics.last_error

    def test_offline_sandbox_yields_no_task(self):
        state = _ready_sandbox()
        state.is_alive = False
        runner = CodingTaskRunner(state, KernelConfig(), threading.Event(), FakeProvider())
        success, latency, verify_success, compile_only, timed_out = runner._run_single_task()
        assert success is False
        assert latency == 0.0
        assert verify_success is False
        assert compile_only is False
        assert timed_out is False


class TestCodingRoundRunner:
    def test_round_records_all_step_timings(self):
        config = KernelConfig(coding_language="ts")
        provider = FakeProvider()
        state = _ready_sandbox()
        runner = CodingRoundRunner(state, config, threading.Event(), round_id=0, provider=provider)

        runner.run()  # one round, synchronous

        metrics = state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        assert metrics.verify_success_count == 1
        # every step in CODING_STEP_ORDER should have recorded a timing.
        assert set(metrics.get_step_times_copy()) == {"find", "read", "edit", "verify", "diff"}
        assert state.get_last_task_time() > 0.0

    def test_edit_failure_is_recorded(self):
        config = KernelConfig(coding_language="ts")
        pair = config.coding_source_files[0]
        edit_cmd = _build_edit_command(config.coding_project_dir, pair["file"], pair["find"], pair["replace"])
        provider = FakeProvider(exec_results={edit_cmd: CommandResult(1, "", "edit boom")})
        state = _ready_sandbox()
        runner = CodingRoundRunner(state, config, threading.Event(), round_id=0, provider=provider)

        runner.run()

        metrics = state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.failed_count == 1
        assert metrics.verify_success_count == 0
        assert "edit failed" in metrics.last_error

    def test_verify_failure_records_failed_step(self):
        config = KernelConfig(coding_language="ts")
        provider = _VerifyFailingProvider()
        state = _ready_sandbox()
        runner = CodingRoundRunner(state, config, threading.Event(), round_id=0, provider=provider)

        runner.run()

        metrics = state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.failed_count == 1
        # edit ran (timed), verify ran (timed) then failed.
        assert "edit" in metrics.get_step_times_copy()
        assert "verify" in metrics.get_step_times_copy()
        assert "verify failed" in metrics.last_error

    def test_skips_when_not_ready(self):
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        runner = CodingRoundRunner(state, KernelConfig(), threading.Event(), 0, FakeProvider())
        runner.run()
        assert state.coding_metrics.total_tasks == 0
