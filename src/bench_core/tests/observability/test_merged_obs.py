"""Fleet-wide obs workbook for a sharded trial: series -> model -> workbook.

The parity test is the load-bearing one: a sharded trial's accumulators die
with their kernel process, so the merged workbook is rebuilt from the series.
If that reconstruction ever diverges from what ``ReplayMetrics.add`` would have
accumulated live, a sharded trial's Overview silently reports different numbers
than the same fleet run single-process.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl", reason="openpyxl is a core dep")
from openpyxl import load_workbook  # noqa: E402

from bench_core.observability.lifecycle_reconstruct import replay_states_from_events  # noqa: E402
from bench_core.oversub_shards import render_merged_obs  # noqa: E402
from bench_core.schemas import ReplayMetrics  # noqa: E402

_DURATIONS = (
    "resume_sec",
    "exec_sec",
    "pause_sec",
    "slice_total_sec",
    "slot_contention_wait_sec",
    "natural_delay_sec",
    "capacity_wait_sec",
    "resume_rate_pacing_wait_sec",
    "resume_api_sec",
    "resume_ready_wait_sec",
    "resume_inflight_wait_sec",
    "pause_rate_pacing_wait_sec",
    "pause_api_sec",
    "pause_inflight_wait_sec",
    "running_slot_held_sec",
    "interaction_total_sec",
)


def _step_event(
    *,
    index: int = 0,
    exit_code: int = 0,
    timed_out: bool = False,
    action_type: str = "shell",
    retries_in_slice: int | None = 0,
    **extra,
) -> dict:
    """A series ``step`` record shaped like the runner's write (zeros + overrides)."""
    event = {
        "event": "step",
        "sandbox_index": index,
        "step_index": 0,
        "action_type": action_type,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "slice_failed": exit_code != 0,
        **{k: extra.pop(k, 0.0) for k in _DURATIONS},
        **extra,
    }
    if retries_in_slice is not None:
        event["retries_in_slice"] = retries_in_slice
    return event


def _retry_event(operation: str, *, rate: float, inflight: float, api: float, index: int = 0) -> dict:
    return {
        "event": "retry_queued",
        "sandbox_index": index,
        "operation": operation,
        "accumulated_rate_pacing_wait_sec": rate,
        "accumulated_inflight_wait_sec": inflight,
        "accumulated_api_sec": api,
    }


def _native(steps: list[dict], retries: list[tuple[str, float]] = ()) -> ReplayMetrics:
    """The same inputs through the kernel's live accumulator (the oracle)."""
    m = ReplayMetrics()
    for e in steps:
        timed_out = bool(e["timed_out"])
        m.add(
            latency=e["exec_sec"],
            success=e["exit_code"] == 0 and not timed_out,
            timeout=timed_out,
            step_times={e["action_type"]: e["exec_sec"]},
            action_type=e["action_type"],
            **{k: e[k] for k in _DURATIONS if k != "exec_sec"},  # exec_sec arrives as `latency`
        )
        if "retries_in_slice" in e:
            m.append_retries_per_slice(e["retries_in_slice"])
    for operation, time_lost_sec in retries:
        m.record_retry_event("retry_queued", operation=operation, time_lost_sec=time_lost_sec)
    return m


def _assert_parity(rebuilt: ReplayMetrics, native: ReplayMetrics) -> None:
    assert rebuilt.total_tasks == native.total_tasks
    assert rebuilt.success_count == native.success_count
    assert rebuilt.failed_count == native.failed_count
    assert rebuilt.timeout_count == native.timeout_count
    assert rebuilt.latencies == native.latencies
    assert rebuilt.action_type_latencies == native.action_type_latencies
    assert rebuilt.resume_secs == native.resume_secs
    assert rebuilt.pause_secs == native.pause_secs
    assert rebuilt.slice_total_secs == native.slice_total_secs
    assert rebuilt.running_slot_held_secs == native.running_slot_held_secs
    assert rebuilt.interaction_total_secs == native.interaction_total_secs
    assert rebuilt.natural_delay_secs == native.natural_delay_secs
    assert rebuilt.resume_api_secs == native.resume_api_secs
    assert rebuilt.pause_api_secs == native.pause_api_secs
    assert rebuilt.retry_queued_count == native.retry_queued_count
    assert rebuilt.retry_queued_count_by_op == native.retry_queued_count_by_op
    assert rebuilt.time_lost_to_retry_sec == pytest.approx(native.time_lost_to_retry_sec)
    assert rebuilt.retries_per_slice == native.retries_per_slice


