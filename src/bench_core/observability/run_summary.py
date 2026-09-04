"""Machine-readable run summary for replay workflows (oversub driver contract).

A third rendering of the same data :mod:`bench_core.observability.replay_obs`
and the admission snapshot already hold -- it consumes them, never recomputes
an independent path. Written for replay workflows only (browser/coding/
document have different success semantics and no consumer yet).

Layering rule: RAW FACTS ONLY. The kernel does not know it is "ratio k of a
sweep," so it emits what happened; the oversub driver computes ``valid``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bench_core.observability.replay_obs import ReplayObservability

if TYPE_CHECKING:
    from bench_core.config import KernelConfig
    from bench_core.observability.stats_collector import StatsCollector

SCHEMA_VERSION = 1


def _iso(epoch: float) -> str:
    """ISO-8601 with local tz, millisecond precision (matches the repo's time.time() convention)."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


def write_run_summary(
    config: KernelConfig,
    stats_collector: StatsCollector,
    *,
    series_path: Path | None,
    obs_xlsx_path: Path | None,
    report_path: str | None,
    error: str | None = None,
) -> Path | None:
    """Write ``{output_dir}/{filename_prefix}_run_summary.json`` (replay only).

    Returns the path written, or ``None`` when skipped (non-replay workflow).
    Consumes ``ReplayObservability`` + ``stats_collector.admission_snapshot``
    + each sandbox's ``ReplayMetrics`` accumulators -- it never recomputes a
    metric the kernel already holds. See module docstring for the layering rule.
    """
    if config.workflow_type != "replay":
        return None

    admission_snapshot = stats_collector.admission_snapshot
    sandbox_states = stats_collector.sandbox_states
    wall_sec = stats_collector._resolved_wall_sec()

    obs = ReplayObservability(
        config,
        sandbox_states,
        admission_snapshot=admission_snapshot,
        wall_sec=wall_sec,
    )

    # Throughput from the same ReplayMetrics accumulators the txt report reads.
    total = sum(s.replay_metrics.total_tasks for s in sandbox_states.values())
    succeeded = sum(s.replay_metrics.success_count for s in sandbox_states.values())
    failed = sum(s.replay_metrics.failed_count for s in sandbox_states.values())
    tasks_per_sec = (succeeded / wall_sec) if wall_sec and wall_sec > 0 else None
    steps_per_sec = obs.steps_per_sec if (wall_sec and wall_sec > 0) else None

    # Lifecycle overhead (lifecycle/trajectory only; lists empty for exec_only).
    # Matches the txt report's "Overhead aggregate" exactly: slices below
    # MIN_SLICE_SEC (synthesized zero-placeholders on exception paths) are
    # excluded so a consumer cross-checking JSON vs txt sees identical numbers.
    from bench_core.observability.stats_collector import MIN_SLICE_SEC

    metrics = [s.replay_metrics for s in sandbox_states.values()]
    slice_triples = [(r, p, s) for m in metrics for r, p, s in zip(m.resume_secs, m.pause_secs, m.slice_total_secs)]
    agg = [(r, p, s) for r, p, s in slice_triples if s >= MIN_SLICE_SEC]
    resume_sum = sum(r for r, _, _ in agg)
    pause_sum = sum(p for _, p, _ in agg)
    slice_sum = sum(s for _, _, s in agg)
    lifecycle_overhead = None
    if config.replay_mode in ("lifecycle", "trajectory") and slice_sum > 0:
        lifecycle_overhead = {
            "pause_sec_sum": round(pause_sum, 6),
            "resume_sec_sum": round(resume_sum, 6),
            "pct_of_slice_total": round((resume_sum + pause_sum) / slice_sum * 100, 3),
        }

    # Admission block (present only when an admission controller was built).
    admission_block = None
    if admission_snapshot is not None:
        rs = admission_snapshot.get("running_slots") or {}
        ql = admission_snapshot.get("qps_limiter") or {}
        qd = admission_snapshot.get("qps_dispatched")
        admission_block = {
            "maximum": rs.get("maximum"),
            "peak_active": admission_snapshot.get("peak_active"),
            "granted": rs.get("granted"),
            "avg_queue_wait_sec": admission_snapshot.get("avg_queue_wait_sec"),
            "control_qps": admission_snapshot.get("qps"),
            "control_dispatched": qd if qd is not None else ql.get("dispatched"),
        }

    started_epoch = stats_collector.start_time if stats_collector.start_time else None
    completed_epoch = time.time()

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trajectory_index = output_dir / "trajectories" / "index.json"
    vm_monitor_dir = output_dir / "vm_monitor"

    summary = {
        "schema_version": SCHEMA_VERSION,
        "workflow_type": config.workflow_type,
        "replay_mode": config.replay_mode,
        "provider": stats_collector.provider_label or None,
        "started_at": _iso(started_epoch) if started_epoch else None,
        "completed_at": _iso(completed_epoch),
        "started_epoch": started_epoch,
        "completed_epoch": completed_epoch,
        "wall_sec": round(wall_sec, 3) if wall_sec is not None else None,
        "total_count": config.total_count,
        "running_concurrency": config.replay_running_concurrency,
        "overcommit_ratio": round(obs.overcommit_ratio, 3),
        "throughput": {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "total_steps": obs.total_steps,
            "steps_per_sec": round(steps_per_sec, 3) if steps_per_sec is not None else None,
            "tasks_per_sec": round(tasks_per_sec, 3) if tasks_per_sec is not None else None,
        },
        "admission": admission_block,
        "lifecycle_overhead": lifecycle_overhead,
        "paths": {
            "report": report_path,
            "obs_xlsx": str(obs_xlsx_path) if obs_xlsx_path else None,
            "lifecycle_series": str(series_path) if series_path else None,
            "trajectory_index": str(trajectory_index) if trajectory_index.exists() else None,
            "vm_monitor_dir": str(vm_monitor_dir) if vm_monitor_dir.is_dir() else None,
        },
        "error": error,
    }

    path = output_dir / f"{config.filename_prefix}_run_summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path
