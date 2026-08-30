"""Host-agnostic metrics model + the kernel's working sandbox state.

``bench_core`` measures three orthogonal workflows -- browser, coding, document --
over a fleet of sandboxes. The metric types here are workflow-specific but
host-agnostic: they track task counters, latencies, and step-level timing with
no reference to e2b or docker. :class:`BenchSandbox` carries them alongside the
lifecycle state inherited from :class:`env_provider.SandboxInstance`.

The coding/document *payload* data (replacement pairs, verify-script templates,
language profiles) is also host-agnostic benchmark content -- it lives in
:mod:`bench_core.coding_payload`. Only the metric *machinery* lives here.
"""
from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass, field, fields
from typing import Any

from env_provider import SandboxInstance, SandboxStatus

# Step order constants for workflow dispatch.
BROWSER_STEP_ORDER = ["open_tab", "page_load", "snapshot", "click", "screenshot"]
# Real AI coding agent workflow (verified against captured openclaw trajectories
# on vuejs/core and gohugoio/hugo): locate file (find), inspect it (read), apply a
# real edit, verify the edit by writing an ad-hoc test file and running it (verify),
# then produce the verification artifact (git diff). `git checkout --` reset runs as
# setup inside the `find` step, not a separate step.
CODING_STEP_ORDER = ["find", "read", "edit", "verify", "diff"]
DOCUMENT_XLSX_STEP_ORDER = [
    "XLSX-P01-inspect_prepare",
    "XLSX-P02-build",
    "XLSX-P03-process_publish",
    "XLSX-P04-verify_deliver",
]
DOCUMENT_PDF_STEP_ORDER = [
    "PDF-P01-inspect_prepare",
    "PDF-P02-build",
    "PDF-P03-process_publish",
    "PDF-P04-verify_deliver",
]
# Trajectory replay action-type taxonomy (drives per-bucket metrics). Replay
# has no fixed pipeline (unlike coding's find->read->edit->verify->diff); the
# "step" axis is the recorded action's type, bucketed by classify_action.
REPLAY_STEP_ORDER = ["shell", "str_replace_editor", "bash", "other"]


def get_step_order(workflow_type: str, document_case_kind: str = "xlsx") -> list[str]:
    """Return the ordered step names for a workflow (+ document case kind)."""
    if workflow_type == "coding":
        return CODING_STEP_ORDER
    if workflow_type == "document":
        if document_case_kind == "pdf":
            return DOCUMENT_PDF_STEP_ORDER
        if document_case_kind == "xlsx":
            return DOCUMENT_XLSX_STEP_ORDER
        raise ValueError("document case_kind must be 'pdf' or 'xlsx'")
    if workflow_type == "browser":
        return BROWSER_STEP_ORDER
    if workflow_type == "replay":
        return REPLAY_STEP_ORDER
    raise ValueError(f"Unsupported workflow_type: {workflow_type}")


