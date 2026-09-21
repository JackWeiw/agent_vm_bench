"""Tests for the shared workflow-driven :class:`ReadyChecker`.

The checker owns the poll-until-ready algorithm; the backend exec primitive is
injected as a closure. These tests verify the per-workflow retry semantics
(document: completed-non-zero is an immediate failure; coding: retry on
exception; ports: swallow per-port errors) + stop-event cancellation, using a
fake exec_fn -- no SDK is involved.
"""
from __future__ import annotations

from threading import Event

import pytest

from env_provider._ready import ReadyChecker


def _checker(stop_event: Event | None = None, **kw) -> ReadyChecker:
    return ReadyChecker(stop_event or Event(), _no_exec, max_wait=2, interval=1, **kw)


def _no_exec(handle, cmd, timeout):  # pragma: no cover - replaced per-test
    raise AssertionError("exec_fn not configured")


# --------------------------------------------------------------------------- coding
class TestCoding:
    def test_ready_on_first_success(self):
        calls = []

        def exec_fn(handle, cmd, timeout):
            calls.append(cmd)
            return 0, "Linux sandbox 6.8.0\n", ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=2, interval=1)
        result = checker.check(object(), "coding", "Sandbox1")

        assert result["success"] is True
        assert "uname" in calls[0]
        assert result["wait_elapsed"] >= 0.0

    def test_retries_on_exception_then_succeeds(self):
        attempts = {"n": 0}

        def exec_fn(handle, cmd, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("service starting")
            return 0, "Linux sandbox\n", ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=5, interval=0.01)
        result = checker.check(object(), "coding", "Sandbox1")

        assert result["success"] is True
        assert attempts["n"] == 3

    def test_empty_stdout_not_ready(self):
        def exec_fn(handle, cmd, timeout):
            return 0, "   \n", ""  # exit 0 but blank stdout -> not ready

        checker = ReadyChecker(Event(), exec_fn, max_wait=0.05, interval=0.01)
        result = checker.check(object(), "coding", "Sandbox1")

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_timeout_returns_failure(self):
        def exec_fn(handle, cmd, timeout):
            raise RuntimeError("stuck")

        checker = ReadyChecker(Event(), exec_fn, max_wait=0.05, interval=0.01)
        result = checker.check(object(), "coding", "Sandbox1")

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_stop_event_cancels(self):
        stop = Event()

        def exec_fn(handle, cmd, timeout):
            stop.set()  # signal stop on first probe
            return 1, "", ""

        checker = ReadyChecker(stop, exec_fn, max_wait=2, interval=0.01)
        result = checker.check(object(), "coding", "Sandbox1")

        assert result["success"] is False
        assert result["error"] == "Stop event"


# ------------------------------------------------------------------------- document
class TestDocument:
    def test_ready_on_exit_zero(self):
        def exec_fn(handle, cmd, timeout):
            assert "document-bench-validate" in cmd
            return 0, "ok\n", ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=2, interval=1)
        result = checker.check(object(), "document", "Sandbox2")

        assert result["success"] is True

    def test_completed_nonzero_is_immediate_failure_not_retried(self):
        attempts = {"n": 0}

        def exec_fn(handle, cmd, timeout):
            attempts["n"] += 1
            return 2, "", "validator: missing asset\n"

        checker = ReadyChecker(Event(), exec_fn, max_wait=2, interval=0.01)
        result = checker.check(object(), "document", "Sandbox2")

        assert result["success"] is False
        assert attempts["n"] == 1, "completed non-zero exit must NOT be retried"
        assert "validation failed" in result["error"]
        assert "missing asset" in result["error"]

    def test_retries_on_exception_then_succeeds(self):
        attempts = {"n": 0}

        def exec_fn(handle, cmd, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("command service starting")
            return 0, "", ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=5, interval=0.01)
        result = checker.check(object(), "document", "Sandbox2")

        assert result["success"] is True
        assert attempts["n"] == 3


# -------------------------------------------------------------------------- browser
class TestBrowser:
    def test_ready_when_all_ports_listening(self):
        seen = []

        def exec_fn(handle, cmd, timeout):
            # Both ports present in the probe output from the first call.
            seen.append(cmd)
            return 0, "0.0.0.0:18789 0.0.0.0:*\n", ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=2, interval=1, ports=[18789, 11436])
        result = checker.check(object(), "browser", "Sandbox3")

        assert result["success"] is True

    def test_partial_ports_keeps_polling(self):
        calls = {"n": 0}

        def exec_fn(handle, cmd, timeout):
            calls["n"] += 1
            # 18789 ready immediately; 11436 only after the 3rd port probe.
            if ":11436" in cmd and calls["n"] < 3:
                return 1, "", ""
            return 0, "0.0.0.0:%s 0.0.0.0:*\n" % ("18789" if "18789" in cmd else "11436"), ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=2, interval=0.01, ports=[18789, 11436])
        result = checker.check(object(), "browser", "Sandbox3")

        assert result["success"] is True

    def test_per_port_exception_swallowed(self):
        def exec_fn(handle, cmd, timeout):
            if ":18789" in cmd:
                raise RuntimeError("ss missing")  # swallowed, keep polling
            return 0, "0.0.0.0:11436 0.0.0.0:*\n", ""

        checker = ReadyChecker(Event(), exec_fn, max_wait=0.05, interval=0.01, ports=[18789, 11436])
        result = checker.check(object(), "browser", "Sandbox3")

        assert result["success"] is False  # 18789 never confirmed
        assert "18789" in result["error"]

    def test_timeout_lists_missing_ports(self):
        def exec_fn(handle, cmd, timeout):
            return 1, "", ""  # nothing listening

        checker = ReadyChecker(Event(), exec_fn, max_wait=0.05, interval=0.01, ports=[18789, 11436])
        result = checker.check(object(), "browser", "Sandbox3")

        assert result["success"] is False
        assert "18789" in result["error"] and "11436" in result["error"]


# ----------------------------------------------------------------------- dispatch
class TestDispatch:
    def test_unsupported_workflow_raises(self):
        checker = ReadyChecker(Event(), _no_exec, max_wait=1, interval=1)
        with pytest.raises(ValueError, match="Unsupported workflow_type"):
            checker.check(object(), "database", "Sandbox9")

    def test_no_handle_returns_failure(self):
        checker = ReadyChecker(Event(), _no_exec, max_wait=1, interval=1)
        for wf in ("coding", "document", "browser"):
            result = checker.check(None, wf, "SandboxX")
            assert result["success"] is False
            assert result["error"] == "No sandbox handle"
