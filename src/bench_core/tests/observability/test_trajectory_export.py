"""Phase 3: trajectory_export -- per-trajectory replay_result.json + index.json.

Aligns with the reference replay-aenv-main replay-result.json (steps[] +
aggregates + pause_resume_overhead) but is a SUPERSET: it also carries the
bench-core sub-segments (resume_queue_wait / pause_api / slot_contention /
running_slot_held), the trajectory-level create/kill error string, and a
trajectories/index.json catalog so a fleet of dozens/hundreds of trajectories
is browsable without walking folders.
"""
from __future__ import annotations

import json
from pathlib import Path

from bench_core.observability.trajectory_export import export_trajectories
from bench_core.observability.lifecycle_series import LifecycleSeriesWriter


def _step(
    tid,
    *,
    step_index=0,
    sandbox_index=0,
    resume_sec=0.1,
    exec_sec=0.4,
    pause_sec=0.2,
    slice_failed=False,
    timed_out=False,
    exit_code=0,
    slot_contention_wait_sec=0.0,
    resume_start=None,
    pause_end=None,
    resume_queue_wait_sec=0.0,
    resume_api_sec=0.0,
    resume_ready_wait_sec=0.0,
    pause_queue_wait_sec=0.0,
    pause_api_sec=0.0,
    running_slot_held_sec=0.0,
):
    s = round(resume_sec + exec_sec + pause_sec, 3)
    return {
        "event": "step",
        "sandbox_index": sandbox_index,
        "trajectory_id": tid,
        "step_index": step_index,
        "action_type": "shell",
        "resume_start": resume_start,
        "exec_start": None,
        "exec_end": None,
        "pause_start": None,
        "pause_end": pause_end,
        "resume_end": None,
        "resume_sec": resume_sec,
        "exec_sec": exec_sec,
        "pause_sec": pause_sec,
        "slice_total_sec": s,
        "interaction_total_sec": round(s + 0.05, 3),
        "slot_contention_wait_sec": slot_contention_wait_sec,
        "resume_queue_wait_sec": resume_queue_wait_sec,
        "resume_api_sec": resume_api_sec,
        "resume_ready_wait_sec": resume_ready_wait_sec,
        "pause_queue_wait_sec": pause_queue_wait_sec,
        "pause_api_sec": pause_api_sec,
        "running_slot_held_sec": running_slot_held_sec,
        "slice_failed": slice_failed,
        "timed_out": timed_out,
        "exit_code": exit_code,
    }


def _write(sp, events):
    w = LifecycleSeriesWriter(sp)
    for ev in events:
        w.write(ev)
    w.close()


def _records(out_dir):
    """Map trajectory_id -> its replay_result.json dict."""
    base = Path(out_dir) / "trajectories"
    out = {}
    for sub in sorted(base.iterdir()):
        p = sub / "replay_result.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            out[d["trajectory_id"]] = d
    return out


def test_export_writes_one_json_per_trajectory_and_returns_count(tmp_path):
    sp = tmp_path / "s.jsonl"
    _write(sp, [_step("traj-a"), _step("traj-a", step_index=1), _step("traj-b")])
    n = export_trajectories(sp, tmp_path)
    assert n == 2
    base = tmp_path / "trajectories"
    files = sorted(p.name for p in base.glob("*/replay_result.json"))
    assert files == ["replay_result.json", "replay_result.json"]  # two trajectories
    recs = _records(tmp_path)
    assert set(recs) == {"traj-a", "traj-b"}