class TaskMetricsBase:
    """Base class for workflow task metrics (thread-safe for concurrent access).

    Provides the shared metrics tracking pattern: total/success/failed/timeout
    counters, latency collection, step-level timing, and percentile accessors.
    Subclasses override ``step_order`` and may extend :meth:`add` with
    workflow-specific parameters (e.g. verify_success for coding).

    Extending for a new workflow type::

        class DatabaseMetrics(TaskMetricsBase):
            step_order = DATABASE_STEP_ORDER
    """

    # Override in subclass to define workflow-specific step names.
    step_order: list[str] = []

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_tasks: int = 0
        self._success_count: int = 0
        self._failed_count: int = 0
        self._timeout_count: int = 0
        self._latencies: list[float] = []
        self._last_error: str = ""
        self._step_times: dict[str, list[float]] = {}

    def add(
        self,
        latency: float,
        success: bool,
        timeout: bool = False,
        step_times: dict[str, float] | None = None,
    ) -> None:
        """Add a task result (thread-safe).

        Args:
            latency: Total latency for the task (seconds).
            success: Whether the task succeeded.
            timeout: Whether the task timed out.
            step_times: Optional dict of step name -> latency in seconds.
        """
        with self._lock:
            self._total_tasks += 1
            if timeout:
                self._timeout_count += 1
                self._failed_count += 1
            elif success:
                self._success_count += 1
                self._latencies.append(latency)
            else:
                self._failed_count += 1

            if step_times:
                for step_name, step_latency in step_times.items():
                    self._step_times.setdefault(step_name, []).append(step_latency)

    @property
    def total_tasks(self) -> int:
        with self._lock:
            return self._total_tasks

    @property
    def success_count(self) -> int:
        with self._lock:
            return self._success_count

    @property
    def failed_count(self) -> int:
        with self._lock:
            return self._failed_count

    @property
    def timeout_count(self) -> int:
        with self._lock:
            return self._timeout_count

    @property
    def latencies(self) -> list[float]:
        # Return a copy so callers cannot mutate under the lock.
        with self._lock:
            return list(self._latencies)

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @last_error.setter
    def last_error(self, value: str) -> None:
        with self._lock:
            self._last_error = value

    @property
    def avg_latency(self) -> float:
        """Average latency (seconds)."""
        with self._lock:
            return statistics.mean(self._latencies) if self._latencies else 0.0

    @property
    def p99_latency(self) -> float:
        """P99 latency (seconds)."""
        with self._lock:
            if not self._latencies:
                return 0.0
            sorted_lat = sorted(self._latencies)
            if len(sorted_lat) >= 100:
                return sorted_lat[int(len(sorted_lat) * 0.99)]
            return sorted_lat[-1]

    def get_step_stats(self) -> dict[str, dict[str, float]]:
        """Get statistics for each step (avg, p99, count)."""
        with self._lock:
            result: dict[str, dict[str, float]] = {}
            for step_name, times in self._step_times.items():
                if not times:
                    continue
                sorted_times = sorted(times)
                avg = statistics.mean(times)
                p99 = sorted_times[-1] if len(sorted_times) < 100 else sorted_times[int(len(sorted_times) * 0.99)]
                result[step_name] = {"avg": avg, "p99": p99, "count": len(times)}
            return result

    def get_step_times_copy(self) -> dict[str, list[float]]:
        """Get a thread-safe copy of all step times (for tail-latency analysis)."""
        with self._lock:
            return {step_name: list(times) for step_name, times in self._step_times.items()}

    def get_latencies_since(self, start_count: int) -> list[float]:
        """Get latencies added after a certain count (for round delta calculation)."""
        with self._lock:
            if start_count >= len(self._latencies):
                return []
            return list(self._latencies[start_count:])


class BrowserMetrics(TaskMetricsBase):
    """Browser task metrics. Step order: open_tab, page_load, snapshot, click, screenshot."""

    step_order = BROWSER_STEP_ORDER


class CodingMetrics(TaskMetricsBase):
    """Coding task metrics; extends the base with verify-success tracking.

    Step order: find, read, edit, verify, diff. The ``verify`` step mirrors the
    real agent trace: write an ad-hoc test file to /tmp + run it (npx tsx for ts,
    go run for go). ``verify_success`` tracks whether that step passed; it is kept
    separate from the base success counter so a compile-only pass is never read as
    an assertion pass.
    """

    step_order = CODING_STEP_ORDER

    def __init__(self) -> None:
        super().__init__()
        self._verify_success_count: int = 0  # pairs with a real-assertion verify_script that passed
        self._compile_only_count: int = 0  # compile+ran without an assertion (no per-pair script)

    def add(
        self,
        latency: float,
        success: bool,
        timeout: bool = False,
        step_times: dict[str, float] | None = None,
        verify_success: bool = False,
        compile_only: bool = False,
    ) -> None:
        """Add a coding task result (thread-safe).

        Extends the base counters with verify-success tracking. The base counters
        are inlined here (not via super().add()) so both run under a single Lock
        acquisition -- threading.Lock is non-reentrant, so calling super().add()
        while already holding self._lock would deadlock.

        ``verify_success`` and ``compile_only`` are mutually exclusive; reported
        separately so a compile-only pass is never mistaken for an assertion pass.
        """
        with self._lock:
            self._total_tasks += 1
            if timeout:
                self._timeout_count += 1
                self._failed_count += 1
            elif success:
                self._success_count += 1
                self._latencies.append(latency)
            else:
                self._failed_count += 1

            if verify_success:
                self._verify_success_count += 1
            if compile_only:
                self._compile_only_count += 1

            if step_times:
                for step_name, step_latency in step_times.items():
                    self._step_times.setdefault(step_name, []).append(step_latency)

    @property
    def verify_success_count(self) -> int:
        with self._lock:
            return self._verify_success_count

    @property
    def compile_only_count(self) -> int:
        with self._lock:
            return self._compile_only_count


