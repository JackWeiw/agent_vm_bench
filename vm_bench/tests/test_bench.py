"""
Unit tests for vm_bench bench.py (orchestration)

Tests CLI argument parsing and mode selection
"""

import unittest
import argparse
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.bench import build_arg_parser, BatchController


class TestBuildArgParser(unittest.TestCase):
    """Test CLI argument parser"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_basic_args(self):
        args = self.parser.parse_args(['-n', '10', '--start-ip', '192.168.110.11'])
        self.assertEqual(args.total, 10)
        self.assertEqual(args.start_ip, '192.168.110.11')

    def test_config_file_arg(self):
        args = self.parser.parse_args(['--config', 'test.yaml'])
        self.assertEqual(args.config, 'test.yaml')

    def test_create_only_flag(self):
        args = self.parser.parse_args(['--create-only'])
        self.assertTrue(args.create_only)

    def test_detect_flag(self):
        args = self.parser.parse_args(['--detect'])
        self.assertTrue(args.detect)

    def test_warmup_only_flag(self):
        args = self.parser.parse_args(['--warmup-only'])
        self.assertTrue(args.warmup_only)

    def test_duration_arg(self):
        args = self.parser.parse_args(['-t', '300'])
        self.assertEqual(args.duration, 300)

    def test_browser_url_arg(self):
        args = self.parser.parse_args(['--browser-url', 'http://example.com'])
        self.assertEqual(args.browser_url, ['http://example.com'])

    def test_multiple_browser_urls(self):
        args = self.parser.parse_args([
            '--browser-url', 'http://a.com',
            '--browser-url', 'http://b.com'
        ])
        self.assertEqual(args.browser_url, ['http://a.com', 'http://b.com'])

    def test_batch_args(self):
        args = self.parser.parse_args([
            '--create-batch-size', '20',
            '--create-batch-interval', '5'
        ])
        self.assertEqual(args.create_batch_size, 20)
        self.assertEqual(args.create_batch_interval, 5)

    def test_benchmark_percent(self):
        args = self.parser.parse_args(['--benchmark-percent', '0.5'])
        self.assertEqual(args.benchmark_percent, 0.5)

    def test_task_mode_arg(self):
        args = self.parser.parse_args(['--task-mode', 'qa'])
        self.assertEqual(args.task_mode, 'qa')

    def test_delete_after_test(self):
        args = self.parser.parse_args(['--delete-after-test'])
        self.assertTrue(args.delete_after_test)


class TestArgParserDefaults(unittest.TestCase):
    """Test default values for CLI arguments"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_default_total(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.total)

    def test_default_start_ip(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.start_ip)

    def test_default_duration(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.duration)

    def test_default_create_batch_size(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.create_batch_size)


class TestBatchController(unittest.TestCase):
    """Test BatchController class"""

    def test_init_basic(self):
        config = type('Config', (), {
            'task_batch_size': 10,
            'task_batch_interval': 5,
            'total_count': 30
        })()

        controller = BatchController(config, [1, 2, 3, 4, 5])

        # Should have batches
        self.assertGreater(len(controller.batch_ready), 0)

    def test_batch_mapping(self):
        config = type('Config', (), {
            'task_batch_size': 2,
            'task_batch_interval': 5,
            'total_count': 6
        })()

        controller = BatchController(config, [1, 2, 3, 4, 5, 6])

        # 6 VMs with batch_size=2 should create 3 batches
        self.assertEqual(len(controller.batch_ready), 3)

        # VM 1, 2 should be in batch 0
        self.assertEqual(controller.vm_batch_map[1], 0)
        self.assertEqual(controller.vm_batch_map[2], 0)
        self.assertEqual(controller.vm_batch_map[3], 1)

    def test_is_batch_ready(self):
        config = type('Config', (), {
            'task_batch_size': 10,
            'task_batch_interval': 5,
            'total_count': 20
        })()

        controller = BatchController(config, list(range(1, 21)))

        # Initially batch 0 not ready
        self.assertFalse(controller.is_batch_ready(0))

        # After setting ready
        controller.batch_ready[0] = True
        self.assertTrue(controller.is_batch_ready(0))

    def test_notify_stress_started(self):
        config = type('Config', (), {
            'task_batch_size': 10,
            'task_batch_interval': 5,
            'total_count': 20
        })()

        controller = BatchController(config, list(range(1, 21)))

        # Notify VM 1 started
        controller.notify_stress_started(1)
        self.assertEqual(controller.batch_started_count[0], 1)


