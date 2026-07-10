"""
Tests for Docker Bench configuration
"""

import os
import tempfile

import pytest

from docker_bench.config import Config


class TestConfigDefaults:
    """Test Config default values"""

    def test_default_docker_settings(self):
        """Test default Docker configuration"""
        config = Config()
        assert config.docker_image == "ubuntu-openclaw-chromium:arm64"
        assert config.container_prefix == "oc-bench"
        assert config.cpu_limit == 2.0
        assert config.memory_limit == "2g"
        assert config.create_timeout == 300

    def test_default_container_settings(self):
        """Test default container configuration"""
        config = Config()
        assert config.total_count == 10
        assert config.detect_existing == False
        assert config.create_only == False

    def test_default_browser_settings(self):
        """Test default browser configuration"""
        config = Config()
        assert config.browser_timeout == 200
        assert config.browser_interval_min == 0.5
        assert config.browser_interval_max == 3.0

    def test_default_test_settings(self):
        """Test default test configuration"""
        config = Config()
        assert config.test_duration == 600
        assert config.stats_interval == 10
        assert config.benchmark_percent == 1.0


class TestConfigProperties:
    """Test Config calculated properties"""

    def test_create_batch_count_no_batch(self):
        """Test batch count when no batching"""
        config = Config(total_count=10, create_batch_size=None)
        assert config.create_batch_count == 1

    def test_create_batch_count_with_batch(self):
        """Test batch count with batching"""
        config = Config(total_count=10, create_batch_size=3)
        # (10 + 3 - 1) // 3 = 4
        assert config.create_batch_count == 4

    def test_task_batch_count_no_batch(self):
        """Test task batch count when no batching"""
        config = Config(total_count=10, task_batch_size=None)
        assert config.task_batch_count == 1

    def test_task_batch_count_with_batch(self):
        """Test task batch count with batching"""
        config = Config(total_count=10, task_batch_size=3)
        # (10 + 3 - 1) // 3 = 4
        assert config.task_batch_count == 4

    def test_benchmark_count_full(self):
        """Test benchmark count at 100%"""
        config = Config(total_count=10, benchmark_percent=1.0)
        assert config.benchmark_count == 10

    def test_benchmark_count_half(self):
        """Test benchmark count at 50%"""
        config = Config(total_count=10, benchmark_percent=0.5)
        assert config.benchmark_count == 5

    def test_benchmark_count_minimum_one(self):
        """Test benchmark count minimum is 1"""
        config = Config(total_count=10, benchmark_percent=0.01)
        assert config.benchmark_count == 1


class TestConfigFromDict:
    """Test Config._from_dict method"""

    def test_from_dict_minimal(self):
        """Test creating config from minimal dict"""
        config = Config._from_dict({})
        assert config.docker_image == "ubuntu-openclaw-chromium:arm64"
        assert config.total_count == 10

    def test_from_dict_full(self):
        """Test creating config from full dict"""
        data = {
            "docker": {
                "image": "custom-image:latest",
                "cpu_limit": 4.0,
                "memory_limit": "4g",
            },
            "container": {
                "total_count": 20,
                "detect_existing": True,
            },
            "test": {
                "duration": 300,
                "benchmark_percent": 0.75,
            },
        }
        config = Config._from_dict(data)
        assert config.docker_image == "custom-image:latest"
        assert config.cpu_limit == 4.0
        assert config.total_count == 20
        assert config.detect_existing == True
        assert config.test_duration == 300
        assert config.benchmark_percent == 0.75


class TestConfigYamlLoading:
    """Test Config YAML file loading"""

    def test_load_from_yaml(self):
        """Test loading config from YAML file"""
        yaml_content = """
docker:
  image: test-image:v1
  cpu_limit: 3.0
container:
  total_count: 15
test:
  duration: 120
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name

        try:
            config = Config.load_from_yaml(yaml_path)
            assert config.docker_image == "test-image:v1"
            assert config.cpu_limit == 3.0
            assert config.total_count == 15
            assert config.test_duration == 120
        finally:
            os.unlink(yaml_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
