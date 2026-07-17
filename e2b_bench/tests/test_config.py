"""
Test Config Module

Tests for Config dataclass: loading, merging, properties
"""

import os
import tempfile

import pytest

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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

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

        config = Config(e2b_access_token="my_token", e2b_api_key="my_key", e2b_domain="my.domain")
        config.setup_e2b_env()

        assert os.environ["E2B_ACCESS_TOKEN"] == "my_token"
        assert os.environ["E2B_API_KEY"] == "my_key"
        assert os.environ["E2B_DOMAIN"] == "my.domain"


class TestConfigRoundRobin:
    """Tests for round-robin mode configuration"""

    def test_default_benchmark_mode(self):
        """Default benchmark_mode is 'fixed'"""
        config = Config()
        assert config.benchmark_mode == "fixed"
        assert config.round_count is None
        assert config.round_interval == 30

    def test_set_via_constructor(self):
        """Set round-robin config via constructor"""
        config = Config(benchmark_mode="round_robin", round_count=5, round_interval=60)
        assert config.benchmark_mode == "round_robin"
        assert config.round_count == 5
        assert config.round_interval == 60

    def test_load_from_yaml(self):
        """Load round-robin config from YAML"""
        yaml_content = """
test:
  benchmark_mode: round_robin
  round_count: 10
  round_interval: 45
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.benchmark_mode == "round_robin"
        assert config.round_count == 10
        assert config.round_interval == 45

    def test_load_yaml_with_defaults(self):
        """Load YAML with round-robin fields using defaults"""
        yaml_content = """
test:
  duration: 160
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        # Should use defaults when not specified
        assert config.benchmark_mode == "fixed"
        assert config.round_count is None
        assert config.round_interval == 30

    def test_merge_with_args_override(self):
        """CLI args override YAML config for round-robin"""
        yaml_content = """
test:
  benchmark_mode: fixed
  round_count: 3
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        yaml_config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        import argparse

        args = argparse.Namespace(
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            sandbox_ids_file=None,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode="round_robin",
            round_count=7,
            round_interval=25,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.merge_with_args(yaml_config, args)
        assert config.benchmark_mode == "round_robin"
        assert config.round_count == 7
        assert config.round_interval == 25

    def test_from_args(self):
        """Build Config from CLI args with round-robin"""
        import argparse

        args = argparse.Namespace(
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            sandbox_ids_file=None,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode="round_robin",
            round_count=4,
            round_interval=20,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.from_args(args)
        assert config.benchmark_mode == "round_robin"
        assert config.round_count == 4
        assert config.round_interval == 20

    def test_yaml_priority_over_cli_default(self):
        """YAML config takes priority over CLI default for benchmark_mode"""
        yaml_content = """
