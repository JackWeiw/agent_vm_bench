"""
Test Config Module

Tests for Config dataclass: loading, merging, properties
"""

import pytest
import os
import tempfile

from e2b_bench.config import Config


class TestConfigDefaults:
    """Tests for Config default values"""

    def test_defaults(self):
        """Default configuration values"""
        config = Config()
        assert config.template == "openclaw-browser-v1"
        assert config.total_count == 100
        assert config.benchmark_percent == 1.0
        assert config.test_duration == 600
        assert config.browser_timeout == 200
        assert config.smap_tool_enabled == False
        assert config.vm_monitor_enabled == False

    def test_benchmark_count(self):
        """benchmark_count property calculation"""
        config = Config(total_count=100, benchmark_percent=0.5)
        assert config.benchmark_count == 50

        config = Config(total_count=100, benchmark_percent=1.0)
        assert config.benchmark_count == 100

        config = Config(total_count=10, benchmark_percent=0.3)
        assert config.benchmark_count == 3  # max(1, int(10 * 0.3))

    def test_create_batch_count_no_batch(self):
        """No batch size = 1 batch (full concurrent)"""
        config = Config(total_count=100, create_batch_size=None)
        assert config.create_batch_count == 1

    def test_create_batch_count_with_batch(self):
        """Batch count calculation"""
        config = Config(total_count=100, create_batch_size=10)
        assert config.create_batch_count == 10

        config = Config(total_count=100, create_batch_size=30)
        assert config.create_batch_count == 4  # (100 + 30 - 1) // 30

    def test_task_batch_count(self):
        """task_batch_count property"""
        config = Config(total_count=100, task_batch_size=None)
        assert config.task_batch_count == 1

        config = Config(total_count=100, task_batch_size=20)
        assert config.task_batch_count == 5


class TestConfigLoadYaml:
    """Tests for loading Config from YAML"""

    def test_load_basic(self):
        """Load basic config"""
        yaml_content = """
sandbox:
  template: custom-template
  total_count: 50

test:
  duration: 300
  benchmark_percent: 0.75

browser:
  urls:
    - http://example.com/page1
  task_timeout: 150

report:
  output_dir: custom_results
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        assert config.template == "custom-template"
        assert config.total_count == 50
        assert config.test_duration == 300
        assert config.benchmark_percent == 0.75
        assert config.browser_timeout == 150
        assert config.output_dir == "custom_results"

    def test_load_e2b_env(self):
        """Load E2B environment settings"""
        yaml_content = """
e2b_env:
  E2B_ACCESS_TOKEN: test_token
  E2B_API_KEY: test_key
  E2B_DOMAIN: custom.domain
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        assert config.e2b_access_token == "test_token"
        assert config.e2b_api_key == "test_key"
        assert config.e2b_domain == "custom.domain"

    def test_load_warmup(self):
        """Load warmup settings"""
        yaml_content = """
browser:
  warmup_urls:
    - http://example.com/warmup1
    - http://example.com/warmup2
  warmup_loops: 3
  warmup_delay: 15
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        assert len(config.warmup_urls) == 2
        assert config.warmup_loops == 3
        assert config.warmup_delay == 15

    def test_load_smap_tool(self):
        """Load smap_tool settings"""
        yaml_content = """
smap_tool:
  enabled: true
  ratio: 20
  src_nid: 0
  dest_nid: 3
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        assert config.smap_tool_enabled == True
        assert config.smap_tool_ratio == 20
        assert config.smap_tool_src_nid == 0
        assert config.smap_tool_dest_nid == 3

    def test_load_vm_monitor(self):
        """Load vm_monitor settings"""
        yaml_content = """
vm_monitor:
  enabled: true
  vmm_type: qemu
  duration: 120
  numa: "0,1"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        assert config.vm_monitor_enabled == True
        assert config.vm_monitor_vmm_type == "qemu"
        assert config.vm_monitor_duration == 120
        assert config.vm_monitor_numa == "0,1"


class TestConfigSetupEnv:
    """Tests for setup_e2b_env"""

    def test_setup_env(self):
        """Environment variables are set"""
        # Clear first
        for key in ["E2B_ACCESS_TOKEN", "E2B_API_KEY", "E2B_DOMAIN"]:
            if key in os.environ:
                del os.environ[key]

        config = Config(
            e2b_access_token="my_token",
            e2b_api_key="my_key",
            e2b_domain="my.domain"
        )
        config.setup_e2b_env()

        assert os.environ["E2B_ACCESS_TOKEN"] == "my_token"
        assert os.environ["E2B_API_KEY"] == "my_key"
        assert os.environ["E2B_DOMAIN"] == "my.domain"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])