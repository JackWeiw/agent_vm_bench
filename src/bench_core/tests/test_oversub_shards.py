"""Unit + stub-e2e tests for process-sharded oversub trials (oversub_shards).

Mechanism units (planning, per-shard config, merge algebra, group supervision)
are pure/stdlib-only; the e2e test drives ``oversub.main`` with a stub
bench-core (the established ``--bench-core-bin`` pattern) so no kernel, SDK,
or host is needed.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import psutil
import yaml

from bench_core import oversub_shards
from bench_core.oversub import main
from bench_core.oversub_shards import (
    GroupResult,
    ShardArtifacts,
    ShardGroupRunner,
    ShardStressBracket,
    build_shard_command,
    build_shard_config,
    coerce_shards,
    collect_shard_artifacts,
    merge_lifecycle_series,
    merge_run_summaries,
    merge_trajectory_indexes,
    plan_shards,
    resolve_shard_count,
    shard_sizes,
    start_host_monitor,
    write_merged_artifacts,
    write_shard_configs,
)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_coerce_shards_forms():
    assert coerce_shards("auto") == "auto"
    assert coerce_shards("AUTO") == "auto"
    assert coerce_shards("4") == 4
    assert coerce_shards(4) == 4
    with pytest.raises(ValueError):
        coerce_shards(0)
    with pytest.raises(ValueError):
        coerce_shards("lots")
    with pytest.raises(ValueError):
        coerce_shards(True)


def test_resolve_shard_count_auto_follows_ratio_for_shardable_modes():
    assert resolve_shard_count("auto", mode="exec_ephemeral", ratio=10, n=384) == 10
    assert resolve_shard_count("auto", mode="exec_only", ratio=3, n=4) == 3


def test_resolve_shard_count_global_admission_modes_never_shard():
    # lifecycle enforces a GLOBAL running_concurrency; per-shard admission
    # would over-admit to P*N, so it collapses to single-process.
    assert resolve_shard_count("auto", mode="lifecycle", ratio=8, n=384) == 1
    assert resolve_shard_count(4, mode="lifecycle", ratio=8, n=384) == 1  # explicit downgraded


def test_resolve_shard_count_explicit_and_capped():
    assert resolve_shard_count(2, mode="exec_ephemeral", ratio=4, n=8) == 2
    assert resolve_shard_count(1, mode="exec_ephemeral", ratio=4, n=8) == 1
    # capped at the fleet size: every shard needs >= 1 slot
    assert resolve_shard_count(100, mode="exec_ephemeral", ratio=2, n=4) == 8


def test_shard_sizes_balanced_remainder_first():
    assert shard_sizes(8, 2) == [4, 4]
    assert shard_sizes(10, 3) == [4, 3, 3]
    assert shard_sizes(7, 7) == [1] * 7


def test_plan_shards_legacy_only_for_global_admission_modes(tmp_path, caplog):
    # lifecycle (global running_concurrency) can never be split: no plan, the
    # legacy single-process path, at any P. The autodetected default is silent,
    # but an explicit P > 1 is a documented downgrade and is surfaced with a
    # WARNING (we validate, not silently rewrite, the operator's request).
    assert plan_shards(trial_dir=tmp_path, mode="lifecycle", ratio=4, n=8, setting="auto") is None
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    caplog.clear()
    plan_shards(trial_dir=tmp_path, mode="lifecycle", ratio=4, n=8, setting=2)
    plan_shards(trial_dir=tmp_path, mode="lifecycle", ratio=4, n=8, setting=4)
    assert (
        sum(
            1
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "cannot be sharded" in r.getMessage()
            and "running single-process" in r.getMessage()
        )
        == 2
    )


def test_plan_shards_single_shard_for_ratio_one_and_offsets_cumulative(tmp_path):
    # P == 1 still builds a plan for shardable modes: ratio-1 (auto -> P=1) and
    # an explicit --shards 1 both run one shard-00 through the sharded
    # machinery, so ratio-1 trials share the shard-00//merged//driver-owned
    # vm_monitor layout with P >= 2 trials (one structure across every ratio).
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=1, n=8, setting="auto")
    assert plan is not None and plan.count == 1
    assert [s.size for s in plan.specs] == [8]
    assert [s.slot_offset for s in plan.specs] == [0]
    assert plan.specs[0].directory == tmp_path / "shard-00"
    assert plan.specs[0].log_path == tmp_path / "shard-00" / "driver.log"

    single = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting=1)
    assert single is not None and single.count == 1
    assert [s.size for s in single.specs] == [8]

    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=5, setting="auto")
    assert plan.count == 2
    assert [s.size for s in plan.specs] == [5, 5]
    assert [s.slot_offset for s in plan.specs] == [0, 5]
    assert plan.specs[0].directory == tmp_path / "shard-00"
    assert plan.specs[1].log_path == tmp_path / "shard-01" / "driver.log"


def test_plan_shards_group_aligned_for_p_le_ratio(tmp_path):
    # ratio=3, P=2, N=4 -> groups [2,1] -> sizes [8,4]: every shard size is a
    # multiple of N, so slot k -> pool[k % N] gives exact per-trajectory parity
    # with a single-process trial (same run counts per trajectory, not just totals).
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=3, n=4, setting=2)
    assert [s.size for s in plan.specs] == [8, 4]
    assert [s.slot_offset for s in plan.specs] == [0, 8]


def test_plan_shards_slot_level_with_warning_for_p_gt_ratio(tmp_path, caplog):
    # P > ratio: shards fall below the pool -> slot-level split, totals
    # preserved but per-trajectory counts redistribute; must warn.
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting=4)
    assert [s.size for s in plan.specs] == [2, 2, 2, 2]
    assert [s.slot_offset for s in plan.specs] == [0, 2, 4, 6]
    assert any("redistribute" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Per-shard config + command
# ---------------------------------------------------------------------------


def _trial_cfg() -> dict:
    return {
        "sandbox": {"total_count": 8},
        "replay": {"running_concurrency": 4, "trajectory_limit": 4, "mode": "exec_ephemeral", "multiplier": 2},
        "test": {"round_size": 8, "duration": 60},
        "report": {"output_dir": "trial", "filename_prefix": "rb"},
        "docker": {"image": "img", "container_prefix": "oc-bench"},
    }


def test_build_shard_config_derives_self_contained_shard(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    cfg = build_shard_config(_trial_cfg(), plan.specs[1], count=plan.count)
    assert cfg["sandbox"]["total_count"] == 4  # shard share, not k*N
    assert cfg["test"]["round_size"] == 4
    assert cfg["report"]["output_dir"] == str(tmp_path / "shard-01")
    # global-by-design knobs stay untouched
    assert cfg["replay"]["trajectory_limit"] == 4
    assert cfg["replay"]["running_concurrency"] == 4
    assert cfg["replay"]["multiplier"] == 2
    # docker names get a shard suffix (keeps startswith(base) for cleanup reach)
    assert cfg["docker"]["container_prefix"] == "oc-bench-s1"
    assert cfg["docker"]["container_prefix"].startswith("oc-bench")


def test_build_shard_config_hoists_shard0_monitor_dir_to_trial_root(tmp_path):
    """Host metrics are fleet-wide: one fixed path, whether or not we sharded."""
    cfg_in = {**_trial_cfg(), "monitor": {"interval": 2, "log_dir": None}}
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")

    shard0 = build_shard_config(cfg_in, plan.specs[0], count=plan.count)
    assert shard0["monitor"]["log_dir"] == str(tmp_path / "vm_monitor")
    assert shard0["monitor"]["interval"] == 2  # other monitor keys untouched

    # Non-owning shards keep the default (they run --no-vm-monitor anyway).
    assert build_shard_config(cfg_in, plan.specs[1], count=plan.count)["monitor"]["log_dir"] is None

    # An explicit log_dir is the operator's call -- never overridden.
    cfg_explicit = {**_trial_cfg(), "monitor": {"log_dir": "/mnt/fast/vm"}}
    assert build_shard_config(cfg_explicit, plan.specs[0], count=plan.count)["monitor"]["log_dir"] == "/mnt/fast/vm"


def test_build_shard_config_gives_every_shard_a_marker_and_no_collector(tmp_path):
    """The driver owns the host collector: shards only publish their window."""
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    shard0 = build_shard_config(_trial_cfg(), plan.specs[0], count=plan.count)["monitor"]
    shard1 = build_shard_config(_trial_cfg(), plan.specs[1], count=plan.count)["monitor"]

    assert shard0["stress_file"] == str(tmp_path / "shard-00" / "stress.lock")
    assert shard1["stress_file"] == str(tmp_path / "shard-01" / "stress.lock")
    assert shard0["external_collector"] is True and shard1["external_collector"] is True
    # One fleet gauge per trial; shards > 0 never poll the provider API (P
    # simultaneous samplers would multiply the control-plane list-sandbox load).
    assert shard0["api_state"] == "true"
    assert shard1["api_state"] == "false"


def test_build_shard_config_keeps_one_fleet_gauge_and_clamps_concurrency(tmp_path):
    """Shard 0 samples the fleet gauge unless explicitly disabled; other shards
    never poll the provider API. On the P > ratio slot split a shard's
    running_concurrency clamps to its own total_count so the kernel's config
    validate does not reject the shard at startup."""
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting=4)  # P = 4 > ratio
    assert [s.size for s in plan.specs] == [2, 2, 2, 2]
    base = _trial_cfg()
    base["replay"]["running_concurrency"] = 8

    m0 = build_shard_config(base, plan.specs[0], count=plan.count)["monitor"]
    assert m0["api_state"] == "true"
    # An explicit "auto" (the schema default) must not silently drop the gauge.
    auto = {**_trial_cfg(), "monitor": {"api_state": "auto"}}
    assert build_shard_config(auto, plan.specs[0], count=plan.count)["monitor"]["api_state"] == "true"
    # An explicit "false" is an operator opt-out.
    off = {**_trial_cfg(), "monitor": {"api_state": "false"}}
    assert build_shard_config(off, plan.specs[0], count=plan.count)["monitor"]["api_state"] == "false"

    for spec in plan.specs[1:]:
        assert build_shard_config(base, spec, count=plan.count)["monitor"]["api_state"] == "false"
        cfg = build_shard_config(base, spec, count=plan.count)
        assert cfg["replay"]["running_concurrency"] == spec.size


def test_build_shard_config_leaves_monitor_alone_when_driver_does_not_own_it(tmp_path):
    """--no-vm-monitor on the driver: no markers, no external-collector handoff."""
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    monitor = build_shard_config(_trial_cfg(), plan.specs[0], count=plan.count, owns_collector=False)["monitor"]
    assert "external_collector" not in monitor
    assert "api_state" not in monitor
    assert monitor["log_dir"] == str(tmp_path / "vm_monitor")  # still the fixed path

    off = {**_trial_cfg(), "monitor": {"enabled": "false"}}
    assert "external_collector" not in build_shard_config(off, plan.specs[0], count=plan.count)["monitor"]


def test_build_shard_config_scales_launch_interval_to_preserve_global_rate(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=3, n=4, setting="auto")
    cfg_in = _trial_cfg()
    cfg_in["replay"]["launch_interval_sec"] = 0.5
    cfg = build_shard_config(cfg_in, plan.specs[0], count=plan.count)
    assert cfg["replay"]["launch_interval_sec"] == pytest.approx(1.5)  # P * interval


def test_build_shard_config_scales_batch_intervals_to_preserve_global_rate(tmp_path):
    # P independently-pacing shards would otherwise create/dispatch at P x the
    # global rate: create_batch/task_batch intervals scale by P too.
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    cfg_in = _trial_cfg()
    cfg_in["create_batch"] = {"interval": 1}
    cfg_in["task_batch"] = {"interval": 2}
    cfg = build_shard_config(cfg_in, plan.specs[0], count=plan.count)
    assert cfg["create_batch"]["interval"] == 2
    assert cfg["task_batch"]["interval"] == 4


def test_write_shard_configs_materializes_one_yaml_per_shard(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    paths = write_shard_configs(_trial_cfg(), plan)
    assert paths == [tmp_path / "shard-00" / "shard.yaml", tmp_path / "shard-01" / "shard.yaml"]
    for p in paths:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert loaded["sandbox"]["total_count"] == 4


def test_write_shard_configs_removes_a_stale_stress_marker(tmp_path):
    """A prior attempt killed hard leaves shard-XX/stress.lock behind (a fresh
    kernel clears it only at monitor start, after create + warmup). The driver's
    bracket polls marker EXISTENCE, so the next attempt must start clean or it
    opens the host window on its first poll, over create/warmup."""
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    for spec in plan.specs:
        spec.directory.mkdir(parents=True)
        spec.stress_path.write_text("")
    write_shard_configs(_trial_cfg(), plan)
    assert all(not spec.stress_path.exists() for spec in plan.specs)


# ---------------------------------------------------------------------------
# Driver-owned host monitor
# ---------------------------------------------------------------------------


def test_stress_bracket_latches_open_until_stop(tmp_path):
    """A gapped union -- one shard finishing while another is still creating --
    must never end the sampling: the collector is single-shot and would export
    the create/warmup window before the tail shards' stress starts."""
    m1 = tmp_path / "shard-00" / "stress.lock"
    m2 = tmp_path / "shard-01" / "stress.lock"
    ctl = _RecordingController()
    bracket = ShardStressBracket([m1, m2], ctl, poll_sec=0.02)
    bracket.start()
    try:
        m1.parent.mkdir(parents=True)
        m1.touch()
        _wait_for(lambda: ctl.calls == ["begin"], 2.0)
        m1.unlink()  # first shard's window ends ...
        m2.parent.mkdir(parents=True)
        m2.touch()  # ... while the second shard only now reaches stress
        _wait_for(lambda: ctl.calls == ["begin"] and m2.exists(), 2.0)
        assert ctl.calls == ["begin"], "the gap must not close the sampling window"
    finally:
        bracket.stop()
    assert ctl.calls == ["begin", "end"]