class DocumentMetrics(TaskMetricsBase):
    """PDF/XLSX task metrics; phase IDs are recorded dynamically as step times."""

    step_order = DOCUMENT_XLSX_STEP_ORDER


class ReplayMetrics(TaskMetricsBase):
    """Trajectory replay metrics; the "step" axis is the action_type bucket.

    Unlike coding's fixed find->read->edit->verify->diff pipeline, a replay
    step is one recorded action, so step-level timing is bucketed by
    ``action_type`` (shell / str_replace_editor / bash / ...). Extends the base
    with action-type latency buckets, delay-fidelity tracking, and trajectory
    completion counting.

    Success semantics: a step is successful iff ``exit_code == 0`` and not
    timed out. ``trajectory_complete`` is True only when the runner executed
    every step of a trajectory through to its end (a ``stop_on_error`` abort
    mid-trajectory does NOT count as complete).
    """

    step_order = REPLAY_STEP_ORDER

    def __init__(self) -> None:
        super().__init__()
        self._action_type_latencies: dict[str, list[float]] = {}
        self._delay_requested: float = 0.0
        self._delay_actual: float = 0.0
        self._trajectory_completions: int = 0
        self.initial_pause_sec: float = (
            0.0  # one-time snapshot-creation pause (single writer in _init_lifecycle; no lock)
        )
        self._resume_secs: list[float] = []
        self._pause_secs: list[float] = []
        self._slice_total_secs: list[float] = []
        self._resume_api_secs: list[float] = []
        self._resume_ready_wait_secs: list[float] = []
        self._slot_contention_wait_secs: list[float] = []
        self._pause_api_secs: list[float] = []
        self._resume_queue_wait_secs: list[float] = []
        self._running_slot_held_secs: list[float] = []
        self._interaction_total_secs: list[float] = []
        self._create_secs: list[float] = []  # trajectory mode only; empty otherwise
        self._kill_secs: list[float] = []  # trajectory mode only; empty otherwise

        # Phase 2/3: retry-event accumulators. ``record_retry_event`` is called
        # by the runner each time it emits a retry_* event to the series, so the
        # report reads aggregated retry impact from ReplayMetrics (its
        # consistent data source), NOT by re-reading the series JSONL. The
        # per-slice retry count is a SEPARATE concern: the runner appends it at
        # slice-end via ``append_retries_per_slice`` (not through
        # ``record_retry_event``) so it never double-counts the counter.
        self._retry_queued_count: int = 0
        self._retry_queued_count_by_op: dict[str, int] = {}
        self._time_lost_to_retry_sec: float = 0.0
        self._retries_per_slice: list[int] = []

    def add(
        self,
        latency: float,
        success: bool,
        timeout: bool = False,
        step_times: dict[str, float] | None = None,
        *,
        action_type: str = "shell",
        requested_delay: float = 0.0,
        actual_delay: float = 0.0,
        trajectory_complete: bool = False,
        resume_sec: float = 0.0,
        pause_sec: float = 0.0,
        slice_total_sec: float = 0.0,
        resume_api_sec: float = 0.0,
        resume_ready_wait_sec: float = 0.0,
        slot_contention_wait_sec: float = 0.0,
        pause_api_sec: float = 0.0,
        resume_queue_wait_sec: float = 0.0,
        running_slot_held_sec: float = 0.0,
        interaction_total_sec: float = 0.0,
        create_sec: float = 0.0,
        kill_sec: float = 0.0,
    ) -> None:
        """Add a replay step result (thread-safe).

        Base counters are inlined here (not via super().add()) so all updates
        run under one Lock acquisition (threading.Lock is non-reentrant).
        """
        with self._lock:
            self._total_tasks += 1
            if timeout:
                self._timeout_count += 1
                self._failed_count += 1
            elif success:
                self._success_count += 1
                self._latencies.append(latency)
                self._action_type_latencies.setdefault(action_type, []).append(latency)
            else:
                self._failed_count += 1
                self._action_type_latencies.setdefault(action_type, []).append(latency)

            self._delay_requested += requested_delay
            self._delay_actual += actual_delay
            if trajectory_complete:
                self._trajectory_completions += 1

            if step_times:
                for step_name, step_latency in step_times.items():
                    self._step_times.setdefault(step_name, []).append(step_latency)

            # Lifecycle duration lists (P2.5): aligned, failure-free for
            # percentile math. slice_total_sec == 0 means the runner
            # synthesized a zero-placeholder StepResult on an exception path
            # (resume/exec/pause threw) -- not a measurement, so excluded
            # from all lists to keep them length-aligned and avoid
            # divide-by-zero in overhead math. P2.6 adds four segment lists
            # (resume_api, resume_ready_wait, slot_contention_wait, pause_api)
            # that must stay aligned with the original three. P2.6 Task 4 adds
            # resume_queue_wait_secs (QPS limiter queue wait on resume) as the
            # eighth list. L7 adds running_slot_held_secs, interaction_total_secs,
            # create_secs, kill_secs as lists 9-12; all twelve append atomically.
            if slice_total_sec > 0.0:
                self._resume_secs.append(resume_sec)
                self._pause_secs.append(pause_sec)
                self._slice_total_secs.append(slice_total_sec)
                self._resume_api_secs.append(resume_api_sec)
                self._resume_ready_wait_secs.append(resume_ready_wait_sec)
                self._slot_contention_wait_secs.append(slot_contention_wait_sec)
                self._pause_api_secs.append(pause_api_sec)
                self._resume_queue_wait_secs.append(resume_queue_wait_sec)
                self._running_slot_held_secs.append(running_slot_held_sec)
                self._interaction_total_secs.append(interaction_total_sec)
                self._create_secs.append(create_sec)
                self._kill_secs.append(kill_sec)

    def record_retry_event(self, event_type: str, *, operation: str, time_lost_sec: float = 0.0) -> None:
        """Record a retry_* event for the report's retry-impact block.

        Thread-safe. Advances the retry counters only for ``retry_queued``
        events (recovered/exhausted are series-only; their counts derive from
        queued). Does NOT touch ``retries_per_slice`` -- the per-slice count
        is appended separately at slice-end via :meth:`append_retries_per_slice`
        so the counters never double-count.
        """
        with self._lock:
            if event_type == "retry_queued":
                self._retry_queued_count += 1
                self._retry_queued_count_by_op[operation] = self._retry_queued_count_by_op.get(operation, 0) + 1
                self._time_lost_to_retry_sec += time_lost_sec

    def append_retries_per_slice(self, count: int) -> None:
        """Append one slice's final retry count for percentile math.

        Called by the runner at slice-end with the slice's retry_queued count
        (a delta of :attr:`retry_queued_count` captured across the slice). Only
        successful slices reach this call; synthesized-failure slices (where the
        runner raises before slice-end) are excluded by the ``slice_total_sec >
        0.0`` gate in :meth:`add`, so this list stays length-aligned with the
        duration lists -- both exclude failure slices.
        """
        with self._lock:
            self._retries_per_slice.append(count)

    @property
    def action_type_latencies(self) -> dict[str, list[float]]:
        """Per-action-type latency lists (copy under lock)."""
        with self._lock:
            return {k: list(v) for k, v in self._action_type_latencies.items()}

    @property
    def resume_secs(self) -> list[float]:
        """Per-step resume durations, copy under lock (failure-free)."""
        with self._lock:
            return list(self._resume_secs)

    @property
    def pause_secs(self) -> list[float]:
        """Per-step pause durations, copy under lock (failure-free)."""
        with self._lock:
            return list(self._pause_secs)

    @property
    def slice_total_secs(self) -> list[float]:
        """Per-step slice totals, copy under lock (failure-free)."""
        with self._lock:
            return list(self._slice_total_secs)

    @property
    def resume_api_secs(self) -> list[float]:
        """Per-step provider.resume() wall times, copy under lock (P2.6)."""
        with self._lock:
            return list(self._resume_api_secs)

    @property
    def resume_ready_wait_secs(self) -> list[float]:
        """Per-step post-resume ready-probe waits, copy under lock (P2.6)."""
        with self._lock:
            return list(self._resume_ready_wait_secs)

    @property
    def slot_contention_wait_secs(self) -> list[float]:
        """Per-step RunningSlotScheduler.acquire() queue waits, copy under lock (P2.6).

        Phase 1 (G2): this is the total scheduler-imposed wait = natural_delay_sec
        + capacity_wait_sec (the ready_at pre-delay + the FIFO capacity wait).
        """
        with self._lock:
            return list(self._slot_contention_wait_secs)

    @property
    def pause_api_secs(self) -> list[float]:
        """Per-step provider.pause() wall times, copy under lock (P2.6)."""
        with self._lock:
            return list(self._pause_api_secs)

    @property
    def resume_queue_wait_secs(self) -> list[float]:
        """Per-step QPS limiter queue waits on resume, copy under lock (P2.6)."""
        with self._lock:
            return list(self._resume_queue_wait_secs)

    @property
    def running_slot_held_secs(self) -> list[float]:
        """Per-step slot-hold durations, copy under lock (L7)."""
        with self._lock:
            return list(self._running_slot_held_secs)

    @property
    def interaction_total_secs(self) -> list[float]:
        """Per-step full interaction budgets, copy under lock (L7)."""
        with self._lock:
            return list(self._interaction_total_secs)

    @property
    def create_secs(self) -> list[float]:
        """Per-trajectory create durations (trajectory mode), copy under lock (L7)."""
        with self._lock:
            return list(self._create_secs)

    @property
    def kill_secs(self) -> list[float]:
        """Per-trajectory kill durations (trajectory mode), copy under lock (L7)."""
        with self._lock:
            return list(self._kill_secs)

    @property
    def retry_queued_count(self) -> int:
        """Total retry_queued events recorded (Phase 2/3)."""
        with self._lock:
            return self._retry_queued_count

    @property
    def retry_queued_count_by_op(self) -> dict[str, int]:
        """retry_queued count per operation (resume/pause), copy under lock (Phase 2/3)."""
        with self._lock:
            return dict(self._retry_queued_count_by_op)

    @property
    def time_lost_to_retry_sec(self) -> float:
        """Sum of time_lost_sec across all retry_queued events (Phase 2/3)."""
        with self._lock:
            return self._time_lost_to_retry_sec

    @property
    def retries_per_slice(self) -> list[int]:
        """Per-slice retry_queued counts, copy under lock (Phase 2/3)."""
        with self._lock:
            return list(self._retries_per_slice)

    @property
    def delay_fidelity(self) -> float:
        """sum(actual_delay) / sum(requested_delay); 0.0 when no delay requested."""
        with self._lock:
            if self._delay_requested <= 0.0:
                return 0.0
            return self._delay_actual / self._delay_requested

    @property
    def trajectory_completions(self) -> int:
        with self._lock:
            return self._trajectory_completions

    def _mark_completion(self) -> None:
        """Increment the completion counter (called by the runner when a
        trajectory ran all its steps). Thread-safe."""
        with self._lock:
            self._trajectory_completions += 1


