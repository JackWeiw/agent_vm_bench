from __future__ import annotations

from pathlib import Path

import pytest

from bench_core.replay_payload import ReplayStep, Trajectory, classify_action, load_trajectory

FIXTURES = Path(__file__).parent / "fixtures" / "replay"


# ---------------------------------------------------------------------------
# classify_action
# ---------------------------------------------------------------------------


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


def test_classify_action_terminal_done():
    assert classify_action("done") == "done"


def test_classify_action_empty_and_whitespace():
    assert classify_action("") == "shell"
    assert classify_action("   ") == "shell"


# ---------------------------------------------------------------------------
# ReplayStep / Trajectory frozen dataclasses
# ---------------------------------------------------------------------------


def test_replay_step_is_frozen():
    step = ReplayStep(index=0, action="ls", delay_time_sec=1.0, action_type="shell")
    with pytest.raises(AttributeError):
        step.index = 1  # type: ignore[misc]


def test_trajectory_construction():
    step = ReplayStep(index=0, action="ls", delay_time_sec=1.0, action_type="shell")
    traj = Trajectory(path=Path("/tmp/t.json"), instance_id="x", environment="main", steps=(step,))
    assert len(traj.steps) == 1


# ---------------------------------------------------------------------------
# load_trajectory
# ---------------------------------------------------------------------------


def test_load_trajectory_truncates_at_terminal():
    traj = load_trajectory(FIXTURES / "with_terminal.replay.json")
    assert traj.instance_id == "django-money__django-money.835c1ab8"
    assert traj.environment == "main"
    # terminal submit excluded; its delay_time discarded; 2 executable steps remain.
    assert len(traj.steps) == 2
    assert traj.steps[0].index == 0
    assert traj.steps[0].action_type == "shell"
    assert traj.steps[1].action_type == "str_replace_editor"
    # delay on the terminal step must NOT surface in the executable steps.
    assert traj.steps[0].delay_time_sec == 3.0
    assert traj.steps[1].delay_time_sec == 11.8


def test_load_trajectory_no_terminal_keeps_all():
    traj = load_trajectory(FIXTURES / "no_terminal.json")
    assert len(traj.steps) == 2  # no truncation; no error


def test_load_trajectory_instance_id_from_file_wins():
    traj = load_trajectory(FIXTURES / "no_terminal.json")
    # instance_id present in file wins.
    assert traj.instance_id == "interrupted-traj"


def test_load_trajectory_missing_instance_id_uses_filename_stem(tmp_path):
    import json
    from bench_core.replay_payload import load_trajectory

    p = tmp_path / "_no_id.json"
    p.write_text(json.dumps({"environment": "main", "trajectory": [{"action": "ls", "delay_time": 1.0}]}))
    traj = load_trajectory(p)
    assert traj.instance_id == "_no_id"