def test_stress_bracket_without_any_shard_marker_stays_closed(tmp_path):
    """No shard reached stress: the window never opens and stop() does not end
    one -- the driver then abandons the collector instead of exporting a window
    that cannot exist."""
    ctl = _RecordingController()
    bracket = ShardStressBracket([tmp_path / "shard-00" / "stress.lock"], ctl, poll_sec=0.02)
    bracket.start()
    time.sleep(0.1)
    bracket.stop()
    assert not bracket.opened
    assert ctl.calls == []


def test_start_host_monitor_bounds_sampling_by_the_trial_deadline(tmp_path, monkeypatch):
    """vm_monitor's -t back-stop must cover the WHOLE union window (inter-shard
    start skew + the trial) -- the group deadline is the sound bound, because
    the window cannot outlive it. test_duration alone understates exec_only
    trials where every shard runs a full create_all first."""
    captured: dict[str, int | None] = {}

    class _StubController:
        def __init__(self, cfg, hint) -> None:  # noqa: ANN001 (test stub)
            captured["test_duration"] = cfg.test_duration
            self.proc = object()  # not None -> start() returns the collector

        def start(self) -> None:
            pass

    monkeypatch.setattr(oversub_shards, "MonitorController", _StubController)
    start_host_monitor(_trial_cfg(), tmp_path, provider="fake", test_duration=60, timeout_sec=240)
    assert captured["test_duration"] == 240
    start_host_monitor(_trial_cfg(), tmp_path, provider="fake", test_duration=180, timeout_sec=None)
    assert captured["test_duration"] == 180