@dataclass
class BenchSandbox(SandboxInstance):
    """The kernel's working per-sandbox state.

    Extends the lean :class:`SandboxInstance` (lifecycle + creation timing) with
    the workflow task metrics, round-robin tab state, and runtime counters the
    stats collector and task runners read. Providers construct this for the
    kernel and keep the SDK handle in their own ``{index: handle}`` table; the
    instance never carries a provider-specific handle.
    """

    workflow_type: str = "browser"

    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    coding_metrics: CodingMetrics = field(default_factory=CodingMetrics)
    document_metrics: DocumentMetrics = field(default_factory=DocumentMetrics)
    replay_metrics: ReplayMetrics = field(default_factory=ReplayMetrics)

    stopped_by_cleanup: bool = False  # cleanly stopped by normal benchmark cleanup
    consecutive_failures: int = 0  # consecutive task failures (used to flag a sandbox bad)
    last_task_time: float = 0.0  # wall-clock of the last completed task
    lifecycle_paused: bool = False  # one-time initial pause done (P2 lifecycle); persists across rounds
    tab_ids: list[str] = field(default_factory=list)  # active tab IDs for browser round-robin

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def task_metrics(self) -> TaskMetricsBase:
        """Polymorphic metrics access -- the metrics object for the active workflow.

        Lets the stats collector and runners use one path without
        ``if workflow_type == ...`` dispatch.
        """
        if self.workflow_type == "coding":
            return self.coding_metrics
        if self.workflow_type == "document":
            return self.document_metrics
        if self.workflow_type == "browser":
            return self.browser_metrics
        if self.workflow_type == "replay":
            return self.replay_metrics
        raise ValueError(f"Unsupported workflow_type: {self.workflow_type}")

    def update_last_task_time(self, timestamp: float) -> None:
        """Thread-safe update of ``last_task_time``."""
        with self._lock:
            self.last_task_time = timestamp

    def get_last_task_time(self) -> float:
        """Thread-safe read of ``last_task_time``."""
        with self._lock:
            return self.last_task_time

    @classmethod
    def from_instance(cls, inst: SandboxInstance, workflow_type: str) -> BenchSandbox:
        """Promote a provider's lean :class:`SandboxInstance` to a kernel state.

        Copies every lifecycle + creation-metrics field generically (so a new
        ``SandboxInstance`` field is picked up automatically) and attaches fresh
        workflow metrics. The provider keeps its SDK handle in its own
        ``{index: handle}`` table; the resulting ``BenchSandbox`` is handle-free
        and the provider can still look it up by ``index``/``id``.
        """
        kwargs = {f.name: getattr(inst, f.name) for f in fields(SandboxInstance)}
        return cls(**kwargs, workflow_type=workflow_type)


