"""
Test Tab Operation Runner Module

Tests for TabOperationRunner exception classification and failure handling:
- _classify_exception: unreachable / open_tab / snapshot / unknown / exception
  branches, and that reported timeout values track the class constants.
- _handle_failure: error log includes sandbox_id and round_id, and the
  consecutive-error circuit breaker marks the sandbox dead after 3 failures.
"""

import threading
from unittest.mock import Mock

import pytest

from e2b_bench.config import Config
from e2b_bench.schemas import SandboxState
from e2b_bench.task_runner import TabOperationRunner


def _make_runner(round_id: int = 0, sandbox_id: int = 7) -> TabOperationRunner:
    """Build a TabOperationRunner without launching its thread."""
    state = SandboxState(sandbox_id=sandbox_id)
    state.sandbox_obj = Mock()
    config = Config()
    stop_event = threading.Event()
    return TabOperationRunner(state, config, stop_event, round_id=round_id)


class TestClassifyException:
    """Tests for TabOperationRunner._classify_exception."""

    def test_unreachable_routing_failure(self):
        """E2B routing failure is bucketed as 'unreachable', not 'exception'."""
        runner = _make_runner()
        exc = RuntimeError("Failed to route request to sandbox sbx-abc")
        step, detail = runner._classify_exception(exc, {})
        assert step == "unreachable"
        assert "sandbox unreachable" in detail
        assert "Failed to route" in detail

    def test_unreachable_takes_precedence_over_timeout_keyword(self):
        """A routing error must not be misread as a step timeout."""
        runner = _make_runner()
        exc = RuntimeError("Failed to route request to sandbox (request timed out)")
        step, _ = runner._classify_exception(exc, {})
        assert step == "unreachable"

    def test_open_tab_timeout_when_step_unstarted(self):
        """Timeout before open_tab recorded is classified as open_tab."""
        runner = _make_runner()
        exc = TimeoutError("context deadline exceeded")
        step, detail = runner._classify_exception(exc, {})
        assert step == "open_tab"
        assert str(runner.OPEN_TAB_TIMEOUT) in detail

    def test_snapshot_timeout_when_open_tab_done(self):
        """Timeout after open_tab but before snapshot is classified as snapshot."""
        runner = _make_runner()
        exc = TimeoutError("operation timed out")
        step, detail = runner._classify_exception(exc, {"open_tab": 0.5})
        assert step == "snapshot"
        assert str(runner.SNAPSHOT_TIMEOUT) in detail

    def test_unknown_timeout_when_all_known_steps_done(self):
        """Timeout after both open_tab and snapshot is 'unknown'."""
        runner = _make_runner()
        exc = TimeoutError("timed out")
        step, detail = runner._classify_exception(exc, {"open_tab": 0.5, "snapshot": 0.3})
        assert step == "unknown"
        assert "timed out" in detail

    def test_other_exception(self):
        """Non-timeout, non-routing errors fall through to 'exception'."""
        runner = _make_runner()
        exc = ValueError("bad value")
        step, detail = runner._classify_exception(exc, {})
        assert step == "exception"
        assert "bad value" in detail

    def test_timeout_value_tracks_constant(self):
        """Reported timeout tracks the class constant, not a hardcoded literal.

        Raising OPEN_TAB_TIMEOUT must change the message — proves the value
        is sourced from the constant rather than a duplicated magic number.
        """
        runner = _make_runner()
        original = runner.OPEN_TAB_TIMEOUT
        try:
            type(runner).OPEN_TAB_TIMEOUT = original + 60
            _, detail = runner._classify_exception(TimeoutError("timed out"), {})
            assert str(original + 60) in detail
            assert str(original) + "s" not in detail or str(original + 60) in detail
        finally:
            type(runner).OPEN_TAB_TIMEOUT = original


class TestHandleFailureLogging:
    """Tests for TabOperationRunner._handle_failure."""

    def test_log_includes_sandbox_id_and_round(self, caplog):
        """Failure log must surface sandbox_id and round_id for diagnosis."""
        runner = _make_runner(round_id=4, sandbox_id=12)
        with caplog.at_level("ERROR", logger="e2b_bench.task_runner"):
            runner._handle_failure("https://example.com/page", "snapshot", "snapshot failed: exit_code=1")
        assert any("Sandbox12" in r.message and "Round 4" in r.message for r in caplog.records)

    def test_unreachable_failure_logged_with_round(self, caplog):
        """Routing failures classified as unreachable are logged with round_id."""
        runner = _make_runner(round_id=9, sandbox_id=3)
        with caplog.at_level("ERROR", logger="e2b_bench.task_runner"):
            runner._handle_failure("https://example.com", "unreachable", "sandbox unreachable: Failed to route")
        assert any("Sandbox3" in r.message and "Round 9" in r.message for r in caplog.records)
        assert any("unreachable" in r.message for r in caplog.records)

    def test_consecutive_errors_disable_sandbox(self):
        """Three consecutive failures mark the sandbox as not alive."""
        runner = _make_runner(round_id=0, sandbox_id=1)
        assert runner.state.is_alive is True
        for _ in range(3):
            runner._handle_failure("https://example.com", "snapshot", "snapshot failed")
        assert runner.state.is_alive is False
        assert runner.consecutive_errors == 3

    def test_two_failures_keep_sandbox_alive(self):
        """Fewer than three consecutive failures keep the sandbox alive."""
        runner = _make_runner(round_id=0, sandbox_id=1)
        for _ in range(2):
            runner._handle_failure("https://example.com", "snapshot", "snapshot failed")
        assert runner.state.is_alive is True
        assert runner.consecutive_errors == 2
