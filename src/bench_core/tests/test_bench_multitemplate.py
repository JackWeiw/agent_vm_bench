"""Tests for bench.py multi-template template-map wiring (Task 6).

Verifies that ``run_benchmark`` computes a round-robin ``templates`` map from
the trajectory pool and threads it into ``provider.create_all(templates=...)``
when a replay template manifest is configured.
"""
from __future__ import annotations

import json
from pathlib import Path

from bench_core.bench import _replay_template_map, run_benchmark
from bench_core.config import KernelConfig
from bench_core.payload.replay_payload import reset_pool_cache
from bench_core.task_runner.replay import ReplayConfig
from env_provider.fake import FakeProvider


def _seed_pool(tmp_path: Path) -> Path:
    """Write two minimal trajectories + a manifest; return the manifest path."""
    traj = tmp_path / "traj"
    traj.mkdir()
    (traj / "a.replay.json").write_text(
        json.dumps({"instance_id": "a", "trajectory": [{"action": "echo a", "delay_time": 0}]}),
        encoding="utf-8",
    )
    (traj / "b.replay.json").write_text(
        json.dumps({"instance_id": "b", "trajectory": [{"action": "echo b", "delay_time": 0}]}),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"a.replay.json": "swb-a", "b.replay.json": "swb-b"}),
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# _replay_template_map (focused unit test — no spine needed)
# ---------------------------------------------------------------------------


def test_replay_template_map_round_robin(tmp_path):
    """Round-robin the pool's templates over total_count slots."""
    manifest = _seed_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        output_dir=str(tmp_path),
        filename_prefix="mt",
        workflow_config=ReplayConfig(
            replay_trajectory_dir=str(tmp_path / "traj"),
            replay_template_manifest=str(manifest),
            replay_mode="exec_only",
        ),
    )
    result = _replay_template_map(cfg)
    assert result == {0: "swb-a", 1: "swb-b", 2: "swb-a", 3: "swb-b"}


def test_replay_template_map_none_when_no_manifest(tmp_path):
    """No manifest -> None (legacy single-template path)."""
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        output_dir=str(tmp_path),
        filename_prefix="mt",
        workflow_config=ReplayConfig(
            replay_trajectory_dir=str(tmp_path / "traj"),
            replay_mode="exec_only",
        ),
    )
    assert _replay_template_map(cfg) is None


def test_replay_template_map_none_for_non_replay(tmp_path):
    """Non-replay workflow -> None (guard; bench.py skips the call anyway)."""
    cfg = KernelConfig(
        workflow_type="browser",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        output_dir=str(tmp_path),
        filename_prefix="mt",
    )
    assert _replay_template_map(cfg) is None


# ---------------------------------------------------------------------------
# End-to-end: run_benchmark threads templates= into create_all
# ---------------------------------------------------------------------------


def test_run_benchmark_passes_template_map_to_create_all(tmp_path):
    manifest = _seed_pool(tmp_path)
    reset_pool_cache()
    cfg = KernelConfig(
        workflow_type="replay",
        total_count=4,
        benchmark_mode="fixed",
        test_duration=1,
        output_dir=str(tmp_path),
        filename_prefix="mt",
        workflow_config=ReplayConfig(
            replay_trajectory_dir=str(tmp_path / "traj"),
            replay_template_manifest=str(manifest),
            replay_mode="exec_only",
            replay_delay_scale=0.0,
        ),
    )
    provider = FakeProvider(count=4)
    run_benchmark(cfg, provider)
    # Sandbox 0..3 templates: round-robin over pool [swb-a, swb-b] -> a,b,a,b
    seen = [provider._instances[i].template for i in range(4)]
    assert seen == ["swb-a", "swb-b", "swb-a", "swb-b"]


# ---------------------------------------------------------------------------
# Balanced fleet + within-group rank (oversub multi-template linearity)
# ---------------------------------------------------------------------------