_STEP_A = _step_event(index=0, exec_sec=0.5, resume_sec=0.1, pause_sec=0.2, slice_total_sec=0.8)
_STEP_B = _step_event(
    index=1,
    exec_sec=1.5,
    action_type="str_replace_editor",
    resume_sec=0.3,
    pause_sec=0.4,
    slice_total_sec=2.2,
    natural_delay_sec=1.0,
    running_slot_held_sec=2.2,
    interaction_total_sec=3.2,
    retries_in_slice=2,
)
_RETRY = _retry_event("resume", rate=0.05, inflight=0.02, api=0.7, index=1)
_STEP_C = _step_event(index=1, exit_code=124, timed_out=True, exec_sec=5.0, slice_total_sec=5.0)
_FAILED = _step_event(index=2, exit_code=1, retries_in_slice=None)  # runner exception path


def test_replay_states_match_live_accumulation() -> None:
    states = replay_states_from_events([_STEP_A, _STEP_B, _RETRY, _STEP_C, _FAILED])

    assert sorted(states) == [0, 1, 2]
    assert all(s.replay_metrics is not None for s in states.values())
    _assert_parity(states[0].replay_metrics, _native([_STEP_A]))
    _assert_parity(states[1].replay_metrics, _native([_STEP_B, _STEP_C], [("resume", 0.05 + 0.02 + 0.7)]))
    _assert_parity(states[2].replay_metrics, _native([_FAILED]))


def test_failed_slice_is_counted_but_not_timed() -> None:
    m = replay_states_from_events([_FAILED])[2].replay_metrics
    assert (m.total_tasks, m.failed_count, m.success_count) == (1, 1, 0)
    assert m.slice_total_secs == []  # slice_total 0 -> not a measurement
    assert m.latencies == []
    assert m.retries_per_slice == []  # the failure record carries no retry key


def _write_series(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def _trial_cfg() -> dict:
    """The trial's UNSHARDED raw config: k*N slots over N concurrency."""
    return {
        "workflow_type": "replay",
        "sandbox": {"total_count": 4},
        "test": {"duration": 30},
        "report": {"format": "both"},  # the kernel's own workbook gate
        "replay": {"mode": "exec_ephemeral", "running_concurrency": 2},
    }


def test_render_merged_obs_respects_the_txt_gate(tmp_path: Path) -> None:
    series = _write_series(tmp_path / "s.jsonl", [_STEP_A])
    cfg = _trial_cfg()
    cfg["report"] = {"format": "txt"}
    assert render_merged_obs(cfg, tmp_path, "p", wall_sec=1.0, series_path=series) is None
    assert not (tmp_path / "merged" / "p_obs.xlsx").exists()


def test_render_merged_obs_writes_one_workbook(tmp_path: Path) -> None:
    t = 1_700_000_000.0
    # Three slots as a 2-shard trial re-keyed them; timestamps drive the
    # series-backed sheets (Gantt / Concurrency states / Step detail).
    events = [
        _step_event(
            index=idx,
            exec_sec=0.5 + idx,
            resume_sec=0.01,
            pause_sec=0.02,
            slice_total_sec=0.6 + idx,
            resume_start=t,
            resume_end=t + 0.01,
            exec_start=t + 0.01,
            exec_end=t + 0.51 + idx,
            pause_start=t + 0.51 + idx,
            pause_end=t + 0.53 + idx,
        )
        for idx in range(3)
    ]
    series = _write_series(tmp_path / "merged" / "t_series.jsonl", events)

    out = render_merged_obs(_trial_cfg(), tmp_path, "t", wall_sec=10.0, series_path=series)

    assert out == tmp_path / "merged" / "t_obs.xlsx"
    wb = load_workbook(out)
    assert set(wb.sheetnames) >= {
        "Overview",
        "Per-step timings",
        "Lifecycle overhead",
        "Trajectory summary",
        "Step detail",
        "Concurrency states",
        "Gantt",
    }
    overview = {row[0].value: row[1].value for row in wb["Overview"].iter_rows() if len(row) > 1 and row[0].value}
    # Trial-level identity, not one shard's: k*N slots over N concurrency.
    assert overview["total_count"] == 4
    assert overview["running_concurrency"] == 2
    assert overview["total_steps"] == 3
    assert overview["wall_sec"] == 10.0


def test_render_merged_obs_never_fails_the_trial(tmp_path: Path, caplog) -> None:
    """A render error degrades to None + a warning -- a report must never fail a trial."""
    assert render_merged_obs(_trial_cfg(), tmp_path, "p", wall_sec=1.0, series_path=tmp_path) is None
    assert any("merged obs workbook skipped" in r.message for r in caplog.records)
