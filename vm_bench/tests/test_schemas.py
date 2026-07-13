"""
Unit tests for vm_bench schemas (data structures)

Tests:
- VMStatus enum values
- OOMType enum values
- CreationMetrics dataclass
- ConnectionMetrics dataclass
- QAMetrics dataclass
- BrowserMetrics dataclass
- StressMetrics dataclass
- VMHealth dataclass
- VMState dataclass
- TestSnapshot dataclass
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.schemas import (
    BrowserMetrics,
    ConnectionMetrics,
    CreationMetrics,
    OOMType,
    QAMetrics,
    StressMetrics,
    TestSnapshot,
    VMHealth,
    VMState,
    VMStatus,
)


class TestVMStatusEnum(unittest.TestCase):
    """Test VMStatus enumeration"""

    def test_creation_phase_statuses(self):
        self.assertEqual(VMStatus.PENDING.value, "pending")
        self.assertEqual(VMStatus.CREATING.value, "creating")
        self.assertEqual(VMStatus.CREATED.value, "created")
        self.assertEqual(VMStatus.ACTIVE.value, "active")
        self.assertEqual(VMStatus.CREATE_FAILED.value, "create_failed")
        self.assertEqual(VMStatus.TIMEOUT.value, "timeout")

    def test_connection_phase_statuses(self):
        self.assertEqual(VMStatus.CONNECTING.value, "connecting")
        self.assertEqual(VMStatus.CONNECTED.value, "connected")
        self.assertEqual(VMStatus.PORT_READY.value, "port_ready")
        self.assertEqual(VMStatus.RUNNING.value, "running")

    def test_runtime_statuses(self):
        self.assertEqual(VMStatus.OFFLINE.value, "offline")
        self.assertEqual(VMStatus.SHUTOFF.value, "shutoff")
        self.assertEqual(VMStatus.ERROR.value, "error")
        self.assertEqual(VMStatus.DELETED.value, "deleted")

    def test_all_statuses_defined(self):
        """Ensure all expected statuses exist"""
        expected = [
            "pending",
            "creating",
            "created",
            "active",
            "create_failed",
            "timeout",
            "connecting",
            "connected",
            "port_ready",
            "running",
            "offline",
            "shutoff",
            "error",
            "deleted",
        ]
        actual = [s.value for s in VMStatus]
        self.assertEqual(sorted(expected), sorted(actual))


class TestOOMTypeEnum(unittest.TestCase):
    """Test OOMType enumeration"""

    def test_oom_types(self):
        self.assertEqual(OOMType.NONE.value, "none")
        self.assertEqual(OOMType.START_OOM.value, "start_oom")
        self.assertEqual(OOMType.RUNTIME_OOM.value, "runtime_oom")
        self.assertEqual(OOMType.CRASH.value, "crash")
        self.assertEqual(OOMType.UNKNOWN.value, "unknown")


class TestCreationMetrics(unittest.TestCase):
    """Test CreationMetrics dataclass"""

    def test_default_values(self):
        metrics = CreationMetrics()
        self.assertEqual(metrics.submit_time, 0.0)
        self.assertEqual(metrics.active_time, 0.0)
        self.assertEqual(metrics.elapsed, 0.0)
        self.assertEqual(metrics.status, VMStatus.PENDING)
        self.assertEqual(metrics.vm_uuid, "")
        self.assertEqual(metrics.error_msg, "")

    def test_custom_values(self):
        metrics = CreationMetrics(
            submit_time=100.0,
            active_time=120.0,
            elapsed=20.0,
            status=VMStatus.ACTIVE,
            vm_uuid="abc123",
            error_msg="test error",
        )
        self.assertEqual(metrics.submit_time, 100.0)
        self.assertEqual(metrics.elapsed, 20.0)
        self.assertEqual(metrics.status, VMStatus.ACTIVE)


class TestConnectionMetrics(unittest.TestCase):
    """Test ConnectionMetrics dataclass"""

    def test_default_values(self):
        metrics = ConnectionMetrics()
        self.assertEqual(metrics.connect_time, 0.0)
        self.assertEqual(metrics.ready_time, 0.0)
        self.assertEqual(metrics.connect_elapsed, 0.0)
        self.assertEqual(metrics.port_wait_elapsed, 0.0)
        self.assertEqual(metrics.total_elapsed, 0.0)
        self.assertEqual(metrics.status, VMStatus.PENDING)

    def test_custom_values(self):
        metrics = ConnectionMetrics(connect_time=10.0, ready_time=15.0, connect_elapsed=5.0, status=VMStatus.CONNECTED)
        self.assertEqual(metrics.connect_elapsed, 5.0)
        self.assertEqual(metrics.status, VMStatus.CONNECTED)


class TestQAMetrics(unittest.TestCase):
    """Test QAMetrics dataclass"""

    def test_default_values(self):
        metrics = QAMetrics()
        self.assertEqual(metrics.total_queries, 0)
        self.assertEqual(metrics.success_count, 0)
        self.assertEqual(metrics.failed_count, 0)
        self.assertEqual(metrics.timeout_count, 0)
        self.assertEqual(len(metrics.latencies), 0)

    def test_add_success(self):
        metrics = QAMetrics()
        metrics.add(1.5, success=True)
        self.assertEqual(metrics.total_queries, 1)
        self.assertEqual(metrics.success_count, 1)
        self.assertEqual(metrics.failed_count, 0)
        self.assertEqual(len(metrics.latencies), 1)
        self.assertEqual(metrics.latencies[0], 1.5)

    def test_add_failure(self):
        metrics = QAMetrics()
        metrics.add(2.0, success=False)
        self.assertEqual(metrics.total_queries, 1)
        self.assertEqual(metrics.success_count, 0)
        self.assertEqual(metrics.failed_count, 1)

    def test_add_timeout(self):
        metrics = QAMetrics()
        metrics.add(10.0, success=False, timeout=True)
        self.assertEqual(metrics.total_queries, 1)
        self.assertEqual(metrics.timeout_count, 1)
        self.assertEqual(metrics.failed_count, 1)

    def test_avg_latency(self):
        metrics = QAMetrics()
        metrics.add(1.0, success=True)
        metrics.add(2.0, success=True)
        metrics.add(3.0, success=True)
        self.assertEqual(metrics.avg_latency, 2.0)

    def test_avg_latency_empty(self):
        metrics = QAMetrics()
        self.assertEqual(metrics.avg_latency, 0.0)

    def test_p99_latency(self):
        metrics = QAMetrics()
        # Add 100 latencies
        for i in range(100):
            metrics.add(i * 0.1, success=True)
        # P99 should be around 99 * 0.1 = 9.9
        self.assertAlmostEqual(metrics.p99_latency, 9.9, places=1)

    def test_p99_latency_small_sample(self):
        metrics = QAMetrics()
        metrics.add(1.0, success=True)
        metrics.add(2.0, success=True)
        metrics.add(3.0, success=True)
        # Less than 100 samples, return max
        self.assertEqual(metrics.p99_latency, 3.0)


class TestBrowserMetrics(unittest.TestCase):
    """Test BrowserMetrics dataclass"""

    def test_default_values(self):
        metrics = BrowserMetrics()
        self.assertEqual(metrics.total_tasks, 0)
        self.assertEqual(metrics.success_count, 0)
        self.assertEqual(metrics.failed_count, 0)
        self.assertEqual(metrics.last_error, "")

    def test_add_with_task_type(self):
        metrics = BrowserMetrics()
        metrics.add(5.0, success=True, task_type="Page Access")
        self.assertEqual(metrics.total_tasks, 1)
        self.assertEqual(metrics.success_count, 1)
        self.assertIn("Page Access", metrics.task_type_counts)
        self.assertEqual(metrics.task_type_counts["Page Access"]["success"], 1)

    def test_add_failure_with_task_type(self):
        metrics = BrowserMetrics()
        metrics.add(10.0, success=False, task_type="Page Access")
        self.assertEqual(metrics.task_type_counts["Page Access"]["failed"], 1)

    def test_multiple_task_types(self):
        metrics = BrowserMetrics()
        metrics.add(1.0, success=True, task_type="Page Access")
        metrics.add(2.0, success=False, task_type="Content Extraction")
        metrics.add(3.0, success=True, task_type="Page Access")

        self.assertEqual(metrics.task_type_counts["Page Access"]["success"], 2)
        self.assertEqual(metrics.task_type_counts["Page Access"]["failed"], 0)
        self.assertEqual(metrics.task_type_counts["Content Extraction"]["success"], 0)
        self.assertEqual(metrics.task_type_counts["Content Extraction"]["failed"], 1)


class TestStressMetrics(unittest.TestCase):
    """Test StressMetrics dataclass"""

    def test_default_values(self):
        metrics = StressMetrics()
        self.assertEqual(metrics.start_count, 0)
        self.assertEqual(metrics.restart_count, 0)
        self.assertEqual(metrics.current_pid, None)

    def test_oom_events_default(self):
        metrics = StressMetrics()
        # All OOM types should have 0 count
        for oom_type in OOMType:
            self.assertEqual(metrics.oom_events[oom_type], 0)

    def test_oom_events_custom(self):
        metrics = StressMetrics()
        metrics.oom_events[OOMType.RUNTIME_OOM] = 5
        self.assertEqual(metrics.oom_events[OOMType.RUNTIME_OOM], 5)


class TestVMHealth(unittest.TestCase):
    """Test VMHealth dataclass"""

    def test_default_values(self):
        health = VMHealth()
        self.assertEqual(health.is_connected, True)
        self.assertEqual(health.consecutive_failures, 0)
        self.assertEqual(health.last_error, "")

    def test_mark_failure(self):
        health = VMHealth()
        health.mark_failure("SSH timeout")
        self.assertEqual(health.consecutive_failures, 1)
        self.assertEqual(health.last_error, "SSH timeout")

    def test_mark_success(self):
        health = VMHealth()
        health.mark_failure("error")
        health.mark_success()
        self.assertEqual(health.consecutive_failures, 0)
        self.assertEqual(health.last_error, "")

    def test_check_offline(self):
        health = VMHealth()
        self.assertFalse(health.check_offline())

        health.mark_failure("error1")
        self.assertFalse(health.check_offline())

        health.mark_failure("error2")
        self.assertTrue(health.check_offline(threshold=2))

    def test_error_history_limit(self):
        health = VMHealth()
        for i in range(15):
            health.mark_failure(f"error{i}")
        # Should only keep last 10
        self.assertLessEqual(len(health.error_history), 10)


class TestVMState(unittest.TestCase):
    """Test VMState dataclass"""

    def test_default_values(self):
        state = VMState(vm_id=1)
        self.assertEqual(state.vm_id, 1)
        self.assertEqual(state.vm_name, "")
        self.assertEqual(state.fixed_ip, "")
        self.assertEqual(state.vm_uuid, "")
        self.assertEqual(state.is_stress_vm, False)
        self.assertEqual(state.stress_started, False)
        self.assertEqual(state.warmup_done, False)

    def test_custom_values(self):
        state = VMState(
            vm_id=5, vm_name="test_vm_5", fixed_ip="192.168.110.15", vm_uuid="uuid123", is_stress_vm=True, batch_id=2
        )
        self.assertEqual(state.vm_id, 5)
        self.assertEqual(state.vm_name, "test_vm_5")
        self.assertEqual(state.fixed_ip, "192.168.110.15")
        self.assertEqual(state.is_stress_vm, True)

    def test_has_task_failure(self):
        state = VMState(vm_id=1)
        self.assertFalse(state.has_task_failure)

        state.record_qa_failure()
        self.assertTrue(state.has_task_failure)

    def test_record_failures(self):
        state = VMState(vm_id=1)
        state.record_qa_failure()
        state.record_stress_failure()
        state.record_browser_failure()
        self.assertEqual(state.qa_failure_count, 1)
        self.assertEqual(state.stress_failure_count, 1)
        self.assertEqual(state.browser_failure_count, 1)


class TestTestSnapshot(unittest.TestCase):
    """Test TestSnapshot dataclass"""

    def test_default_values(self):
        snapshot = TestSnapshot(
            timestamp=time.time(), elapsed=100.0, total_vms=10, active_vms=8, offline_vms=2, total_failure_vms=1
        )
        self.assertEqual(snapshot.total_vms, 10)
        self.assertEqual(snapshot.active_vms, 8)
        self.assertEqual(snapshot.browser_total, 0)
        self.assertEqual(snapshot.browser_success, 0)

    def test_browser_metrics(self):
        snapshot = TestSnapshot(
            timestamp=time.time(),
            elapsed=200.0,
            total_vms=10,
            active_vms=10,
            offline_vms=0,
            total_failure_vms=0,
            browser_total=100,
            browser_success=95,
            browser_avg_latency=5.0,
            browser_p99_latency=10.0,
            browser_type_stats={"Page Access": {"success": 95, "failed": 5}},
        )
        self.assertEqual(snapshot.browser_total, 100)
        self.assertEqual(snapshot.browser_success, 95)
        self.assertEqual(snapshot.browser_type_stats["Page Access"]["success"], 95)


if __name__ == "__main__":
    unittest.main()
