"""Tests for the host-agnostic metrics model + BenchSandbox."""
from __future__ import annotations

import pytest

from bench_core.provider import SandboxInstance, SandboxStatus
from bench_core.schemas import (
    BROWSER_STEP_ORDER,
    CODING_STEP_ORDER,
    BenchSandbox,
    BrowserMetrics,
    CodingMetrics,
    DocumentMetrics,
    TaskMetricsBase,
    Snapshot,
    get_step_order,
)


class TestTaskMetricsBase:
    def test_add_success_counts(self):
        m = TaskMetricsBase()
        m.add(latency=1.0, success=True)
        m.add(latency=2.0, success=True)
        assert m.total_tasks == 2
        assert m.success_count == 2
        assert m.failed_count == 0
        assert m.latencies == [1.0, 2.0]

    def test_add_failure_and_timeout(self):
        m = TaskMetricsBase()
        m.add(latency=1.0, success=False)
        m.add(latency=2.0, success=False, timeout=True)
        assert m.total_tasks == 2
        assert m.success_count == 0
        assert m.failed_count == 2
        assert m.timeout_count == 1
        # failures do not contribute latencies
        assert m.latencies == []

    def test_latencies_returns_a_copy(self):
        m = TaskMetricsBase()
        m.add(latency=1.0, success=True)
        lat = m.latencies
        lat.append(99.0)
        # mutating the returned list must not affect internal state
        assert m.latencies == [1.0]

    def test_step_times_recorded(self):
        m = TaskMetricsBase()
        m.add(latency=1.0, success=True, step_times={"open_tab": 0.2, "page_load": 0.8})
        copy = m.get_step_times_copy()
        assert copy["open_tab"] == [0.2]
        assert copy["page_load"] == [0.8]

    def test_get_latencies_since(self):
        m = TaskMetricsBase()
        for v in [1.0, 2.0, 3.0, 4.0]:
            m.add(latency=v, success=True)
        assert m.get_latencies_since(2) == [3.0, 4.0]
        assert m.get_latencies_since(10) == []


class TestCodingMetrics:
    def test_verify_and_compile_only_tracked_separately(self):
        m = CodingMetrics()
        m.add(latency=1.0, success=True, verify_success=True)
        m.add(latency=2.0, success=True, compile_only=True)
        m.add(latency=3.0, success=True)  # neither flag
        assert m.success_count == 3
        assert m.verify_success_count == 1
        assert m.compile_only_count == 1
        assert m.step_order == CODING_STEP_ORDER


class TestBenchSandbox:
    def test_requires_id_and_index(self):
        s = BenchSandbox(id="sbx-1", index=0)
        assert s.id == "sbx-1"
        assert s.index == 0
        assert s.workflow_type == "browser"
        assert s.is_alive is True
        assert s.warmup_done is False
        assert s.tab_ids == []
        assert isinstance(s.creation_metrics.status, SandboxStatus)
        assert s.creation_metrics.status == SandboxStatus.PENDING

    def test_is_a_sandbox_instance(self):
        # Liskov: the kernel's working type subclasses the contract type.
        s = BenchSandbox(id="sbx-1", index=0)
        assert isinstance(s, SandboxInstance)

    def test_task_metrics_polymorphism(self):
        for wf, attr in [
            ("browser", "browser_metrics"),
            ("coding", "coding_metrics"),
            ("document", "document_metrics"),
        ]:
            s = BenchSandbox(id="x", index=0, workflow_type=wf)
            assert s.task_metrics is getattr(s, attr)

    def test_task_metrics_rejects_bad_workflow(self):
        s = BenchSandbox(id="x", index=0, workflow_type="bogus")
        with pytest.raises(ValueError):
            _ = s.task_metrics

    def test_last_task_time_thread_safe(self):
        s = BenchSandbox(id="x", index=0)
        assert s.get_last_task_time() == 0.0
        s.update_last_task_time(12.5)
        assert s.get_last_task_time() == 12.5


class TestStepOrder:
    def test_browser(self):
        assert get_step_order("browser") == BROWSER_STEP_ORDER

    def test_coding(self):
        assert get_step_order("coding") == CODING_STEP_ORDER

    def test_document_xlsx_default(self):
        order = get_step_order("document")
        assert order == get_step_order("document", "xlsx")

    def test_document_pdf(self):
        assert get_step_order("document", "pdf") != get_step_order("document", "xlsx")

    def test_bad_workflow(self):
        with pytest.raises(ValueError):
            get_step_order("bogus")

    def test_bad_case_kind(self):
        with pytest.raises(ValueError):
            get_step_order("document", "csv")


class TestSnapshot:
    def test_defaults(self):
        snap = Snapshot(timestamp=0.0, elapsed=0.0, total_sandboxes=0, active_sandboxes=0, offline_sandboxes=0)
        assert snap.browser_total == 0
        assert snap.creation_stats == {}
        assert snap.round_total == 0