def test_collect_warns_when_summary_lacks_trajectory_index(tmp_path, caplog):
    """A shard that produced a summary but lost its index.json must not vanish
    silently from the merged index -- symmetric with the series-missing check."""
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=1, n=4, setting="auto")
    write_shard_configs(_trial_cfg(), plan)
    run_dir = plan.specs[0].directory / "rb_20260101_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "rb_run_summary.json").write_text(json.dumps({"started_epoch": 1, "succeeded": 4}), encoding="utf-8")

    out = collect_shard_artifacts(plan, prefix="rb")
    assert len(out.summaries) == 1 and out.summaries[0]["succeeded"] == 4
    assert out.catalogs == []
    assert any("without trajectories/index.json" in r.getMessage() for r in caplog.records)


def test_build_shard_command_vm_monitor_owned_by_shard_zero(tmp_path):
    plan = plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")
    cmd0 = build_shard_command(["bench-core"], plan.specs[0], provider="aenv", no_vm_monitor=False)
    cmd1 = build_shard_command(["bench-core"], plan.specs[1], provider="aenv", no_vm_monitor=False)
    assert "--no-vm-monitor" not in cmd0
    assert "--no-vm-monitor" in cmd1
    assert cmd0[:5] == ["bench-core", "--config", str(plan.specs[0].config_path), "--provider", "aenv"]
    # global opt-out applies to every shard
    cmd0n = build_shard_command(["bench-core"], plan.specs[0], provider="aenv", no_vm_monitor=True)
    assert "--no-vm-monitor" in cmd0n


# ---------------------------------------------------------------------------
# Driver-owned host collector
# ---------------------------------------------------------------------------


class _RecordingController:
    """Stands in for MonitorController: records the bracket it was given."""

    def __init__(self):
        self.calls: list[str] = []

    def begin_stress(self):
        self.calls.append("begin")

    def end_stress(self):
        self.calls.append("end")


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


def test_stress_bracket_spans_the_union_of_shard_markers(tmp_path):
    """First shard start to driver stop -- an early finisher never cuts it."""
    markers = [tmp_path / "a.lock", tmp_path / "b.lock"]
    ctl = _RecordingController()
    bracket = ShardStressBracket(markers, ctl, poll_sec=0.01)
    bracket.start()
    try:
        markers[1].touch()
        _wait_for(lambda: ctl.calls == ["begin"])
        markers[0].touch()
        markers[1].unlink()
        markers[0].unlink()  # union momentarily empty mid-trial ...
        time.sleep(0.05)
        assert ctl.calls == ["begin"]  # ... but the window stays latched open
    finally:
        bracket.stop()
    assert ctl.calls == ["begin", "end"]


def test_stress_bracket_closes_on_stop_when_a_shard_died_mid_stress(tmp_path):
    marker = tmp_path / "shard.lock"
    ctl = _RecordingController()
    bracket = ShardStressBracket([marker], ctl, poll_sec=0.01)
    bracket.start()
    marker.touch()  # a SIGKILLed shard never removes its own marker
    _wait_for(lambda: ctl.calls == ["begin"])
    bracket.stop()
    assert ctl.calls == ["begin", "end"]
    assert bracket.opened is True


def test_stress_bracket_reports_never_opened_when_no_shard_starts(tmp_path):
    """Every shard crashed before stress: the driver must abandon, not wait."""
    ctl = _RecordingController()
    bracket = ShardStressBracket([tmp_path / "a.lock", tmp_path / "b.lock"], ctl, poll_sec=0.01)
    bracket.start()
    time.sleep(0.05)
    bracket.stop()
    assert ctl.calls == []
    assert bracket.opened is False


def test_start_host_monitor_uses_the_trial_root_and_a_driver_lock(tmp_path, monkeypatch):
    seen = {}

    class _FakeController:
        proc = object()  # "spawned": start_host_monitor returns the controller

        def __init__(self, cfg, provider):
            seen["cfg"], seen["provider"] = cfg, provider

        def start(self):
            pass

    monkeypatch.setattr(oversub_shards, "MonitorController", _FakeController)
    monkeypatch.setattr(oversub_shards, "provider_vmm_type", lambda _name: "cubesandbox")

    ctl = start_host_monitor(_trial_cfg(), tmp_path, provider="cubesandbox", test_duration=60)

    assert isinstance(ctl, _FakeController)
    mc = seen["cfg"].monitor
    assert mc.log_dir == str(tmp_path / "vm_monitor")
    assert mc.stress_file == str(tmp_path / "vm_monitor" / "stress.lock")
    assert mc.external_collector is False
    assert (seen["cfg"].output_dir, seen["cfg"].test_duration) == (str(tmp_path), 60)
    assert seen["provider"].name == "cubesandbox-driver"  # log lines must not claim the provider has no list API
    assert seen["provider"].vmm_type == "cubesandbox"
    assert seen["provider"].list_sandbox_states() is None  # no handle: shard 0 gauges


# ---------------------------------------------------------------------------
# Merge algebra
# ---------------------------------------------------------------------------


def _shard_summary(**over) -> dict:
    s = {
        "schema_version": 2,
        "workflow_type": "replay",
        "replay_mode": "exec_ephemeral",
        "provider": "aenv",
        "started_at": "2026-10-30T10:00:00+08:00",
        "completed_at": "2026-10-30T10:18:00+08:00",
        "started_epoch": 1000.0,
        "completed_epoch": 2080.0,
        "test_duration": 3600,
        "wall_sec": 1080.0,
        "total_count": 4,
        "running_concurrency": 4,
        "overcommit_ratio": 1.0,
        "throughput": {
            "total": 8,
            "succeeded": 8,
            "failed": 0,
            "total_steps": 40,
            "steps_per_sec": 0.037,
            "tasks_per_sec": 0.0074,
        },
        "admission": None,
        "lifecycle_overhead": None,
        "paths": {"trajectory_index": "/x/trajectories/index.json", "vm_monitor_dir": None},
        "error": None,
    }
    s.update(over)
    return s


