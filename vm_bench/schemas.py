"""
Data Structure Definitions Module

Defines VMStatus, CreationMetrics, ConnectionMetrics, QAMetrics, BrowserMetrics,
StressMetrics, VMHealth, VMState, TestSnapshot, OOMType
"""

import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class VMStatus(Enum):
    """VM status enumeration (covers both OpenStack creation and SSH connection)"""

    # Phase 0: OpenStack Creation
    PENDING = "pending"  # Not created yet
    CREATING = "creating"  # OpenStack API call in progress
    CREATED = "created"  # openstack create succeeded
    ACTIVE = "active"  # VM status = ACTIVE (OpenStack confirmed)
    CREATE_FAILED = "create_failed"  # OpenStack creation failed
    TIMEOUT = "timeout"  # Creation timeout

    # Phase 1: SSH Connection + Benchmark
    CONNECTING = "connecting"  # SSH connection in progress
    CONNECTED = "connected"  # SSH success
    PORT_READY = "port_ready"  # Application ports verified (ready for tasks)
    RUNNING = "running"  # Benchmark tasks in progress

    # Runtime states
    OFFLINE = "offline"  # SSH connection lost
    SHUTOFF = "shutoff"  # OpenStack SHUTOFF (memory overcommit)
    ERROR = "error"  # OpenStack ERROR state
    DELETED = "deleted"  # VM deleted


class OOMType(Enum):
    """OOM Type Classification for stress_tool failures"""

    NONE = "none"
    START_OOM = "start_oom"  # OOM at startup (memory allocation failed)
    RUNTIME_OOM = "runtime_oom"  # OOM at runtime (killed by OOM Killer)
    CRASH = "crash"  # Program crash (segmentation fault etc.)
    UNKNOWN = "unknown"  # Unknown cause


@dataclass
class CreationMetrics:
    """VM creation performance metrics (OpenStack)"""

    submit_time: float = 0.0  # Creation submit time
    active_time: float = 0.0  # VM becomes ACTIVE time
    elapsed: float = 0.0  # submit -> ACTIVE duration
    status: VMStatus = VMStatus.PENDING
    vm_uuid: str = ""  # OpenStack VM UUID
    error_msg: str = ""


@dataclass
class ConnectionMetrics:
    """SSH connection metrics"""

    connect_time: float = 0.0  # SSH connect start time
    ready_time: float = 0.0  # SSH + ports ready time
    connect_elapsed: float = 0.0  # SSH connection duration
    port_wait_elapsed: float = 0.0  # Port check duration (if applicable)
    total_elapsed: float = 0.0  # connect + port_wait
    status: VMStatus = VMStatus.PENDING
    error_msg: str = ""


@dataclass
class QAMetrics:
    """QA performance metrics"""

    total_queries: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    memory_init_done: bool = False
    memory_init_time: float = 0.0
    current_query_index: int = 0
    query_round: int = 0

    def add(self, latency: float, success: bool, timeout: bool = False):
        """Add a query result"""
        self.total_queries += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
        else:
            self.failed_count += 1

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(self.latencies) < 100:
            return sorted_lat[-1]
        return sorted_lat[int(len(self.latencies) * 0.99)]


@dataclass
class BrowserMetrics:
    """Browser task metrics"""

    total_tasks: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    task_type_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    last_error: str = ""

    def add(self, latency: float, success: bool, timeout: bool = False, task_type: str = ""):
        """Add a task result"""
        self.total_tasks += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
        else:
            self.failed_count += 1

        if task_type:
            if task_type not in self.task_type_counts:
                self.task_type_counts[task_type] = {"success": 0, "failed": 0}
            if success and not timeout:
                self.task_type_counts[task_type]["success"] += 1
            else:
                self.task_type_counts[task_type]["failed"] += 1

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(self.latencies) < 100:
            return sorted_lat[-1]
        return sorted_lat[int(len(self.latencies) * 0.99)]


@dataclass
class StressMetrics:
    """Stress process metrics"""

    start_count: int = 0
    restart_count: int = 0
    oom_events: Dict[OOMType, int] = field(default_factory=lambda: dict.fromkeys(OOMType, 0))
    last_start_time: float = 0.0
    current_pid: Optional[str] = None


@dataclass
class VMHealth:
    """VM health status"""

    is_connected: bool = True
    last_seen: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    error_history: List[Tuple[float, str]] = field(default_factory=list)

    def mark_failure(self, error: str):
        self.consecutive_failures += 1
        self.last_error = error
        self.error_history.append((0.0, error))  # Will be updated with time.time() by caller
        if len(self.error_history) > 10:
            self.error_history.pop(0)

    def mark_success(self):
        self.consecutive_failures = 0
        self.last_error = ""

    def check_offline(self, threshold: int = 2) -> bool:
        return self.consecutive_failures >= threshold


@dataclass
class VMState:
    """Complete VM state (creation + connection + benchmark)"""

    vm_id: int  # Sequence number (1, 2, 3...)
    vm_name: str = ""  # OpenStack VM name
    fixed_ip: str = ""  # Fixed IP address
    vm_uuid: str = ""  # OpenStack UUID

    # Phase 0: Creation (OpenStack)
    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    # Phase 1: Connection (SSH)
    connection_metrics: ConnectionMetrics = field(default_factory=ConnectionMetrics)
    vm_connection: Optional[object] = None  # VMConnection handle (SSH client)

    # Benchmark metrics
    qa_metrics: QAMetrics = field(default_factory=QAMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    stress_metrics: StressMetrics = field(default_factory=StressMetrics)

    # Health
    health: VMHealth = field(default_factory=VMHealth)
    batch_id: int = -1

    # Task flags
    is_stress_vm: bool = False
    stress_started: bool = False
    last_stress_check: float = 0.0
    last_qa_time: float = 0.0
    last_browser_time: float = 0.0
    warmup_done: bool = False

    # Failure counts
    qa_failure_count: int = 0
    stress_failure_count: int = 0
    browser_failure_count: int = 0

    @property
    def has_task_failure(self) -> bool:
        return self.qa_failure_count > 0 or self.stress_failure_count > 0 or self.browser_failure_count > 0

    def record_qa_failure(self):
        self.qa_failure_count += 1

    def record_stress_failure(self):
        self.stress_failure_count += 1

    def record_browser_failure(self):
        self.browser_failure_count += 1


@dataclass
class TestSnapshot:
    """Test snapshot for real-time stats"""

    timestamp: float
    elapsed: float
    total_vms: int
    active_vms: int
    offline_vms: int
    total_failure_vms: int

    # Creation stats (Phase 0)
    creation_stats: Dict[str, any] = field(default_factory=dict)

    # Connection stats (Phase 1)
    connection_stats: Dict[str, any] = field(default_factory=dict)

    # Task stats
    qa_total: int = 0
    qa_success: int = 0
    qa_avg_latency: float = 0.0
    qa_p99_latency: float = 0.0

    browser_total: int = 0
    browser_success: int = 0
    browser_avg_latency: float = 0.0
    browser_p99_latency: float = 0.0
    browser_type_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)

    stress_restart_count: int = 0
    oom_events: Dict[OOMType, int] = field(default_factory=dict)
