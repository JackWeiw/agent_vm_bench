"""Tests for template-affinity routing + orphan-skip + create_one(template=).

Task 7 of the multi-template replay plan. The runner must route each trajectory
only to a sandbox whose template matches (``_affinity_pool``), skip an orphan
sandbox whose template has no matching trajectory, and pass ``template=`` to
``create_one`` in trajectory mode. Legacy (no manifest, all templates None)
must behave identically to the pre-Task-7 flat cursor.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from bench_core.config import KernelConfig
from bench_core.payload.replay_payload import ReplayStep, Trajectory, reset_pool_cache
from bench_core.schemas import BenchSandbox, ReplayMetrics
from bench_core.task_runner.replay import ReplayRoundRunner, ReplayTaskRunner
from env_provider import SandboxInstance
from env_provider.fake import FakeProvider


def _sandbox(index: int, template: str | None) -> BenchSandbox:
    """Build a replay BenchSandbox from a lean SandboxInstance (ready for the runner)."""
    return BenchSandbox.from_instance(
        SandboxInstance(id=f"s{index}", index=index, template=template, ready=True),
        "replay",
    )


def _seed(tmp_path: Path, entries: list[tuple[str, str, str | None]]) -> KernelConfig:
    """Write trajectory files + manifest under ``tmp_path`` and return a KernelConfig.

    Each entry is ``(filename, instance_id, template)``. A None template writes
    no manifest entry (the trajectory will have ``template=None`` when loaded
    via a manifest that omits it). When every entry's template is None, no
    manifest file is produced (legacy single-template path).
    """
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    manifest: dict[str, str] = {}
    has_any_template = False
    for name, tid, tmpl in entries:
        (traj_dir / name).write_text(
            json.dumps(
                {
                    "instance_id": tid,
                    "environment": "main",
                    "trajectory": [{"action": f"echo {tid}", "delay_time": 0}],
                }
            ),
            encoding="utf-8",
        )
        if tmpl is not None:
            manifest[name] = tmpl
            has_any_template = True
    kwargs: dict = dict(
        workflow_type="replay",
        replay_trajectory_dir=str(traj_dir),
        replay_delay_scale=0.0,
        replay_stop_on_error=False,
    )
    if has_any_template:
        m = tmp_path / "manifest.json"
        m.write_text(json.dumps(manifest), encoding="utf-8")
        kwargs["replay_template_manifest"] = str(m)
    return KernelConfig(**kwargs)


# ---------------------------------------------------------------------------
# _affinity_pool
# ---------------------------------------------------------------------------


def test_affinity_pool_filters_by_template():
    """_affinity_pool returns only trajectories matching the given template."""
    from bench_core.task_runner.replay import _affinity_pool

    pool = (
        Trajectory(path=Path("a"), instance_id="a", environment="main", steps=(), template="swb-a"),
        Trajectory(path=Path("b"), instance_id="b", environment="main", steps=(), template="swb-b"),
        Trajectory(path=Path("c"), instance_id="c", environment="main", steps=(), template="swb-a"),
    )
    assert [t.instance_id for t in _affinity_pool(pool, "swb-a")] == ["a", "c"]
    assert [t.instance_id for t in _affinity_pool(pool, "swb-b")] == ["b"]
    assert _affinity_pool(pool, "swb-z") == []


def test_affinity_pool_legacy_none_returns_whole_pool():
    """Legacy (all templates None): _affinity_pool returns the whole pool."""
    from bench_core.task_runner.replay import _affinity_pool

    pool = (
        Trajectory(path=Path("a"), instance_id="a", environment="main", steps=()),
        Trajectory(path=Path("b"), instance_id="b", environment="main", steps=()),
    )
    result = _affinity_pool(pool, None)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# ReplayMetrics orphan counter
# ---------------------------------------------------------------------------


def test_replay_metrics_orphan_skip_thread_safe():
    """record_orphan_skip / orphan_skip_count follow the existing lock pattern."""
    m = ReplayMetrics()
    assert m.orphan_skip_count == 0
    m.record_orphan_skip()
    m.record_orphan_skip()
    m.record_orphan_skip()
    assert m.orphan_skip_count == 3

    # Thread safety: concurrent increments must not lose counts.
    m2 = ReplayMetrics()
    barrier = threading.Barrier(4)

    def _inc():
        barrier.wait()
        for _ in range(100):
            m2.record_orphan_skip()

    threads = [threading.Thread(target=_inc) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m2.orphan_skip_count == 400


# ---------------------------------------------------------------------------
# ReplayTaskRunner — non-trajectory (exec_only / lifecycle) affinity routing
# ---------------------------------------------------------------------------


def test_orphan_sandbox_skipped_with_count(tmp_path, monkeypatch):
    """Sandbox whose template matches no trajectory is skipped (orphan)."""
    reset_pool_cache()
    cfg = _seed(tmp_path, [("a.replay.json", "a", "swb-a")])
    stop = threading.Event()
    provider = FakeProvider(count=1)
    monkeypatch.setattr(ReplayTaskRunner, "_replay_trajectory", lambda self, t: None)
    runner = ReplayTaskRunner(_sandbox(1, "swb-other"), cfg, stop, provider)
    runner.run()
    assert runner.state.replay_metrics.orphan_skip_count == 1


def test_affinity_routes_only_matching_template(tmp_path, monkeypatch):
    """A swb-a sandbox must never run a swb-b trajectory."""
    reset_pool_cache()
    cfg = _seed(
        tmp_path,
        [
            ("a.replay.json", "a", "swb-a"),
            ("b.replay.json", "b", "swb-b"),
            ("c.replay.json", "c", "swb-a"),
        ],
    )
    stop = threading.Event()
    provider = FakeProvider(count=1)
    ran: list[str] = []

    def _spy(self, traj):
        ran.append(traj.instance_id)
        stop.set()

    monkeypatch.setattr(ReplayTaskRunner, "_replay_trajectory", _spy)
    runner = ReplayTaskRunner(_sandbox(1, "swb-a"), cfg, stop, provider)
    runner.run()
    assert "b" not in ran
    assert set(ran).issubset({"a", "c"})
    assert len(ran) >= 1


def test_legacy_no_manifest_routes_whole_pool(tmp_path, monkeypatch):
    """No manifest (all template=None): affinity returns whole pool, no orphan."""
    reset_pool_cache()
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    (traj_dir / "a.replay.json").write_text(
        json.dumps(
            {
                "instance_id": "a",
                "environment": "main",
                "trajectory": [{"action": "echo a", "delay_time": 0}],
            }
        ),
        encoding="utf-8",
    )
    cfg = KernelConfig(
        workflow_type="replay",
        replay_trajectory_dir=str(traj_dir),
        replay_delay_scale=0.0,
    )
    stop = threading.Event()
    provider = FakeProvider(count=1)
    ran: list[str] = []

    def _spy(self, traj):
        ran.append(traj.instance_id)
        stop.set()

    monkeypatch.setattr(ReplayTaskRunner, "_replay_trajectory", _spy)
    runner = ReplayTaskRunner(_sandbox(1, None), cfg, stop, provider)
    runner.run()
    assert ran == ["a"]
    assert runner.state.replay_metrics.orphan_skip_count == 0


# ---------------------------------------------------------------------------
# ReplayRoundRunner — non-trajectory affinity routing
# ---------------------------------------------------------------------------


def test_round_runner_orphan_skip(tmp_path, monkeypatch):
    """Round-robin runner skips orphan sandboxes too."""
    reset_pool_cache()
    cfg = _seed(tmp_path, [("a.replay.json", "a", "swb-a")])
    stop = threading.Event()
    provider = FakeProvider(count=1)
    monkeypatch.setattr(ReplayRoundRunner, "_replay_round_loop", lambda self, t: None)
    runner = ReplayRoundRunner(_sandbox(1, "swb-other"), cfg, stop, 0, provider)
    runner.run()
    assert runner.state.replay_metrics.orphan_skip_count == 1


def test_round_runner_affinity_filters(tmp_path, monkeypatch):
    """Round-robin runner picks from the affinity-filtered pool, not the flat pool."""
    reset_pool_cache()
    cfg = _seed(
        tmp_path,
        [
            ("a.replay.json", "a", "swb-a"),
            ("b.replay.json", "b", "swb-b"),
            ("c.replay.json", "c", "swb-a"),
        ],
    )
    stop = threading.Event()
    provider = FakeProvider(count=1)
    captured: list[str] = []
    monkeypatch.setattr(ReplayRoundRunner, "_replay_round_loop", lambda self, t: captured.append(t.instance_id))
    runner = ReplayRoundRunner(_sandbox(0, "swb-a"), cfg, stop, 0, provider)
    runner.run()
    assert len(captured) == 1
    assert captured[0] in ("a", "c")


# ---------------------------------------------------------------------------
# Trajectory mode: create_one(template=traj.template)
# ---------------------------------------------------------------------------


def test_trajectory_create_one_passes_template(tmp_path, monkeypatch):
    """_run_trajectory passes template=traj.template to provider.create_one."""

    provider = FakeProvider(count=1)
    create_calls: list[dict] = []
    real_create_one = provider.create_one

    def _spy_create_one(index, *, template=None, metadata=None):
        create_calls.append({"index": index, "template": template, "metadata": metadata})
        return real_create_one(index, template=template, metadata=metadata)

    provider.create_one = _spy_create_one  # type: ignore[method-assign]

    cfg = KernelConfig(workflow_type="replay", replay_mode="trajectory", replay_delay_scale=0.0)
    state = _sandbox(0, "swb-a")
    state.ready = True
    stop = threading.Event()
    stop.set()  # pre-set so the step loop exits immediately after create

    # launch_pacer=None keeps _wait_for_launch_turn a no-op; explicit so a
    # future default pacer can't silently break or hang this test.
    runner = ReplayTaskRunner(state, cfg, stop, provider, launch_pacer=None)

    # Bypass post-create machinery that FakeProvider doesn't implement.
    monkeypatch.setattr(runner, "_probe_ready", lambda: 0.0)

    traj = Trajectory(
        path=Path("x"),
        instance_id="x",
        environment="main",
        steps=(ReplayStep(index=0, action="echo x", delay_time_sec=0.0, action_type="shell"),),
        template="swb-a",
    )

    runner._run_trajectory(traj)

    assert len(create_calls) == 1
    assert create_calls[0]["template"] == "swb-a"
    assert create_calls[0]["metadata"] == {"trajectory_id": "x"}


def test_trajectory_create_one_none_template(tmp_path, monkeypatch):
    """Legacy trajectory (template=None) passes template=None to create_one."""
    provider = FakeProvider(count=1)
    create_calls: list[dict] = []
    real_create_one = provider.create_one

    def _spy_create_one(index, *, template=None, metadata=None):
        create_calls.append({"index": index, "template": template, "metadata": metadata})
        return real_create_one(index, template=template, metadata=metadata)

    provider.create_one = _spy_create_one  # type: ignore[method-assign]

    cfg = KernelConfig(workflow_type="replay", replay_mode="trajectory", replay_delay_scale=0.0)
    state = _sandbox(0, None)
    state.ready = True
    stop = threading.Event()
    stop.set()

    runner = ReplayTaskRunner(state, cfg, stop, provider)
    monkeypatch.setattr(runner, "_probe_ready", lambda: 0.0)

    traj = Trajectory(
        path=Path("x"),
        instance_id="x",
        environment="main",
        steps=(ReplayStep(index=0, action="echo x", delay_time_sec=0.0, action_type="shell"),),
        template=None,
    )
    runner._run_trajectory(traj)

    assert len(create_calls) == 1
    assert create_calls[0]["template"] is None


# ---------------------------------------------------------------------------
# Report renderer: orphan_skip_count surfaces in the text report
# ---------------------------------------------------------------------------


def test_replay_report_renders_orphan_skipped_line():
    """format_replay_stats_section renders 'Orphan Skipped: N' when > 0.

    Task 8 of the multi-template replay plan. The renderer must aggregate
    ``replay_metrics.orphan_skip_count`` across all sandbox states and emit a
    conditional ``Orphan Skipped: N`` line after ``Trajectory Completions``.
    """
    from bench_core.observability.stats_collector import StatsCollector

    cfg = KernelConfig(workflow_type="replay", replay_delay_scale=0.0)
    state_a = BenchSandbox(id="a", index=0, workflow_type="replay")
    state_b = BenchSandbox(id="b", index=1, workflow_type="replay")
    state_a.replay_metrics.record_orphan_skip()
    state_a.replay_metrics.record_orphan_skip()
    state_b.replay_metrics.record_orphan_skip()

    sc = StatsCollector(cfg, {0: state_a, 1: state_b}, "fake")
    lines = sc.format_replay_stats_section()
    joined = "\n".join(lines)

    # Label is column-aligned (padded), so match the label and the count
    # separately rather than a single-space ``"Orphan Skipped: 3"`` literal.
    assert "Orphan Skipped:" in joined
    assert "3" in joined
    orphan_line = next(line for line in lines if "Orphan Skipped:" in line)
    assert orphan_line.rstrip().endswith("3")


def test_replay_report_omits_orphan_skipped_when_zero():
    """No 'Orphan Skipped' line when no sandboxes orphan (matches the
    conditional-render pattern used for ``initial_pauses``)."""
    from bench_core.observability.stats_collector import StatsCollector

    cfg = KernelConfig(workflow_type="replay", replay_delay_scale=0.0)
    state = BenchSandbox(id="a", index=0, workflow_type="replay")

    sc = StatsCollector(cfg, {0: state}, "fake")
    lines = sc.format_replay_stats_section()
    joined = "\n".join(lines)

    assert "Orphan Skipped" not in joined
