from __future__ import annotations

from pathlib import Path

import pytest

from bench_core.payload.replay_payload import ReplayStep, Trajectory, classify_action, load_trajectory
from bench_core.task_runner.replay import ReplayConfig

FIXTURES = Path(__file__).parent.parent / "fixtures" / "replay"


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

    from bench_core.payload.replay_payload import load_trajectory

    p = tmp_path / "_no_id.json"
    p.write_text(json.dumps({"environment": "main", "trajectory": [{"action": "ls", "delay_time": 1.0}]}))
    traj = load_trajectory(p)
    assert traj.instance_id == "_no_id"


# ---------------------------------------------------------------------------
# find_trajectories
# ---------------------------------------------------------------------------


def test_find_trajectories_globs_extensions_and_sorts():
    from bench_core.payload.replay_payload import find_trajectories

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
    from bench_core.payload.replay_payload import find_trajectories

    paths = find_trajectories(FIXTURES, "*.replay.json")
    assert [p.name for p in paths] == ["with_terminal.replay.json"]


# ---------------------------------------------------------------------------
# load_pool
# ---------------------------------------------------------------------------


def test_load_pool_skips_corrupt_and_empty(caplog):
    import logging

    from bench_core.payload.replay_payload import Trajectory, load_pool, reset_pool_cache

    class _Cfg:
        workflow_config = ReplayConfig(
            replay_trajectory_dir=str(FIXTURES),
            replay_trajectory_glob="*",
            replay_template_manifest=None,
        )

    reset_pool_cache()
    caplog.set_level(logging.WARNING)
    pool = load_pool(_Cfg())  # type: ignore[arg-type]
    # corrupt.json + empty_steps.traj skipped (empty steps); 2 survivors.
    assert len(pool) == 2
    assert all(isinstance(t, Trajectory) for t in pool)
    # a warning was logged for the skipped files.
    assert "corrupt.json" in caplog.text or "empty_steps" in caplog.text


def test_load_pool_is_cached():
    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    class _Cfg:
        workflow_config = ReplayConfig(
            replay_trajectory_dir=str(FIXTURES),
            replay_trajectory_glob="*",
            replay_template_manifest=None,
        )

    reset_pool_cache()
    a = load_pool(_Cfg())  # type: ignore[arg-type]
    b = load_pool(_Cfg())  # type: ignore[arg-type]
    assert a is b  # same cached tuple object


def test_load_pool_trajectory_limit_caps_to_first_n():
    """trajectory_limit caps the pool to the first N per template (natural sort
    within each template). On this single-template fixture that is just the
    first N of the one template. The cap is part of the cache key so a different
    limit does not return the stale unlimited tuple."""
    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    class _CfgLimited:
        workflow_config = ReplayConfig(
            replay_trajectory_dir=str(FIXTURES),
            replay_trajectory_glob="*",
            replay_template_manifest=None,
            replay_trajectory_limit=1,
        )

    reset_pool_cache()
    limited = load_pool(_CfgLimited())  # type: ignore[arg-type]
    # The fixture pool has 2 survivors; limit=1 keeps only the first by sort.
    assert len(limited) == 1

    # A separate unlimited config must still see all 2 (cache key differs).
    class _CfgFull:
        workflow_config = ReplayConfig(
            replay_trajectory_dir=str(FIXTURES),
            replay_trajectory_glob="*",
            replay_template_manifest=None,
        )

    full = load_pool(_CfgFull())  # type: ignore[arg-type]
    assert len(full) == 2


def test_load_pool_trajectory_limit_caps_per_template(tmp_path):
    """trajectory_limit caps EACH template's sub-pool to the first N, not a
    global first-N. With 3 trajectories per template (a1,a2,a3,b1,b2,b3 in
    sort order) and limit=2, a global first-2 cap would yield only [a1,a2]
    and starve template b entirely; the per-template cap yields [a1,a2,b1,b2]
    -- both templates keep their first 2."""
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for i in (1, 2, 3):
        _write_traj(traj_dir, f"a{i}.replay.json", f"a{i}")
        _write_traj(traj_dir, f"b{i}.replay.json", f"b{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({f"{t}{i}.replay.json": f"swb-{t}" for t in ("a", "b") for i in (1, 2, 3)}),
        encoding="utf-8",
    )

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), glob="*.replay.json", limit=2))
    assert len(pool) == 4
    by_tpl: dict[str | None, list[str]] = {}
    for t in pool:
        by_tpl.setdefault(t.template, []).append(t.instance_id)
    # each template keeps its first 2 by natural sort; b is NOT starved.
    assert sorted(by_tpl["swb-a"]) == ["a1", "a2"]
    assert sorted(by_tpl["swb-b"]) == ["b1", "b2"]


