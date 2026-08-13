"""
Data Structure Definitions Module

Defines ContainerStatus, CreationMetrics, BrowserMetrics, ContainerState, TestSnapshot
"""

import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class ContainerStatus(Enum):
    """Container status enumeration"""

    PENDING = "pending"  # Waiting for creation
    CREATING = "creating"  # Creating in progress
    CREATED = "created"  # Container created, waiting for ports
    PORT_READY = "port_ready"  # Ports ready, can execute tasks
    ACTIVE = "active"  # Active, executing tasks
    FAILED = "failed"  # Creation failed
    PORT_FAILED = "port_failed"  # Port check failed
    OFFLINE = "offline"  # Runtime offline
    KILLED = "killed"  # Killed/removed


@dataclass
class CreationMetrics:
    """Container creation performance metrics"""

    submit_time: float = 0.0  # Creation submit time
    create_ready_time: float = 0.0  # Container created time (excluding port wait)
    port_ready_time: float = 0.0  # Ports ready time
    create_elapsed: float = 0.0  # Container creation elapsed time (seconds)
    port_wait_elapsed: float = 0.0  # Port wait elapsed time (seconds)
    total_elapsed: float = 0.0  # Total elapsed = create_elapsed + port_wait_elapsed
    status: ContainerStatus = ContainerStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""  # Port check error message


@dataclass
class BrowserMetrics:
    """Browser task metrics"""

    total_tasks: int = 0  # Total queries completed (5 steps = 1 query)
    success_count: int = 0  # Successful queries
    failed_count: int = 0  # Failed queries
    timeout_count: int = 0  # Timeout queries
    latencies: List[float] = field(default_factory=list)  # Query latencies (seconds)
    step_latencies: Dict[str, List[float]] = field(default_factory=dict)  # Per-step latencies
    last_error: str = ""  # Last error message for debugging

    def add(self, latency: float, success: bool, timeout: bool = False, step_times: Dict[str, float] = None) -> None:
        """Add a task result

        Args:
            latency: Total query latency (5 steps)
            success: Whether query succeeded
            timeout: Whether query timed out
            step_times: Per-step latencies (optional)
        """
        self.total_tasks += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
            # Record per-step latencies if provided
            if step_times:
                for step_name, step_time in step_times.items():
                    if step_name not in self.step_latencies:
                        self.step_latencies[step_name] = []
                    self.step_latencies[step_name].append(step_time)
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

    def get_step_avg_latency(self, step_name: str) -> float:
        """Get average latency for a specific step"""
        if step_name in self.step_latencies and self.step_latencies[step_name]:
            return statistics.mean(self.step_latencies[step_name])
        return 0.0


@dataclass
class ContainerState:
    """Container complete state"""

    container_id: int  # Sequence number (1, 2, 3...)
    container_name: str = ""  # Docker container name
    docker_container: Optional[object] = None  # Docker container object reference
    batch_id: int = -1  # Batch ID

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)

    is_alive: bool = True  # Container alive status
    last_task_time: float = 0.0  # Last task execution time
    consecutive_failures: int = 0  # Consecutive failure count
    browser_started: bool = False  # OpenClaw browser backend started flag
    working_click_element: str = ""  # Element ID that successfully clicked (reuse for same page)


@dataclass
class TestSnapshot:
    """Test snapshot"""

    timestamp: float  # Snapshot timestamp
    elapsed: float  # Time elapsed since test start (seconds)
    total_containers: int  # Total container count
    active_containers: int  # Active container count
    offline_containers: int  # Offline container count
    creation_stats: Dict[str, any] = field(
        default_factory=dict
    )  # {"create": {...}, "port_wait": {...}, "total": {...}}
    browser_total: int = 0  # Browser query total count
    browser_success: int = 0  # Successful query count
    browser_avg_latency: float = 0.0  # Average latency
    browser_p99_latency: float = 0.0  # P99 latency
    qps: float = 0.0  # Current QPS
