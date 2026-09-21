"""Phase 1: trajectory_summaries -- the shared per-trajectory aggregation.

Pure transform over a loaded event list (the same list load_events returns).
Reused by the obs_xlsx Trajectory summary sheet (DRY -- it replaces the inline
sum loop) and by the per-trajectory replay_result.json exporter. Union-keyed
over step + trajectory_create + trajectory_kill + trajectory_failed events so a
trajectory that failed at create (zero step events) still appears -- otherwise
the worst trajectories vanish from the comparison.
"""
from __future__ import annotations

from bench_core.observability.trajectory_summary import SEG_KEYS, aggregate_trajectories, trajectory_summaries


def _step(
    tid,
    *,
    step_index=0,
    sandbox_index=0,
    round_id=None,
    exec_sec=0.4,
    resume_sec=0.1,
    pause_sec=0.2,
    slice_total_sec=None,
    slot_contention_wait_sec=0.0,
    resume_rate_pacing_wait_sec=0.0,
    pause_rate_pacing_wait_sec=0.0,
    running_slot_held_sec=0.0,
    interaction_total_sec=None,
    slice_failed=False,
    timed_out=False,
    exit_code=0,
    resume_start=None,
    pause_end=None,
):
    s = round(resume_sec + exec_sec + pause_sec, 3)
    return {
        "event": "step",
        "sandbox_index": sandbox_index,
        "trajectory_id": tid,
        "round_id": round_id,
        "step_index": step_index,
        "action_type": "shell",
        "resume_sec": resume_sec,
        "exec_sec": exec_sec,
        "pause_sec": pause_sec,
        "slice_total_sec": slice_total_sec if slice_total_sec is not None else s,
        "slot_contention_wait_sec": slot_contention_wait_sec,
        "resume_rate_pacing_wait_sec": resume_rate_pacing_wait_sec,
        "pause_rate_pacing_wait_sec": pause_rate_pacing_wait_sec,
        "running_slot_held_sec": running_slot_held_sec,
        "interaction_total_sec": interaction_total_sec if interaction_total_sec is not None else round(s + 0.05, 3),
        "slice_failed": slice_failed,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "resume_start": resume_start,
        "pause_end": pause_end,
    }


