"""Host-agnostic metrics model + the kernel's working sandbox state.

``bench_core`` measures three orthogonal workflows -- browser, coding, document --
over a fleet of sandboxes. The metric types here are workflow-specific but
host-agnostic: they track task counters, latencies, and step-level timing with
no reference to e2b or docker. :class:`BenchSandbox` carries them alongside the
lifecycle state inherited from :class:`bench_core.provider.SandboxInstance`.

The coding/document *payload* data (replacement pairs, verify-script templates,
language profiles) is also host-agnostic benchmark content -- it lives in
:mod:`bench_core.coding_payload`. Only the metric *machinery* lives here.
"""
from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass, field
from typing import Any

from bench_core.provider import SandboxInstance, SandboxStatus

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

    stopped_by_cleanup: bool = False  # cleanly stopped by normal benchmark cleanup
    consecutive_failures: int = 0  # consecutive task failures (used to flag a sandbox bad)
    last_task_time: float = 0.0  # wall-clock of the last completed task
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
        raise ValueError(f"Unsupported workflow_type: {self.workflow_type}")

    def update_last_task_time(self, timestamp: float) -> None:
        """Thread-safe update of ``last_task_time``."""
        with self._lock:
            self.last_task_time = timestamp

    def get_last_task_time(self) -> float:
        """Thread-safe read of ``last_task_time``."""
        with self._lock:
            return self.last_task_time


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
    # Round comparison fields
    round_total: int = 0
    round_success: int = 0
