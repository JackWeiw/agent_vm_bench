"""Tests for the browser task runners (host-agnostic).

Runners are driven through a :class:`FakeProvider`, so these tests exercise the
exec-only code path with no real sandbox. Thread bodies are invoked directly
(``runner.run()``) rather than via ``start()`` to keep timing deterministic.
"""
from __future__ import annotations

import threading

from bench_core.config import KernelConfig
from bench_core.provider import CommandResult
from bench_core.schemas import BenchSandbox
from bench_core.task_runner.browser import (
    BrowserTaskRunner,
    TabOperationRunner,
    WarmupRunner,
    extract_element_refs,
)
from bench_core.tests.fake_provider import FakeProvider


def _ready_sandbox(index: int = 0) -> BenchSandbox:
    return BenchSandbox(id=f"fake-{index}", index=index, ready=True, is_alive=True)


class TestExtractElementRefs:
    def test_parses_refs(self):
        out = "elements: [ref=e1] foo [ref=e2] bar [ref=e3]"
        assert extract_element_refs(out) == ["e1", "e2", "e3"]

    def test_caps_at_fifty(self):
        out = " ".join(f"[ref=e{i}]" for i in range(60))
        assert len(extract_element_refs(out)) == 50

    def test_no_refs(self):
        assert extract_element_refs("nothing here") == []


class TestWarmupRunner:
    def test_opens_tabs_and_marks_done(self):
        config = KernelConfig(warmup_urls=["http://x", "http://y"], warmup_delay=0, warmup_loops=1)
        # snapshot must return element refs so the click step runs.
        provider = FakeProvider(exec_results={"agent-browser snapshot -i": CommandResult(0, "[ref=e1]\n", "")})
        state = _ready_sandbox()
        runner = WarmupRunner(state, config, provider)

        runner.run()  # synchronous, not start()

        assert state.warmup_done is True
        assert state.tab_ids == ["t1", "t2"]
        assert provider.prepare_calls == 0  # prepare is the spine's job, not the runner's

    def test_skips_when_not_ready(self):
        # A non-ready instance is skipped before the command-issuing body; it is
        # NOT marked warmup_done (the readiness gate failed) -- matching the
        # original wait_for_port_ready early-return.
        config = KernelConfig(warmup_urls=["http://x"], warmup_delay=0)
        provider = FakeProvider()
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        WarmupRunner(state, config, provider).run()
        assert state.warmup_done is False
        assert state.tab_ids == []

    def test_skips_when_agent_browser_missing(self):
        config = KernelConfig(warmup_urls=["http://x"], warmup_delay=0)
        provider = FakeProvider(exec_results={"agent-browser --version": CommandResult(1, "", "not found")})
        state = _ready_sandbox()
        WarmupRunner(state, config, provider).run()
        assert state.warmup_done is True
        assert state.tab_ids == []


class TestBrowserTaskRunner:
    def test_single_task_success(self):
        config = KernelConfig(browser_urls=["http://x"])
        provider = FakeProvider()
        state = _ready_sandbox()
        runner = BrowserTaskRunner(state, config, threading.Event(), provider)

        success, latency = runner._run_single_task()

        assert success is True
        # +10 simulates llm response time, so latency is at least that.
        assert latency >= 10.0

    def test_single_task_failure_records_error(self):
        config = KernelConfig(browser_urls=["http://x"])
        # Match the exact command the runner builds.
        fail_cmd = "openclaw browser --browser-profile openclaw open 'http://x'"
        provider = FakeProvider(exec_results={fail_cmd: CommandResult(1, "", "boom")})
        state = _ready_sandbox()
        runner = BrowserTaskRunner(state, config, threading.Event(), provider)

        success, latency = runner._run_single_task()

        assert success is False
        assert "exit_code=1" in state.browser_metrics.last_error

    def test_offline_sandbox_yields_no_task(self):
        state = _ready_sandbox()
        state.is_alive = False
        runner = BrowserTaskRunner(state, KernelConfig(), threading.Event(), FakeProvider())
        success, latency = runner._run_single_task()
        assert success is False
        assert latency == 0.0


class TestTabOperationRunner:
    def test_round_records_all_step_timings(self):
        config = KernelConfig(browser_urls=["http://x"])
        provider = FakeProvider(exec_results={"agent-browser snapshot -i": CommandResult(0, "[ref=e1]\n[e2]\n", "")})
        state = _ready_sandbox()
        stop = threading.Event()
        runner = TabOperationRunner(state, config, stop, round_id=0, provider=provider)

        runner.run()  # one round, synchronous

        metrics = state.browser_metrics
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        # every step in BROWSER_STEP_ORDER should have recorded a timing
        assert set(metrics.get_step_times_copy()) == {
            "open_tab",
            "page_load",
            "snapshot",
            "click",
            "screenshot",
        }
        assert state.get_last_task_time() > 0.0

    def test_open_tab_failure_is_recorded(self):
        config = KernelConfig(browser_urls=["http://x"])
        provider = FakeProvider(
            exec_results={
                'agent-browser tab new "http://x"': CommandResult(1, "", "no tab"),
            }
        )
        state = _ready_sandbox()
        runner = TabOperationRunner(state, config, threading.Event(), round_id=0, provider=provider)

        runner.run()

        metrics = state.browser_metrics
        assert metrics.total_tasks == 1
        assert metrics.failed_count == 1
        assert "open_tab failed" in metrics.last_error

    def test_skips_when_not_ready(self):
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        runner = TabOperationRunner(state, KernelConfig(), threading.Event(), 0, FakeProvider())
        runner.run()
        assert state.browser_metrics.total_tasks == 0
