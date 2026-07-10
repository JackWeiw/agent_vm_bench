"""
Unit tests for vm_bench Config class

Tests:
- Default values
- YAML loading
- CLI argument parsing
- YAML + CLI merge priority
- Computed properties
"""

import unittest
import argparse
import tempfile
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.config import Config


class TestConfigDefaults(unittest.TestCase):
    """Test default configuration values"""

    def test_default_total_count(self):
        config = Config()
        self.assertEqual(config.total_count, 80)

    def test_default_start_ip(self):
        config = Config()
        self.assertEqual(config.start_ip, "192.168.110.11")

    def test_default_flavor(self):
        config = Config()
        self.assertEqual(config.flavor, "2U_4G_40G")

    def test_default_image(self):
        config = Config()
        self.assertEqual(config.image, "ubuntu-24.04")

    def test_default_ssh_credentials(self):
        config = Config()
        self.assertEqual(config.ssh_port, 22)
        self.assertEqual(config.ssh_username, "root")
        self.assertEqual(config.ssh_password, "openEuler12#$")

    def test_default_create_batch(self):
        config = Config()
        self.assertEqual(config.create_batch_size, 20)
        self.assertEqual(config.create_batch_interval, 3)

    def test_default_task_batch(self):
        config = Config()
        self.assertEqual(config.task_batch_size, 10)
        self.assertEqual(config.task_batch_interval, 5)

    def test_default_browser_interval(self):
        config = Config()
        self.assertEqual(config.browser_interval_min, 5.0)
        self.assertEqual(config.browser_interval_max, 10.0)

    def test_default_warmup(self):
        config = Config()
        self.assertEqual(config.warmup_loops, 1)
        self.assertEqual(config.warmup_delay, 3)

    def test_default_task_mode(self):
        config = Config()
        self.assertEqual(config.task_mode, "browser")

    def test_default_duration(self):
        config = Config()
        self.assertEqual(config.test_duration, 600)


class TestConfigYAMLLoading(unittest.TestCase):
    """Test YAML configuration loading"""

    def setUp(self):
        """Create temporary YAML file for testing"""
        self.temp_dir = tempfile.mkdtemp()
        self.yaml_path = os.path.join(self.temp_dir, "test_config.yaml")

    def tearDown(self):
        """Clean up temporary files"""
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_yaml_basic_loading(self):
        yaml_content = """
vm_create:
  total_count: 100
  start_ip: "192.168.120.1"
  flavor: "4U_8G_80G"
"""
        with open(self.yaml_path, 'w') as f:
            f.write(yaml_content)

        config = Config.load_from_yaml(self.yaml_path)
        self.assertEqual(config.total_count, 100)
        self.assertEqual(config.start_ip, "192.168.120.1")
        self.assertEqual(config.flavor, "4U_8G_80G")

    def test_yaml_nested_sections(self):
        yaml_content = """
vm_create:
  total_count: 50
  start_ip: "192.168.110.200"

create_batch:
  size: 25
  interval: 10

task_batch:
  size: 5
  interval: 2

browser:
  timeout: 300
  interval_min: 10
  interval_max: 20
  benchmark_percent: 0.75
  warmup_urls:
    - "http://example.com/page1.html"
    - "http://example.com/page2.html"
"""
        with open(self.yaml_path, 'w') as f:
            f.write(yaml_content)

        config = Config.load_from_yaml(self.yaml_path)
        self.assertEqual(config.total_count, 50)
        self.assertEqual(config.create_batch_size, 25)
        self.assertEqual(config.create_batch_interval, 10)
        self.assertEqual(config.task_batch_size, 5)
        self.assertEqual(config.task_batch_interval, 2)
        self.assertEqual(config.browser_timeout, 300)
        self.assertEqual(config.browser_interval_min, 10)
        self.assertEqual(config.browser_interval_max, 20)
        self.assertEqual(config.benchmark_percent, 0.75)
        self.assertEqual(len(config.warmup_urls), 2)

    def test_yaml_missing_sections_use_defaults(self):
        yaml_content = """
vm_create:
  total_count: 30
"""
        with open(self.yaml_path, 'w') as f:
            f.write(yaml_content)

        config = Config.load_from_yaml(self.yaml_path)
        self.assertEqual(config.total_count, 30)  # From YAML
        self.assertEqual(config.flavor, "2U_4G_40G")  # Default
        self.assertEqual(config.create_batch_size, 20)  # Default


