"""
Data Structure Definitions Module

Defines SandboxStatus, CreationMetrics, TaskMetricsBase, BrowserMetrics,
CodingMetrics, SandboxState, TestSnapshot, BatchTask, TaskGroup.

Step order constants for workflow dispatch:
- BROWSER_STEP_ORDER: steps in browser round-robin mode
- CODING_STEP_ORDER: steps in coding round-robin mode

Default source files for Ant Design Pro coding benchmark (single definition,
referenced everywhere — config, runners, YAML templates).
"""

import statistics
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# Step order constants for workflow dispatch
BROWSER_STEP_ORDER = ["open_tab", "page_load", "snapshot", "click", "screenshot"]
CODING_STEP_ORDER = ["ensure_dev", "checkout", "edit", "build", "test", "memory"]

# Default source files for Ant Design Pro coding benchmark
# Single definition — referenced by Config dataclass default, _from_dict,
# from_args, YAML templates, and bench_helper.sh
DEFAULT_CODING_SOURCE_FILES = [
    "src/pages/dashboard/analysis/index.tsx",
    "src/pages/dashboard/workplace/index.tsx",
    "src/pages/dashboard/monitor/index.tsx",
    "src/pages/form/basic-form/index.tsx",
    "src/pages/form/step-form/index.tsx",
    "src/pages/form/advanced-form/index.tsx",
    "src/pages/list/basic-list/index.tsx",
    "src/pages/list/card-list/index.tsx",
    "src/pages/list/search/index.tsx",
    "src/pages/table-list/index.tsx",
    "src/pages/profile/basic/index.tsx",
    "src/pages/profile/advanced/index.tsx",
    "src/pages/result/success/index.tsx",
    "src/pages/result/fail/index.tsx",
    "src/pages/exception/403/index.tsx",
    "src/pages/exception/404/index.tsx",
    "src/pages/exception/500/index.tsx",
    "src/pages/user/login/index.tsx",
    "src/pages/user/register/index.tsx",
    "src/pages/account/settings/index.tsx",
    "src/pages/account/center/index.tsx",
    "src/pages/chatbot/index.tsx",
]


class SandboxStatus(Enum):
    """Sandbox status enumeration"""

    PENDING = "pending"  # Waiting for creation
    CREATING = "creating"  # Creating in progress
    CREATED = "created"  # sandbox.create succeeded, waiting for ports
    PORT_READY = "port_ready"  # Ports ready, can execute tasks
    ACTIVE = "active"  # Active, executing tasks
    FAILED = "failed"  # Creation failed
    PORT_FAILED = "port_failed"  # Port check failed
    OFFLINE = "offline"  # Runtime offline
    KILLED = "killed"  # Killed


@dataclass
class CreationMetrics:
    """Sandbox creation performance metrics"""

    submit_time: float = 0.0  # Creation submit time
    create_ready_time: float = 0.0  # sandbox.create success time (excluding port wait)
    port_ready_time: float = 0.0  # Ports ready time
    create_elapsed: float = 0.0  # sandbox.create elapsed time (seconds)
    port_wait_elapsed: float = 0.0  # Port wait elapsed time (seconds)
    total_elapsed: float = 0.0  # Total elapsed = create_elapsed + port_wait_elapsed
    status: SandboxStatus = SandboxStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""  # Port check error message