class TestModeCombinations(unittest.TestCase):
    """Test mode flag combinations"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_create_only_no_detect(self):
        args = self.parser.parse_args(['--create-only'])
        self.assertTrue(args.create_only)
        self.assertFalse(args.detect)

    def test_detect_no_create_only(self):
        args = self.parser.parse_args(['--detect'])
        self.assertFalse(args.create_only)
        self.assertTrue(args.detect)

    def test_warmup_only_combination(self):
        args = self.parser.parse_args(['--warmup-only', '-n', '10'])
        self.assertTrue(args.warmup_only)


class TestWarmupArgs(unittest.TestCase):
    """Test warmup-related arguments"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_warmup_url_single(self):
        args = self.parser.parse_args([
            '--warmup-url', 'http://warmup1.html'
        ])
        self.assertEqual(args.warmup_url, ['http://warmup1.html'])

    def test_warmup_url_multiple(self):
        args = self.parser.parse_args([
            '--warmup-url', 'http://warmup1.html',
            '--warmup-url', 'http://warmup2.html'
        ])
        self.assertEqual(args.warmup_url, ['http://warmup1.html', 'http://warmup2.html'])

    def test_warmup_loops(self):
        args = self.parser.parse_args(['--warmup-loops', '3'])
        self.assertEqual(args.warmup_loops, 3)

    def test_warmup_delay(self):
        args = self.parser.parse_args(['--warmup-delay', '5'])
        self.assertEqual(args.warmup_delay, 5)


class TestSSHArgs(unittest.TestCase):
    """Test SSH-related arguments"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_ssh_port(self):
        args = self.parser.parse_args(['--ssh-port', '2222'])
        self.assertEqual(args.ssh_port, 2222)

    def test_ssh_username(self):
        args = self.parser.parse_args(['--ssh-username', 'admin'])
        self.assertEqual(args.ssh_username, 'admin')

    def test_ssh_password(self):
        args = self.parser.parse_args(['--ssh-password', 'secret'])
        self.assertEqual(args.ssh_password, 'secret')


class TestStressArgs(unittest.TestCase):
    """Test stress-related arguments"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_stress_percent(self):
        args = self.parser.parse_args(['--stress-percent', '0.3'])
        self.assertEqual(args.stress_percent, 0.3)

    def test_stress_memory(self):
        args = self.parser.parse_args(['--stress-memory', '4096'])
        self.assertEqual(args.stress_memory, 4096)

    def test_no_keepalive(self):
        args = self.parser.parse_args(['--no-keepalive'])
        self.assertTrue(args.no_keepalive)


class TestQAArgs(unittest.TestCase):
    """Test QA-related arguments"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_qa_timeout(self):
        args = self.parser.parse_args(['--qa-timeout', '300'])
        self.assertEqual(args.qa_timeout, 300)

    def test_qa_interval(self):
        args = self.parser.parse_args(['--qa-interval', '1.0'])
        self.assertEqual(args.qa_interval, 1.0)

    def test_qa_mode(self):
        args = self.parser.parse_args(['--qa-mode', 'http'])
        self.assertEqual(args.qa_mode, 'http')


class TestReportArgs(unittest.TestCase):
    """Test report-related arguments"""

    def setUp(self):
        self.parser = build_arg_parser()

    def test_output_dir(self):
        args = self.parser.parse_args(['--output-dir', '/tmp/results'])
        self.assertEqual(args.output_dir, '/tmp/results')

    def test_filename_prefix(self):
        args = self.parser.parse_args(['--filename-prefix', 'my_bench'])
        self.assertEqual(args.filename_prefix, 'my_bench')

    def test_stats_interval(self):
        args = self.parser.parse_args(['--stats-interval', '30'])
        self.assertEqual(args.stats_interval, 30)


if __name__ == '__main__':
    unittest.main()