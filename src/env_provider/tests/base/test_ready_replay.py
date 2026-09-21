# src/env_provider/tests/test_ready_replay.py
from __future__ import annotations

import threading

from env_provider._ready import ReadyChecker


def _ok_exec(handle, command, timeout):
    return 0, "Linux sandbox 5.15.0 #1", ""


def test_ready_checker_replay_uses_command_probe():
    stop = threading.Event()
    checker = ReadyChecker(stop, _ok_exec)
    result = checker.check(handle=object(), workflow_type="replay", label="sbx0")
    assert result["success"] is True
    assert result["error"] == ""
