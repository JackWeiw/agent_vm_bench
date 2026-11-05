"""Offline obs.xlsx regeneration from a run's persisted series.

The workbook is normally rendered in-memory at run end (``ReplayObservability``
holds live ``sandbox_states``). This module rebuilds those metrics from the
persisted ``*_lifecycle_series.jsonl`` JSONL -- each step event already carries
every field ``ReplayMetrics.add()`` takes -- and re-renders the workbook with
the current renderer. So an old run (e.g. one whose Overview predates the
timed_out / non-timeout split) can produce a new obs.xlsx without re-running
the bench.

Usage:
    python -m bench_core.observability.regen_obs <run_dir> [--out path]

``<run_dir>`` must contain the run's ``*_lifecycle_series.jsonl`` and
``*_run_summary.json`` (the run key ``(trajectory_id, sandbox_index, round_id)``
/ create+kill+stress decomposition facts live in the summary). Output defaults
to ``<series-prefix>_obs.xlsx`` in the same directory (overwrite).

Untracked in the workbook: ``requested/actual delay`` sums and the
``trajectory_complete`` counter (fed to run_summary/txt, not rendered); the
host-sheet merge (a separate ``host_xlsx`` passed at render time).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench_core.config import KernelConfig
from bench_core.observability.obs_xlsx import XlsxReportRenderer
from bench_core.observability.replay_obs import ReplayObservability
from bench_core.schemas import BenchSandbox, ReplayMetrics, SandboxInstance
from bench_core.task_runner.replay_config import ReplayConfig

_STEP_KWARGS = (  # step-event key -> ReplayMetrics.add() kwarg
    "resume_sec pause_sec slice_total_sec resume_api_sec resume_ready_wait_sec "
    "slot_contention_wait_sec pause_api_sec resume_rate_pacing_wait_sec "
    "pause_rate_pacing_wait_sec running_slot_held_sec interaction_total_sec "
    "natural_delay_sec capacity_wait_sec resume_inflight_wait_sec pause_inflight_wait_sec"
).split()


def _metrics_from_events(events: list[dict]) -> dict[int, ReplayMetrics]:
    """Rebuild per-sandbox ReplayMetrics. Mirrors the runner's series write path
    (replay.py `_record_step`): success = exit_code==0 and not timed_out; a
    slice_failed step event was an exception-path record that never reached
    add(), so it is skipped here too."""
    metrics: dict[int, ReplayMetrics] = {}
    for ev in events:
        if ev.get("event") != "step" or ev.get("slice_failed"):
            continue
        idx = ev["sandbox_index"]
        m = metrics.setdefault(idx, ReplayMetrics())
        timed_out = bool(ev["timed_out"])
        exec_sec = float(ev.get("exec_sec", 0.0))
        m.add(
            latency=exec_sec,
            success=ev["exit_code"] == 0 and not timed_out,
            timeout=timed_out,
            action_type=ev.get("action_type", "shell"),
            **{k: float(ev.get(k, 0.0)) for k in _STEP_KWARGS},
        )
        if "retries_in_slice" in ev:  # older series carried the per-slice delta
            m.append_retries_per_slice(int(ev["retries_in_slice"]))
    for ev in events:
        if ev.get("event") != "retry_queued":
            continue
        m = metrics.setdefault(ev["sandbox_index"], ReplayMetrics())
        m.record_retry_event(
            "retry_queued",
            operation=ev.get("operation", ""),
            time_lost_sec=(
                float(ev.get("accumulated_rate_pacing_wait_sec", 0.0))
                + float(ev.get("accumulated_inflight_wait_sec", 0.0))
                + float(ev.get("accumulated_api_sec", 0.0))
            ),
        )
    return metrics


def _build_observability(run_dir: Path) -> tuple[ReplayObservability, Path]:
    series = next(run_dir.glob("*_lifecycle_series.jsonl"), None)
    summary = next(run_dir.glob("*_run_summary.json"), None)
    if series is None:
        raise SystemExit(f"no *_lifecycle_series.jsonl in {run_dir}")
    if summary is None:
        raise SystemExit(f"no *_run_summary.json in {run_dir} (needed for config/wall_sec facts)")

    facts = json.loads(summary.read_text(encoding="utf-8"))
    rcfg = ReplayConfig(
        replay_mode=facts.get("replay_mode"),
        replay_running_concurrency=facts.get("running_concurrency"),
    )
    cfg = KernelConfig(
        workflow_type=facts.get("workflow_type", "replay"),
        total_count=facts.get("total_count", 0),
        test_duration=facts.get("test_duration", 0),
        workflow_config=rcfg,
    )
    wall_sec = facts.get("wall_sec")

    events = [json.loads(l) for l in series.read_text(encoding="utf-8").splitlines() if l.strip()]
    state_metrics = _metrics_from_events(events)
    states = {}
    for idx, m in state_metrics.items():
        s = BenchSandbox.from_instance(SandboxInstance(id=str(idx), index=idx), "replay")
        s.replay_metrics = m
        states[idx] = s
    obs = ReplayObservability(cfg, states, admission_snapshot=facts.get("admission"), wall_sec=wall_sec)
    total_steps = sum(m.total_tasks for m in state_metrics.values())
    if facts.get("throughput", {}).get("total_steps") is not None and total_steps != facts["throughput"]["total_steps"]:
        print(
            f"WARNING: rebuilt {total_steps} steps != run_summary {facts['throughput']['total_steps']}; "
            f"metrics reconstruction may be incomplete (run_summary is authoritative for facts only)."
        )
    return obs, series


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "run_dir", type=Path, help="run directory holding *_lifecycle_series.jsonl + *_run_summary.json"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="output path (default: <run_dir>/<prefix>_obs.xlsx, overwrite)"
    )
    args = parser.parse_args()

    obs, series = _build_observability(args.run_dir.resolve())
    out = args.out or series.with_name(series.name.replace("_lifecycle_series.jsonl", "_obs.xlsx"))
    XlsxReportRenderer(obs, series_path=series).render(out)
    print(f"regenerated {out}")
    # Self-check the new Overview split: success + timed_out + failed(non-timeout) == total
    r = obs
    ok = [
        sum(s.replay_metrics.success_count for s in r.states.values()),
        sum(s.replay_metrics.timeout_count for s in r.states.values()),
        sum(s.replay_metrics.failed_count - s.replay_metrics.timeout_count for s in r.states.values()),
    ]
    print(f"Overview: total_steps={r.total_steps} success={ok[0]} timed_out={ok[1]} failed(non-timeout)={ok[2]}")


if __name__ == "__main__":
    main()
