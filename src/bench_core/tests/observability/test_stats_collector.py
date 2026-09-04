"""Tests for the host-agnostic stats collector.

Ported from ``e2b_bench/tests/test_stats_collector.py``; the collector now reads
:class:`bench_core.schemas.BenchSandbox` state and a :class:`KernelConfig`, with
the provider identity carried by the ``provider_label`` string instead of
``config.template``.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from bench_core.config import KernelConfig
from bench_core.observability.stats_collector import ErrorClassifier, ReportFormatter, StatsCollector
from bench_core.schemas import BenchSandbox
from env_provider import SandboxStatus


class TestStatsCollectorErrorClassification:
    """Tests for error type classification in generate_report."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.config = Mock(spec=KernelConfig)
        self.config.workflow_type = "browser"
        self.config.total_count = 5
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    @staticmethod
    def _create_sandbox_with_error(index: int, error_msg: str, fail_count: int = 1) -> BenchSandbox:
        """Helper to create a sandbox state with a specific error."""
        state = BenchSandbox(id=f"sbx-{index}", index=index)
        for _ in range(fail_count):
            state.browser_metrics.add(latency=1.0, success=False)
        state.browser_metrics.last_error = error_msg
        return state

    def test_classify_open_tab_failed(self):
        """Open tab failed should be classified correctly."""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "open_tab failed: exit_code=1 | stderr=error"),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Open tab failed" in report
        assert "1" in report  # count

    def test_classify_page_load_failed(self):
        """Page load failed should be classified correctly."""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "page_load failed: exit_code=1 | url=https://example.com"),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Page load failed" in report

    def test_classify_snapshot_failed(self):
        """Snapshot failed should be classified correctly."""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "snapshot failed: exit_code=127 | stderr=not found"),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Snapshot failed" in report

    def test_classify_click_failed(self):
        """Click failed should be classified correctly."""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "click failed: exit_code=1 | element=e1"),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Click failed" in report

    def test_classify_screenshot_failed(self):
        """Screenshot failed should be classified correctly."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "screenshot failed: exit_code=1")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Screenshot failed" in report

    def test_classify_timeout(self):
        """Timeout errors should be classified correctly."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "operation timed out after 60s")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Timeout" in report

    def test_classify_timeout_with_timed_out_keyword(self):
        """'timed out' keyword should also be classified as Timeout."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "request timed out")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Timeout" in report

    def test_classify_legacy_chrome_start_failed(self):
        """Legacy Chrome start failed should still be classified."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "failed to start chrome: process exited")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Chrome start failed" in report

    def test_classify_legacy_dbus_error(self):
        """Legacy D-Bus error should still be classified."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "failed to connect to the bus: D-Bus error")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "D-Bus connection error" in report

    def test_classify_legacy_gateway_error(self):
        """Legacy gateway error should still be classified."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "gateway connection failed: http_unreachable")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Gateway connection error" in report

    def test_classify_sandbox_unreachable_route_failure(self):
        """Routing failures are bucketed as 'Sandbox unreachable'."""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "sandbox unreachable: Failed to route request to sandbox"),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Sandbox unreachable" in report

    def test_classify_gateway_not_shadowed_by_unreachable(self):
        """http_unreachable stays 'Gateway connection error', not 'Sandbox unreachable'."""
        assert ErrorClassifier.classify("gateway failed: http_unreachable") == "Gateway connection error"
        assert (
            ErrorClassifier.classify("sandbox unreachable: Failed to route request to sandbox") == "Sandbox unreachable"
        )

    def test_classify_other_error(self):
        """Unknown errors should be classified as Other."""
        sandbox_states = {1: self._create_sandbox_with_error(1, "some unknown error occurred")}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Other" in report

    @staticmethod
    def _format_workflow_error(workflow_type: str, error_msg: str) -> str:
        config = Mock(spec=KernelConfig)
        config.workflow_type = workflow_type
        state = BenchSandbox(id="sbx-1", index=1, workflow_type=workflow_type)
        state.task_metrics.add(latency=1.0, success=False)
        state.task_metrics.last_error = error_msg
        return "\n".join(ReportFormatter(config, {1: state}).format_error_section())

    def test_document_error_category_falls_back_to_other_for_browser(self):
        report = self._format_workflow_error("browser", "write failed while saving output")

        assert "Other" in report
        assert "Write failed" not in report

    def test_document_error_category_falls_back_to_other_for_coding(self):
        report = self._format_workflow_error("coding", "business verification rejected the output")

        assert "Other" in report
        assert "Verifier failed" not in report

    def test_document_error_category_remains_specific_for_document(self):
        report = self._format_workflow_error("document", "business verification rejected the output")

        assert "Verifier failed" in report
        assert "Other" not in report

    def test_multiple_error_types(self):
        """Multiple error types should all be classified."""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "open_tab failed: exit_code=1", 2),
            2: self._create_sandbox_with_error(2, "snapshot failed: exit_code=1", 3),
            3: self._create_sandbox_with_error(3, "timeout occurred", 1),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Open tab failed" in report
        assert "Snapshot failed" in report
        assert "Timeout" in report


