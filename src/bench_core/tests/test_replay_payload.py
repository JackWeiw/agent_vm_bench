from __future__ import annotations

from bench_core.replay_payload import ReplayStep, Trajectory, classify_action


def test_classify_action_shell():
    assert classify_action("find /testbed -type f -name '*.py'") == "shell"


def test_classify_action_str_replace_editor():
    assert classify_action("str_replace_editor str_replace --old_str a --new_str b") == "str_replace_editor"


def test_classify_action_bash():
    assert classify_action("bash -lc 'echo hi'") == "bash"


def test_classify_action_terminal_submit():
    assert classify_action("submit") == "submit"


def test_classify_action_terminal_finish():
    assert classify_action("finish") == "finish"


def test_replay_step_is_frozen():
    step = ReplayStep(index=0, action="ls", delay_time_sec=1.0, action_type="shell")
    assert step.index == 0
    try:
        step.index = 1  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ReplayStep must be frozen")
