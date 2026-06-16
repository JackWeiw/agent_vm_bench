"""
Data Structure Definitions Module

Defines SandboxStatus, CreationMetrics, BrowserMetrics, SandboxState, TestSnapshot
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum
import statistics


class SandboxStatus(Enum):
    """Sandbox status enumeration"""
    PENDING = "pending"       # Waiting for creation
    CREATING = "creating"     # Creating in progress
    CREATED = "created"       # sandbox.create succeeded, waiting for ports
    PORT_READY = "port_ready" # Ports ready, can execute tasks
    ACTIVE = "active"         # Active, executing tasks
    FAILED = "failed"         # Creation failed
    PORT_FAILED = "port_failed"  # Port check failed
    OFFLINE = "offline"       # Runtime offline
    KILLED = "killed"         # Killed


@dataclass
class CreationMetrics:
    """Sandbox creation performance metrics"""
    submit_time: float = 0.0       # Creation submit time
    create_ready_time: float = 0.0 # sandbox.create success time (excluding port wait)
    port_ready_time: float = 0.0   # Ports ready time
    create_elapsed: float = 0.0    # sandbox.create elapsed time (seconds)
    port_wait_elapsed: float = 0.0 # Port wait elapsed time (seconds)
    total_elapsed: float = 0.0     # Total elapsed = create_elapsed + port_wait_elapsed
    status: SandboxStatus = SandboxStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""     # Port check error message


@dataclass
class BrowserMetrics:
    """Browser task metrics"""
    total_tasks: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)

    def add(self, latency: float, success: bool, timeout: bool = False) -> None:
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

    @property
    def avg_latency(self) -> float:
        """Average latency (seconds)"""
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        """P99 latency (seconds)"""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(sorted_lat) >= 100:
            return sorted_lat[int(len(sorted_lat) * 0.99)]
        return sorted_lat[-1]


@dataclass
class SandboxState:
    """Sandbox complete state"""
    sandbox_id: int              # Sequence number (1, 2, 3...)
    sandbox_obj: Optional[object] = None  # E2B Sandbox object reference (handle)
    batch_id: int = -1           # Batch ID

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)

    is_alive: bool = True        # Sandbox alive status
    last_task_time: float = 0.0  # Last task execution time
    consecutive_failures: int = 0  # Consecutive failure count


@dataclass
class TestSnapshot:
    """Test snapshot"""
    timestamp: float             # Snapshot timestamp
    elapsed: float               # Time elapsed since test start (seconds)
    total_sandboxes: int         # Total sandbox count
    active_sandboxes: int        # Active sandbox count
    offline_sandboxes: int       # Offline sandbox count
    creation_stats: Dict[str, any] = field(default_factory=dict)  # {"create": {...}, "port_wait": {...}, "total": {...}}
    browser_total: int = 0       # Browser task total count
    browser_success: int = 0     # Successful task count
    browser_avg_latency: float = 0.0  # Average latency
    browser_p99_latency: float = 0.0  # P99 latency