class TaskMetricsBase:
    """Base class for workflow task metrics (thread-safe for concurrent access).

    Provides the shared metrics tracking pattern: total/success/failed/timeout
    counters, latency collection, step-level timing, and percentile calculations.
    Subclasses override `step_order` and may extend `add()` with workflow-specific
    parameters (e.g., build_success for coding).

    Extending for a new workflow type:
        class DatabaseMetrics(TaskMetricsBase):
            step_order = DATABASE_STEP_ORDER
            # add build_success/test_success-like fields as needed
    """

    # Override in subclass to define workflow-specific step names
    step_order: List[str] = []

    def __init__(self):
        self._lock = threading.Lock()
        self._total_tasks: int = 0
        self._success_count: int = 0
        self._failed_count: int = 0
        self._timeout_count: int = 0
        self._latencies: List[float] = []
        self._last_error: str = ""
        self._step_times: Dict[str, List[float]] = {}

    def add(self, latency: float, success: bool, timeout: bool = False, step_times: Dict[str, float] = None) -> None:
        """Add a task result (thread-safe).

        Args:
            latency: Total latency for the task (seconds)
            success: Whether the task succeeded
            timeout: Whether the task timed out
            step_times: Optional dict of step name -> latency in seconds
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
                    if step_name not in self._step_times:
                        self._step_times[step_name] = []
                    self._step_times[step_name].append(step_latency)

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
    def latencies(self) -> List[float]:
        with self._lock:
            return list(self._latencies)  # Return a copy for thread safety

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
        """Average latency (seconds)"""
        with self._lock:
            return statistics.mean(self._latencies) if self._latencies else 0.0

    @property
    def p99_latency(self) -> float:
        """P99 latency (seconds)"""
        with self._lock:
            if not self._latencies:
                return 0.0
            sorted_lat = sorted(self._latencies)
            if len(sorted_lat) >= 100:
                return sorted_lat[int(len(sorted_lat) * 0.99)]
            return sorted_lat[-1]

    def get_step_stats(self) -> Dict[str, Dict[str, float]]:
        """Get statistics for each step (avg, p99, count).

        Returns:
            Dict of step_name -> {"avg": float, "p99": float, "count": int}
        """
        with self._lock:
            result = {}
            for step_name, times in self._step_times.items():
                if not times:
                    continue
                sorted_times = sorted(times)
                avg = statistics.mean(times)
                p99 = sorted_times[-1] if len(sorted_times) < 100 else sorted_times[int(len(sorted_times) * 0.99)]
                result[step_name] = {
                    "avg": avg,
                    "p99": p99,
                    "count": len(times),
                }
            return result

    def get_step_times_copy(self) -> Dict[str, List[float]]:
        """Get a thread-safe copy of all step times.

        Used for detailed tail latency analysis across all sandboxes.

        Returns:
            Dict of step_name -> list of latency values (copy)
        """
        with self._lock:
            return {step_name: list(times) for step_name, times in self._step_times.items()}

    def get_latencies_since(self, start_count: int) -> List[float]:
        """Get latencies added after a certain count.

        Args:
            start_count: The latency count at the start (e.g., from previous round)

        Returns:
            List of latencies added since start_count
        """
        with self._lock:
            if start_count >= len(self._latencies):
                return []
            return list(self._latencies[start_count:])


class BrowserMetrics(TaskMetricsBase):
    """Browser task metrics — inherits all shared logic from TaskMetricsBase.

    Step order: open_tab, page_load, snapshot, click, screenshot.
    No workflow-specific extensions; base add() signature is sufficient.
    """

    step_order = BROWSER_STEP_ORDER


class CodingMetrics(TaskMetricsBase):
    """Coding task metrics — extends TaskMetricsBase with build/test success tracking.

    Step order: ensure_dev, checkout, edit, build, test, memory.
    Adds build_success_count and test_success_count beyond the base counters.
    """

    step_order = CODING_STEP_ORDER

    def __init__(self):
        super().__init__()
        # Coding-specific fields
        self._build_success_count: int = 0
        self._test_success_count: int = 0

    def add(
        self,
        latency: float,
        success: bool,
        timeout: bool = False,
        step_times: Dict[str, float] = None,
        build_success: bool = False,
        test_success: bool = False,
    ) -> None:
        """Add a coding task result (thread-safe).

        Extends base add() with build/test success tracking.

        Args:
            latency: Total latency for the task cycle (seconds)
            success: Whether the overall task succeeded
            timeout: Whether the task timed out
            step_times: Optional dict of step name -> latency in seconds
            build_success: Whether the build step succeeded
            test_success: Whether the test step succeeded
        """
        # Call base add() for the standard counters
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

            if build_success:
                self._build_success_count += 1
            if test_success:
                self._test_success_count += 1

            if step_times:
                for step_name, step_latency in step_times.items():
                    if step_name not in self._step_times:
                        self._step_times[step_name] = []
                    self._step_times[step_name].append(step_latency)

    @property
    def build_success_count(self) -> int:
        with self._lock:
            return self._build_success_count

    @property
    def test_success_count(self) -> int:
        with self._lock:
            return self._test_success_count


@dataclass
class SandboxState:
    """Sandbox complete state"""

    sandbox_id: int  # Sequence number (1, 2, 3...)
    sandbox_obj: Optional[object] = None  # E2B Sandbox object reference (handle)
    batch_id: int = -1  # Batch ID

    workflow_type: str = "browser"  # Determines which metrics are primary

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    coding_metrics: CodingMetrics = field(default_factory=CodingMetrics)

    is_alive: bool = True  # Sandbox alive status
    last_task_time: float = 0.0  # Last task execution time (thread-safe via update_last_task_time)
    consecutive_failures: int = 0  # Consecutive failure count
    warmup_done: bool = False  # Warmup phase completed flag

    # Tab state (for round_robin tab-switch mode)
    tab_ids: List[str] = field(default_factory=list)  # Active tab IDs [t1, t2, ...]

    # Thread lock for last_task_time (not serialized)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self):
        """Initialize lock after dataclass creation."""
        if not hasattr(self, "_lock") or not hasattr(self._lock, "acquire"):
            object.__setattr__(self, "_lock", threading.Lock())

    @property
    def task_metrics(self) -> TaskMetricsBase:
        """Polymorphic metrics access — returns the metrics object for the active workflow.

        For browser workflow, returns browser_metrics.
        For coding workflow, returns coding_metrics.
        Enables unified code paths that don't need `if workflow_type == "coding"` dispatch.
        """
        if self.workflow_type == "coding":
            return self.coding_metrics
        return self.browser_metrics

    def update_last_task_time(self, timestamp: float) -> None:
        """Thread-safe update of last_task_time.

        Args:
            timestamp: Wall-clock timestamp (from time.time())
        """
        with self._lock:
            self.last_task_time = timestamp

    def get_last_task_time(self) -> float:
        """Thread-safe read of last_task_time.

        Returns:
            Last task execution timestamp
        """
        with self._lock:
            return self.last_task_time


@dataclass
class TestSnapshot:
    """Test snapshot"""

    timestamp: float  # Snapshot timestamp
    elapsed: float  # Time elapsed since test start (seconds)
    total_sandboxes: int  # Total sandbox count
    active_sandboxes: int  # Active sandbox count
    offline_sandboxes: int  # Offline sandbox count
    creation_stats: Dict[str, any] = field(
        default_factory=dict
    )  # {"create": {...}, "port_wait": {...}, "total": {...}}
    # Browser task metrics
    browser_total: int = 0
    browser_success: int = 0
    browser_avg_latency: float = 0.0
    browser_p99_latency: float = 0.0
    # Coding task metrics (populated when workflow_type="coding")
    coding_total: int = 0
    coding_success: int = 0
    coding_build_success: int = 0
    coding_test_success: int = 0
    coding_avg_latency: float = 0.0
    coding_p99_latency: float = 0.0
    # Round comparison fields (proper dataclass fields, not ad-hoc attributes)
    round_total: int = 0
    round_success: int = 0


@dataclass
class BatchTask:
    """Single batch test task parameters"""

    task_id: str  # Unique ID, e.g. "tc10_ratio10_bp0.5"
    total_count: int  # Sandbox count
    benchmark_percent: float  # Percentage of sandboxes for benchmark
    ratio: int  # Memory migration ratio (%)

    # Runtime state (filled after execution)
    result_dir: Optional[str] = None  # Result directory path
    report_file: Optional[str] = None  # bench_report.txt path
    analysis_file: Optional[str] = None  # analysis_report.xlsx path
    browser_metrics: Optional[Dict[str, Any]] = None  # Extracted browser metrics
    coding_metrics: Optional[Dict[str, Any]] = None  # Extracted coding metrics
    vm_metrics: Optional[Dict[str, Any]] = None  # Extracted vm_monitor metrics
    success: bool = False
    error_msg: Optional[str] = None


@dataclass
class TaskGroup:
    """Group of tasks that can reuse the same sandbox set"""

    group_id: str  # Group ID, e.g. "tc10_ratio10"
    total_count: int  # Shared by all tasks in group
    ratio: int  # Shared by all tasks in group
    tasks: List[BatchTask]  # Tasks with different benchmark_percent

    # Runtime state
    sandbox_states: Optional[Dict[int, Any]] = None  # Shared sandbox states
    smap_tool_manager: Optional[Any] = None  # Shared SmapToolManager
