"""
Tests for Docker Bench schemas
"""

import pytest
from docker_bench.schemas import (
    ContainerStatus,
    CreationMetrics,
    BrowserMetrics,
    ContainerState,
    TestSnapshot,
)


class TestContainerStatus:
    """Test ContainerStatus enum"""

    def test_status_values(self):
        """Test all status values exist"""
        assert ContainerStatus.PENDING.value == "pending"
        assert ContainerStatus.CREATING.value == "creating"
        assert ContainerStatus.CREATED.value == "created"
        assert ContainerStatus.PORT_READY.value == "port_ready"
        assert ContainerStatus.ACTIVE.value == "active"
        assert ContainerStatus.FAILED.value == "failed"
        assert ContainerStatus.PORT_FAILED.value == "port_failed"
        assert ContainerStatus.OFFLINE.value == "offline"
        assert ContainerStatus.KILLED.value == "killed"

    def test_status_count(self):
        """Test we have expected number of statuses"""
        assert len(ContainerStatus) == 9


class TestCreationMetrics:
    """Test CreationMetrics dataclass"""

    def test_default_values(self):
        """Test default initialization"""
        metrics = CreationMetrics()
        assert metrics.submit_time == 0.0
        assert metrics.create_ready_time == 0.0
        assert metrics.port_ready_time == 0.0
        assert metrics.create_elapsed == 0.0
        assert metrics.port_wait_elapsed == 0.0
        assert metrics.total_elapsed == 0.0
        assert metrics.status == ContainerStatus.PENDING
        assert metrics.error_msg == ""
        assert metrics.port_check_error == ""

    def test_custom_values(self):
        """Test custom initialization"""
        metrics = CreationMetrics(
            submit_time=100.0,
            create_elapsed=5.5,
            port_wait_elapsed=10.0,
            total_elapsed=15.5,
            status=ContainerStatus.PORT_READY,
        )
        assert metrics.submit_time == 100.0
        assert metrics.create_elapsed == 5.5
        assert metrics.total_elapsed == 15.5
        assert metrics.status == ContainerStatus.PORT_READY


class TestBrowserMetrics:
    """Test BrowserMetrics dataclass"""

    def test_default_values(self):
        """Test default initialization"""
        metrics = BrowserMetrics()
        assert metrics.total_tasks == 0
        assert metrics.success_count == 0
        assert metrics.failed_count == 0
        assert metrics.timeout_count == 0
        assert metrics.latencies == []
        assert metrics.last_error == ""

    def test_add_success(self):
        """Test adding successful task"""
        metrics = BrowserMetrics()
        metrics.add(latency=1.5, success=True)
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        assert metrics.failed_count == 0
        assert metrics.latencies == [1.5]

    def test_add_failure(self):
        """Test adding failed task"""
        metrics = BrowserMetrics()
        metrics.add(latency=0.0, success=False)
        assert metrics.total_tasks == 1
        assert metrics.success_count == 0
        assert metrics.failed_count == 1
        assert metrics.latencies == []

    def test_add_timeout(self):
        """Test adding timeout task"""
        metrics = BrowserMetrics()
        metrics.add(latency=200.0, success=False, timeout=True)
        assert metrics.total_tasks == 1
        assert metrics.success_count == 0
        assert metrics.failed_count == 1
        assert metrics.timeout_count == 1

    def test_avg_latency(self):
        """Test average latency calculation"""
        metrics = BrowserMetrics()
        metrics.add(1.0, success=True)
        metrics.add(2.0, success=True)
        metrics.add(3.0, success=True)
        assert metrics.avg_latency == 2.0

    def test_avg_latency_empty(self):
        """Test average latency with no data"""
        metrics = BrowserMetrics()
        assert metrics.avg_latency == 0.0

    def test_p99_latency_empty(self):
        """Test P99 latency with no data"""
        metrics = BrowserMetrics()
        assert metrics.p99_latency == 0.0

    def test_p99_latency_small_sample(self):
        """Test P99 latency with small sample (returns max)"""
        metrics = BrowserMetrics()
        for i in range(10):
            metrics.add(float(i), success=True)
        assert metrics.p99_latency == 9.0

    def test_p99_latency_large_sample(self):
        """Test P99 latency with 100+ samples"""
        metrics = BrowserMetrics()
        for i in range(100):
            metrics.add(float(i + 1), success=True)
        # int(100 * 0.99) = 99 -> index 99 -> value 100
        assert metrics.p99_latency == 100.0

    def test_step_latencies(self):
        """Test per-step latency tracking"""
        metrics = BrowserMetrics()
        step_times = {"open": 1.0, "focus": 0.5, "click": 0.3}
        metrics.add(1.8, success=True, step_times=step_times)
        assert metrics.step_latencies["open"] == [1.0]
        assert metrics.step_latencies["focus"] == [0.5]
        assert metrics.step_latencies["click"] == [0.3]

    def test_get_step_avg_latency(self):
        """Test average step latency"""
        metrics = BrowserMetrics()
        metrics.add(1.0, success=True, step_times={"open": 1.0})
        metrics.add(2.0, success=True, step_times={"open": 2.0})
        assert metrics.get_step_avg_latency("open") == 1.5

    def test_get_step_avg_latency_missing(self):
        """Test average step latency for missing step"""
        metrics = BrowserMetrics()
        assert metrics.get_step_avg_latency("missing") == 0.0


class TestContainerState:
    """Test ContainerState dataclass"""

    def test_default_values(self):
        """Test default initialization"""
        state = ContainerState(container_id=1)
        assert state.container_id == 1
        assert state.container_name == ""
        assert state.batch_id == -1
        assert state.is_alive == True
        assert state.browser_started == False

    def test_custom_values(self):
        """Test custom initialization"""
        state = ContainerState(
            container_id=5,
            container_name="oc-bench-5",
            batch_id=2,
        )
        assert state.container_id == 5
        assert state.container_name == "oc-bench-5"
        assert state.batch_id == 2


class TestTestSnapshot:
    """Test TestSnapshot dataclass"""

    def test_default_values(self):
        """Test default initialization"""
        snapshot = TestSnapshot(
            timestamp=100.0,
            elapsed=10.0,
            total_containers=10,
            active_containers=8,
            offline_containers=2,
        )
        assert snapshot.timestamp == 100.0
        assert snapshot.elapsed == 10.0
        assert snapshot.total_containers == 10
        assert snapshot.active_containers == 8
        assert snapshot.offline_containers == 2
        assert snapshot.browser_total == 0
        assert snapshot.qps == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])