def _plan2(tmp_path):
    return plan_shards(trial_dir=tmp_path, mode="exec_ephemeral", ratio=2, n=4, setting="auto")


def test_merge_run_summaries_counts_sum_rates_recompute_span_envelope(tmp_path):
    plan = _plan2(tmp_path)
    s0 = _shard_summary()
    # s1 starts late AND finishes last: envelope (1000..2085) = 1085 differs
    # from max-of-shard-walls (1080) -- the merged wall must be the envelope,
    # matching the kernel's wall_sec == completed_epoch - started_epoch.
    s1 = _shard_summary(
        started_epoch=1005.0,
        started_at="2026-10-30T10:00:05+08:00",
        completed_epoch=2085.0,
        completed_at="2026-10-30T10:18:05+08:00",
        wall_sec=1075.0,
    )
    artifacts = ShardArtifacts(summaries=[s0, s1], summary_paths=["/a.json", "/b.json"])
    result = GroupResult(returncodes=[0, 0], interrupted=False, timed_out=False)
    merged = merge_run_summaries([s0, s1], plan=plan, artifacts=artifacts, result=result)

    assert merged["throughput"]["total"] == 16
    assert merged["throughput"]["succeeded"] == 16
    assert merged["throughput"]["total_steps"] == 80
    assert merged["wall_sec"] == 1085.0  # envelope: max(completed) - min(started)
    # rates recomputed over the envelope wall, NOT summed per-shard rates
    assert merged["throughput"]["tasks_per_sec"] == round(16 / 1085.0, 3)
    assert merged["throughput"]["steps_per_sec"] == round(80 / 1085.0, 3)
    assert merged["started_epoch"] == 1000.0 and merged["started_at"] == s0["started_at"]
    assert merged["completed_epoch"] == 2085.0 and merged["completed_at"] == s1["completed_at"]
    assert merged["shard_start_skew_sec"] == 5.0
    assert merged["total_count"] == 8  # k*N echo restored
    assert merged["overcommit_ratio"] == 2.0  # 8 slots / N=4
    assert merged["admission"] is None and merged["lifecycle_overhead"] is None
    assert merged["error"] is None
    assert merged["shard_count"] == 2 and merged["shard_return_codes"] == [0, 0]
    assert merged["shard_run_summaries"] == ["/a.json", "/b.json"]


def test_merge_run_summaries_span_falls_back_to_max_wall_without_epochs(tmp_path):
    plan = _plan2(tmp_path)
    s0 = _shard_summary(started_epoch=None, completed_epoch=None, wall_sec=1080.0)
    s1 = _shard_summary(started_epoch=None, completed_epoch=None, wall_sec=900.0)
    artifacts = ShardArtifacts(summaries=[s0, s1], summary_paths=["/a.json", "/b.json"])
    result = GroupResult(returncodes=[0, 0], interrupted=False, timed_out=False)
    merged = merge_run_summaries([s0, s1], plan=plan, artifacts=artifacts, result=result)
    assert merged["wall_sec"] == 1080.0  # partially-flushed shard: max wall fallback
    assert merged["started_epoch"] is None and merged["completed_epoch"] is None
    assert merged["throughput"]["tasks_per_sec"] == round(16 / 1080.0, 3)


def test_merge_run_summaries_missing_and_failed_shards_surface_in_error(tmp_path):
    plan = _plan2(tmp_path)
    s0 = _shard_summary(error="create pool exhausted")
    artifacts = ShardArtifacts(summaries=[s0, None], summary_paths=["/a.json", None])
    result = GroupResult(returncodes=[0, 1], interrupted=False, timed_out=False)
    merged = merge_run_summaries([s0, None], plan=plan, artifacts=artifacts, result=result)
    assert merged["error"]
    assert "shard 1: no run_summary.json" in merged["error"]
    assert "shard 1: exit code 1" in merged["error"]
    assert "create pool exhausted" in merged["error"]
    assert merged["throughput"]["succeeded"] == 8  # present shard only; never backfilled
    assert result.trial_return_code() == 1


def test_merge_run_summaries_nothing_present_yields_minimal_invalid_shape(tmp_path):
    plan = _plan2(tmp_path)
    artifacts = ShardArtifacts(summaries=[None, None], summary_paths=[None, None])
    result = GroupResult(returncodes=[-9, -9], interrupted=False, timed_out=False)
    merged = merge_run_summaries([None, None], plan=plan, artifacts=artifacts, result=result)
    assert merged["throughput"] == {} and merged["error"]


def test_group_result_interrupt_maps_to_130():
    assert GroupResult([0, 0], interrupted=True, timed_out=False).trial_return_code() == 130
    assert GroupResult([0, 0], interrupted=False, timed_out=False).trial_return_code() == 0


def _catalog(
    sandbox_index: int,
    *,
    runs_shape: bool = True,
    run_over: dict | None = None,
    entry_over: dict | None = None,
) -> dict:
    run = {
        "sandbox_index": sandbox_index,
        "round_id": 0,
        "n_steps": 2,
        "n_failed": 0,
        "n_timeout": 0,
        "success_rate": 1.0,
        "elapsed_sec": 3.0,
        "time_breakdown_sec": {"exec": 1.0},
        "create_error_type": None,
        "kill_error_type": None,
    }
    run.update(run_over or {})
    entry = {"trajectory_id": "t0", "action_series": "echo hi", "file": "t0/replay_result.json"}
    if runs_shape:
        entry.update(
            {
                "n_runs": 1,
                "n_success": 1,
                "n_failed": 0,
                "success_rate": run["success_rate"],
                "elapsed_sec": run["elapsed_sec"],
                "all_failed": False,
                "n_steps_max": run["n_steps"],
                "time_breakdown_sec": dict(run["time_breakdown_sec"]),
                "create_error_type": run["create_error_type"],
                "kill_error_type": run["kill_error_type"],
                "runs": [run],
            }
        )
    else:
        entry.update(run)  # legacy flat shape: the entry IS the run
    entry.update(entry_over or {})
    return {"n_trajectories": 1, "trajectories": [entry]}


def test_merge_trajectory_indexes_groups_by_tid_and_offsets_sandbox_index():
    merged = merge_trajectory_indexes([(0, _catalog(0)), (4, _catalog(0))])
    assert merged["n_trajectories"] == 1
    entry = merged["trajectories"][0]
    assert entry["trajectory_id"] == "t0"
    assert entry["n_runs"] == 2 and entry["n_success"] == 2 and entry["n_failed"] == 0
    assert [r["sandbox_index"] for r in entry["runs"]] == [0, 4]  # run keys stay unique
    assert entry["action_series"] == "echo hi"  # scalar inherited once
    assert entry.get("sandbox_index") is None  # run-scoped keys never leak to entry scalars
    assert entry["file"] is None  # deep replay_result.json stays per-shard; no dead link