def test_export_schema_matches_reference(tmp_path):
    sp = tmp_path / "s.jsonl"
    _write(sp, [_step("traj-a", resume_start=10.0, pause_end=11.0)])
    export_trajectories(sp, tmp_path)
    rec = _records(tmp_path)["traj-a"]
    # top-level keys (reference fields + bench-core extras)
    for k in (
        "trajectory_id",
        "sandbox_index",
        "round_id",
        "n_steps",
        "n_failed",
        "n_timeout",
        "success_rate",
        "elapsed_sec",
        "requested_delay_sec",
        "create_sec",
        "kill_sec",
        "create_error_type",
        "kill_error_type",
        "create_error",
        "kill_error",
        "sums",
        "overhead",
        "aggregates",
        "steps",
    ):
        assert k in rec, f"missing top-level key {k}"
    # overhead decomposition (reference's pause_resume_overhead)
    oh = rec["overhead"]
    assert {"pause_resume_total_sec", "per_cycle_sec", "pct_of_slice_total"} <= set(oh)
    # steps[] entry carries the reference fields + stderr:null + sub-segments
    st = rec["steps"][0]
    for k in (
        "index",
        "action_type",
        "pause_sec",
        "paused_sec",
        "resume_sec",
        "elapsed_sec",
        "return_code",
        "timed_out",
        "slice_failed",
        "stderr",
    ):
        assert k in st, f"missing step key {k}"
    assert st["stderr"] is None  # series excludes raw stderr by design
    for k in (
        "resume_queue_wait_sec",
        "resume_api_sec",
        "resume_ready_wait_sec",
        "pause_queue_wait_sec",
        "pause_api_sec",
        "slot_contention_wait_sec",
        "running_slot_held_sec",
        "interaction_total_sec",
        "slice_total_sec",
    ):
        assert k in st, f"missing sub-segment {k}"
    # aggregates: per-segment distribution with p99 (consistent with calc_percentiles)
    assert "slice_total_sec" in rec["aggregates"]
    agg = rec["aggregates"]["slice_total_sec"]
    assert {"n", "min", "max", "avg", "p50", "p95", "p99"} <= set(agg)


def test_export_aggregates_exclude_failed_steps(tmp_path):
    # 2 success + 1 failed (all-zero durations); aggregates must reflect only
    # the 2 real steps (zeros would skew p50/avg toward 0); n_failed reports 1.
    sp = tmp_path / "s.jsonl"
    _write(
        sp,
        [
            _step("t", step_index=0, exec_sec=0.4),
            _step("t", step_index=1, exec_sec=0.4),
            _step("t", step_index=2, slice_failed=True, exit_code=1, exec_sec=0.0, resume_sec=0.0, pause_sec=0.0),
        ],
    )
    export_trajectories(sp, tmp_path)
    rec = _records(tmp_path)["t"]
    assert rec["n_failed"] == 1
    assert rec["n_steps"] == 3
    agg = rec["aggregates"]["exec_sec"]
    assert agg["n"] == 2  # the failed step (0.0) excluded
    assert agg["min"] == 0.4 and agg["max"] == 0.4


def test_export_sanitizes_trajectory_id(tmp_path):
    # trajectory_id is an arbitrary string; it can contain /, .., or be two
    # distinct tids that sanitize to the same base -- must not escape the
    # trajectories/ root nor collide. (Empty "" is a degenerate case the real
    # runner never emits -- trajectory_create with tid="" is skipped by
    # trajectory_summaries; the ".." case already exercises the
    # sanitize-to-empty -> "unknown" branch, so the empty-string sanitization
    # itself is covered directly in test_sanitize_tid_handles_edge_cases.)
    sp = tmp_path / "s.jsonl"
    _write(
        sp,
        [
            {
                "event": "trajectory_create",
                "sandbox_index": 0,
                "trajectory_id": "a/b",
                "timestamp": 1.0,
                "create_sec": 0.1,
                "success": True,
            },
            {
                "event": "trajectory_create",
                "sandbox_index": 1,
                "trajectory_id": "a:b",
                "timestamp": 1.0,
                "create_sec": 0.1,
                "success": True,
            },
            {
                "event": "trajectory_create",
                "sandbox_index": 2,
                "trajectory_id": "..",
                "timestamp": 1.0,
                "create_sec": 0.1,
                "success": True,
            },
            {
                "event": "trajectory_create",
                "sandbox_index": 3,
                "trajectory_id": "a/b/c",
                "timestamp": 1.0,
                "create_sec": 0.1,
                "success": True,
            },
        ],
    )
    export_trajectories(sp, tmp_path)
    base = tmp_path / "trajectories"
    # nothing escapes the trajectories root (no parent traversal)
    all_files = [str(p) for p in base.rglob("replay_result.json")]
    assert all(str(p).startswith(str(base)) for p in all_files), "path escape detected"
    # four distinct directories (a/b and a:b both sanitize to "a_b" but must NOT collide)
    dirs = [p.parent.name for p in base.glob("*/replay_result.json")]
    assert len(dirs) == len(set(dirs)), f"sanitized dirs collided: {dirs}"
    assert len(dirs) == 4
    # the original trajectory_ids round-trip inside the JSON
    recs = _records(tmp_path)
    assert set(recs) == {"a/b", "a:b", "..", "a/b/c"}


