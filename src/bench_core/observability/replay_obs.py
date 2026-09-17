"""Phase 3: shared replay observability data model.

Pure data-prep -- no I/O, no openpyxl. Aggregates ReplayMetrics + admission
snapshot + wall-clock into the metrics both the text ReportFormatter and the
xlsx renderer consume. Built incrementally: throughput (here), retry-impact
(Task 9), trajectory summary (Task 10).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from bench_core.utils import calc_percentiles

if TYPE_CHECKING:
    from bench_core.config import KernelConfig
    from bench_core.schemas import BenchSandbox


class ReplayObservability:
    """Aggregated replay observability metrics (one per run)."""

    def __init__(
        self,
        config: KernelConfig,
        sandbox_states: dict[int, BenchSandbox],
        *,
        admission_snapshot: dict | None = None,
        wall_sec: float | None = None,
    ) -> None:
        self.config = config
        self.states = sandbox_states
        self.admission_snapshot = admission_snapshot
        # Fall back to config.test_duration when no explicit wall-clock given
        # (the e2e path passes the measured wall; unit tests may omit it).
        self.wall_sec = wall_sec if wall_sec is not None else float(getattr(config, "test_duration", 0) or 0.0)

    @property
    def _metrics_lists(self) -> list:
        return [s.replay_metrics for s in self.states.values() if s.replay_metrics is not None]

    @property
    def total_steps(self) -> int:
        return sum(m.total_tasks for m in self._metrics_lists)

    @property
    def _slot_held_sum(self) -> float:
        return sum(sum(m.running_slot_held_secs) for m in self._metrics_lists)

    @property
    def _exec_sum(self) -> float:
        # exec per slice ~= slice_total - resume - pause (the non-lifecycle portion).
        # The three property lists append together under the same slice_total>0 gate
        # in ReplayMetrics.add(), so they stay length-aligned (zip is safe).
        return sum(
            sum(s - r - p for s, r, p in zip(m.slice_total_secs, m.resume_secs, m.pause_secs))
            for m in self._metrics_lists
        )

    @property
    def concurrency(self) -> int:
        from bench_core.task_runner.replay import ReplayConfig

        rcfg = self.config.workflow_config
        rc = rcfg.replay_running_concurrency if isinstance(rcfg, ReplayConfig) else None
        return rc or self.config.total_count

    @property
    def steps_per_sec(self) -> float | None:
        if self.wall_sec <= 0:
            return None
        return self.total_steps / self.wall_sec

    @property
    def effective_parallelism(self) -> float | None:
        if self.wall_sec <= 0:
            return None
        return self._slot_held_sum / self.wall_sec

    @property
    def exec_wall_utilization(self) -> float | None:
        denom = self.wall_sec * self.concurrency
        if denom <= 0:
            return None
        return self._exec_sum / denom

    @property
    def overcommit_ratio(self) -> float:
        return self.config.total_count / self.concurrency if self.concurrency else 0.0

    @property
    def lifecycle_overhead(self) -> dict | None:
        """Lifecycle overhead ratio (resume + pause) / slice_total, as a model-owned
        semantic metric. Matches the txt report's "Overhead aggregate" /
        "Overhead per-sample" and run_summary's ``lifecycle_overhead.pct_of_slice_total``
        exactly: near-zero slices (``< MIN_SLICE_SEC``, synthesized zero-placeholders
        on exception paths) are excluded so a tiny slice cannot explode the per-sample
        ratio. Returns ``None`` when no qualifying slices (exec_only with no recorded
        steps, or a fully-failed run) so the Overview section is skipped. Per the
        obs_xlsx layering rule (semantic metrics in the model), the xlsx Overview reads
        this rather than recomputing inline; run_summary + the txt report still carry
        their own inline copies (a separate DRY cleanup).
        """
        from bench_core.observability.report_helpers import MIN_SLICE_SEC

        agg = [
            (r, p, s)
            for m in self._metrics_lists
            for r, p, s in zip(m.resume_secs, m.pause_secs, m.slice_total_secs)
            if s >= MIN_SLICE_SEC
        ]
        slice_sum = sum(s for _, _, s in agg)
        if slice_sum <= 0:
            return None
        resume_sum = sum(r for r, _, _ in agg)
        pause_sum = sum(p for _, p, _ in agg)
        per_sample = [(r + p) / s for r, p, s in agg]
        sp = calc_percentiles(per_sample)
        return {
            "aggregate_pct": (resume_sum + pause_sum) / slice_sum * 100,
            "mean_pct": sp["avg"] * 100,
            "p50_pct": sp["p50"] * 100,
            "p95_pct": sp["p95"] * 100,
            "n": len(agg),
        }

    # --- Phase 3.3: retry-impact (read from ReplayMetrics accumulators, not the series) ---

    @property
    def retry_count(self) -> int:
        return sum(m.retry_queued_count for m in self._metrics_lists)

    @property
    def retry_count_by_op(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self._metrics_lists:
            for op, n in m.retry_queued_count_by_op.items():
                out[op] = out.get(op, 0) + n
        return out

    @property
    def time_lost_to_retry_sec(self) -> float:
        return sum(m.time_lost_to_retry_sec for m in self._metrics_lists)

    @property
    def retries_per_slice_p95(self) -> float:
        vals = [v for m in self._metrics_lists for v in m.retries_per_slice]
        return calc_percentiles(vals)["p95"]

    # --- Phase 3.4: trajectory summary percentiles (trajectory mode) ---

    @property
    def create_sec_stats(self) -> dict[str, float]:
        vals = [v for m in self._metrics_lists for v in m.create_secs]
        return calc_percentiles(vals)

    @property
    def kill_sec_stats(self) -> dict[str, float]:
        vals = [v for m in self._metrics_lists for v in m.kill_secs]
        return calc_percentiles(vals)

    @property
    def slot_held_stats(self) -> dict[str, float]:
        vals = [v for m in self._metrics_lists for v in m.running_slot_held_secs]
        return calc_percentiles(vals)