def test_merge_trajectory_indexes_recomputes_entry_aggregates_with_kernel_rules():
    # shard 0: t0 succeeds (elapsed 10.0). shard 1: t0 fails at create.
    # The merged entry must describe the FULL run set, not shard 0's subset.
    ok = _catalog(0, run_over={"elapsed_sec": 10.0})
    bad = _catalog(
        0,
        run_over={
            "success_rate": 0.0,
            "n_steps": 0,
            "elapsed_sec": None,
            "time_breakdown_sec": {"create": 30.0},
            "create_error_type": "timeout",
        },
        entry_over={
            "success_rate": 0.0,
            "n_success": 0,
            "n_failed": 1,
            "all_failed": True,
            "elapsed_sec": None,
            "create_error_type": "timeout",
            "time_breakdown_sec": {"create": 30.0},
        },
    )
    merged = merge_trajectory_indexes([(0, ok), (4, bad)])
    e = merged["trajectories"][0]
    assert e["n_runs"] == 2 and e["n_success"] == 1 and e["n_failed"] == 1
    assert e["all_failed"] is False
    assert e["success_rate"] == 0.5  # mean of per-run rates, NOT shard 0's inherited 1.0
    assert e["elapsed_sec"] == 10.0  # success-only p50 (the failing run's None is excluded)
    assert e["create_error_type"] == "timeout"  # first-seen across ALL shards' runs
    assert e["n_steps_max"] == 2
    # breakdown p50 over the success use-set only (kernel rule)
    assert e["time_breakdown_sec"] == {"exec": 1.0}
    assert [r["sandbox_index"] for r in e["runs"]] == [0, 4]  # sorted by (sandbox_index, round_id)


def test_merge_trajectory_indexes_all_failed_falls_back_to_all_runs():
    f1 = _catalog(
        0,
        run_over={"success_rate": 0.0, "elapsed_sec": 4.0},
        entry_over={"success_rate": 0.0, "n_success": 0, "n_failed": 1, "all_failed": True, "elapsed_sec": 4.0},
    )
    f2 = _catalog(
        0,
        run_over={"success_rate": 0.0, "elapsed_sec": 8.0},
        entry_over={"success_rate": 0.0, "n_success": 0, "n_failed": 1, "all_failed": True, "elapsed_sec": 8.0},
    )
    merged = merge_trajectory_indexes([(0, f1), (4, f2)])
    e = merged["trajectories"][0]
    assert e["all_failed"] is True and e["n_success"] == 0 and e["n_failed"] == 2
    assert e["success_rate"] == 0.0
    assert e["elapsed_sec"] == 8.0  # all-runs fallback, kernel upper-median p50 (NOT the 6.0 mean)


def test_merge_trajectory_indexes_elapsed_p50_is_upper_median_like_kernel():
    # kernel calc_percentiles p50 = sorted[n//2]: for 2 runs (10.0, 12.0) it is
    # 12.0, not statistics.median's 11.0 -- k=2 is the common multiplier case.
    a = _catalog(0, run_over={"elapsed_sec": 10.0})
    b = _catalog(0, run_over={"elapsed_sec": 12.0})
    merged = merge_trajectory_indexes([(0, a), (4, b)])
    assert merged["trajectories"][0]["elapsed_sec"] == 12.0


def test_merge_trajectory_indexes_promotes_legacy_flat_entries():
    merged = merge_trajectory_indexes([(0, _catalog(1, runs_shape=False))])
    entry = merged["trajectories"][0]
    assert entry["n_runs"] == 1
    assert entry["runs"][0]["sandbox_index"] == 1
    assert entry["runs"][0]["n_steps"] == 2


def test_write_merged_artifacts_fixed_path_and_index_patching(tmp_path):
    summary = {"throughput": {"total": 1}, "paths": {"trajectory_index": None}}
    index = {"n_trajectories": 1, "trajectories": []}
    path = write_merged_artifacts(tmp_path, "rb", summary, index)
    assert path == tmp_path / "merged" / "rb_run_summary.json"
    assert json.loads(path.read_text(encoding="utf-8"))["paths"]["trajectory_index"].endswith(
        str(Path("merged") / "trajectories" / "index.json")
    )
    assert (tmp_path / "merged" / "trajectories" / "index.json").exists()


def _series(path: Path, n: int) -> Path:
    path.write_text(
        "".join(json.dumps({"event": "step", "sandbox_index": i, "timestamp": 1000.0 + i}) + "\n" for i in range(n)),
        encoding="utf-8",
    )
    return path


def test_merge_lifecycle_series_concats_and_rekeys_all_shards(tmp_path, caplog):
    # Fleet-wide inflight analysis needs every shard's events with re-keyed
    # sandbox_index; shard-local keys would conflate sandboxes across shards
    # (and reading one shard caps the observed peak at N -- the T2 misread).
    plan = _plan2(tmp_path)  # sizes [4, 4] -> offsets [0, 4]
    s0 = {"paths": {"lifecycle_series": str(_series(tmp_path / "a.jsonl", 2))}}
    s1 = {"paths": {"lifecycle_series": str(_series(tmp_path / "b.jsonl", 3))}}
    gone = {"paths": {"lifecycle_series": str(tmp_path / "gone.jsonl")}}  # missing file skipped
    out = merge_lifecycle_series([s0, s1, gone, None, {"paths": {}}], plan, tmp_path / "merged", "rb")
    assert out == tmp_path / "merged" / "rb_lifecycle_series.jsonl"
    events = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(events) == 5
    assert [e["sandbox_index"] for e in events[:2]] == [0, 1]  # shard 0: offset 0
    assert [e["sandbox_index"] for e in events[2:]] == [4, 5, 6]  # shard 1: offset 4
    # A shard that reported a series it no longer has must surface: the merged
    # series then under-counts against the merged summary, and silence would
    # hide exactly that.
    partial = merge_lifecycle_series([s0, gone], plan, tmp_path / "m3", "rb")
    assert len([x for x in partial.read_text(encoding="utf-8").splitlines() if x.strip()]) == 2
    assert any("reported lifecycle series" in r.getMessage() for r in caplog.records)
    assert merge_lifecycle_series([None, {"paths": {}}], plan, tmp_path / "m2", "rb") is None


def _write_shard_summary(directory: Path, stamp: str, started_epoch: float) -> Path:
    run_dir = directory / f"rb_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "rb_run_summary.json"
    path.write_text(json.dumps(_shard_summary(started_epoch=started_epoch)), encoding="utf-8")
    return path


