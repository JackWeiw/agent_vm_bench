"""Tests for the fixed-mode TaskManager orchestrator (host-agnostic).

Runners are driven through a :class:`FakeProvider`. Warmup runners are short
(one verify / tab-open / workspace-prep cycle), so wait_warmup completes
deterministically. Fixed-mode task runners loop on stop_event; the tests start
the runners, assert counts/types synchronously, then set stop_event so the
runner threads wake from ``stop_event.wait`` and exit -- no racing the loop
body.
"""
from __future__ import annotations

import threading

from bench_core.config import KernelConfig
from bench_core.provider import CreationMetrics, SandboxStatus
from bench_core.schemas import BenchSandbox
from bench_core.task_manager import TaskManager
from bench_core.task_runner import BrowserTaskRunner
from bench_core.tests.fake_provider import FakeProvider


def _ready_sbx(i: int, *, warmup_done: bool = False) -> BenchSandbox:
    return BenchSandbox(
        id=f"f-{i}",
        index=i,
        ready=True,
        is_alive=True,
        warmup_done=warmup_done,
        creation_metrics=CreationMetrics(status=SandboxStatus.READY),
    )


def _states(n: int, *, warmup_done: bool = False) -> dict[int, BenchSandbox]:
    return {i: _ready_sbx(i, warmup_done=warmup_done) for i in range(n)}


class TestStartWarmup:
    def test_browser_creates_warmup_runners(self):
        config = KernelConfig(workflow_type="browser", warmup_urls=["http://x"], warmup_delay=0, warmup_loops=1)
        provider = FakeProvider(count=3)
        states = _states(3)
        mgr = TaskManager(config, states, threading.Event(), provider)

        mgr.start_warmup()

        assert len(mgr.warmup_runners) == 3
        completed, failed = mgr.wait_warmup(timeout=10)
        assert completed == 3
        assert failed == 0
        assert all(s.warmup_done for s in states.values())

    def test_browser_skips_when_no_warmup_urls(self):
        config = KernelConfig(workflow_type="browser", warmup_urls=[])
        provider = FakeProvider(count=2)
        states = _states(2)
        mgr = TaskManager(config, states, threading.Event(), provider)

        mgr.start_warmup()

        assert len(mgr.warmup_runners) == 0
        assert all(s.warmup_done for s in states.values())  # marked done without runners

    def test_coding_creates_warmup_runners(self):
        config = KernelConfig(workflow_type="coding", coding_language="ts")
        provider = FakeProvider(count=2)
        states = _states(2)
        mgr = TaskManager(config, states, threading.Event(), provider)

        mgr.start_warmup()

        assert len(mgr.warmup_runners) == 2
        mgr.wait_warmup(timeout=10)
        assert all(s.warmup_done for s in states.values())

    def test_coding_skip_verify_marks_done_without_runners(self):
        config = KernelConfig(workflow_type="coding", coding_skip_verify=True)
        provider = FakeProvider(count=2)
        states = _states(2)
        mgr = TaskManager(config, states, threading.Event(), provider)

        mgr.start_warmup()

        assert len(mgr.warmup_runners) == 0
        assert all(s.warmup_done for s in states.values())


class TestStartAll:
    def test_creates_browser_task_runners(self):
        config = KernelConfig(workflow_type="browser", browser_urls=["http://x"])
        provider = FakeProvider(count=3)
        states = _states(3, warmup_done=True)
        stop = threading.Event()
        mgr = TaskManager(config, states, stop, provider)

        mgr.start_all()

        assert len(mgr.runners) == 3
        assert all(isinstance(r, BrowserTaskRunner) for r in mgr.runners)
        stop.set()
        mgr.wait_all(timeout=5)

    def test_subset_by_benchmark_percent(self):
        config = KernelConfig(workflow_type="browser", browser_urls=["http://x"], benchmark_percent=0.5)
        provider = FakeProvider(count=4)
        states = _states(4, warmup_done=True)
        stop = threading.Event()
        mgr = TaskManager(config, states, stop, provider)

        mgr.start_all()

        # 50% of 4 ready sandboxes = 2 task runners.
        assert len(mgr.runners) == 2
        stop.set()
        mgr.wait_all(timeout=5)

    def test_skips_when_not_warmed(self):
        config = KernelConfig(workflow_type="browser", browser_urls=["http://x"])
        provider = FakeProvider(count=2)
        states = _states(2, warmup_done=False)  # not warmed up
        mgr = TaskManager(config, states, threading.Event(), provider)

        mgr.start_all()

        assert len(mgr.runners) == 0  # nothing ready for tasks

    def test_batched_start(self):
        config = KernelConfig(
            workflow_type="browser",
            browser_urls=["http://x"],
            task_batch_size=2,
            task_batch_interval=0,
        )
        provider = FakeProvider(count=4)
        states = _states(4, warmup_done=True)
        stop = threading.Event()
        mgr = TaskManager(config, states, stop, provider)

        mgr.start_all()

        # 4 sandboxes / batch_size 2 = 2 batches, all 4 runners started.
        assert len(mgr.runners) == 4
        stop.set()
        mgr.wait_all(timeout=5)