# ---------------------------------------------------------------------------
# load_pool — template manifest
# ---------------------------------------------------------------------------


def _cfg_for_manifest(
    tmp_path: Path, *, manifest: str | None = None, glob: str = "*.replay.json", limit: int | None = None
) -> KernelConfig:
    """Minimal KernelConfig for manifest tests; all other fields use defaults."""
    from bench_core.config import KernelConfig
    from bench_core.task_runner.replay import ReplayConfig

    return KernelConfig(
        workflow_type="replay",
        total_count=2,
        benchmark_mode="fixed",
        test_duration=1,
        workflow_config=ReplayConfig(
            replay_trajectory_dir=str(tmp_path / "traj"),
            replay_trajectory_glob=glob,
            replay_template_manifest=manifest,
            replay_trajectory_limit=limit,
        ),
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
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    _write_traj(traj_dir, "b.replay.json", "b")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"a.replay.json": "swb-a", "b.replay.json": "swb-b"}), encoding="utf-8")

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    by_id = {t.instance_id: t for t in pool}
    assert by_id["a"].template == "swb-a"
    assert by_id["b"].template == "swb-b"


def test_load_pool_missing_manifest_entry_is_skipped(tmp_path, caplog):
    """A trajectory absent from the manifest is skipped, not fallen back."""
    import json
    import logging

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({}), encoding="utf-8")  # no entry for a

    reset_pool_cache()
    caplog.set_level(logging.WARNING)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    assert "no manifest entry" in caplog.text


def test_load_pool_missing_manifest_file_raises(tmp_path):
    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    reset_pool_cache()
    with pytest.raises(ValueError, match="template_manifest not readable"):
        load_pool(_cfg_for_manifest(tmp_path, manifest=str(tmp_path / "nope.json")))


def test_load_pool_no_manifest_keeps_template_none(tmp_path):
    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

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

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

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

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    m1 = tmp_path / "m1.json"
    m1.write_text(json.dumps({"a.replay.json": "swb-a"}), encoding="utf-8")
    m2 = tmp_path / "m2.json"
    m2.write_text(json.dumps({"a.replay.json": "swb-a2"}), encoding="utf-8")

    reset_pool_cache()
    assert load_pool(_cfg_for_manifest(tmp_path, manifest=str(m1)))[0].template == "swb-a"
    assert load_pool(_cfg_for_manifest(tmp_path, manifest=str(m2)))[0].template == "swb-a2"


def test_load_pool_non_string_manifest_value_is_skipped(tmp_path, caplog):
    """A manifest entry mapping to a non-string value is skipped, not ignored."""
    import json
    import logging

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"a.replay.json": 123}), encoding="utf-8")

    reset_pool_cache()
    caplog.set_level(logging.WARNING)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    assert "is not a string" in caplog.text