def test_collect_shard_artifacts_scopes_to_current_attempt(tmp_path):
    # Shard dirs keep one run dir per attempt. A shard that crashes before
    # flushing this attempt must read as missing -- not as the previous
    # attempt's summary, whose counts would silently join the merge.
    plan = _plan2(tmp_path)
    s0, s1 = plan.specs
    _write_shard_summary(s0.directory, "20260101-000000", 100.0)  # previous attempt
    current = _write_shard_summary(s0.directory, "20260102-000000", 2000.0)
    _write_shard_summary(s1.directory, "20260101-000000", 100.0)  # stale only

    scoped = collect_shard_artifacts(plan, "rb", since_epoch=1500.0)
    assert scoped.summary_paths[0] == str(current)
    assert scoped.summary_paths[1] is None and scoped.summaries[1] is None
    assert scoped.missing == [1]

    # Unscoped (pre-restart behavior): newest-by-name wins for both.
    unscoped = collect_shard_artifacts(plan, "rb")
    assert unscoped.summary_paths[0] == str(current)
    assert unscoped.summary_paths[1] is not None


# ---------------------------------------------------------------------------
# Group supervision (real subprocesses, stdlib-only payloads)
# ---------------------------------------------------------------------------


def test_shard_group_runner_collects_all_return_codes(tmp_path):
    cmds = [
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        [sys.executable, "-c", "import sys; sys.exit(3)"],
    ]
    result = ShardGroupRunner(cmds, [tmp_path / "a.log", tmp_path / "b.log"], 60).run()
    assert result.returncodes == [0, 3]
    assert not result.interrupted and not result.timed_out
    assert result.trial_return_code() == 3


def test_shard_group_runner_deadline_terminates_whole_group(tmp_path):
    # One fast shard, one sleeper: the shared deadline must tear down BOTH
    # (an orphaned shard would keep stressing the host).
    cmds = [
        [sys.executable, "-c", "pass"],
        [sys.executable, "-c", "import time; time.sleep(60)"],
    ]
    started = time.monotonic()
    result = ShardGroupRunner(cmds, [tmp_path / "a.log", tmp_path / "b.log"], 2).run()
    assert time.monotonic() - started < 40  # killed, not waited out
    assert result.timed_out
    assert all(rc != 0 for rc in result.returncodes[1:])


