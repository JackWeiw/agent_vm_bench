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


# ---------------------------------------------------------------------------
# find_trajectories
# ---------------------------------------------------------------------------


def test_find_trajectories_globs_extensions_and_sorts():
    from bench_core.replay_payload import find_trajectories

    paths = find_trajectories(FIXTURES, "*")
    names = [p.name for p in paths]
    # sorted deterministically by path
    assert names == sorted(names)
    # all four fixtures picked up (.replay.json, .json, .traj)
    assert "with_terminal.replay.json" in names
    assert "no_terminal.json" in names
    assert "empty_steps.traj" in names
    assert "corrupt.json" in names


def test_find_trajectories_respects_glob():
    from bench_core.replay_payload import find_trajectories

    paths = find_trajectories(FIXTURES, "*.replay.json")
    assert [p.name for p in paths] == ["with_terminal.replay.json"]


# ---------------------------------------------------------------------------
# load_pool
# ---------------------------------------------------------------------------


def test_load_pool_skips_corrupt_and_empty(caplog):
    import logging
    from bench_core.replay_payload import Trajectory, load_pool, reset_pool_cache

    class _Cfg:
        replay_trajectory_dir = str(FIXTURES)
        replay_trajectory_glob = "*"
        replay_template_manifest = None

    reset_pool_cache()
    caplog.set_level(logging.WARNING)
    pool = load_pool(_Cfg())  # type: ignore[arg-type]
    # corrupt.json + empty_steps.traj skipped (empty steps); 2 survivors.
    assert len(pool) == 2
    assert all(isinstance(t, Trajectory) for t in pool)
    # a warning was logged for the skipped files.
    assert "corrupt.json" in caplog.text or "empty_steps" in caplog.text


def test_load_pool_is_cached():
    from bench_core.replay_payload import load_pool, reset_pool_cache

    class _Cfg:
        replay_trajectory_dir = str(FIXTURES)
        replay_trajectory_glob = "*"
        replay_template_manifest = None

    reset_pool_cache()
    a = load_pool(_Cfg())  # type: ignore[arg-type]
    b = load_pool(_Cfg())  # type: ignore[arg-type]
    assert a is b  # same cached tuple object


# ---------------------------------------------------------------------------
# load_pool — template manifest
# ---------------------------------------------------------------------------


def _cfg_for_manifest(tmp_path: Path, *, manifest: str | None = None, glob: str = "*.replay.json") -> KernelConfig:
    """Minimal KernelConfig for manifest tests; all other fields use defaults."""
    from bench_core.config import KernelConfig

    return KernelConfig(
        workflow_type="replay",
        total_count=2,
        benchmark_mode="fixed",
        test_duration=1,
        replay_trajectory_dir=str(tmp_path / "traj"),
        replay_trajectory_glob=glob,
        replay_template_manifest=manifest,
    )


def _write_traj(dir_: Path, name: str, instance_id: str) -> Path:
    import json

    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_text(
        json.dumps({"instance_id": instance_id, "trajectory": [{"action": "echo hi", "delay_time": 0}]}),
        encoding="utf-8",
    )
    return p


def test_load_pool_attaches_template_from_manifest(tmp_path):
    from bench_core.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    _write_traj(traj_dir, "b.replay.json", "b")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        __import__("json").dumps({"a.replay.json": "swb-a", "b.replay.json": "swb-b"}), encoding="utf-8"
    )

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    by_id = {t.instance_id: t for t in pool}
    assert by_id["a"].template == "swb-a"
    assert by_id["b"].template == "swb-b"


def test_load_pool_missing_manifest_entry_is_none_with_warning(tmp_path, caplog):
    import logging
    from bench_core.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(__import__("json").dumps({}), encoding="utf-8")  # no entry for a

    reset_pool_cache()
    caplog.set_level(logging.WARNING)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert pool[0].template is None
    assert "no manifest entry" in caplog.text


def test_load_pool_missing_manifest_file_raises(tmp_path):
    from bench_core.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    reset_pool_cache()
    with pytest.raises((FileNotFoundError, ValueError)):
        load_pool(_cfg_for_manifest(tmp_path, manifest=str(tmp_path / "nope.json")))


def test_load_pool_no_manifest_keeps_template_none(tmp_path):
    from bench_core.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=None))
    assert all(t.template is None for t in pool)


def test_load_pool_manifest_key_is_relpath_not_basename(tmp_path):
    """Manifest keys must be ``os.path.relpath(path, trajectory_dir)``, not the
    bare filename.  A subdirectory trajectory with the same basename as a flat
    one would collide under basename lookup; relpath keeps them distinct.
    """
    import json
    from bench_core.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "flat_a")
    _write_traj(traj_dir / "sub", "a.replay.json", "sub_a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"a.replay.json": "swb-flat", "sub/a.replay.json": "swb-sub"}),
        encoding="utf-8",
    )

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), glob="**/*.replay.json"))
    by_id = {t.instance_id: t for t in pool}
    assert by_id["flat_a"].template == "swb-flat"
    assert by_id["sub_a"].template == "swb-sub"


def test_load_pool_cache_invalidates_on_manifest_change(tmp_path):
    import json
    from bench_core.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    m1 = tmp_path / "m1.json"
    m1.write_text(json.dumps({"a.replay.json": "swb-a"}), encoding="utf-8")
    m2 = tmp_path / "m2.json"
    m2.write_text(json.dumps({"a.replay.json": "swb-a2"}), encoding="utf-8")

    reset_pool_cache()
    assert load_pool(_cfg_for_manifest(tmp_path, manifest=str(m1)))[0].template == "swb-a"
    assert load_pool(_cfg_for_manifest(tmp_path, manifest=str(m2)))[0].template == "swb-a2"