def _seed_grouped_pool(tmp_path: Path, specs: dict[str, int]) -> Path:
    """Write a template-grouped pool: for each (template, count) write ``count``
    trajectories named ``{template}{i}.replay.json`` all mapped to ``swb-{template}``.

    Filenames sort grouped-by-template (a1..aN then b1..bN ...), reproducing the
    SWE-bench pool shape (filename ~= repo ~= template) that breaks the old
    ``pool[k % len(pool)].template`` mapping at ratio 1.
    """
    traj = tmp_path / "traj"
    traj.mkdir()
    manifest: dict[str, str] = {}
    for tpl, count in specs.items():
        for i in range(1, count + 1):
            name = f"{tpl}{i}.replay.json"
            (traj / name).write_text(
                json.dumps({"instance_id": f"{tpl}{i}", "trajectory": [{"action": "echo", "delay_time": 0}]}),
                encoding="utf-8",
            )
            manifest[name] = f"swb-{tpl}"
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps(manifest), encoding="utf-8")
    return m


def _mt_cfg(tmp_path: Path, manifest: Path, total_count: int) -> KernelConfig:
    """Minimal replay KernelConfig wired to the grouped pool at ``total_count``."""
    return KernelConfig(
        workflow_type="replay",
        total_count=total_count,
        benchmark_mode="fixed",
        test_duration=1,
        output_dir=str(tmp_path),
        filename_prefix="mt",
        workflow_config=ReplayConfig(
            replay_trajectory_dir=str(tmp_path / "traj"),
            replay_template_manifest=str(manifest),
            replay_mode="exec_only",
        ),
    )


def test_replay_template_map_balanced_grouped(tmp_path):
    """The bug case: pool sorted grouped-by-template (all A filenames before all
    B's), total_count < len(pool) at ratio 1. The old ``pool[k % len].template``
    mapped every sandbox to the first-sorted template (100% A); the balanced
    ``distinct[k % T]`` fleet must spread sandboxes across BOTH templates
    (~total/T each), so the ratio-1 baseline is NOT a different workload than k>=2."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 5, "b": 5})
    reset_pool_cache()
    fleet = _replay_template_map(_mt_cfg(tmp_path, manifest, 5))
    counts: dict[str | None, int] = {}
    for tpl in fleet.values():
        counts[tpl] = counts.get(tpl, 0) + 1
    # distinct=[swb-a, swb-b] (T=2); k%2 -> a,b,a,b,a => a:3, b:2 (NOT 100% a).
    assert set(counts) == {"swb-a", "swb-b"}
    assert counts["swb-a"] == 3  # ceil(5/2)
    assert counts["swb-b"] == 2  # floor(5/2)


def test_replay_template_map_balanced_uneven_345(tmp_path):
    """Uneven pool A=3/B=4/C=5 trajectories. The fleet is balanced over the 3
    DISTINCT templates (NOT over trajectory counts), so each template gets
    ~total/3 sandboxes regardless of how few trajectories it has. With total a
    multiple of 3 each template's fleet share scales LINEARLY with ratio
    (3->6->9) -- the precondition for the oversub linearity invariant
    per-issue-execution = (fleet_share * round_count) / count_t being linear in k."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 3, "b": 4, "c": 5})
    for total, per in [(9, 3), (18, 6), (27, 9)]:  # ratios 1/2/3 at N=9, T=3
        reset_pool_cache()
        fleet = _replay_template_map(_mt_cfg(tmp_path, manifest, total))
        counts: dict[str | None, int] = {}
        for tpl in fleet.values():
            counts[tpl] = counts.get(tpl, 0) + 1
        # total % 3 == 0 -> each distinct template gets exactly total/3 sandboxes.
        assert counts == {"swb-a": per, "swb-b": per, "swb-c": per}, f"total={total}"