@dataclass
class Snapshot:
    """A point-in-time sample of the running benchmark, collected by StatsCollector."""

    timestamp: float  # snapshot wall-clock
    elapsed: float  # seconds since benchmark start
    total_sandboxes: int
    active_sandboxes: int
    offline_sandboxes: int
    creation_stats: dict[str, Any] = field(
        default_factory=dict
    )  # {"create": {...}, "ready_check": {...}, "total": {...}}
    # Browser task metrics
    browser_total: int = 0
    browser_success: int = 0
    browser_avg_latency: float = 0.0
    browser_p99_latency: float = 0.0
    # Coding task metrics (workflow_type="coding")
    coding_total: int = 0
    coding_success: int = 0
    coding_verify_success: int = 0  # real-assertion verify_script passed
    coding_compile_only: int = 0  # compile_only verify passed (no assertion)
    coding_avg_latency: float = 0.0
    coding_p99_latency: float = 0.0
    # Document task metrics (workflow_type="document")
    document_total: int = 0
    document_success: int = 0
    document_avg_latency: float = 0.0
    document_p99_latency: float = 0.0
    # Replay task metrics (workflow_type="replay")
    replay_total: int = 0
    replay_success: int = 0
    replay_avg_latency: float = 0.0
    replay_p99_latency: float = 0.0
    # Round comparison fields
    round_total: int = 0
    round_success: int = 0
