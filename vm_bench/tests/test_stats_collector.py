"""
Unit tests for vm_bench stats collector module

Tests statistics collection and report generation
"""

import unittest
import threading
import time
import sys
import os
import tempfile
import shutil
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.config import Config
from vm_bench.schemas import VMState, VMStatus, BrowserMetrics
from vm_bench.stats_collector import StatsCollector


class TestStatsCollector(unittest.TestCase):
    """Test StatsCollector class"""

    def setUp(self):
        self.config = Config(
            total_count=5,
            task_mode="browser",
            test_duration=60,
            stats_interval=5,
            output_dir=tempfile.mkdtemp()
        )

        # Create mock VM states
        self.vm_states = {}
        for i in range(5):
            state = VMState(vm_id=i + 1)
            state.creation_metrics.status = VMStatus.ACTIVE
            state.connection_metrics.status = VMStatus.CONNECTED
            state.browser_metrics.total_tasks = 10 + i
            state.browser_metrics.success_count = 8 + i
            state.browser_metrics.latencies = [1.0, 2.0, 3.0, 4.0, 5.0]
            self.vm_states[i + 1] = state

    def tearDown(self):
        shutil.rmtree(self.config.output_dir)

    def test_init(self):
        collector = StatsCollector(self.config, self.vm_states)
        self.assertEqual(len(collector.vm_states), 5)
        self.assertEqual(len(collector.snapshots), 0)

    def test_start_time_tracking(self):
        collector = StatsCollector(self.config, self.vm_states)
        collector.start()
        self.assertGreater(collector.start_time, 0)
        collector.stop()

    def test_snapshot_collection(self):
        collector = StatsCollector(self.config, self.vm_states)
        collector._take_snapshot()

        self.assertEqual(len(collector.snapshots), 1)
        snapshot = collector.snapshots[0]
        self.assertEqual(snapshot.total_vms, 5)

    def test_active_vm_count(self):
        # Mark one VM as offline
        self.vm_states[3].health.is_connected = False

        collector = StatsCollector(self.config, self.vm_states)
        collector._take_snapshot()

        snapshot = collector.snapshots[0]
        self.assertEqual(snapshot.total_vms, 5)
        self.assertEqual(snapshot.offline_vms, 1)

    def test_browser_metrics_in_snapshot(self):
        collector = StatsCollector(self.config, self.vm_states)
        collector._take_snapshot()

        snapshot = collector.snapshots[0]
        # Should aggregate browser metrics
        self.assertGreater(snapshot.browser_total, 0)
        self.assertGreater(snapshot.browser_success, 0)


class TestReportGeneration(unittest.TestCase):
    """Test report generation"""

    def setUp(self):
        self.config = Config(
            total_count=3,
            task_mode="browser",
            output_dir=tempfile.mkdtemp()
        )

        self.vm_states = {}
        for i in range(3):
            state = VMState(vm_id=i + 1)
            state.creation_metrics.status = VMStatus.ACTIVE
            state.connection_metrics.status = VMStatus.CONNECTED
            state.connection_metrics.connect_elapsed = 5.0 + i
            state.creation_metrics.elapsed = 10.0 + i * 2
            state.browser_metrics.total_tasks = 10
            state.browser_metrics.success_count = 9
            state.browser_metrics.failed_count = 1
            state.browser_metrics.latencies = [1.0, 2.0, 3.0]
            self.vm_states[i + 1] = state

    def tearDown(self):
        shutil.rmtree(self.config.output_dir)

    def test_generate_report(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = collector.generate_report()

        self.assertIn("VM Bench", report)
        self.assertIn("VM Status", report)
        self.assertIn("Browser Task Statistics", report)

    def test_report_contains_vm_count(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = collector.generate_report()

        self.assertIn("Total VMs", report)
        self.assertIn("3", report)

    def test_report_contains_browser_stats(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = collector.generate_report()

        self.assertIn("Total Tasks", report)
        self.assertIn("Success", report)

    def test_report_contains_latency_stats(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = collector.generate_report()

        self.assertIn("Avg Latency", report)
        self.assertIn("P99", report)


class TestReportSaving(unittest.TestCase):
    """Test report file saving"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = Config(
            output_dir=self.temp_dir,
            filename_prefix="test_bench"
        )
        self.vm_states = {1: VMState(vm_id=1)}

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_save_report(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = "Test report content"
        filepath = collector.save_report(report)

        self.assertTrue(os.path.exists(filepath))
        self.assertIn("test_bench", filepath)
        self.assertIn(self.temp_dir, filepath)

    def test_saved_report_content(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = "Test report content"
        filepath = collector.save_report(report)

        with open(filepath, 'r') as f:
            content = f.read()
        self.assertEqual(content, report)


class TestCreationStats(unittest.TestCase):
    """Test creation timing statistics in report"""

    def setUp(self):
        self.config = Config(output_dir=tempfile.mkdtemp())

        self.vm_states = {}
        for i in range(5):
            state = VMState(vm_id=i + 1)
            state.creation_metrics.status = VMStatus.ACTIVE
            state.creation_metrics.elapsed = 10.0 + i * 5  # 10, 15, 20, 25, 30
            self.vm_states[i + 1] = state

    def tearDown(self):
        shutil.rmtree(self.config.output_dir)

    def test_creation_performance_in_report(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = collector.generate_report()

        self.assertIn("Creation Performance", report)
        self.assertIn("Min:", report)
        self.assertIn("Max:", report)
        self.assertIn("Avg:", report)
        self.assertIn("P99:", report)


class TestConnectionStats(unittest.TestCase):
    """Test connection timing statistics"""

    def setUp(self):
        self.config = Config(output_dir=tempfile.mkdtemp())

        self.vm_states = {}
        for i in range(3):
            state = VMState(vm_id=i + 1)
            state.connection_metrics.status = VMStatus.CONNECTED
            state.connection_metrics.connect_elapsed = 2.0 + i  # 2, 3, 4
            self.vm_states[i + 1] = state

    def tearDown(self):
        shutil.rmtree(self.config.output_dir)

    def test_connection_performance_in_report(self):
        collector = StatsCollector(self.config, self.vm_states)
        report = collector.generate_report()

        self.assertIn("Connection Performance", report)


if __name__ == '__main__':
    unittest.main()