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
# load_pool — oversub linearity clamp (trajectory_limit N -> N//T for T>1)
# ---------------------------------------------------------------------------


def test_load_pool_limit_clamped_to_fleet_share(tmp_path):
    """Oversub linearity clamp: 3 templates, trajectory_limit=N=6, running_concurrency=6.
    The kernel clamps N to N//T = 6//3 = 2 (the ratio-1 per-template fleet share)
    so count_t <= fleet_t(ratio 1) and the issue set stays fixed across ratios.
    Without the clamp each template would keep 6 but only 2 run at ratio 1
    (drifting with ratio until k>=T)."""
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for tpl in ("a", "b", "c"):
        for i in range(1, 7):  # 6 trajectories each (all >= N)
            _write_traj(traj_dir, f"{tpl}{i}.replay.json", f"{tpl}{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({f"{t}{i}.replay.json": f"swb-{t}" for t in ("a", "b", "c") for i in range(1, 7)}),
        encoding="utf-8",
    )

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), limit=6, rc=6))
    by_tpl: dict[str | None, list[str]] = {}
    for t in pool:
        by_tpl.setdefault(t.template, []).append(t.instance_id)
    # clamped to N//T = 2 per template (NOT 6); first 2 by natural sort kept.
    assert len(by_tpl) == 3
    assert all(len(v) == 2 for v in by_tpl.values())
    assert sorted(by_tpl["swb-a"]) == ["a1", "a2"]


def test_load_pool_no_clamp_when_single_template(tmp_path):
    """T=1: running_concurrency//T == N, so the clamp is a no-op (cap stays N).
    Single-template matches the legacy ``trajectories[:N]`` strategy unchanged."""
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for i in range(1, 6):  # 5 trajectories, one template
        _write_traj(traj_dir, f"a{i}.replay.json", f"a{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({f"a{i}.replay.json": "swb-a" for i in range(1, 6)}), encoding="utf-8")

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), limit=3, rc=3))
    # T=1 -> 3//1 = 3, min(3, 3) = 3: keeps first 3 (no clamp).
    assert len(pool) == 3


def test_load_pool_clamp_respects_smaller_limit(tmp_path):
    """An explicit trajectory_limit smaller than N//T is honored (min). The clamp
    only caps the MAXIMUM (N//T); a user-set smaller limit stays."""
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for tpl in ("a", "b", "c"):
        for i in range(1, 7):
            _write_traj(traj_dir, f"{tpl}{i}.replay.json", f"{tpl}{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({f"{t}{i}.replay.json": f"swb-{t}" for t in ("a", "b", "c") for i in range(1, 7)}),
        encoding="utf-8",
    )

    reset_pool_cache()
    # limit=1, rc=6 -> N//T=2, min(1, 2)=1 (explicit smaller limit wins).
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), limit=1, rc=6))
    by_tpl: dict[str | None, list[str]] = {}
    for t in pool:
        by_tpl.setdefault(t.template, []).append(t.instance_id)
    assert len(by_tpl) == 3
    assert all(len(v) == 1 for v in by_tpl.values())


def test_load_pool_no_clamp_when_rc_unset(tmp_path):
    """rc unset (non-oversub direct run): no N//T clamp, honor the explicit
    limit as-is -- preserves direct bench-core use that sets trajectory_limit
    but not running_concurrency."""
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for tpl in ("a", "b", "c"):
        for i in range(1, 7):
            _write_traj(traj_dir, f"{tpl}{i}.replay.json", f"{tpl}{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({f"{t}{i}.replay.json": f"swb-{t}" for t in ("a", "b", "c") for i in range(1, 7)}),
        encoding="utf-8",
    )

    reset_pool_cache()
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), limit=6, rc=None))
    by_tpl: dict[str | None, list[str]] = {}
    for t in pool:
        by_tpl.setdefault(t.template, []).append(t.instance_id)
    # rc None -> no clamp -> each template keeps all 6.
    assert all(len(v) == 6 for v in by_tpl.values())


def test_load_pool_cache_separates_by_rc(tmp_path):
    """The clamp depends on rc, so the cache key must too: same dir/glob/manifest/
    limit but rc=6 (clamps to 2) vs rc=None (no clamp, keeps 6) must NOT share a
    cached pool (a shared slot would silently return the clamped pool for the
    no-rc case, or vice versa)."""
    import json

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for tpl in ("a", "b", "c"):
        for i in range(1, 7):
            _write_traj(traj_dir, f"{tpl}{i}.replay.json", f"{tpl}{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({f"{t}{i}.replay.json": f"swb-{t}" for t in ("a", "b", "c") for i in range(1, 7)}),
        encoding="utf-8",
    )

    reset_pool_cache()
    clamped = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), limit=6, rc=6))
    unclamped = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest), limit=6, rc=None))
    assert len(clamped) == 6  # 3 templates x 2 (N//T=2)
    assert len(unclamped) == 18  # 3 templates x 6 (no clamp)
    assert clamped is not unclamped