def test_shard_group_runner_launch_failure_terminates_started_shards(tmp_path, monkeypatch):
    # A Popen failure mid-launch (fd exhaustion, missing interpreter) must tear
    # down the shards already stressing the host, not orphan them.
    real_popen = subprocess.Popen
    started = []

    def fake_popen(cmd, **kwargs):
        if len(started) == 1:
            raise OSError("simulated launch failure")
        proc = real_popen(cmd, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(oversub_shards.subprocess, "Popen", fake_popen)
    cmds = [
        [sys.executable, "-c", "import time; time.sleep(60)"],
        [sys.executable, "-c", "pass"],
    ]
    with pytest.raises(OSError, match="simulated launch failure"):
        ShardGroupRunner(cmds, [tmp_path / "a.log", tmp_path / "b.log"], 60).run()
    assert len(started) == 1
    assert started[0].poll() is not None  # terminated, not orphaned


# ---------------------------------------------------------------------------
# Stub end-to-end: main() -> sharded trial -> merged contract artifacts
# ---------------------------------------------------------------------------

_SHARD_STUB = """import json, sys, time
from pathlib import Path
import yaml
cfg = yaml.safe_load(open(sys.argv[sys.argv.index("--config") + 1]))
pfx = cfg["report"]["filename_prefix"]
out = Path(cfg["report"]["output_dir"])
out.mkdir(parents=True, exist_ok=True)
(out / "argv.json").write_text(json.dumps(sys.argv[1:]))
# Stand in for the kernel's stress bracket: publish this shard's marker for a
# window that differs per shard, so a union bracket is distinguishable from
# one shard's window. (The driver-owned collector watches exactly this.)
lock = Path((cfg.get("monitor") or {}).get("stress_file", "/dev/shm/stub.lock"))
lock.parent.mkdir(parents=True, exist_ok=True)
idx = int(out.name.rsplit("-", 1)[-1])
time.sleep(0.3 * idx)
began = time.time()
lock.touch()
time.sleep(0.5 + 0.8 * idx)
lock.unlink()
(out / "marker.json").write_text(json.dumps({"start": began, "end": time.time()}))
run_dir = out / f"{pfx}_{time.strftime('%Y%m%d-%H%M%S')}"
(run_dir / "trajectories").mkdir(parents=True, exist_ok=True)
total = cfg["sandbox"]["total_count"]
m = (cfg.get("replay") or {}).get("multiplier") or 1
# Real wall-clock epochs, like the kernel writes: the driver scopes artifact
# selection to the current attempt by started_epoch (a stale-summary guard).
finished = time.time()
summary = {
  "schema_version": 2, "workflow_type": "replay",
  "replay_mode": cfg["replay"]["mode"], "provider": "stub",
  "started_at": "2026-10-30T10:00:00+08:00", "completed_at": "2026-10-30T10:18:00+08:00",
  "started_epoch": began, "completed_epoch": finished,
  "test_duration": cfg["test"]["duration"], "wall_sec": round(finished - began, 3),
  "total_count": total, "running_concurrency": cfg["replay"]["running_concurrency"],
  "overcommit_ratio": 1.0,
  "throughput": {"total": total * m, "succeeded": total * m, "failed": 0,
                 "total_steps": 10 * m, "steps_per_sec": 0.01, "tasks_per_sec": 0.7},
  "admission": None, "lifecycle_overhead": None,
  "paths": {"report": None, "obs_xlsx": None,
            "lifecycle_series": str(run_dir / (pfx + "_lifecycle_series.jsonl")),
            "trajectory_index": None, "vm_monitor_dir": None},
  "error": None,
}
(run_dir / f"{pfx}_run_summary.json").write_text(json.dumps(summary, indent=2) + "\\n")
run = {"sandbox_index": 0, "round_id": 0, "n_steps": 1, "n_failed": 0, "n_timeout": 0,
       "success_rate": 1.0, "elapsed_sec": 1.0,
       "time_breakdown_sec": {"slice_total": 0.8, "exec": 0.5, "create": 0.05, "kill": 0.05,
                              "stress": 0.9},
       "create_error_type": None, "kill_error_type": None}
idx = {"n_trajectories": 1, "trajectories": [
  {"trajectory_id": "t0", "n_runs": 1, "n_success": 1, "n_failed": 0, "elapsed_sec": 1.0,
   "action_series": "echo hi", "file": "t0/replay_result.json", "runs": [run]}]}
(run_dir / "trajectories" / "index.json").write_text(json.dumps(idx, indent=2) + "\\n")
series = [{"event": "step", "sandbox_index": i, "timestamp": 1000.0 + i} for i in range(2)]
(run_dir / f"{pfx}_lifecycle_series.jsonl").write_text("".join(json.dumps(e) + "\\n" for e in series))
"""


def _base_yaml(tmp_path) -> Path:
    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "workflow_type": "replay",
                "sandbox": {"total_count": 4},
                "replay": {"running_concurrency": 4, "mode": "exec_ephemeral"},
                "test": {"duration": 60, "round_size": 4, "round_count": 1, "benchmark_mode": "fixed"},
                "report": {"output_dir": "results/replay", "filename_prefix": "rb", "format": "both"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return base


def _common_argv(tmp_path, base: Path, out_root: Path, stub: Path) -> list[str]:
    return [
        "--config",
        str(base),
        "--provider",
        "aenv",
        "--output-root",
        str(out_root),
        "--bench-core-bin",
        sys.executable,
        str(stub),
        "--cooldown-sec",
        "0",
        "--cleanup-between-trials",
        "off",
    ]


def test_e2e_sharded_trial_merges_into_contract_artifacts(tmp_path):
    """ratio 2 x M 2 -> P=2 shards x 4 slots; merged summary passes M*k*N validity."""
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + [
            "--ratios",
            "2",
            "--modes",
            "exec_ephemeral",
            "--multiplier",
            "2",
            "--shards",
            "auto",
            "--test-duration",
            "60",
        ]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"

    # Per-shard layout + vm_monitor ownership (shard 0 monitors, shard 1 doesn't).
    argv0 = json.loads((trial_dir / "shard-00" / "argv.json").read_text(encoding="utf-8"))
    argv1 = json.loads((trial_dir / "shard-01" / "argv.json").read_text(encoding="utf-8"))
    assert "--no-vm-monitor" not in argv0
    assert "--no-vm-monitor" in argv1

    # Merged summary: the schema-2 contract, with shard provenance attached.
    merged_path = trial_dir / "merged" / "rb_run_summary.json"
    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    assert merged["shard_count"] == 2
    assert merged["throughput"]["succeeded"] == 16  # M*k*N = 2*2*4
    assert merged["throughput"]["total"] == 16
    assert merged["total_count"] == 8  # k*N echo

    # Fleet-wide merged series: both shards' events, sandbox_index re-keyed by
    # slot offset (shard 1's local 0/1 -> 4/5); inflight analysis must not be
    # capped at one shard's N slots nor conflate cross-shard sandboxes.
    series = trial_dir / "merged" / "rb_lifecycle_series.jsonl"
    events = [json.loads(x) for x in series.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(events) == 4  # 2 shards x 2 events
    assert sorted(e["sandbox_index"] for e in events) == [0, 1, 4, 5]
    assert merged["paths"]["lifecycle_series"] == str(series)

    # Driver outputs see ONE trial row, valid, keyed to the merged artifacts.
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["valid"] == "True" and rows[0]["succeeded"] == "16" and rows[0]["target_count"] == "8"
    assert rows[0]["run_summary_path"].endswith(str(merged_path))

    # trajectory-detail: per-run rows with re-keyed sandbox_index (0 and 4).
    with open(out_root / "trajectory-detail.csv", newline="", encoding="utf-8") as f:
        trj = list(csv.DictReader(f))
    assert len(trj) == 2
    assert sorted(int(r["sandbox_index"]) for r in trj) == [0, 4]
    assert all(r["trajectory_id"] == "t0" and r["exec_sec"] == "0.5" for r in trj)


def test_e2e_ratio_one_trial_shares_the_sharded_layout(tmp_path):
    """ratio 1 (auto -> P=1) runs through the sharded machinery: one shard.

    The whole point of P=1 sharding: an agent comparing oversub ratios reads
    ONE structure at every ratio (trial.yaml + shard-00/ + merged/) instead of
    a special flat layout at ratio 1 -- and the host collector is driver-owned
    at every ratio, so a short ratio-1 window can never strand it idle.
    """
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "1", "--modes", "exec_ephemeral", "--shards", "auto", "--test-duration", "60"]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-01" / "repeat-00"

    assert not (trial_dir / "replay_bench").exists()  # no legacy flat run dir
    assert (trial_dir / "shard-00" / "shard.yaml").exists()
    assert (trial_dir / "shard-00" / "driver.log").exists()
    assert not (trial_dir / "shard-01").exists()

    merged = json.loads((trial_dir / "merged" / "rb_run_summary.json").read_text(encoding="utf-8"))
    assert merged["shard_count"] == 1
    assert merged["throughput"]["succeeded"] == 4  # k*N = 1*4
    assert merged["total_count"] == 4  # k*N echo
    assert sorted(trial_dir.glob("merged/*"))  # merged artifacts exist

    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["valid"] == "True"
    assert rows[0]["run_summary_path"].endswith(str(trial_dir / "merged" / "rb_run_summary.json"))


def test_e2e_ratio_one_driver_owns_the_host_collector(tmp_path, monkeypatch):
    """P=1 still hands the host collector to the driver, over the one shard's window.

    This is the case the original report was about (ratio-1 host report missing):
    a kernel-owned collector could miss a short window and idle forever. With the
    driver owning it, the output lands at <trial_dir>/vm_monitor and is bracketed
    by the shard's marker -- the same contract as P >= 2.
    """
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    vm_stub = tmp_path / "vm-monitor-stub.py"
    vm_stub.write_text(_VM_MONITOR_STUB, encoding="utf-8")

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: str(vm_stub) if n == "vm-monitor" else real_which(n))
    real_popen = subprocess.Popen

    def _popen(cmd, **kw):  # the stub is a .py script, not an executable
        return real_popen([sys.executable, *cmd] if str(cmd[0]).endswith(".py") else cmd, **kw)

    monkeypatch.setattr(subprocess, "Popen", _popen)

    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "1", "--modes", "exec_ephemeral", "--shards", "auto", "--test-duration", "60"]
    )
    assert rc == 0

    trial_dir = out_root / "exec_ephemeral" / "ratio-01" / "repeat-00"
    events = json.loads((trial_dir / "vm_monitor" / "stub.json").read_text(encoding="utf-8"))
    assert [e[0] for e in events] == ["start", "end"]  # one bracket over the single shard

    mark = json.loads((trial_dir / "shard-00" / "marker.json").read_text(encoding="utf-8"))
    began, ended = events[0][1], events[1][1]
    assert began <= mark["start"] + 1.0  # opened on the shard's window
    assert ended >= mark["end"] - 0.1  # closed only after it ended
    assert not (trial_dir / "shard-00" / "vm_monitor").exists()  # never under the shard


def test_e2e_explicit_shards_override_and_cap(tmp_path):
    """--shards 4 on ratio 2 (8 slots) -> 4 shards x 2 slots."""
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "2", "--modes", "exec_ephemeral", "--shards", "4", "--test-duration", "60"]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"
    shard_cfgs = sorted(trial_dir.glob("shard-*/shard.yaml"))
    assert len(shard_cfgs) == 4
    for p in shard_cfgs:
        assert yaml.safe_load(p.read_text(encoding="utf-8"))["sandbox"]["total_count"] == 2
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # non-multiplier validity: merged total = 8 attempts >= 0.9 * N(=4)
    assert rows[0]["valid"] == "True" and rows[0]["total"] == "8"


