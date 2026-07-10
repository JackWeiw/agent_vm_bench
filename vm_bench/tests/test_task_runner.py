"""
Unit tests for vm_bench task runner module

Tests use mocks to simulate task execution
"""

import os
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.config import Config
from vm_bench.schemas import OOMType, VMState
from vm_bench.task_runner import BrowserTaskManager, QATaskManager, StressTaskManager, VMTaskRunner


class TestQATaskManager(unittest.TestCase):
    """Test QA task manager"""

    def setUp(self):
        self.config = Config(qa_timeout=60, qa_init_timeout=60, qa_interval=0.5, qa_mode="cli")

    def test_init(self):
        manager = QATaskManager(self.config)
        self.assertEqual(manager.config.qa_timeout, 60)

    @patch("vm_bench.task_runner.QATaskManager._execute_http_query")
    def test_memory_init_already_done(self, mock_http):
        manager = QATaskManager(self.config)
        state = VMState(vm_id=1)
        state.qa_metrics.memory_init_done = True

        mock_vm = Mock()
        result = manager.run_memory_init(mock_vm, state)
        self.assertTrue(result)
        mock_http.assert_not_called()

    def test_run_qa_query_not_initialized(self):
        manager = QATaskManager(self.config)
        state = VMState(vm_id=1)
        # memory_init_done = False

        # Should fail because memory not initialized
        with patch.object(manager, "run_memory_init", return_value=False):
            mock_vm = Mock()
            success, latency = manager.run_qa_query(mock_vm, state)
            self.assertFalse(success)


class TestStressTaskManager(unittest.TestCase):
    """Test Stress task manager"""

    def setUp(self):
        self.config = Config(stress_memory_mb=2048, stress_duration=300, stress_keepalive=True)

    def test_init(self):
        manager = StressTaskManager(self.config)
        self.assertEqual(manager.config.stress_memory_mb, 2048)

    def test_diagnose_failure_none(self):
        manager = StressTaskManager(self.config)
        mock_vm = Mock()
        mock_vm.execute.return_value = (True, "Stress Tool Started\nFinished", "", 1.0, 0)

        oom_type = manager._diagnose_failure(mock_vm, "test_log", "runtime")
        self.assertEqual(oom_type, OOMType.NONE)

    def test_check_process_running(self):
        manager = StressTaskManager(self.config)
        mock_vm = Mock()
        mock_vm.execute.return_value = (True, "1234", "", 1.0, 0)

        result = manager._check_process(mock_vm, "1234")
        self.assertTrue(result)

    def test_check_process_dead(self):
        manager = StressTaskManager(self.config)
        mock_vm = Mock()
        mock_vm.execute.return_value = (True, "DEAD", "", 1.0, 0)

        result = manager._check_process(mock_vm, "1234")
        self.assertFalse(result)


class TestBrowserTaskManager(unittest.TestCase):
    """Test Browser task manager"""

    def setUp(self):
        self.config = Config(
            browser_urls=["http://example.com/test.html"],
            browser_timeout=200,
            browser_use_llm=False,
            warmup_urls=["http://example.com/warmup.html"],
            warmup_loops=1,
            warmup_delay=2,
        )

    def test_init(self):
        manager = BrowserTaskManager(self.config)
        self.assertEqual(manager.config.browser_timeout, 200)

    def test_run_browser_task_direct(self):
        manager = BrowserTaskManager(self.config)
        state = VMState(vm_id=1)
        mock_vm = Mock()
        mock_vm.execute.return_value = (True, "", "", 5.0, 0)

        success, latency, task_type = manager.run_browser_task(mock_vm, state)
        # Direct browser adds 10s delay
        self.assertEqual(latency, 15.0)
        self.assertEqual(state.browser_metrics.total_tasks, 1)

    def test_warmup_phase_no_urls(self):
        config = Config(warmup_urls=[])
        manager = BrowserTaskManager(config)
        state = VMState(vm_id=1)
        mock_vm = Mock()

        result = manager.warmup_phase(mock_vm, state)
        self.assertTrue(result)
        self.assertTrue(state.warmup_done)

    def test_warmup_phase_with_urls(self):
        manager = BrowserTaskManager(self.config)
        state = VMState(vm_id=1)
        mock_vm = Mock()
        mock_vm.execute.return_value = (True, "", "", 5.0, 0)

        result = manager.warmup_phase(mock_vm, state)
        self.assertTrue(state.warmup_done)


class TestTaskTypeTracking(unittest.TestCase):
    """Test task type tracking in BrowserMetrics"""

    def setUp(self):
        self.config = Config(
            browser_urls=["http://example.com/test.html"],
            browser_timeout=200,
        )

    def test_task_type_counting(self):
        manager = BrowserTaskManager(self.config)
        state = VMState(vm_id=1)
        mock_vm = Mock()
        mock_vm.execute.return_value = (True, "", "", 5.0, 0)

        # Run multiple tasks
        for i in range(3):
            manager.run_browser_task(mock_vm, state)

        self.assertEqual(state.browser_metrics.total_tasks, 3)


class TestVMTaskRunnerInit(unittest.TestCase):
    """Test VMTaskRunner initialization"""

    def test_init(self):
        config = Config(task_mode="browser")
        stop_event = threading.Event()
        state = VMState(vm_id=1)
        mock_vm = Mock()
        mock_vm.vm_id = 1  # Set the attribute directly

        runner = VMTaskRunner(vm=mock_vm, state=state, config=config, stop_event=stop_event, browser_manager=Mock())

        self.assertEqual(runner.state.vm_id, 1)
        self.assertEqual(runner.config.task_mode, "browser")
        self.assertEqual(runner.consecutive_errors, 0)


class TestMetricsAccumulation(unittest.TestCase):
    """Test metrics accumulation across multiple tasks"""

    def test_qa_metrics_accumulation(self):
        state = VMState(vm_id=1)

        for i in range(10):
            state.qa_metrics.add(1.0 + i * 0.1, success=True)

        self.assertEqual(state.qa_metrics.total_queries, 10)
        self.assertEqual(state.qa_metrics.success_count, 10)
        self.assertEqual(len(state.qa_metrics.latencies), 10)

    def test_browser_metrics_accumulation(self):
        state = VMState(vm_id=1)

        for i in range(20):
            state.browser_metrics.add(2.0 + i * 0.05, success=True, task_type="Page Access")

        self.assertEqual(state.browser_metrics.total_tasks, 20)
        self.assertEqual(state.browser_metrics.success_count, 20)
        self.assertEqual(state.browser_metrics.task_type_counts["Page Access"]["success"], 20)


if __name__ == "__main__":
    unittest.main()