class TestStatsCollectorConfigurationCompatibility:
    @staticmethod
    def _generate_report(workflow_type: str) -> str:
        config = KernelConfig(workflow_type=workflow_type)
        if workflow_type == "document":
            config.document_case_kind = "pdf"
        state = BenchSandbox(id="sbx-1", index=1, workflow_type=workflow_type)
        return StatsCollector(config, {1: state}).generate_report()

    @pytest.mark.parametrize("workflow_type", ["browser", "coding"])
    def test_existing_workflow_reports_do_not_gain_workflow_line(self, workflow_type):
        report = self._generate_report(workflow_type)

        assert "  Workflow:" not in report
        assert "  Document Case:" not in report

    def test_document_report_includes_workflow_and_case(self):
        report = self._generate_report("document")

        assert "  Workflow:        document" in report
        assert "  Document Case:   pdf" in report


class TestSandboxRuntimeStatusReporting:
    @staticmethod
    def _format_status(state: BenchSandbox) -> str:
        config = KernelConfig(workflow_type="document", document_case_kind="pdf")
        return "\n".join(ReportFormatter(config, {state.index: state}).format_sandbox_status_section())

    def test_normal_cleanup_is_not_reported_as_runtime_offline(self):
        state = BenchSandbox(id="sbx-1", index=1, workflow_type="document")
        state.creation_metrics.status = SandboxStatus.READY
        state.is_alive = False
        state.stopped_by_cleanup = True

        report = self._format_status(state)

        assert "Offline (runtime):   0" in report
        assert "Offline IDs:" not in report

    def test_real_runtime_stop_is_still_reported_offline(self):
        state = BenchSandbox(id="sbx-7", index=7, workflow_type="document")
        state.creation_metrics.status = SandboxStatus.READY
        state.is_alive = False

        report = self._format_status(state)

        assert "Offline (runtime):   1" in report
        assert "Offline IDs:         [7]" in report