def test_e2e_crashed_shard_invalidates_trial_without_backfill(tmp_path):
    """A shard that exits nonzero with no summary -> trial invalid, error recorded."""
    stub = tmp_path / "stub.py"
    # Shard 1 crashes before writing anything (rc from env), shard 0 succeeds.
    stub.write_text(
        "import os, sys\n"
        "if os.environ.get('SHARD_CRASH') and 'shard-01' in sys.argv[sys.argv.index('--config') + 1]:\n"
        "    sys.exit(2)\n" + _SHARD_STUB,
        encoding="utf-8",
    )
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    old = os.environ.get("SHARD_CRASH")
    os.environ["SHARD_CRASH"] = "1"
    try:
        rc = main(
            _common_argv(tmp_path, base, out_root, stub)
            + ["--ratios", "2", "--modes", "exec_ephemeral", "--shards", "auto", "--test-duration", "60"]
        )
    finally:
        if old is None:
            del os.environ["SHARD_CRASH"]
        else:
            os.environ["SHARD_CRASH"] = old
    assert rc == 0  # the sweep continues; the TRIAL is what fails
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["valid"] == "False"
    assert rows[0]["return_code"] == "2"
    assert "shard 1" in rows[0]["error"]
    # the surviving shard's 4 attempts are merged as-is -- never padded to 8
    assert rows[0]["succeeded"] == "4"


def test_e2e_dry_run_writes_shard_yamls(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "3", "--modes", "exec_ephemeral", "--shards", "auto", "--dry-run", "--test-duration", "60"]
    )
    assert rc == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-03" / "repeat-00"
    assert (trial_dir / "trial.yaml").exists()  # logical provenance kept
    yamls = sorted(trial_dir.glob("shard-*/shard.yaml"))
    assert len(yamls) == 3
    for p in yamls:
        assert yaml.safe_load(p.read_text(encoding="utf-8"))["sandbox"]["total_count"] == 4  # 3*4/3


_IDLE_VM_MONITOR_STUB = """import os, time
from pathlib import Path
Path(os.environ["VM_STUB_PID_FILE"]).write_text(str(os.getpid()))
time.sleep(120)  # idle: the stress file never appears
"""


def test_e2e_idle_collector_is_abandoned_when_no_shard_reaches_stress(tmp_path, monkeypatch):
    """Every shard dies before dispatch -> no window, no report, no 60 s wait."""
    dying = tmp_path / "dying.py"
    dying.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    vm_stub = tmp_path / "vm-monitor-stub.py"
    vm_stub.write_text(_IDLE_VM_MONITOR_STUB, encoding="utf-8")
    pid_file = tmp_path / "vm.pid"
    monkeypatch.setenv("VM_STUB_PID_FILE", str(pid_file))

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: str(vm_stub) if n == "vm-monitor" else real_which(n))
    real_popen = subprocess.Popen

    def _popen(cmd, **kw):
        return real_popen([sys.executable, *cmd] if str(cmd[0]).endswith(".py") else cmd, **kw)

    monkeypatch.setattr(subprocess, "Popen", _popen)

    base = _base_yaml(tmp_path)
    cfg = yaml.safe_load(base.read_text(encoding="utf-8"))
    cfg["monitor"] = {"enabled": True, "vmm": "qemu", "report_timeout": 60}
    base.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    out_root = tmp_path / "sweep"
    started = time.monotonic()
    rc = main(
        _common_argv(tmp_path, base, out_root, dying)
        + ["--ratios", "2", "--modes", "exec_ephemeral", "--shards", "2", "--test-duration", "60"]
    )
    elapsed = time.monotonic() - started
    assert rc == 0
    # Abandoned, not waited out: the collector's report_timeout is 60 s and an
    # idle collector would also never exit on its own.
    assert elapsed < 30, f"sweep blocked {elapsed:.1f}s on an idle collector"

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        alive = psutil.pid_exists(pid)
        if not alive:
            break
        time.sleep(0.05)
    assert not alive, "idle collector left running after the trial"

    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["valid"] == "False"


_VM_MONITOR_STUB = """import json, sys, time
from pathlib import Path
argv = sys.argv[1:]
lock = Path(argv[argv.index("--stress-file") + 1])
log_dir = Path(argv[argv.index("--log-dir") + 1])
log_dir.mkdir(parents=True, exist_ok=True)
while not lock.exists():
    time.sleep(0.02)
events = [["start", time.time()]]
while lock.exists():
    time.sleep(0.02)
events.append(["end", time.time()])
(log_dir / "stub.json").write_text(json.dumps(events))
(log_dir / "resource_report.xlsx").write_text("stub")
"""


def test_e2e_driver_owns_one_host_collector_over_the_union_window(tmp_path, monkeypatch):
    """One collector for the whole trial, bracketed by the UNION of shard windows.

    The collector must still be sampling when the LAST shard is stressing (shard
    1 here outlives shard 0), which a shard-0-owned collector would not do, and
    its output must land at the trial root, never under ``shard-00/``.
    """
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    vm_stub = tmp_path / "vm-monitor-stub.py"
    vm_stub.write_text(_VM_MONITOR_STUB, encoding="utf-8")

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: str(vm_stub) if n == "vm-monitor" else real_which(n))
    real_popen = subprocess.Popen

    def _popen(cmd, **kw):  # the stub is a .py script, not an executable
        return real_popen([sys.executable, *cmd] if str(cmd[0]).endswith(".py") else cmd, **kw)

    monkeypatch.setattr(subprocess, "Popen", _popen)

    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    rc = main(
        _common_argv(tmp_path, base, out_root, stub)
        + ["--ratios", "2", "--modes", "exec_ephemeral", "--shards", "2", "--test-duration", "60"]
    )
    assert rc == 0

    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"
    events = json.loads((trial_dir / "vm_monitor" / "stub.json").read_text(encoding="utf-8"))
    assert [e[0] for e in events] == ["start", "end"]  # one bracket, not one per shard
    began, ended = events[0][1], events[1][1]

    marks = [json.loads((trial_dir / f"shard-0{i}" / "marker.json").read_text(encoding="utf-8")) for i in range(2)]
    assert marks[1]["end"] > marks[0]["end"] + 0.2  # shard 1 outlives shard 0
    assert began <= min(m["start"] for m in marks) + 1.0  # opened on the first shard's window
    assert ended >= max(m["end"] for m in marks) - 0.1  # closed only after the last one
    assert not (trial_dir / "shard-00" / "vm_monitor").exists()


def test_e2e_reuse_skips_completed_sharded_trial(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text(_SHARD_STUB, encoding="utf-8")
    base = _base_yaml(tmp_path)
    out_root = tmp_path / "sweep"
    argv = _common_argv(tmp_path, base, out_root, stub) + [
        "--ratios",
        "2",
        "--modes",
        "exec_ephemeral",
        "--multiplier",
        "2",
        "--shards",
        "auto",
        "--test-duration",
        "60",
    ]
    assert main(argv) == 0
    trial_dir = out_root / "exec_ephemeral" / "ratio-02" / "repeat-00"
    marker = trial_dir / "shard-00" / "argv.json"
    first_run = marker.read_text(encoding="utf-8")
    marker.write_text("SENTINEL", encoding="utf-8")
    assert main(argv + ["--reuse"]) == 0
    # reuse hit the merged summary: the stub never re-ran (sentinel intact)
    assert marker.read_text(encoding="utf-8") == "SENTINEL"
    assert first_run != "SENTINEL"
    with open(out_root / "trial-summary.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[-1]["reused"] == "True" and rows[-1]["valid"] == "True"