class TestTrajectorySummaries:
    def test_groups_and_sums_two_trajectories(self):
        # 2 trajectories x 3 steps; distinct resume/pause so sums separate.
        events = []
        for tid, resume, pause, slot_wait in (("traj-a", 0.10, 0.20, 0.01), ("traj-b", 0.50, 0.60, 0.05)):
            for i in range(3):
                events.append(
                    _step(
                        tid,
                        step_index=i,
                        sandbox_index=0,
                        resume_sec=resume,
                        pause_sec=pause,
                        slot_contention_wait_sec=slot_wait,
                    )
                )
        out = trajectory_summaries(events)
        assert [s["trajectory_id"] for s in out] == ["traj-a", "traj-b"]
        a, b = out
        # traj-a: resume 3x0.10=0.30, pause 3x0.20=0.60, exec 3x0.4=1.2,
        # slice_total 3x0.70=2.10, slot_wait 3x0.01=0.03. Sums are RAW floats
        # (the renderer rounds at the boundary); compare at 3dp.
        assert a["n_steps"] == 3
        assert a["n_failed"] == 0
        assert a["n_timeout"] == 0
        assert a["success_rate"] == 1.0
        assert round(a["sums"]["resume_sec"], 3) == 0.3
        assert round(a["sums"]["pause_sec"], 3) == 0.6
        assert round(a["sums"]["exec_sec"], 3) == 1.2
        assert round(a["sums"]["slice_total_sec"], 3) == 2.1
        assert round(a["sums"]["slot_contention_wait_sec"], 3) == 0.03
        assert round(a["avg_slice"], 3) == 0.7
        # traj-b resume 3x0.50=1.50 > traj-a 0.30 -- per-instance separation
        assert round(b["sums"]["resume_sec"], 3) == 1.5
        # sum invariant: slice_total == resume + exec + pause
        assert round(a["sums"]["resume_sec"] + a["sums"]["exec_sec"] + a["sums"]["pause_sec"], 3) == round(
            a["sums"]["slice_total_sec"], 3
        )
        # every SEG_KEY present in sums
        for k in SEG_KEYS:
            assert k in a["sums"]

    def test_includes_create_only_trajectory_with_zero_steps(self):
        # A trajectory that created + killed but ran NO steps must still appear
        # (union-keyed) -- otherwise the worst trajectories vanish from comparison.
        events = [
            {
                "event": "trajectory_create",
                "sandbox_index": 0,
                "trajectory_id": "empty",
                "timestamp": 1.0,
                "create_sec": 1.2,
                "success": True,
            },
            {
                "event": "trajectory_kill",
                "sandbox_index": 0,
                "trajectory_id": "empty",
                "timestamp": 2.0,
                "kill_sec": 0.8,
                "success": True,
            },
        ]
        out = trajectory_summaries(events)
        assert len(out) == 1
        e = out[0]
        assert e["trajectory_id"] == "empty"
        assert e["n_steps"] == 0
        assert e["n_failed"] == 0
        assert e["n_timeout"] == 0
        assert e["success_rate"] is None  # no steps attempted, distinct from 0.0 (all failed)
        assert e["avg_slice"] == 0.0
        assert e["create_sec"] == 1.2
        assert e["kill_sec"] == 0.8
        # all sums zero
        for k in SEG_KEYS:
            assert e["sums"][k] == 0.0

    def test_includes_create_failed_trajectory(self):
        # A trajectory that failed at create (trajectory_failed event, no step
        # events, no trajectory_create success) must still appear.
        events = [
            {
                "event": "trajectory_failed",
                "sandbox_index": 1,
                "trajectory_id": "boom",
                "create_sec": 0.5,
                "kill_sec": 0.0,
            },
        ]
        out = trajectory_summaries(events)
        assert len(out) == 1
        f = out[0]
        assert f["trajectory_id"] == "boom"
        assert f["n_steps"] == 0
        assert f["success_rate"] is None
        assert f["create_sec"] == 0.5

    def test_success_rate_zero_steps_is_none_no_crash(self):
        events = [
            {
                "event": "trajectory_create",
                "sandbox_index": 0,
                "trajectory_id": "z",
                "timestamp": 1.0,
                "create_sec": 1.0,
                "success": True,
            },
        ]
        out = trajectory_summaries(events)
        # No ZeroDivisionError; None signals "no steps attempted".
        assert out[0]["success_rate"] is None

    def test_success_rate_all_failed_is_zero(self):
        # n_steps>0 but every step failed -> success_rate 0.0 (not None).
        events = [_step("bad", slice_failed=True, exit_code=1) for _ in range(3)]
        out = trajectory_summaries(events)
        assert out[0]["n_failed"] == 3
        assert out[0]["success_rate"] == 0.0

    def test_sorted_by_trajectory_id(self):
        events = [
            _step("zzz"),
            _step("aaa"),
            _step("mmm"),
        ]
        out = trajectory_summaries(events)
        assert [s["trajectory_id"] for s in out] == ["aaa", "mmm", "zzz"]

    def test_per_run_keying_separates_rounds(self):
        # Per-run keying: the same trajectory_id in two rounds (same sandbox) is
        # TWO runs -- not one summed record. Each run keeps its own sums; the
        # per-trajectory aggregate (aggregate_trajectories) rolls them up via
        # median, NOT sum (summing across runs is the pool<N inflation bug).
        events = [
            _step("traj-x", round_id=0, sandbox_index=0, step_index=0, exec_sec=0.4, resume_sec=0.1, pause_sec=0.2),
            _step("traj-x", round_id=1, sandbox_index=0, step_index=0, exec_sec=0.4, resume_sec=0.1, pause_sec=0.2),
        ]
        out = trajectory_summaries(events)
        assert len(out) == 2  # two runs, not one summed record
        for r in out:
            assert r["trajectory_id"] == "traj-x"
            assert r["sandbox_index"] == 0
            assert r["n_steps"] == 1
            assert round(r["sums"]["exec_sec"], 3) == 0.4
        # aggregate rolls the two runs up: median exec == 0.4 (NOT sum 0.8).
        agg = aggregate_trajectories(out)
        assert len(agg) == 1
        assert agg[0]["n_runs"] == 2
        assert agg[0]["all_runs"]["time_breakdown_sec"]["exec_sec"]["p50"] == 0.4  # median, not sum

    def test_create_kill_error_surfaced(self):
        # trajectory_create(success=False) carries error_type + error ([:120]);
        # the short error string is the failure signal a user sees for a failed
        # trajectory (per-step stderr is unavailable by design).
        events = [
            {
                "event": "trajectory_create",
                "sandbox_index": 0,
                "trajectory_id": "fail",
                "timestamp": 1.0,
                "create_sec": 0.3,
                "success": False,
                "error_type": "TimeoutError",
                "error": "create timed out after 30s",
            },
        ]
        out = trajectory_summaries(events)
        f = out[0]
        assert f["create_error_type"] == "TimeoutError"
        assert f["create_error"] == "create timed out after 30s"
        assert f["kill_error_type"] is None

    def test_failed_step_contributes_zero_to_sums_but_counts(self):
        # A failed slice contributes 0 to the sums (it did no work) but still
        # counts as an attempted step in n_steps -- avg_slice reflects per-attempt.
        events = [
            _step("t", step_index=0, exec_sec=0.4, resume_sec=0.1, pause_sec=0.2),
            _step("t", step_index=1, slice_failed=True, exit_code=1, exec_sec=0.0, resume_sec=0.0, pause_sec=0.0),
        ]
        out = trajectory_summaries(events)
        t = out[0]
        assert t["n_steps"] == 2
        assert t["n_failed"] == 1
        # only the success step contributed
        assert round(t["sums"]["exec_sec"], 3) == 0.4
        assert round(t["avg_slice"], 3) == round(0.7 / 2, 3)  # slice_total 0.7 over 2 attempts

    def test_empty_events_returns_empty_list(self):
        assert trajectory_summaries([]) == []

    def test_ignores_non_trajectory_events(self):
        # initial_pause / slot_acquire / slot_release / snapshot_size carry NO
        # trajectory_id -- they must not create phantom trajectories.
        events = [
            {
                "event": "initial_pause",
                "sandbox_index": 0,
                "pause_start": 1.0,
                "pause_end": 1.5,
                "initial_pause_sec": 0.5,
            },
            {"event": "slot_acquire", "sandbox_index": 0, "timestamp": 1.0, "lease_id": 1},
            {"event": "snapshot_size", "sandbox_index": 0, "pause_seq": 1, "logical_bytes": 100},
        ]
        assert trajectory_summaries(events) == []