def test_load_pool_ratio_invariant_under_clamp(tmp_path):
    """The oversub linearity invariant end-to-end: the loaded POOL is IDENTICAL
    across ratios 1/2/3, because the clamp pins count_t to running_concurrency//T
    (N and T are ratio-invariant), so only concurrency (total_count=k*N) varies.
    Per-issue execution = (fleet_t * round_count)/count_t then scales linearly
    with k. (All->=N case: 3 templates x 10 trajectories.)

    ``reset_pool_cache`` between ratios forces a recompute so a buggy clamp that
    used total_count//T (ratio-dependent) would make the pools differ and fail."""
    import json

    from bench_core.config import KernelConfig
    from bench_core.payload.replay_payload import load_pool, reset_pool_cache
    from bench_core.task_runner.replay import ReplayConfig

    traj_dir = tmp_path / "traj"
    for tpl in ("a", "b", "c"):
        for i in range(1, 11):  # 10 per template, all >= N
            _write_traj(traj_dir, f"{tpl}{i}.replay.json", f"{tpl}{i}")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({f"{t}{i}.replay.json": f"swb-{t}" for t in ("a", "b", "c") for i in range(1, 11)}),
        encoding="utf-8",
    )

    n = 6
    pools = {}
    for k in (1, 2, 3):
        reset_pool_cache()
        cfg = KernelConfig(
            workflow_type="replay",
            total_count=k * n,  # the oversub variable (concurrency)
            benchmark_mode="fixed",
            test_duration=1,
            workflow_config=ReplayConfig(
                replay_trajectory_dir=str(traj_dir),
                replay_template_manifest=str(manifest),
                replay_trajectory_limit=n,  # driver sets N
                replay_running_concurrency=n,  # driver pins N (ratio-invariant)
                replay_mode="exec_only",
            ),
        )
        pools[k] = load_pool(cfg)
    # The pool is identical across ratios (only concurrency varied).
    assert pools[1] == pools[2] == pools[3]
    # And each template keeps N//T = 6//3 = 2 (the ratio-1 fleet share), not N=6.
    by_tpl: dict[str | None, list[str]] = {}
    for t in pools[1]:
        by_tpl.setdefault(t.template, []).append(t.instance_id)
    assert len(by_tpl) == 3
    assert all(len(v) == 2 for v in by_tpl.values())


# ---------------------------------------------------------------------------
# load_pool — template manifest
# ---------------------------------------------------------------------------


def _cfg_for_manifest(
    tmp_path: Path,
    *,
    manifest: str | None = None,
    glob: str = "*.replay.json",
    limit: int | None = None,
    rc: int | None = None,
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
            replay_running_concurrency=rc,
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
    caplog.set_level(logging.INFO)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    assert "skipped 1 trajectory file(s)" in caplog.text
    assert "a.replay.json" in caplog.text
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
    caplog.set_level(logging.INFO)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    assert "skipped 1 trajectory file(s)" in caplog.text
    assert "not a string" in caplog.text


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
    caplog.set_level(logging.INFO)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    assert "skipped 1 trajectory file(s)" in caplog.text
    assert "no manifest entry" in caplog.text


def test_load_pool_manifest_skips_aggregate_into_one_summary(tmp_path, caplog):
    """Many missing-entry trajectories collapse to ONE summary line, not a
    WARNING per file. Guards the noise fix: the oversub driver spawns one
    bench-core subprocess per trial, so per-file WARNINGs would repeat across
    every ratio against the same manifest + trajectory dir."""
    import json
    import logging

    from bench_core.payload.replay_payload import load_pool, reset_pool_cache

    traj_dir = tmp_path / "traj"
    for n in ("a", "b", "c"):
        _write_traj(traj_dir, f"{n}.replay.json", n)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({}), encoding="utf-8")  # no entries

    reset_pool_cache()
    caplog.set_level(logging.INFO)
    pool = load_pool(_cfg_for_manifest(tmp_path, manifest=str(manifest)))
    assert len(pool) == 0
    # exactly ONE summary line, not a per-file WARNING per skipped trajectory
    assert caplog.text.count("not mapped to a usable template") == 1
    assert "skipped 3 trajectory file(s)" in caplog.text
    for name in ("a.replay.json", "b.replay.json", "c.replay.json"):
        assert name in caplog.text


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


def test_template_at_is_distinct_k_mod_T():
    """template_at(k) == distinct[k % T] -- the per-worker template for ephemeral
    modes (no create_all fleet to read from). Mods on T directly, so it is valid
    for 1-based shell indices (bench.py builds range(1, total+1)) as well as 0-based."""
    from bench_core.payload.replay_payload import _distinct_templates, template_at

    pool = (_traj("a"), _traj("b"), _traj("c"))  # T=3, distinct=[a,b,c]
    d = _distinct_templates(pool)
    assert [template_at(pool, k) for k in range(9)] == [d[k % 3] for k in range(9)]
    # 1-based (real shell indexing) still maps to a valid template, never None.
    assert [template_at(pool, k) for k in range(1, 10)] == [d[k % 3] for k in range(1, 10)]


def test_template_at_empty_pool_returns_none():
    from bench_core.payload.replay_payload import template_at

    assert template_at((), 0) is None
    assert template_at((), 5) is None


def test_template_at_single_template():
    """T=1: every sandbox maps to the one template."""
    from bench_core.payload.replay_payload import template_at

    pool = (_traj("only"),)
    assert [template_at(pool, k) for k in range(5)] == ["only"] * 5


def test_template_at_matches_template_fleet_on_zero_based():
    """template_at(k) for 0-based k equals template_fleet(total)[k] -- both are
    distinct[k % T] (shared cached _distinct_templates, no drift). For exec_ephemeral's
    real 1-based shell indices template_at mods on T directly (stays valid), where
    template_fleet(total)[1-based index] would go out of range on the last sandbox."""
    from bench_core.payload.replay_payload import template_fleet, template_at

    pool = (_traj("a"), _traj("b"), _traj("c"))  # T=3
    fleet = template_fleet(pool, 7)
    for k in range(7):
        assert template_at(pool, k) == fleet[k]