test:
  benchmark_mode: round_robin
  round_interval: 30
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        yaml_config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        import argparse

        # Simulate argparse with default value (user didn't specify --benchmark-mode)
        args = argparse.Namespace(
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            sandbox_ids_file=None,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode=None,  # argparse default value (None means not specified)
            round_count=None,
            round_size=None,
            round_interval=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.merge_with_args(yaml_config, args)
        # YAML value should win over CLI default
        assert config.benchmark_mode == "round_robin"
        assert config.round_interval == 30  # YAML value preserved

    def test_cli_explicit_override_yaml(self):
        """CLI explicitly provided value overrides YAML config when YAML is empty"""
        yaml_content = """
test:
  benchmark_mode: round_robin
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        yaml_config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        import argparse

        args = argparse.Namespace(
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            sandbox_ids_file=None,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode="fixed",  # User explicitly provided different value
            round_count=10,
            round_size=None,
            round_interval=60,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.merge_with_args(yaml_config, args)
        # CLI explicitly provided value wins over YAML
        assert config.benchmark_mode == "fixed"  # CLI wins
        assert config.round_count == 10  # CLI wins (YAML was None)
        assert config.round_interval == 60  # CLI wins (YAML was None)

    def test_actual_yaml_file_loads_round_robin(self):
        """Test that the actual e2b_bench.yaml loads round_robin correctly"""
        import os.path

        yaml_path = "config/e2b_bench.yaml"
        if os.path.exists(yaml_path):
            config = Config.load_from_yaml(yaml_path)
            # The config file should have benchmark_mode: "round_robin"
            assert config.benchmark_mode == "round_robin"
            assert config.round_count == 5
            assert config.round_interval == 30


def _create_minimal_args():
    """Helper to create minimal argparse Namespace with all round-robin fields"""
    import argparse

    return argparse.Namespace(
        e2b_access_token=None,
        e2b_api_key=None,
        e2b_domain=None,
        e2b_api_url=None,
        e2b_http_ssl=None,
        template=None,
        create_timeout=None,
        total=None,
        detect=False,
        create_only=False,
        sandbox_ids_file=None,
        create_batch_size=None,
        create_batch_interval=None,
        task_batch_size=None,
        task_batch_interval=None,
        browser_url=None,
        browser_timeout=None,
        browser_interval_min=None,
        browser_interval_max=None,
        warmup_url=None,
        warmup_loops=None,
        warmup_delay=None,
        warmup_only=False,
        benchmark_percent=None,
        benchmark_mode=None,  # Add round-robin field
        round_count=None,  # Add round-robin field
        round_interval=None,  # Add round-robin field
        duration=None,
        stats_interval=None,
        output_dir=None,
        filename_prefix=None,
    )


class TestConfigSandboxIdsFile:
    """Tests for sandbox_ids_file configuration"""

    def test_default_none(self):
        """Default sandbox_ids_file is None"""
        config = Config()
        assert config.sandbox_ids_file is None

    def test_set_via_constructor(self):
        """Set sandbox_ids_file via constructor"""
        config = Config(sandbox_ids_file="ids.txt")
        assert config.sandbox_ids_file == "ids.txt"

    def test_load_from_yaml(self):
        """Load sandbox_ids_file from YAML"""
        yaml_content = """
sandbox:
  template: custom-template
  sandbox_ids_file: my_sandbox_ids.txt
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.sandbox_ids_file == "my_sandbox_ids.txt"

    def test_merge_with_args(self):
        """CLI arg overrides YAML config"""
        yaml_content = """
sandbox:
  sandbox_ids_file: yaml_ids.txt
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        yaml_config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        import argparse

        args = argparse.Namespace(
            sandbox_ids_file="cli_ids.txt",
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode=None,
            round_count=None,
            round_interval=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.merge_with_args(yaml_config, args)
        assert config.sandbox_ids_file == "cli_ids.txt"

    def test_from_args(self):
        """Build Config from CLI args only"""
        import argparse

        args = argparse.Namespace(
            sandbox_ids_file="args_ids.txt",
            e2b_access_token="token",
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode=None,
            round_count=None,
            round_interval=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.from_args(args)
        assert config.sandbox_ids_file == "args_ids.txt"


class TestConfigNumaBind:
    """Tests for numa_bind configuration"""

    def test_default_numa_bind(self):
        """Default numa_bind is 2"""
        config = Config()
        assert config.numa_bind == 2

    def test_set_via_constructor(self):
        """Set numa_bind via constructor"""
        config = Config(numa_bind=3)
        assert config.numa_bind == 3

    def test_set_null_via_constructor(self):
        """Set numa_bind to None (disabled)"""
        config = Config(numa_bind=None)
        assert config.numa_bind is None

    def test_load_from_yaml(self):
        """Load numa_bind from YAML"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: 5
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == 5

    def test_load_null_from_yaml(self):
        """Load numa_bind as null from YAML (disabled)"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: null
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind is None

    def test_load_missing_numa_bind_defaults_to_2(self):
        """Missing numa_bind in YAML defaults to 2"""
        yaml_content = """
sandbox:
  template: custom-template
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == 2

    def test_merge_with_args_uses_yaml_value(self):
        """merge_with_args uses YAML value for numa_bind"""
        yaml_content = """
sandbox:
  numa_bind: 4
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        yaml_config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        import argparse

        args = argparse.Namespace(
            sandbox_ids_file=None,
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode=None,
            round_count=None,
            round_interval=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.merge_with_args(yaml_config, args)
        assert config.numa_bind == 4

    def test_from_args_defaults_to_2(self):
        """from_args defaults numa_bind to 2"""
        import argparse

        args = argparse.Namespace(
            sandbox_ids_file=None,
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            benchmark_mode=None,
            round_count=None,
            round_interval=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.from_args(args)
        assert config.numa_bind == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
