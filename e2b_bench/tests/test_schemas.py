"""
Test Schemas Module

Tests for data structures: SandboxStatus, CreationMetrics, BrowserMetrics, SandboxState, BatchTask, TaskGroup
"""

import pytest
from unittest.mock import Mock
import statistics

from e2b_bench.schemas import (
    SandboxStatus,
    CreationMetrics,
    BrowserMetrics,
    SandboxState,
    BatchTask,
    TaskGroup
)


class TestSandboxStatus:
    """Tests for SandboxStatus enum"""

    def test_all_statuses_exist(self):
        """Verify all status values"""
        assert SandboxStatus.PENDING.value == "pending"
        assert SandboxStatus.CREATING.value == "creating"
        assert SandboxStatus.CREATED.value == "created"
        assert SandboxStatus.PORT_READY.value == "port_ready"
        assert SandboxStatus.ACTIVE.value == "active"
        assert SandboxStatus.FAILED.value == "failed"
        assert SandboxStatus.PORT_FAILED.value == "port_failed"
        assert SandboxStatus.OFFLINE.value == "offline"
        assert SandboxStatus.KILLED.value == "killed"


class TestCreationMetrics:
    """Tests for CreationMetrics dataclass"""

    def test_defaults(self):
        """Default values should be zeros and PENDING"""
        metrics = CreationMetrics()
        assert metrics.submit_time == 0.0
        assert metrics.create_elapsed == 0.0
        assert metrics.port_wait_elapsed == 0.0
        assert metrics.total_elapsed == 0.0
        assert metrics.status == SandboxStatus.PENDING

    def test_custom_values(self):
        """Custom values"""
        metrics = CreationMetrics(
            submit_time=100.0,
            create_elapsed=5.5,
            port_wait_elapsed=3.0,
            total_elapsed=8.5,
            status=SandboxStatus.PORT_READY
        )
        assert metrics.submit_time == 100.0
        assert metrics.total_elapsed == 8.5
        assert metrics.status == SandboxStatus.PORT_READY


class TestBrowserMetrics:
    """Tests for BrowserMetrics dataclass"""

    def test_defaults(self):
        """Default empty state"""
        metrics = BrowserMetrics()
        assert metrics.total_tasks == 0
        assert metrics.success_count == 0
        assert metrics.failed_count == 0
        assert metrics.latencies == []
        assert metrics.avg_latency == 0.0
        assert metrics.p99_latency == 0.0

    def test_add_success(self):
        """Adding successful task"""
        metrics = BrowserMetrics()
        metrics.add(latency=1.5, success=True)
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        assert metrics.failed_count == 0
        assert metrics.latencies == [1.5]
        assert metrics.avg_latency == 1.5

    def test_add_failure(self):
        """Adding failed task"""
        metrics = BrowserMetrics()
        metrics.add(latency=2.0, success=False)
        assert metrics.total_tasks == 1
        assert metrics.success_count == 0
        assert metrics.failed_count == 1
        assert metrics.latencies == []

    def test_add_timeout(self):
        """Timeout counts as failure"""
        metrics = BrowserMetrics()
        metrics.add(latency=10.0, success=False, timeout=True)
        assert metrics.timeout_count == 1
        assert metrics.failed_count == 1

    def test_multiple_tasks(self):
        """Multiple tasks accumulate correctly"""
        metrics = BrowserMetrics()
        for i in range(10):
            metrics.add(latency=i * 0.5, success=True)
        assert metrics.total_tasks == 10
        assert metrics.success_count == 10
        assert len(metrics.latencies) == 10

    def test_p99_with_100_values(self):
        """P99 with 100 values"""
        metrics = BrowserMetrics()
        for i in range(1, 101):
            metrics.add(latency=i, success=True)
        assert metrics.p99_latency == 99

    def test_p99_small_list(self):
        """P99 returns max for small lists"""
        metrics = BrowserMetrics()
        for i in range(1, 11):
            metrics.add(latency=i, success=True)
        assert metrics.p99_latency == 10


class TestSandboxState:
    """Tests for SandboxState dataclass"""

    def test_defaults(self):
        """Default state"""
        state = SandboxState(sandbox_id=1)
        assert state.sandbox_id == 1
        assert state.sandbox_obj is None
        assert state.batch_id == -1
        assert state.is_alive == True
        assert state.warmup_done == False

    def test_custom_values(self):
        """Custom state"""
        mock_sandbox = Mock()
        state = SandboxState(
            sandbox_id=5,
            sandbox_obj=mock_sandbox,
            batch_id=2,
            is_alive=False,
            consecutive_failures=3,
            warmup_done=True
        )
        assert state.sandbox_id == 5
        assert state.is_alive == False
        assert state.warmup_done == True


class TestBatchTask:
    """Tests for BatchTask dataclass"""

    def test_task_creation(self):
        """Create task with required fields"""
        task = BatchTask(
            task_id="tc10_ratio10_bp0.5",
            total_count=10,
            benchmark_percent=0.5,
            ratio=10
        )
        assert task.task_id == "tc10_ratio10_bp0.5"
        assert task.total_count == 10
        assert task.benchmark_percent == 0.5
        assert task.ratio == 10
        assert task.success == False

    def test_task_runtime_fields(self):
        """Runtime fields start as None"""
        task = BatchTask(task_id="test", total_count=10, benchmark_percent=1.0, ratio=15)
        assert task.result_dir is None
        assert task.report_file is None
        assert task.analysis_file is None
        assert task.browser_metrics is None
        assert task.vm_metrics is None


class TestTaskGroup:
    """Tests for TaskGroup dataclass"""

    def test_group_creation(self):
        """Create group with tasks"""
        tasks = [
            BatchTask(task_id="tc10_ratio10_bp0.5", total_count=10, benchmark_percent=0.5, ratio=10),
            BatchTask(task_id="tc10_ratio10_bp1.0", total_count=10, benchmark_percent=1.0, ratio=10)
        ]
        group = TaskGroup(
            group_id="tc10_ratio10",
            total_count=10,
            ratio=10,
            tasks=tasks
        )
        assert group.group_id == "tc10_ratio10"
        assert len(group.tasks) == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])