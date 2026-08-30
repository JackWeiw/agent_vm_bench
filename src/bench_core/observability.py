"""Phase 3: shared replay observability data model.

Pure data-prep -- no I/O, no openpyxl. Aggregates ReplayMetrics + admission
snapshot + wall-clock into the metrics both the text ReportFormatter and the
xlsx renderer consume. Built incrementally: throughput (here), retry-impact
(Task 9), trajectory summary (Task 10).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

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
        rc = getattr(self.config, "replay_running_concurrency", None)
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