class TestStatsCollectorRoundComparison:
    """Tests for round comparison latency calculation."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.config = Mock(spec=KernelConfig)
        self.config.workflow_type = "browser"
        self.config.total_count = 3
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    @staticmethod
    def _create_sandbox_with_latencies(index: int, latencies: list[float]) -> BenchSandbox:
        """Helper to create a sandbox state with specific latencies."""
        state = BenchSandbox(id=f"sbx-{index}", index=index)
        for lat in latencies:
            state.browser_metrics.add(latency=lat, success=True)
        state.creation_metrics.status = SandboxStatus.READY
        return state

    def test_round_comparison_no_rounds(self):
        """No round comparison should appear without round data."""
        sandbox_states = {1: self._create_sandbox_with_latencies(1, [1.0, 2.0, 3.0])}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "[Round Comparison]" not in report

    def test_round_comparison_with_single_round(self):
        """Single round is redundant with cumulative stats -> suppressed."""
        sandbox_states = {
            1: self._create_sandbox_with_latencies(1, [1.0, 2.0, 3.0]),
            2: self._create_sandbox_with_latencies(2, [2.0, 3.0, 4.0]),
        }

        collector = StatsCollector(self.config, sandbox_states)
        # Simulate round 0 baseline
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0, 2: 0},
        }

        report = collector.generate_report()

        assert "[Round Comparison]" not in report

    def test_round_comparison_latency_extraction(self):
        """Round latency should be extracted from correct range."""
        # Round 0: latencies [1.0, 2.0] for sandbox 1, [3.0, 4.0] for sandbox 2
        # Round 1: latencies [5.0, 6.0] for sandbox 1, [7.0, 8.0] for sandbox 2
        sandbox_states = {
            1: self._create_sandbox_with_latencies(1, [1.0, 2.0, 5.0, 6.0]),
            2: self._create_sandbox_with_latencies(2, [3.0, 4.0, 7.0, 8.0]),
        }

        collector = StatsCollector(self.config, sandbox_states)
        # Round 0 starts with 0 latencies
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0, 2: 0},
        }
        # Round 1 starts after 2 latencies per sandbox
        collector._round_start_totals[1] = {
            "total": 4,
            "success": 4,
            "sandbox_latency_counts": {1: 2, 2: 2},
        }

        report = collector.generate_report()

        assert "[Round Comparison]" in report
        # Round 0: 4 tasks (2 per sandbox); Round 1: 4 tasks (2 per sandbox)
        assert "Summary: 8 tasks across 2 rounds" in report

    def test_error_message_truncation(self):
        """Error messages should be truncated in report."""
        long_error = "x" * 200  # Very long error message
        state = BenchSandbox(id="sbx-1", index=1)
        state.browser_metrics.add(latency=1.0, success=False)
        state.browser_metrics.last_error = long_error

        report = StatsCollector(self.config, {1: state}).generate_report()

        # Error should be truncated to 150 chars in display
        assert "x" * 200 not in report
        # But error type classification should still work
        assert "Other" in report


class TestStatsCollectorRoundBaselineTiming:
    """Tests for round baseline timing correctness.

    The key bug: set_round(round_id) records the baseline at round START,
    before tasks begin. But tasks from the PREVIOUS round may not have finished
    yet (they run in separate threads). The fix records the baseline AFTER the
    previous round's tasks have completed; this test suite pins both the fixed
    and the buggy behavior.
    """

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.config = Mock(spec=KernelConfig)
        self.config.workflow_type = "browser"
        self.config.total_count = 4
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    @staticmethod
    def _create_sandbox_with_latencies(index: int, latencies: list[float]) -> BenchSandbox:
        state = BenchSandbox(id=f"sbx-{index}", index=index)
        for lat in latencies:
            state.browser_metrics.add(latency=lat, success=True)
        state.creation_metrics.status = SandboxStatus.READY
        return state

    def test_four_rounds_each_20_tasks(self):
        """4 rounds of 20 sandboxes each should show 20 tasks per round.

        80 sandboxes, 4 rounds, each round has 20 sandboxes doing 1 task.
        Expected: Round 0=20, Round 1=20, Round 2=20, Round 3=20.
        """
        sandbox_states = {i: self._create_sandbox_with_latencies(i, [46.23]) for i in range(80)}

        collector = StatsCollector(self.config, sandbox_states)
        # Correct baseline recording (after tasks complete), including the
        # post-last-round sentinel (totals[4]) with tasks=0.
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {i: 0 for i in range(80)},
        }
        collector._round_start_totals[1] = {
            "total": 20,
            "success": 20,
            "sandbox_latency_counts": {i: 1 for i in range(20)} | {i: 0 for i in range(20, 80)},
        }
        collector._round_start_totals[2] = {
            "total": 40,
            "success": 40,
            "sandbox_latency_counts": {i: 1 for i in range(40)} | {i: 0 for i in range(40, 80)},
        }
        collector._round_start_totals[3] = {
            "total": 60,
            "success": 60,
            "sandbox_latency_counts": {i: 1 for i in range(60)} | {i: 0 for i in range(60, 80)},
        }
        # Post-last-round baseline: cumulative totals after all rounds complete
        collector._round_start_totals[4] = {
            "total": 80,
            "success": 80,
            "sandbox_latency_counts": {i: 1 for i in range(80)},
        }

        report = collector.generate_report()

        assert "[Round Comparison]" in report
        assert "Summary: 80 tasks across 4 rounds" in report

        round_finals = ReportFormatter(self.config, sandbox_states)._calculate_round_finals(
            collector._round_start_totals
        )
        assert round_finals[0]["tasks"] == 20
        assert round_finals[1]["tasks"] == 20
        assert round_finals[2]["tasks"] == 20
        assert round_finals[3]["tasks"] == 20

    def test_four_rounds_without_post_last_baseline(self):
        """4 rounds without post-last baseline should still work via final totals.

        Even if the post-last-round baseline is not recorded (e.g. early
        termination), _calculate_round_finals falls back to the final cumulative
        totals for the last round.
        """
        sandbox_states = {i: self._create_sandbox_with_latencies(i, [46.23]) for i in range(80)}

        collector = StatsCollector(self.config, sandbox_states)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {i: 0 for i in range(80)},
        }
        collector._round_start_totals[1] = {
            "total": 20,
            "success": 20,
            "sandbox_latency_counts": {i: 1 for i in range(20)} | {i: 0 for i in range(20, 80)},
        }
        collector._round_start_totals[2] = {
            "total": 40,
            "success": 40,
            "sandbox_latency_counts": {i: 1 for i in range(40)} | {i: 0 for i in range(40, 80)},
        }
        collector._round_start_totals[3] = {
            "total": 60,
            "success": 60,
            "sandbox_latency_counts": {i: 1 for i in range(60)} | {i: 0 for i in range(60, 80)},
        }

        round_finals = ReportFormatter(self.config, sandbox_states)._calculate_round_finals(
            collector._round_start_totals
        )
        # Round 3 falls back to final cumulative total = 80
        assert round_finals[0]["tasks"] == 20
        assert round_finals[1]["tasks"] == 20
        assert round_finals[2]["tasks"] == 20
        assert round_finals[3]["tasks"] == 20

    def test_buggy_baseline_shows_wrong_tasks(self):
        """Demonstrate the bug: wrong baseline timing gives wrong task counts.

        When baseline is recorded at round START (before tasks complete), the
        attribution shifts. This pins the buggy behavior so the fix can be
        validated against it.
        """
        sandbox_states = {i: self._create_sandbox_with_latencies(i, [46.23]) for i in range(80)}

        collector = StatsCollector(self.config, sandbox_states)
        # BUGGY baseline (recorded at round start before tasks complete)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {i: 0 for i in range(80)},
        }
        collector._round_start_totals[1] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {i: 0 for i in range(80)},
        }
        collector._round_start_totals[2] = {
            "total": 20,
            "success": 20,
            "sandbox_latency_counts": {i: 1 for i in range(20)} | {i: 0 for i in range(20, 80)},
        }
        collector._round_start_totals[3] = {
            "total": 40,
            "success": 40,
            "sandbox_latency_counts": {i: 1 for i in range(40)} | {i: 0 for i in range(40, 80)},
        }

        round_finals = ReportFormatter(self.config, sandbox_states)._calculate_round_finals(
            collector._round_start_totals
        )
        # BUGGY results:
        assert round_finals[0]["tasks"] == 0  # BUG: should be 20
        assert round_finals[1]["tasks"] == 20  # wrong attribution (Round 0's data)
        assert round_finals[2]["tasks"] == 20  # wrong attribution (Round 1's data)
        assert round_finals[3]["tasks"] == 40  # BUG: cumulative of Round 2 + Round 3


class TestStatsCollectorRoundLatencyDelta:
    """Tests for round latency delta calculation using get_latencies_since."""

    def setup_method(self) -> None:
        self.config = Mock(spec=KernelConfig)
        self.config.workflow_type = "browser"
        self.config.total_count = 2
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    @staticmethod
    def _create_sandbox_with_latencies(index: int, latencies: list[float]) -> BenchSandbox:
        state = BenchSandbox(id=f"sbx-{index}", index=index)
        for lat in latencies:
            state.browser_metrics.add(latency=lat, success=True)
        state.creation_metrics.status = SandboxStatus.READY
        return state

    def test_latency_extraction_per_round(self):
        """Each round should extract its own latencies correctly."""
        # Sandbox 1: 6 latencies (2 per round for 3 rounds)
        # Sandbox 2: 6 latencies (2 per round for 3 rounds)
        sandbox_states = {
            1: self._create_sandbox_with_latencies(1, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
            2: self._create_sandbox_with_latencies(2, [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]),
        }

        collector = StatsCollector(self.config, sandbox_states)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0, 2: 0},
        }
        collector._round_start_totals[1] = {
            "total": 4,
            "success": 4,
            "sandbox_latency_counts": {1: 2, 2: 2},
        }
        collector._round_start_totals[2] = {
            "total": 8,
            "success": 8,
            "sandbox_latency_counts": {1: 4, 2: 4},
        }

        report = collector.generate_report()

        assert "[Round Comparison]" in report
        assert "Summary: 12 tasks across 3 rounds" in report


class TestStatsCollectorTailLatency:
    """Tests for tail latency analysis in reports."""

    def setup_method(self) -> None:
        self.config = Mock(spec=KernelConfig)
        self.config.workflow_type = "browser"
        self.config.total_count = 2
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    @staticmethod
    def _create_sandbox_with_step_times(index: int, step_times: dict[str, float]) -> BenchSandbox:
        state = BenchSandbox(id=f"sbx-{index}", index=index)
        total_latency = sum(step_times.values())
        state.browser_metrics.add(latency=total_latency, success=True, step_times=step_times)
        state.creation_metrics.status = SandboxStatus.READY
        return state

    def test_step_level_timing_shows_tail_ratio(self):
        """Step-Level Timing should include tail ratio."""
        sandbox_states = {
            1: self._create_sandbox_with_step_times(1, {"open_tab": 0.8, "page_load": 1.2}),
            2: self._create_sandbox_with_step_times(2, {"open_tab": 0.9, "page_load": 1.3}),
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "[Step-Level Timing" in report
        assert "Tail" in report
        assert "open_tab" in report
        assert "page_load" in report

    def test_step_level_timing_shows_percentiles(self):
        """Step-Level Timing should show Avg, P50, P95, P99."""
        sandbox_states = {1: self._create_sandbox_with_step_times(1, {"open_tab": 0.8})}
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert "Avg(ms)" in report
        assert "P50(ms)" in report
        assert "P95(ms)" in report
        assert "P99(ms)" in report

    def test_round_comparison_shows_tail_ratio(self):
        """Round Comparison should include tail ratio."""
        s1 = self._create_sandbox_with_step_times(1, {"open_tab": 1.0})
        s1.browser_metrics.add(latency=2.0, success=True, step_times={"open_tab": 2.0})
        s2 = self._create_sandbox_with_step_times(2, {"open_tab": 2.0})
        s2.browser_metrics.add(latency=3.0, success=True, step_times={"open_tab": 3.0})
        sandbox_states = {1: s1, 2: s2}
        collector = StatsCollector(self.config, sandbox_states)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0, 2: 0},
        }
        collector._round_start_totals[1] = {
            "total": 2,
            "success": 2,
            "sandbox_latency_counts": {1: 1, 2: 1},
        }
        report = collector.generate_report()

        assert "[Round Comparison]" in report
        assert "Tail" in report

    def test_round_comparison_shows_percentiles(self):
        """Round Comparison should show Avg, P50, P95, P99."""
        state = self._create_sandbox_with_step_times(1, {"open_tab": 1.0})
        state.browser_metrics.add(latency=2.0, success=True, step_times={"open_tab": 2.0})
        sandbox_states = {1: state}
        collector = StatsCollector(self.config, sandbox_states)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0},
        }
        collector._round_start_totals[1] = {
            "total": 1,
            "success": 1,
            "sandbox_latency_counts": {1: 1},
        }
        report = collector.generate_report()

        assert "Avg(s)" in report
        assert "P50(s)" in report
        assert "P95(s)" in report
        assert "P99(s)" in report

    def test_tail_latency_severity_classification(self):
        """Report should show severity classification."""
        # Significant tail: most values at 1.0, one at 5.0
        sandbox_states = {
            i: self._create_sandbox_with_step_times(i, {"open_tab": 1.0 if i < 8 else 5.0}) for i in range(10)
        }
        report = StatsCollector(self.config, sandbox_states).generate_report()

        assert any(level in report for level in ["minimal", "moderate", "significant"])


class TestReplayInitialPauseReport:
    """Initial Pause line in the replay stats section (P2 lifecycle)."""

    def _replay_config(self) -> KernelConfig:
        cfg = Mock(spec=KernelConfig)
        cfg.workflow_type = "replay"
        cfg.total_count = 2
        cfg.detect_existing = False
        cfg.create_only = False
        cfg.create_batch_size = None
        cfg.task_batch_size = None
        cfg.test_duration = 60
        cfg.output_dir = "/tmp/test"
        cfg.filename_prefix = "test"
        cfg.stats_interval = 5
        cfg.replay_running_concurrency = None
        return cfg

    def test_initial_pause_line_rendered_when_set(self):
        """A sandbox with initial_pause_sec > 0 surfaces an 'Initial Pause' line."""
        state = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        state.replay_metrics.initial_pause_sec = 0.42
        report = StatsCollector(self._replay_config(), {0: state}).generate_report()

        assert "Initial Pause" in report
        assert "0.420s" in report
        assert "1 sandbox" in report

    def test_initial_pause_line_omitted_when_zero(self):
        """exec_only sandboxes (initial_pause_sec == 0) render no Initial Pause line."""
        state = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        # initial_pause_sec stays 0.0 (default)
        report = StatsCollector(self._replay_config(), {0: state}).generate_report()

        assert "Initial Pause" not in report

    def test_initial_pause_averages_across_sandboxes(self):
        """Multiple paused sandboxes: mean of their initial_pause_sec values."""
        s0 = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        s0.replay_metrics.initial_pause_sec = 0.20
        s1 = BenchSandbox(id="sbx-1", index=1, workflow_type="replay")
        s1.replay_metrics.initial_pause_sec = 0.40
        report = StatsCollector(self._replay_config(), {0: s0, 1: s1}).generate_report()

        assert "Initial Pause" in report
        assert "0.300s" in report  # mean(0.20, 0.40)
        assert "2 sandbox" in report


class TestReplayLifecycleOverheadByRound:
    """Per-round lifecycle overhead sub-table (multi-round replay lifecycle)."""

    def _config(self, replay_mode: str = "lifecycle") -> KernelConfig:
        cfg = Mock(spec=KernelConfig)
        cfg.workflow_type = "replay"
        cfg.replay_mode = replay_mode
        cfg.total_count = 1
        cfg.detect_existing = False
        cfg.create_only = False
        cfg.create_batch_size = None
        cfg.task_batch_size = None
        cfg.test_duration = 60
        cfg.output_dir = "/tmp/test"
        cfg.filename_prefix = "test"
        cfg.stats_interval = 5
        cfg.replay_running_concurrency = None
        return cfg

    @staticmethod
    def _baseline(sandbox_idx: int, resume: int, pause: int, slice_n: int, slot_held: int = 0) -> dict[str, int]:
        return {
            sandbox_idx: {
                "resume_secs": resume,
                "pause_secs": pause,
                "slice_total_secs": slice_n,
                "running_slot_held_secs": slot_held,
            }
        }

    def test_per_round_overhead_rendered_for_lifecycle_multiround(self):
        """lifecycle + 2 rounds -> per-round overhead table with both rows.

        No admission controller -> no Slot held column (it is conditional on
        admission_snapshot being set, mirroring the cumulative section).
        """
        state = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        # Round 0 slice: resume .1 / pause .05 / slice 1.0 -> 15.0% overhead
        state.replay_metrics.add(latency=1.0, success=True, resume_sec=0.1, pause_sec=0.05, slice_total_sec=1.0)
        # Round 1 slice: resume .2 / pause .1 / slice 1.5 -> 20.0% overhead
        state.replay_metrics.add(latency=1.5, success=True, resume_sec=0.2, pause_sec=0.1, slice_total_sec=1.5)

        collector = StatsCollector(self._config(), {0: state})
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {0: 0},
            "replay_baselines": self._baseline(0, 0, 0, 0),
        }
        collector._round_start_totals[1] = {
            "total": 1,
            "success": 1,
            "sandbox_latency_counts": {0: 1},
            "replay_baselines": self._baseline(0, 1, 1, 1),
        }
        report = collector.generate_report()

        assert "[Lifecycle Overhead by Round]" in report
        assert "Overhead%" in report
        assert "Resume P95(s)" in report  # P95 columns now present
        assert "15.0" in report  # round 0 overhead
        assert "20.0" in report  # round 1 overhead
        # No admission controller -> Slot held column omitted.
        assert "Slot held" not in report

    def test_per_round_slot_held_column_when_admission_present(self):
        """With an admission controller, the per-round Slot held column renders."""
        state = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        # Round 0: slot held 0.30; Round 1: slot held 0.50
        state.replay_metrics.add(
            latency=1.0,
            success=True,
            resume_sec=0.1,
            pause_sec=0.05,
            slice_total_sec=1.0,
            running_slot_held_sec=0.30,
        )
        state.replay_metrics.add(
            latency=1.5,
            success=True,
            resume_sec=0.2,
            pause_sec=0.1,
            slice_total_sec=1.5,
            running_slot_held_sec=0.50,
        )

        collector = StatsCollector(self._config(), {0: state})
        collector.admission_snapshot = {"running_slots": {}}  # non-None -> admission active
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {0: 0},
            "replay_baselines": self._baseline(0, 0, 0, 0, 0),
        }
        collector._round_start_totals[1] = {
            "total": 1,
            "success": 1,
            "sandbox_latency_counts": {0: 1},
            "replay_baselines": self._baseline(0, 1, 1, 1, 1),
        }
        report = collector.generate_report()

        assert "[Lifecycle Overhead by Round]" in report
        assert "Slot held P50(s)" in report
        # Round 0 slot held P50 = 0.300; round 1 = 0.500
        assert "0.300" in report
        assert "0.500" in report

    def test_per_round_overhead_omitted_for_exec_only(self):
        """exec_only has no lifecycle data -> sub-table skipped, task table kept."""
        state = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        state.replay_metrics.add(latency=1.0, success=True)
        state.replay_metrics.add(latency=1.5, success=True)

        collector = StatsCollector(self._config("exec_only"), {0: state})
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {0: 0},
            "replay_baselines": self._baseline(0, 0, 0, 0),
        }
        collector._round_start_totals[1] = {
            "total": 1,
            "success": 1,
            "sandbox_latency_counts": {0: 1},
            "replay_baselines": self._baseline(0, 0, 0, 0),
        }
        report = collector.generate_report()

        assert "[Lifecycle Overhead by Round]" not in report
        # Multi-round task comparison still renders.
        assert "[Round Comparison]" in report

    def test_per_round_overhead_omitted_for_single_round(self):
        """Single round -> both round comparison and per-round overhead suppressed."""
        state = BenchSandbox(id="sbx-0", index=0, workflow_type="replay")
        state.replay_metrics.add(latency=1.0, success=True, resume_sec=0.1, pause_sec=0.05, slice_total_sec=1.0)

        collector = StatsCollector(self._config(), {0: state})
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {0: 0},
            "replay_baselines": self._baseline(0, 0, 0, 0),
        }
        report = collector.generate_report()

        assert "[Lifecycle Overhead by Round]" not in report
        assert "[Round Comparison]" not in report  # single-round redundancy suppressed