def test_load_pool_empty_string_manifest_value_is_skipped(tmp_path, caplog):
    """An empty-string manifest value is unusable: skip, don't attach template=''."""
    import json
    import logging

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    _write_traj(traj_dir, "a.replay.json", "a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"a.replay.json": ""}), encoding="utf-8")

    reset_pool_cache()
    caplog.set_level(logging.WARNING)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    assert "no manifest entry" in caplog.text


# ---------------------------------------------------------------------------
# Fleet-assignment helpers: _distinct_templates / template_fleet / within_template_rank
# ---------------------------------------------------------------------------


def _traj(template: str | None, iid: str | None = None) -> Trajectory:
    """A throwaway Trajectory for helper tests -- only ``.template`` is consulted."""
    return Trajectory(
        path=Path("/tmp/x"),
        instance_id=iid if iid is not None else (template or "none"),
        environment="main",
        steps=(),
        template=template,
    )


def test_distinct_templates_mixed_none_and_named_does_not_crash():
    """A pool could in principle mix template=None (no manifest) with named
    templates. ``sorted({None, "swb"})`` raises TypeError in Py3 (None < str);
    ``_distinct_templates`` must be total and sort None first."""
    from bench_core.payload.replay_payload import _distinct_templates

    pool = (_traj(None), _traj("swb-a"), _traj(None), _traj("swb-b"))
    assert _distinct_templates(pool) == [None, "swb-a", "swb-b"]


def test_distinct_templates_empty_pool():
    from bench_core.payload.replay_payload import _distinct_templates

    assert _distinct_templates(()) == []


def test_template_fleet_balanced_round_robin():
    """distinct[k % T]: each template gets ~total/T sandboxes regardless of how
    many pool entries each template has (here both templates have 2 entries, but
    the fleet is driven by distinct templates, not pool length)."""
    from bench_core.payload.replay_payload import template_fleet

    pool = (_traj("swb-a"), _traj("swb-b"), _traj("swb-a"), _traj("swb-b"))  # T=2
    assert template_fleet(pool, 6) == {0: "swb-a", 1: "swb-b", 2: "swb-a", 3: "swb-b", 4: "swb-a", 5: "swb-b"}


def test_template_fleet_t_greater_than_total_count():
    """More distinct templates than sandboxes: each of the first ``total`` slots
    gets a distinct template; no template gets two. The fleet is as balanced as
    the small total allows (remainder toward the earlier distinct templates)."""
    from bench_core.payload.replay_payload import template_fleet

    pool = (_traj("a"), _traj("b"), _traj("c"), _traj("d"), _traj("e"))  # T=5
    assert template_fleet(pool, 3) == {0: "a", 1: "b", 2: "c"}


def test_template_fleet_remainder_distributes_to_earlier_distinct():
    """total_count % T != 0: the +1 remainder sandboxes always land on the
    EARLIER distinct templates (a fixed, deterministic bias, locked here). 7
    sandboxes / 3 templates -> a:3, b:2, c:2 (the lone remainder goes to a)."""
    from bench_core.payload.replay_payload import template_fleet

    pool = (_traj("swb-a"), _traj("swb-b"), _traj("swb-c"))  # T=3
    counts: dict[str | None, int] = {}
    for tpl in template_fleet(pool, 7).values():
        counts[tpl] = counts.get(tpl, 0) + 1
    assert counts == {"swb-a": 3, "swb-b": 2, "swb-c": 2}


def test_template_fleet_empty_pool_returns_empty():
    from bench_core.payload.replay_payload import template_fleet

    assert template_fleet((), 10) == {}


def test_template_fleet_single_template_all_same():
    """T=1 (legacy single-template pool, or one template): every sandbox gets it."""
    from bench_core.payload.replay_payload import template_fleet

    pool = (_traj("only"), _traj("only"), _traj("only"))  # T=1
    fleet = template_fleet(pool, 5)
    assert len(fleet) == 5 and all(t == "only" for t in fleet.values())


def test_within_template_rank_is_k_div_T():
    """Under the balanced fleet, a sandbox's within-group rank is exactly k // T
    (all same-template sandboxes share k % T and are ordered 0,1,2,... by k)."""
    from bench_core.payload.replay_payload import within_template_rank

    pool = (_traj("a"), _traj("b"), _traj("c"))  # T=3
    assert [within_template_rank(pool, k) for k in range(9)] == [k // 3 for k in range(9)]


def test_within_template_rank_empty_pool_returns_zero():
    """Empty pool: T=0 would divide-by-zero; the helper guards it to 0 (callers
    guard ``if not pool`` first, so this path is defense-in-depth)."""
    from bench_core.payload.replay_payload import within_template_rank

    assert within_template_rank((), 5) == 0


def test_within_template_rank_single_template_is_k():
    """T=1: every sandbox shares the one template, so rank == k (0,1,2,...). The
    runner then picks my_pool[(k + round_id) % len] -- stride 1, full reach."""
    from bench_core.payload.replay_payload import within_template_rank

    pool = (_traj("only"),)  # T=1
    assert [within_template_rank(pool, k) for k in range(5)] == [0, 1, 2, 3, 4]
