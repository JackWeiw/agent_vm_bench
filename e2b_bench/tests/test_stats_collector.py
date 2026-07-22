"""
Test Stats Collector Module

Tests for statistics collection, error classification, round comparison,
and tail latency analysis
"""

from unittest.mock import Mock, patch

import pytest

from e2b_bench.config import Config
from e2b_bench.schemas import BrowserMetrics, SandboxState, SandboxStatus
from e2b_bench.stats_collector import ReportFormatter, StatsCollector


class TestStatsCollectorErrorClassification:
    """Tests for error type classification in generate_report"""

    def setup_method(self):
        """Set up test fixtures"""
        self.config = Mock(spec=Config)
        self.config.template = "test-template"
        self.config.total_count = 5
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    def _create_sandbox_with_error(self, sandbox_id: int, error_msg: str, fail_count: int = 1):
        """Helper to create a sandbox state with a specific error"""
        state = SandboxState(sandbox_id=sandbox_id)
        state.browser_metrics = BrowserMetrics()
        for _ in range(fail_count):
            state.browser_metrics.add(latency=1.0, success=False)
        state.browser_metrics._last_error = error_msg
        return state

    def test_classify_open_tab_failed(self):
        """Open tab failed should be classified correctly"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "open_tab failed: exit_code=1 | stderr=error"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Open tab failed" in report
        assert "1" in report  # count

    def test_classify_page_load_failed(self):
        """Page load failed should be classified correctly"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "page_load failed: exit_code=1 | url=https://example.com"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Page load failed" in report
        assert "1" in report

    def test_classify_snapshot_failed(self):
        """Snapshot failed should be classified correctly"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "snapshot failed: exit_code=127 | stderr=not found"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Snapshot failed" in report
        assert "1" in report

    def test_classify_click_failed(self):
        """Click failed should be classified correctly"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "click failed: exit_code=1 | element=e1"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Click failed" in report
        assert "1" in report

    def test_classify_screenshot_failed(self):
        """Screenshot failed should be classified correctly"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "screenshot failed: exit_code=1"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Screenshot failed" in report
        assert "1" in report

    def test_classify_timeout(self):
        """Timeout errors should be classified correctly"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "operation timed out after 60s"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Timeout" in report
        assert "1" in report

    def test_classify_timeout_with_timed_out_keyword(self):
        """'timed out' keyword should also be classified as Timeout"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "request timed out"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Timeout" in report

    def test_classify_legacy_chrome_start_failed(self):
        """Legacy Chrome start failed should still be classified"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "failed to start chrome: process exited"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Chrome start failed" in report

    def test_classify_legacy_dbus_error(self):
        """Legacy D-Bus error should still be classified"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "failed to connect to the bus: D-Bus error"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "D-Bus connection error" in report

    def test_classify_legacy_gateway_error(self):
        """Legacy gateway error should still be classified"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "gateway connection failed: http_unreachable"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Gateway connection error" in report

    def test_classify_other_error(self):
        """Unknown errors should be classified as Other"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "some unknown error occurred"),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Other" in report

    def test_multiple_error_types(self):
        """Multiple error types should all be classified"""
        sandbox_states = {
            1: self._create_sandbox_with_error(1, "open_tab failed: exit_code=1", 2),
            2: self._create_sandbox_with_error(2, "snapshot failed: exit_code=1", 3),
            3: self._create_sandbox_with_error(3, "timeout occurred", 1),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Open tab failed" in report
        assert "Snapshot failed" in report
        assert "Timeout" in report


class TestStatsCollectorRoundComparison:
    """Tests for round comparison latency calculation"""

    def setup_method(self):
        """Set up test fixtures"""
        self.config = Mock(spec=Config)
        self.config.template = "test-template"
        self.config.total_count = 3
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    def _create_sandbox_with_latencies(self, sandbox_id: int, latencies: list):
        """Helper to create a sandbox state with specific latencies"""
        state = SandboxState(sandbox_id=sandbox_id)
        state.browser_metrics = BrowserMetrics()
        for lat in latencies:
            state.browser_metrics.add(latency=lat, success=True)
        state.creation_metrics.status = SandboxStatus.PORT_READY
        return state

    def test_round_comparison_no_rounds(self):
        """No round comparison should appear without round data"""
        sandbox_states = {
            1: self._create_sandbox_with_latencies(1, [1.0, 2.0, 3.0]),
        }
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        # Should not have round comparison section
        assert "[Round Comparison]" not in report

    def test_round_comparison_with_single_round(self):
        """Single round should show correct stats"""
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

        assert "[Round Comparison]" in report
        assert "Summary: 6 tasks across 1 rounds" in report

    def test_round_comparison_latency_extraction(self):
        """Round latency should be extracted from correct range"""
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
        # Round 0: 4 tasks (2 per sandbox)
        # Round 1: 4 tasks (2 per sandbox)
        assert "Summary: 8 tasks across 2 rounds" in report

    def test_error_message_truncation(self):
        """Error messages should be truncated in report"""
        long_error = "x" * 200  # Very long error message
        state = SandboxState(sandbox_id=1)
        state.browser_metrics = BrowserMetrics()
        state.browser_metrics.add(latency=1.0, success=False)
        state.browser_metrics._last_error = long_error

        sandbox_states = {1: state}
        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        # Error should be truncated to 150 chars in display
        assert "x" * 200 not in report  # Full 200-char string should not appear
        # But error type classification should still work
        assert "Other" in report


class TestStatsCollectorRoundBaselineTiming:
    """Tests for round baseline timing correctness.

    The key bug: set_round(round_id) records the baseline at round START,
    before tasks begin. But tasks from the PREVIOUS round may not have
    finished yet (they run in separate threads). This causes:

    - Round 0 baseline = 0 (correct, no prior tasks)
    - Round 1 baseline = 0 (wrong! should be 20, because Round 0 tasks
      haven't completed yet when set_round(1) is called)
    - Round 3 shows cumulative of Round 2 + Round 3 (because Round 3
      baseline was wrong)

    The fix: baseline should be recorded AFTER the previous round's tasks
    have completed, not at the start of the next round.
    """

    def setup_method(self):
        """Set up test fixtures"""
        self.config = Mock(spec=Config)
        self.config.template = "test-template"
        self.config.total_count = 4
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    def _create_sandbox_with_latencies(self, sandbox_id: int, latencies: list):
        """Helper to create a sandbox state with specific latencies"""
        state = SandboxState(sandbox_id=sandbox_id)
        state.browser_metrics = BrowserMetrics()
        for lat in latencies:
            state.browser_metrics.add(latency=lat, success=True)
        state.creation_metrics.status = SandboxStatus.PORT_READY
        return state

    def test_four_rounds_each_20_tasks(self):
        """4 rounds of 20 sandboxes each should show 20 tasks per round.

        This is the core scenario from the bug report:
        80 sandboxes, 4 rounds, each round has 20 sandboxes doing 1 task.
        Expected: Round 0=20, Round 1=20, Round 2=20, Round 3=20

        The fix records baselines AFTER tasks complete:
        - Round 0: baseline=0 (initialized before any round starts)
        - Round 1: baseline=20 (recorded after Round 0 completes)
        - Round 2: baseline=40 (recorded after Round 1 completes)
        - Round 3: baseline=60 (recorded after Round 2 completes)
        - Final:    total=80
        """
        # 80 sandboxes, each completing 1 task
        sandbox_states = {
            i: self._create_sandbox_with_latencies(i, [46.23])
            for i in range(80)
        }

        collector = StatsCollector(self.config, sandbox_states)

        # Simulate correct baseline recording (after tasks complete)
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

        report = collector.generate_report()

        assert "[Round Comparison]" in report
        assert "Summary: 80 tasks across 4 rounds" in report

        # Each round should have 20 tasks
        # Parse the report to verify round tasks
        formatter = ReportFormatter(self.config, sandbox_states)
        round_finals = formatter._calculate_round_finals(collector._round_start_totals)

        assert round_finals[0]["tasks"] == 20
        assert round_finals[1]["tasks"] == 20
        assert round_finals[2]["tasks"] == 20
        assert round_finals[3]["tasks"] == 20

    def test_buggy_baseline_shows_wrong_tasks(self):
        """Demonstrate the bug: wrong baseline timing gives wrong task counts.

        When baseline is recorded at round START (before tasks complete):
        - Round 0: baseline=0 (correct)
        - Round 1: baseline=0 (wrong! tasks from Round 0 haven't completed)
        - Round 2: baseline=20 (wrong! only Round 0 tasks completed)
        - Round 3: baseline=40 (wrong! Round 2 tasks haven't completed yet)

        Calculation:
        - Round 0: totals[1]-totals[0] = 0-0 = 0 (wrong!)
        - Round 1: totals[2]-totals[1] = 20-0 = 20 (wrong attribution!)
        - Round 2: totals[3]-totals[2] = 40-20 = 20 (wrong attribution!)
        - Round 3: final-totals[3] = 80-40 = 40 (cumulative of Round 2+3!)

        This matches the user's observed output exactly.
        """
        sandbox_states = {
            i: self._create_sandbox_with_latencies(i, [46.23])
            for i in range(80)
        }

        collector = StatsCollector(self.config, sandbox_states)

        # Simulate BUGGY baseline (recorded at round start before tasks complete)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {i: 0 for i in range(80)},
        }
        # Round 1 baseline recorded before Round 0 tasks completed
        collector._round_start_totals[1] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {i: 0 for i in range(80)},
        }
        # Round 2 baseline recorded after only Round 0 tasks completed
        collector._round_start_totals[2] = {
            "total": 20,
            "success": 20,
            "sandbox_latency_counts": {i: 1 for i in range(20)} | {i: 0 for i in range(20, 80)},
        }
        # Round 3 baseline recorded after only Round 0 and Round 1 tasks completed
        collector._round_start_totals[3] = {
            "total": 40,
            "success": 40,
            "sandbox_latency_counts": {i: 1 for i in range(40)} | {i: 0 for i in range(40, 80)},
        }

        formatter = ReportFormatter(self.config, sandbox_states)
        round_finals = formatter._calculate_round_finals(collector._round_start_totals)

        # BUGGY results matching user's observed output:
        assert round_finals[0]["tasks"] == 0   # BUG: should be 20
        assert round_finals[1]["tasks"] == 20  # Wrong attribution (Round 0's data)
        assert round_finals[2]["tasks"] == 20  # Wrong attribution (Round 1's data)
        assert round_finals[3]["tasks"] == 40  # BUG: cumulative of Round 2 + Round 3


class TestStatsCollectorRoundLatencyDelta:
    """Tests for round latency delta calculation using get_latencies_since"""

    def setup_method(self):
        """Set up test fixtures"""
        self.config = Mock(spec=Config)
        self.config.template = "test-template"
        self.config.total_count = 2
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    def _create_sandbox_with_latencies(self, sandbox_id: int, latencies: list):
        """Helper to create a sandbox state with specific latencies"""
        state = SandboxState(sandbox_id=sandbox_id)
        state.browser_metrics = BrowserMetrics()
        for lat in latencies:
            state.browser_metrics.add(latency=lat, success=True)
        state.creation_metrics.status = SandboxStatus.PORT_READY
        return state

    def test_latency_extraction_per_round(self):
        """Each round should extract its own latencies correctly"""
        # Sandbox 1: 6 latencies total (2 per round for 3 rounds)
        # Sandbox 2: 6 latencies total (2 per round for 3 rounds)
        sandbox_states = {
            1: self._create_sandbox_with_latencies(1, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
            2: self._create_sandbox_with_latencies(2, [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]),
        }

        collector = StatsCollector(self.config, sandbox_states)

        # Round 0 baseline
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0, 2: 0},
        }

        # Round 1 baseline (after 2 latencies each)
        collector._round_start_totals[1] = {
            "total": 4,
            "success": 4,
            "sandbox_latency_counts": {1: 2, 2: 2},
        }

        # Round 2 baseline (after 4 latencies each)
        collector._round_start_totals[2] = {
            "total": 8,
            "success": 8,
            "sandbox_latency_counts": {1: 4, 2: 4},
        }

        report = collector.generate_report()

        # Verify round comparison exists
        assert "[Round Comparison]" in report
        assert "Summary: 12 tasks across 3 rounds" in report

        # Round 0 should have avg of [1.0, 2.0, 2.0, 3.0] = 2.0
        # Round 1 should have avg of [3.0, 4.0, 4.0, 5.0] = 4.0
        # Round 2 should have avg of [5.0, 6.0, 6.0, 7.0] = 6.0
        # Note: The report shows latency in seconds, so we check for these values


class TestStatsCollectorTailLatency:
    """Tests for tail latency analysis in reports"""

    def setup_method(self):
        """Set up test fixtures"""
        self.config = Mock(spec=Config)
        self.config.template = "test-template"
        self.config.total_count = 2
        self.config.detect_existing = False
        self.config.create_only = False
        self.config.create_batch_size = None
        self.config.task_batch_size = None
        self.config.test_duration = 60
        self.config.output_dir = "/tmp/test"
        self.config.filename_prefix = "test"
        self.config.stats_interval = 5

    def _create_sandbox_with_step_times(self, sandbox_id: int, step_times: dict):
        """Helper to create a sandbox state with step times"""
        state = SandboxState(sandbox_id=sandbox_id)
        state.browser_metrics = BrowserMetrics()
        # Add step times using the add method
        total_latency = sum(step_times.values())
        state.browser_metrics.add(latency=total_latency, success=True, step_times=step_times)
        state.creation_metrics.status = SandboxStatus.PORT_READY
        return state

    def test_step_level_timing_shows_tail_ratio(self):
        """Step-Level Timing should include tail ratio"""
        sandbox_states = {
            1: self._create_sandbox_with_step_times(1, {"open_tab": 0.8, "page_load": 1.2}),
            2: self._create_sandbox_with_step_times(2, {"open_tab": 0.9, "page_load": 1.3}),
        }

        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "[Step-Level Timing" in report
        assert "Tail" in report
        assert "open_tab" in report
        assert "page_load" in report

    def test_step_level_timing_shows_percentiles(self):
        """Step-Level Timing should show Avg, P50, P95, P99"""
        sandbox_states = {
            1: self._create_sandbox_with_step_times(1, {"open_tab": 0.8}),
        }

        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        assert "Avg(ms)" in report
        assert "P50(ms)" in report
        assert "P95(ms)" in report
        assert "P99(ms)" in report

    def test_round_comparison_shows_tail_ratio(self):
        """Round Comparison should include tail ratio"""
        sandbox_states = {
            1: self._create_sandbox_with_step_times(1, {"open_tab": 1.0}),
            2: self._create_sandbox_with_step_times(2, {"open_tab": 2.0}),
        }

        collector = StatsCollector(self.config, sandbox_states)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0, 2: 0},
        }

        report = collector.generate_report()

        assert "[Round Comparison]" in report
        assert "Tail" in report

    def test_round_comparison_shows_percentiles(self):
        """Round Comparison should show Avg, P50, P95, P99"""
        sandbox_states = {
            1: self._create_sandbox_with_step_times(1, {"open_tab": 1.0}),
        }

        collector = StatsCollector(self.config, sandbox_states)
        collector._round_start_totals[0] = {
            "total": 0,
            "success": 0,
            "sandbox_latency_counts": {1: 0},
        }

        report = collector.generate_report()

        assert "Avg(s)" in report
        assert "P50(s)" in report
        assert "P95(s)" in report
        assert "P99(s)" in report

    def test_tail_latency_severity_classification(self):
        """Report should show severity classification"""
        # Create significant tail: most values at 1.0, one at 5.0
        sandbox_states = {
            i: self._create_sandbox_with_step_times(i, {"open_tab": 1.0 if i < 8 else 5.0}) for i in range(10)
        }

        collector = StatsCollector(self.config, sandbox_states)
        report = collector.generate_report()

        # Should show one of the severity levels
        assert any(level in report for level in ["minimal", "moderate", "significant"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
