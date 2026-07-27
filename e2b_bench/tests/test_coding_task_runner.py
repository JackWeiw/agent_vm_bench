"""Unit tests for CodingMetrics, coding config, and workflow dispatch"""

import unittest
from e2b_bench.config import Config
from e2b_bench.schemas import CodingMetrics, CODING_STEP_ORDER, BROWSER_STEP_ORDER, SandboxState


class TestCodingMetrics(unittest.TestCase):
    """Test CodingMetrics thread-safety and functionality"""

    def test_basic_add(self):
        """Test basic metrics recording"""
        m = CodingMetrics()
        m.add(1.5, True, step_times={"build": 1.2}, build_success=True, test_success=True)
        self.assertEqual(m.total_tasks, 1)
        self.assertEqual(m.success_count, 1)
        self.assertEqual(m.failed_count, 0)
        self.assertEqual(m.build_success_count, 1)
        self.assertEqual(m.test_success_count, 1)
        self.assertAlmostEqual(m.avg_latency, 1.5)

    def test_failed_task(self):
        """Test recording a failed task"""
        m = CodingMetrics()
        m.add(3.0, False, build_success=False)
        self.assertEqual(m.total_tasks, 1)
        self.assertEqual(m.success_count, 0)
        self.assertEqual(m.failed_count, 1)
        self.assertEqual(m.build_success_count, 0)

    def test_timeout_task(self):
        """Test recording a timed-out task"""
        m = CodingMetrics()
        m.add(5.0, False, timeout=True, build_success=False)
        self.assertEqual(m.timeout_count, 1)
        self.assertEqual(m.failed_count, 1)

    def test_mixed_build_test_success(self):
        """Test build succeeded but test failed"""
        m = CodingMetrics()
        m.add(2.0, False, build_success=True, test_success=False)
        self.assertEqual(m.build_success_count, 1)
        self.assertEqual(m.test_success_count, 0)

    def test_step_times(self):
        """Test step-level timing recording"""
        m = CodingMetrics()
        step_times = {
            "checkout": 0.1,
            "edit": 0.05,
            "build": 1.2,
            "test": 0.15,
            "memory": 0.01,
        }
        m.add(1.5, True, step_times=step_times, build_success=True, test_success=True)

        stats = m.get_step_stats()
        self.assertEqual(set(stats.keys()), set(step_times.keys()))
        self.assertAlmostEqual(stats["build"]["avg"], 1.2)
        self.assertEqual(stats["build"]["count"], 1)

    def test_multiple_tasks_p99(self):
        """Test p99 latency with multiple tasks"""
        m = CodingMetrics()
        for i in range(10):
            m.add(float(i), True, build_success=True, test_success=True)
        self.assertEqual(m.total_tasks, 10)
        self.assertEqual(m.success_count, 10)
        # p99 with <100 samples = max
        self.assertAlmostEqual(m.p99_latency, 9.0)

    def test_get_latencies_since(self):
        """Test get_latencies_since for round delta calculation"""
        m = CodingMetrics()
        m.add(1.0, True, build_success=True, test_success=True)
        m.add(2.0, True, build_success=True, test_success=True)
        m.add(3.0, True, build_success=True, test_success=True)

        # Get latencies after count 1
        since = m.get_latencies_since(1)
        self.assertEqual(len(since), 2)
        self.assertAlmostEqual(since[0], 2.0)
        self.assertAlmostEqual(since[1], 3.0)

        # Get latencies after count >= total
        since_empty = m.get_latencies_since(10)
        self.assertEqual(len(since_empty), 0)

    def test_last_error(self):
        """Test last_error property"""
        m = CodingMetrics()
        self.assertEqual(m.last_error, "")
        m.add(1.0, False)
        m.last_error = "build failed: exit_code=1"
        self.assertEqual(m.last_error, "build failed: exit_code=1")


class TestCodingConfig(unittest.TestCase):
    """Test Config with coding workflow type"""

    def test_default_workflow_type(self):
        """Test default workflow type is browser"""
        c = Config()
        self.assertEqual(c.workflow_type, "browser")

    def test_coding_workflow_type(self):
        """Test setting workflow type to coding"""
        c = Config(workflow_type="coding")
        self.assertEqual(c.workflow_type, "coding")

    def test_coding_config_defaults(self):
        """Test coding config default values"""
        c = Config(workflow_type="coding")
        self.assertEqual(c.coding_project_dir, "/opt/coding-bench")
        self.assertEqual(c.coding_dev_wait, 20)
        self.assertEqual(c.coding_build_cmd, "npm run build")
        self.assertEqual(c.coding_test_cmd, "npm test")
        self.assertEqual(c.coding_build_timeout, 300)
        self.assertEqual(c.coding_test_timeout, 120)
        self.assertEqual(len(c.coding_source_files), 22)

    def test_yaml_coding_config(self):
        """Test loading coding config from YAML file"""
        c = Config.load_from_yaml("config/e2b_coding_bench.yaml")
        self.assertEqual(c.workflow_type, "coding")
        self.assertEqual(c.template, "openclaw-coding-v1")
        self.assertEqual(c.coding_project_dir, "/opt/coding-bench")
        self.assertEqual(len(c.coding_source_files), 22)
        self.assertEqual(c.benchmark_mode, "round_robin")

    def test_yaml_browser_config_unaffected(self):
        """Test that browser config loading still works"""
        c = Config.load_from_yaml("config/e2b_bench.yaml")
        self.assertEqual(c.workflow_type, "browser")
        self.assertEqual(c.template, "openclaw-browser-v1")


class TestStepOrderConstants(unittest.TestCase):
    """Test step order constants"""

    def test_coding_step_order(self):
        """Test coding step order matches expected steps"""
        self.assertEqual(CODING_STEP_ORDER, ["ensure_dev", "checkout", "edit", "build", "test", "memory"])

    def test_browser_step_order(self):
        """Test browser step order unchanged"""
        self.assertEqual(BROWSER_STEP_ORDER, ["open_tab", "page_load", "snapshot", "click", "screenshot"])


class TestSandboxStateCodingMetrics(unittest.TestCase):
    """Test SandboxState with coding_metrics field"""

    def test_coding_metrics_default(self):
        """Test SandboxState has coding_metrics by default"""
        state = SandboxState(sandbox_id=1)
        self.assertIsNotNone(state.coding_metrics)
        self.assertEqual(state.coding_metrics.total_tasks, 0)

    def test_browser_metrics_unchanged(self):
        """Test browser_metrics still works"""
        state = SandboxState(sandbox_id=1)
        self.assertIsNotNone(state.browser_metrics)
        self.assertEqual(state.browser_metrics.total_tasks, 0)


if __name__ == "__main__":
    unittest.main()