class TestAggregateTrajectories:
    def test_median_not_sum_across_runs(self):
        # 3 runs of the same tid with elapsed 1, 2, 3. Per-tid median elapsed
        # == 2.0 (NOT sum 6.0 -- summing across runs is the pool<N inflation bug).
        events = [
            _step("t", sandbox_index=sb, round_id=0, step_index=0, exec_sec=exec_sec, resume_sec=0.0, pause_sec=0.0)
            for sb, exec_sec in ((0, 1.0), (1, 2.0), (2, 3.0))
        ]
        agg = aggregate_trajectories(trajectory_summaries(events))
        assert len(agg) == 1
        a = agg[0]
        assert a["n_runs"] == 3
        assert a["n_success"] == 3
        assert a["elapsed_sec"] == 2.0  # success median == median of [1,2,3]
        assert a["all_runs"]["elapsed_sec"]["p50"] == 2.0
        assert a["all_runs"]["time_breakdown_sec"]["exec_sec"]["p50"] == 2.0  # median, not sum 6

    def test_successful_runs_excludes_failed_runs(self):
        # 4 short failed runs (elapsed 1) + 2 long successful runs (50, 60).
        # all_runs median is dragged to 1 by the failures; the headline
        # (success-only median = 60) is NOT -- exactly the user's concern.
        events = [
            *(
                _step(
                    "t",
                    sandbox_index=sb,
                    round_id=0,
                    step_index=0,
                    exec_sec=1.0,
                    resume_sec=0.0,
                    pause_sec=0.0,
                    slice_failed=True,
                    exit_code=1,
                )
                for sb in range(4)
            ),
            _step("t", sandbox_index=4, round_id=0, step_index=0, exec_sec=50.0, resume_sec=0.0, pause_sec=0.0),
            _step("t", sandbox_index=5, round_id=0, step_index=0, exec_sec=60.0, resume_sec=0.0, pause_sec=0.0),
        ]
        a = aggregate_trajectories(trajectory_summaries(events))[0]
        assert a["n_runs"] == 6
        assert a["n_success"] == 2
        assert a["n_failed"] == 4
        assert a["all_failed"] is False
        assert a["all_runs"]["elapsed_sec"]["p50"] == 1.0  # dragged by failures
        assert a["successful_runs"]["elapsed_sec"]["p50"] == 60.0
        assert a["successful_runs"]["elapsed_sec"]["n"] == 2
        assert a["elapsed_sec"] == 60.0  # headline = success median, not all_runs

    def test_all_failed_falls_back_to_all_runs_median(self):
        # Every run failed -> successful_runs empty, headline = all_runs median,
        # all_failed=True.
        events = [
            _step(
                "t",
                sandbox_index=0,
                round_id=0,
                step_index=0,
                exec_sec=2.0,
                resume_sec=0.0,
                pause_sec=0.0,
                slice_failed=True,
                exit_code=1,
            ),
            _step(
                "t",
                sandbox_index=1,
                round_id=0,
                step_index=0,
                exec_sec=4.0,
                resume_sec=0.0,
                pause_sec=0.0,
                slice_failed=True,
                exit_code=1,
            ),
            _step(
                "t",
                sandbox_index=2,
                round_id=0,
                step_index=0,
                exec_sec=6.0,
                resume_sec=0.0,
                pause_sec=0.0,
                slice_failed=True,
                exit_code=1,
            ),
        ]
        a = aggregate_trajectories(trajectory_summaries(events))[0]
        assert a["n_runs"] == 3
        assert a["n_success"] == 0
        assert a["all_failed"] is True
        assert a["successful_runs"] == {}  # empty set
        assert a["elapsed_sec"] == 4.0  # fallback to all_runs median of [2,4,6]
        assert a["all_runs"]["elapsed_sec"]["p50"] == 4.0

    def test_single_run_aggregate_equals_run(self):
        # pool>=N (1 run per tid): median of 1 == the run's value; the two sets
        # coincide; behaviour unchanged in magnitude vs the old per-tid sums.
        events = [_step("t", sandbox_index=0, round_id=0, step_index=0, exec_sec=5.0, resume_sec=0.5, pause_sec=0.5)]
        per_run = trajectory_summaries(events)
        a = aggregate_trajectories(per_run)[0]
        assert a["n_runs"] == 1
        assert a["n_success"] == 1
        assert a["all_failed"] is False
        assert a["elapsed_sec"] == per_run[0]["elapsed_sec"]
        assert a["all_runs"]["elapsed_sec"]["p50"] == per_run[0]["elapsed_sec"]
        assert a["successful_runs"]["elapsed_sec"]["p50"] == per_run[0]["elapsed_sec"]