class TestConfigCLIArgs(unittest.TestCase):
    """Test CLI argument parsing"""

    def test_from_args_basic(self):
        args = argparse.Namespace(
            total=10,
            start_ip="192.168.110.100",
            flavor="custom_flavor",
        )
        # Add missing attributes with None
        for attr in ['auth_source', 'image', 'network_id', 'az', 'subnet_prefix',
                     'vm_prefix', 'create_timeout', 'create_only', 'detect',
                     'create_batch_size', 'create_batch_interval', 'ssh_port',
                     'ssh_username', 'ssh_password', 'ssh_connect_timeout',
                     'connect_batch_size', 'connect_batch_interval', 'task_batch_size',
                     'task_batch_interval', 'task_mode', 'duration', 'browser_url',
                     'browser_timeout', 'browser_interval_min', 'browser_interval_max',
                     'browser_use_llm', 'benchmark_percent', 'warmup_url', 'warmup_loops',
                     'warmup_delay', 'warmup_only', 'qa_timeout', 'qa_init_timeout',
                     'qa_interval', 'qa_mode', 'stress_percent', 'stress_memory',
                     'no_keepalive', 'stats_interval', 'output_dir', 'filename_prefix',
                     'delete_after_test']:
            if not hasattr(args, attr):
                setattr(args, attr, None)

        config = Config.from_args(args)
        self.assertEqual(config.total_count, 10)
        self.assertEqual(config.start_ip, "192.168.110.100")
        self.assertEqual(config.flavor, "custom_flavor")

    def test_from_args_zero_value_override(self):
        """Test that 0 can override defaults (is not None check)"""
        args = argparse.Namespace(
            total=0,
            create_batch_size=0,
            browser_timeout=0,
        )
        # Add missing attributes
        for attr in ['auth_source', 'start_ip', 'flavor', 'image', 'network_id',
                     'az', 'subnet_prefix', 'vm_prefix', 'create_timeout', 'create_only',
                     'detect', 'create_batch_interval', 'ssh_port', 'ssh_username',
                     'ssh_password', 'ssh_connect_timeout', 'connect_batch_size',
                     'connect_batch_interval', 'task_batch_size', 'task_batch_interval',
                     'task_mode', 'duration', 'browser_url', 'browser_interval_min',
                     'browser_interval_max', 'browser_use_llm', 'benchmark_percent',
                     'warmup_url', 'warmup_loops', 'warmup_delay', 'warmup_only',
                     'qa_timeout', 'qa_init_timeout', 'qa_interval', 'qa_mode',
                     'stress_percent', 'stress_memory', 'no_keepalive', 'stats_interval',
                     'output_dir', 'filename_prefix', 'delete_after_test']:
            if not hasattr(args, attr):
                setattr(args, attr, None)

        config = Config.from_args(args)
        self.assertEqual(config.total_count, 0)
        self.assertEqual(config.create_batch_size, 0)
        self.assertEqual(config.browser_timeout, 0)


class TestConfigMergePriority(unittest.TestCase):
    """Test YAML + CLI merge priority (CLI > YAML > defaults)"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.yaml_path = os.path.join(self.temp_dir, "merge_config.yaml")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_cli_overrides_yaml(self):
        yaml_content = """
vm_create:
  total_count: 100
  start_ip: "192.168.110.50"
  flavor: "yaml_flavor"
"""
        with open(self.yaml_path, 'w') as f:
            f.write(yaml_content)

        yaml_config = Config.load_from_yaml(self.yaml_path)
        args = argparse.Namespace(
            config=self.yaml_path,
            total=20,  # Override YAML's 100
            start_ip="192.168.110.200",  # Override YAML's 50
            flavor=None,  # Use YAML value
        )
        # Add missing attributes
        for attr in ['auth_source', 'image', 'network_id', 'az', 'subnet_prefix',
                     'vm_prefix', 'create_timeout', 'create_only', 'detect',
                     'create_batch_size', 'create_batch_interval', 'ssh_port',
                     'ssh_username', 'ssh_password', 'ssh_connect_timeout',
                     'connect_batch_size', 'connect_batch_interval', 'task_batch_size',
                     'task_batch_interval', 'task_mode', 'duration', 'browser_url',
                     'browser_timeout', 'browser_interval_min', 'browser_interval_max',
                     'browser_use_llm', 'benchmark_percent', 'warmup_url', 'warmup_loops',
                     'warmup_delay', 'warmup_only', 'qa_timeout', 'qa_init_timeout',
                     'qa_interval', 'qa_mode', 'stress_percent', 'stress_memory',
                     'no_keepalive', 'stats_interval', 'output_dir', 'filename_prefix',
                     'delete_after_test']:
            if not hasattr(args, attr):
                setattr(args, attr, None)

        config = Config.merge_with_args(yaml_config, args)

        self.assertEqual(config.total_count, 20)  # CLI override
        self.assertEqual(config.start_ip, "192.168.110.200")  # CLI override
        self.assertEqual(config.flavor, "yaml_flavor")  # YAML value (CLI was None)


class TestConfigComputedProperties(unittest.TestCase):
    """Test computed properties"""

    def test_stress_vm_count(self):
        config = Config(total_count=100, stress_percent=0.5)
        self.assertEqual(config.stress_vm_count, 50)

        config = Config(total_count=80, stress_percent=0.3)
        self.assertEqual(config.stress_vm_count, 24)

    def test_benchmark_vm_count(self):
        config = Config(total_count=100, benchmark_percent=1.0)
        self.assertEqual(config.benchmark_vm_count, 100)

        config = Config(total_count=100, benchmark_percent=0.5)
        self.assertEqual(config.benchmark_vm_count, 50)

        config = Config(total_count=100, benchmark_percent=0.01)
        self.assertEqual(config.benchmark_vm_count, 1)  # min(1, ...)

    def test_create_batch_count(self):
        config = Config(total_count=100, create_batch_size=20)
        self.assertEqual(config.create_batch_count, 5)

        config = Config(total_count=80, create_batch_size=10)
        self.assertEqual(config.create_batch_count, 8)

    def test_get_ip_range(self):
        config = Config(start_ip="192.168.110.11", total_count=5)
        ips = config.get_ip_range()
        self.assertEqual(len(ips), 5)
        self.assertEqual(ips[0], "192.168.110.11")
        self.assertEqual(ips[4], "192.168.110.15")


if __name__ == '__main__':
    unittest.main()