def test_round_robin_rank_covers_all_trajectories(tmp_path):
    """The gcd/T coverage bug: 3 templates (T=3), template A has 3 trajectories,
    round_count=2. Under the balanced fleet A's sandboxes sit at global indices
    all == 0 mod T. The OLD ``(state.index + round_id) % len`` selection hit only
    trajectory indices {0,1} (index 2 starved -- gcd(3,3)=3>1). The rank-based
    ``(k // T + round_id) % len`` reaches all 3."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 3, "b": 3, "c": 3})
    reset_pool_cache()
    from bench_core.payload.replay_payload import load_pool, template_fleet, within_template_rank

    cfg = _mt_cfg(tmp_path, manifest, 12)  # 3 templates x 4 sandboxes each
    pool = load_pool(cfg)
    fleet = template_fleet(pool, 12)
    a_pool = [t for t in pool if t.template == "swb-a"]
    assert len(a_pool) == 3
    a_sandboxes = [k for k in range(12) if fleet[k] == "swb-a"]
    assert a_sandboxes == [0, 3, 6, 9]  # all == 0 mod T=3
    round_count = 2
    # NEW rank-based: (k // T + round_id) % 3 -> {0,1,2} -- all reachable.
    reached_rank = {(within_template_rank(pool, k) + r) % len(a_pool) for k in a_sandboxes for r in range(round_count)}
    assert reached_rank == {0, 1, 2}
    # OLD global-index: (k + round_id) % 3 over k all ==0 mod 3 -> only {0,1}.
    reached_old = {(k + r) % len(a_pool) for k in a_sandboxes for r in range(round_count)}
    assert reached_old == {0, 1}  # trajectory index 2 starved -- the defect fixed


def test_fixed_runner_rank_pure_in_state_index(tmp_path):
    """State recovery: the fixed runner derives its starting cursor from
    ``within_template_rank(pool, state.index)`` -- a PURE function of the
    persisted state.index (k // T), with NO mutable per-sandbox cursor. So a
    sandbox restarted with the same state.index recomputes the identical rank ->
    identical starting trajectory; there is no in-memory cursor state to lose
    across a restart. (The round runner needs no recovery: it holds no cursor
    across rounds -- round_id encodes the position.)"""
    manifest = _seed_grouped_pool(tmp_path, {"a": 2, "b": 2, "c": 2})
    reset_pool_cache()
    from bench_core.payload.replay_payload import load_pool, within_template_rank

    cfg = _mt_cfg(tmp_path, manifest, 9)  # T=3
    pool = load_pool(cfg)
    ranks_run1 = [within_template_rank(pool, k) for k in range(9)]
    ranks_run2 = [within_template_rank(pool, k) for k in range(9)]  # "restart"
    assert ranks_run1 == ranks_run2 == [k // 3 for k in range(9)]


def test_oversub_per_issue_scales_linearly(tmp_path):
    """End-to-end linearity (the user's all->=N case): clamp (count_t = N//T) +
    balanced fleet (distinct[k % T]) + rank-based selection (idx = (k//T +
    round_id) % count_t) => each issue's execution count scales LINEARLY with
    ratio k (1x/2x/3x), and the issue SET is identical across ratios. This is
    the comprehensive fix holding together for every load shape.

    Simulates the round-robin runner's selection (no live SDK): for each sandbox
    picks its template's affinity sub-pool entry by within-template rank + round
    rotation, counts executions per issue across ratios 1/2/3."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 10, "b": 10, "c": 10})  # all >= N
    from bench_core.payload.replay_payload import (
        load_pool,
        reset_pool_cache,
        template_fleet,
        within_template_rank,
    )

    n = 6  # running_concurrency; T=3 -> clamp pins count_t to N//T = 2 per template
    round_count = 1
    by_ratio: dict[int, dict[str, int]] = {}
    for k in (1, 2, 3):
        reset_pool_cache()
        cfg = KernelConfig(
            workflow_type="replay",
            total_count=k * n,  # the oversub variable (concurrency)
            benchmark_mode="round_robin",
            test_duration=1,
            output_dir=str(tmp_path),
            filename_prefix="mt",
            workflow_config=ReplayConfig(
                replay_trajectory_dir=str(tmp_path / "traj"),
                replay_template_manifest=str(manifest),
                replay_trajectory_limit=n,  # driver sets N
                replay_running_concurrency=n,  # driver pins N (ratio-invariant)
                replay_mode="exec_only",
            ),
        )
        pool = load_pool(cfg)
        fleet = template_fleet(pool, k * n)
        execs: dict[str, int] = {}
        for sandbox_idx in range(k * n):
            tpl = fleet[sandbox_idx]
            my_pool = [t for t in pool if t.template == tpl]  # affinity sub-pool
            rank = within_template_rank(pool, sandbox_idx)
            for r in range(round_count):
                idx = (rank + r) % len(my_pool)
                iid = my_pool[idx].instance_id
                execs[iid] = execs.get(iid, 0) + 1
        by_ratio[k] = execs

    # The issue SET is identical across ratios (clamp + per-template cap fix it).
    assert set(by_ratio[1]) == set(by_ratio[2]) == set(by_ratio[3])
    # Each issue's execution count scales linearly with k (1x, 2x, 3x).
    for iid in by_ratio[1]:
        assert by_ratio[1][iid] == 1, f"{iid}: ratio1={by_ratio[1][iid]}"
        assert by_ratio[2][iid] == 2, f"{iid}: ratio2={by_ratio[2][iid]}"
        assert by_ratio[3][iid] == 3, f"{iid}: ratio3={by_ratio[3][iid]}"


def test_oversub_issue_set_fixed_indivisible(tmp_path):
    """N not divisible by T (the remainder case): the clamp count_t = N//T is
    still <= fleet_t(1) for EVERY template (fleet_t(1) is floor or ceil under the
    balanced fleet, and floor == N//T), so all count_t issues run at every ratio
    and the issue SET stays fixed (no drift) even when the fleet has a +-1
    remainder imbalance. Per-issue count stays linear in k (constant slope =
    round_count; only the intercept varies by +-1). This guards the 'every load
    shape' claim beyond the clean T|N case above."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 10, "b": 10, "c": 10})
    from bench_core.payload.replay_payload import (
        load_pool,
        reset_pool_cache,
        template_fleet,
        within_template_rank,
    )

    n = 7  # 7 % 3 = 1 -> remainder; clamp count_t = 7//3 = 2 per template
    round_count = 1
    sets: dict[int, set[str]] = {}
    for k in (1, 2, 3):
        reset_pool_cache()
        cfg = KernelConfig(
            workflow_type="replay",
            total_count=k * n,
            benchmark_mode="round_robin",
            test_duration=1,
            output_dir=str(tmp_path),
            filename_prefix="mt",
            workflow_config=ReplayConfig(
                replay_trajectory_dir=str(tmp_path / "traj"),
                replay_template_manifest=str(manifest),
                replay_trajectory_limit=n,
                replay_running_concurrency=n,
                replay_mode="exec_only",
            ),
        )
        pool = load_pool(cfg)
        fleet = template_fleet(pool, k * n)
        ran: set[str] = set()
        for sandbox_idx in range(k * n):
            tpl = fleet[sandbox_idx]
            my_pool = [t for t in pool if t.template == tpl]
            rank = within_template_rank(pool, sandbox_idx)
            for r in range(round_count):
                ran.add(my_pool[(rank + r) % len(my_pool)].instance_id)
        sets[k] = ran
    # The issue SET is identical across ratios (no drift), even with N%T != 0.
    assert sets[1] == sets[2] == sets[3]
    # Every issue in the clamped pool ran at every ratio (none starved).
    assert len(sets[1]) == 6  # 3 templates x (N//T = 2)


# ---------------------------------------------------------------------------
# exec_ephemeral: ephemeral create/kill per round (B's no-lifecycle model)
# ---------------------------------------------------------------------------


def test_exec_ephemeral_selection_linearity_1_based(tmp_path):
    """exec_ephemeral round-robin selection with 1-based shell indices (the real
    bench.py indexing, range(1, total+1)) + template_at: per-issue exec count
    scales 1x/2x/3x across ratios, issue SET fixed. Guards the 1-based indexing
    + template_at path the draft got wrong (template_fleet(total).get(state.index)
    orphan-skips the last sandbox when shells are 1-based). This is the oversub
    linearity path for exec_ephemeral."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 10, "b": 10, "c": 10})
    from bench_core.payload.replay_payload import (
        load_pool,
        reset_pool_cache,
        template_at,
        within_template_rank,
    )

    n = 6  # T=3 -> clamp count_t = N//T = 2 per template
    round_count = 1
    by_ratio: dict[int, dict[str, int]] = {}
    for k in (1, 2, 3):
        reset_pool_cache()
        cfg = KernelConfig(
            workflow_type="replay",
            total_count=k * n,
            benchmark_mode="round_robin",
            test_duration=1,
            output_dir=str(tmp_path),
            filename_prefix="ee",
            workflow_config=ReplayConfig(
                replay_trajectory_dir=str(tmp_path / "traj"),
                replay_template_manifest=str(manifest),
                replay_trajectory_limit=n,
                replay_running_concurrency=n,
                replay_mode="exec_ephemeral",
            ),
        )
        pool = load_pool(cfg)
        execs: dict[str, int] = {}
        for s in range(1, k * n + 1):  # 1-based shell indices
            my_tpl = template_at(pool, s)
            my_pool = [t for t in pool if t.template == my_tpl]
            rank = within_template_rank(pool, s)
            for r in range(round_count):
                execs[my_pool[(rank + r) % len(my_pool)].instance_id] = (
                    execs.get(my_pool[(rank + r) % len(my_pool)].instance_id, 0) + 1
                )
        by_ratio[k] = execs

    assert set(by_ratio[1]) == set(by_ratio[2]) == set(by_ratio[3])
    for iid in by_ratio[1]:
        assert by_ratio[1][iid] == 1, f"{iid}: ratio1={by_ratio[1][iid]}"
        assert by_ratio[2][iid] == 2, f"{iid}: ratio2={by_ratio[2][iid]}"
        assert by_ratio[3][iid] == 3, f"{iid}: ratio3={by_ratio[3][iid]}"


def test_exec_ephemeral_fixed_runner_issue_pool_fixed(tmp_path):
    """exec_ephemeral FIXED runner: the cursor cycles WITHIN my_pool (never
    leaves), so each worker's touched issue SET == its template's sub-pool
    (count_t) and the union == the full clamped pool. The fixed path isn't the
    oversub linearity metric (round_robin is), but its cursor must not drift
    outside the pool either."""
    manifest = _seed_grouped_pool(tmp_path, {"a": 10, "b": 10, "c": 10})
    from bench_core.payload.replay_payload import (
        load_pool,
        reset_pool_cache,
        template_at,
        within_template_rank,
    )

    n = 6
    for k in (1, 2, 3):
        reset_pool_cache()
        cfg = KernelConfig(
            workflow_type="replay",
            total_count=k * n,
            benchmark_mode="fixed",
            test_duration=1,
            output_dir=str(tmp_path),
            filename_prefix="eef",
            workflow_config=ReplayConfig(
                replay_trajectory_dir=str(tmp_path / "traj"),
                replay_template_manifest=str(manifest),
                replay_trajectory_limit=n,
                replay_running_concurrency=n,
                replay_mode="exec_ephemeral",
            ),
        )
        pool = load_pool(cfg)
        all_touched: set[str] = set()
        for s in range(1, k * n + 1):  # 1-based
            my_tpl = template_at(pool, s)
            my_pool = [t for t in pool if t.template == my_tpl]
            cursor = within_template_rank(pool, s) % len(my_pool)
            touched: set[str] = set()
            for _ in range(k * n):  # fixed runner target = total_count
                touched.add(my_pool[cursor].instance_id)
                cursor = (cursor + 1) % len(my_pool)
            assert touched.issubset({t.instance_id for t in my_pool})  # never leaves my_pool
            all_touched |= touched
        assert all_touched == {t.instance_id for t in pool}  # union == full pool, no outside issues