def test_sanitize_tid_handles_edge_cases():
    from bench_core.observability.trajectory_export import _sanitize_tid

    # empty + ".." both reduce to "unknown" base but MUST differ by hash
    e = _sanitize_tid("")
    d = _sanitize_tid("..")
    assert e.startswith("unknown_") and d.startswith("unknown_")
    assert e != d, "empty and .. collided (same hash)"
    # two distinct tids that sanitize to the same base must NOT collide
    assert _sanitize_tid("a/b") != _sanitize_tid("a:b")
    # no path separators / parent segments survive into the dir name
    for tid in ("a/b", "..", "a/b/c", "../escape", ""):
        name = _sanitize_tid(tid)
        assert "/" not in name and "\\" not in name, f"separator in {name!r}"
        assert name not in (".", ".."), f"traversal dir {name!r}"
    # stable: same input -> same output (deterministic hash)
    assert _sanitize_tid("plain") == _sanitize_tid("plain")


def test_export_no_series_returns_zero(tmp_path):
    sp = tmp_path / "missing.jsonl"
    assert export_trajectories(sp, tmp_path) == 0
    # nothing written, no crash
    assert not (tmp_path / "trajectories").exists()


def test_export_reconstructs_paused_sec_and_elapsed(tmp_path):
    # Two steps with known inter-step gap: pause_end[0]=11, resume_start[1]=12
    # -> paused_sec(step1)=1.0; requested_delay_sec=1.0; elapsed_sec=3.0 (13-10).
    sp = tmp_path / "s.jsonl"
    _write(
        sp,
        [
            _step("t", step_index=0, resume_start=10.0, pause_end=11.0),
            _step("t", step_index=1, resume_start=12.0, pause_end=13.0),
        ],
    )
    export_trajectories(sp, tmp_path)
    rec = _records(tmp_path)["t"]
    assert rec["steps"][0]["paused_sec"] == 0.0  # first step has no predecessor
    assert rec["steps"][1]["paused_sec"] == 1.0
    assert rec["requested_delay_sec"] == 1.0
    assert rec["elapsed_sec"] == 3.0


def test_export_writes_index_json_catalog(tmp_path):
    sp = tmp_path / "s.jsonl"
    _write(sp, [_step("traj-a"), _step("traj-b")])
    n = export_trajectories(sp, tmp_path)
    idx_path = tmp_path / "trajectories" / "index.json"
    assert idx_path.exists()
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    assert idx["n_trajectories"] == n == 2
    rows = idx["trajectories"]
    assert {r["trajectory_id"] for r in rows} == {"traj-a", "traj-b"}
    # every catalog `file` resolves on disk, and carries the short summary
    for r in rows:
        p = tmp_path / "trajectories" / r["file"]
        assert p.exists(), f"catalog file {r['file']} missing"
        for k in (
            "trajectory_id",
            "n_steps",
            "n_failed",
            "n_timeout",
            "success_rate",
            "elapsed_sec",
            "create_error_type",
            "kill_error_type",
        ):
            assert k in r, f"catalog row missing {k}"


def test_export_surfaces_create_kill_error(tmp_path):
    # trajectory_create(success=False) carries error_type + error; the short
    # error string is the failure signal (per-step stderr is unavailable).
    sp = tmp_path / "s.jsonl"
    _write(
        sp,
        [
            {
                "event": "trajectory_create",
                "sandbox_index": 0,
                "trajectory_id": "boom",
                "timestamp": 1.0,
                "create_sec": 0.3,
                "success": False,
                "error_type": "TimeoutError",
                "error": "create timed out after 30s",
            },
        ],
    )
    export_trajectories(sp, tmp_path)
    rec = _records(tmp_path)["boom"]
    assert rec["create_error_type"] == "TimeoutError"
    assert rec["create_error"] == "create timed out after 30s"
    assert rec["n_steps"] == 0
