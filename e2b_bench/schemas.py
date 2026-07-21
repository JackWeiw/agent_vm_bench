"""
Data Structure Definitions Module

Defines SandboxStatus, CreationMetrics, BrowserMetrics, SandboxState, TestSnapshot
"""

import statistics
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


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


class BrowserMetrics:
    """Browser task metrics (thread-safe for concurrent access)"""

    def __init__(self):
        self._lock = threading.Lock()
        self._total_tasks: int = 0
        self._success_count: int = 0
        self._failed_count: int = 0
        self._timeout_count: int = 0
        self._latencies: List[float] = []
        self._last_error: str = ""
        # Step-level timing for tab-switch mode
        self._step_times: Dict[str, List[float]] = {}

    def add(self, latency: float, success: bool, timeout: bool = False, step_times: Dict[str, float] = None) -> None:
        """Add a task result (thread-safe)

        Args:
            latency: Total latency for the task
            success: Whether the task succeeded
            timeout: Whether the task timed out
            step_times: Optional dict of step name -> latency (e.g., {"tab_switch": 0.5, "snapshot": 1.2})
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

            # Record step-level times
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
        """Get statistics for each step (avg, p99)

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


@dataclass
class SandboxState:
    """Sandbox complete state"""

    sandbox_id: int  # Sequence number (1, 2, 3...)
    sandbox_obj: Optional[object] = None  # E2B Sandbox object reference (handle)
    batch_id: int = -1  # Batch ID

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)

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
        # Ensure _lock is a proper lock instance
        # Note: threading.Lock() returns _thread.lock type, not threading.Lock class
        if not hasattr(self, "_lock") or not hasattr(self._lock, "acquire"):
            object.__setattr__(self, "_lock", threading.Lock())

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
    browser_total: int = 0  # Browser task total count
    browser_success: int = 0  # Successful task count
    browser_avg_latency: float = 0.0  # Average latency
    browser_p99_latency: float = 0.0  # P99 latency


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
