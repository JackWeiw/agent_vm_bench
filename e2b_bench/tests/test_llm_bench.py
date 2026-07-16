"""
Unit tests for LLM benchmark functionality

Tests for:
- LLMConfig configuration
- LLMMetrics metrics tracking
- ScenarioLoader scenario loading
- LLM health check
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from e2b_bench.config import Config, LLMConfig
from e2b_bench.schemas import LLMMetrics, SandboxState, SandboxStatus
from e2b_bench.scenario_loader import (
    ScenarioConfig,
    ScenarioDefinition,
    find_scenario_file,
    load_scenarios,
)


class TestLLMConfig:
    """Tests for LLMConfig dataclass"""

    def test_default_values(self):
        """Test default values are set correctly"""
        config = LLMConfig()
        assert config.enabled is False
        assert config.endpoint == ""
        assert config.model == ""
        assert config.timeout == 600
        assert config.request_timeout == 30
        assert config.health_check is True
        assert config.scenario_file == ""
        assert config.interval_min == 5.0
        assert config.interval_max == 15.0

    def test_custom_values(self):
        """Test custom values are set correctly"""
        config = LLMConfig(
            enabled=True,
            endpoint="http://localhost:5199/v1",
            model="test-scenario",
            timeout=300,
            request_timeout=60,
            health_check=False,
            scenario_file="/custom/path/scenarios.yaml",
            interval_min=2.0,
            interval_max=10.0,
        )
        assert config.enabled is True
        assert config.endpoint == "http://localhost:5199/v1"
        assert config.model == "test-scenario"
        assert config.timeout == 300
        assert config.request_timeout == 60
        assert config.health_check is False
        assert config.scenario_file == "/custom/path/scenarios.yaml"
        assert config.interval_min == 2.0
        assert config.interval_max == 10.0

    def test_config_integration(self):
        """Test LLMConfig integration with main Config"""
        config = Config(
            task_mode="llm",
            llm=LLMConfig(
                enabled=True,
                endpoint="http://test:5199/v1",
                model="test-scenario",
            ),
        )
        assert config.task_mode == "llm"
        assert config.llm.enabled is True
        assert config.llm.endpoint == "http://test:5199/v1"


class TestLLMMetrics:
    """Tests for LLMMetrics dataclass"""

    def test_default_values(self):
        """Test default values"""
        metrics = LLMMetrics()
        assert metrics.total_scenarios == 0
        assert metrics.success_count == 0
        assert metrics.failed_count == 0
        assert metrics.timeout_count == 0
        assert metrics.latencies == []
        assert metrics.last_error == ""

    def test_add_success_result(self):
        """Test adding successful result"""
        metrics = LLMMetrics()
        metrics.add_result(1.5, success=True)
        metrics.add_result(2.0, success=True)

        assert metrics.total_scenarios == 2
        assert metrics.success_count == 2
        assert metrics.failed_count == 0
        assert metrics.timeout_count == 0
        assert len(metrics.latencies) == 2
        assert metrics.avg_latency == 1.75
        assert metrics.p99_latency == 2.0

    def test_add_failed_result(self):
        """Test adding failed result"""
        metrics = LLMMetrics()
        metrics.add_result(1.0, success=False)

        assert metrics.total_scenarios == 1
        assert metrics.success_count == 0
        assert metrics.failed_count == 1
        assert len(metrics.latencies) == 0

    def test_add_timeout_result(self):
        """Test adding timeout result"""
        metrics = LLMMetrics()
        metrics.add_result(10.0, success=False, timeout=True)

        assert metrics.total_scenarios == 1
        assert metrics.success_count == 0
        assert metrics.failed_count == 1
        assert metrics.timeout_count == 1

    def test_avg_latency_empty(self):
        """Test average latency with no data"""
        metrics = LLMMetrics()
        assert metrics.avg_latency == 0.0

    def test_p99_latency_calculation(self):
        """Test P99 latency calculation"""
        metrics = LLMMetrics()
        # Add 100 values
        for i in range(100):
            metrics.add_result(float(i), success=True)

        # P99 should be around 99th percentile
        assert metrics.p99_latency == pytest.approx(99.0, rel=0.01)

    def test_p99_latency_small_sample(self):
        """Test P99 latency with small sample"""
        metrics = LLMMetrics()
        metrics.add_result(1.0, success=True)
        metrics.add_result(2.0, success=True)
        metrics.add_result(3.0, success=True)

        # With small sample, P99 is the max
        assert metrics.p99_latency == 3.0


class TestScenarioLoader:
    """Tests for scenario configuration loading"""

    def test_scenario_definition(self):
        """Test ScenarioDefinition"""
        definition = ScenarioDefinition(prompt="Test prompt")
        assert definition.prompt == "Test prompt"

    def test_scenario_config_get_prompt(self):
        """Test ScenarioConfig.get_prompt"""
        config = ScenarioConfig(
            scenarios={
                "scenario-1": ScenarioDefinition(prompt="Prompt 1"),
                "scenario-2": ScenarioDefinition(prompt="Prompt 2"),
            }
        )
        assert config.get_prompt("scenario-1") == "Prompt 1"
        assert config.get_prompt("scenario-2") == "Prompt 2"

    def test_scenario_config_get_prompt_not_found(self):
        """Test ScenarioConfig.get_prompt with invalid scenario"""
        config = ScenarioConfig(scenarios={})
        with pytest.raises(ValueError, match="not found"):
            config.get_prompt("invalid")

    def test_scenario_config_get_default(self):
        """Test ScenarioConfig.get_default_scenario"""
        config = ScenarioConfig(
            scenarios={
                "scenario-1": ScenarioDefinition(prompt="Prompt 1"),
            },
            default="scenario-1"
        )
        assert config.get_default_scenario() == "scenario-1"

    def test_scenario_config_get_default_no_explicit(self):
        """Test ScenarioConfig.get_default_scenario without explicit default"""
        config = ScenarioConfig(
            scenarios={
                "scenario-1": ScenarioDefinition(prompt="Prompt 1"),
            }
        )
        assert config.get_default_scenario() == "scenario-1"

    def test_scenario_config_list_scenarios(self):
        """Test ScenarioConfig.list_scenarios"""
        config = ScenarioConfig(
            scenarios={
                "scenario-1": ScenarioDefinition(prompt="Prompt 1"),
                "scenario-2": ScenarioDefinition(prompt="Prompt 2"),
            }
        )
        scenarios = config.list_scenarios()
        assert len(scenarios) == 2
        assert "scenario-1" in scenarios
        assert "scenario-2" in scenarios

    def test_load_scenarios_from_file(self):
        """Test loading scenarios from YAML file"""
        # Create temporary YAML file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
scenarios:
  test-scenario-1:
    prompt: "Test prompt 1"
  test-scenario-2:
    prompt: "Test prompt 2"
default: "test-scenario-1"
""")
            temp_path = f.name

        try:
            config = load_scenarios(temp_path)
            assert len(config.scenarios) == 2
            assert config.get_prompt("test-scenario-1") == "Test prompt 1"
            assert config.get_prompt("test-scenario-2") == "Test prompt 2"
            assert config.default == "test-scenario-1"
        finally:
            os.unlink(temp_path)

    def test_load_scenarios_file_not_found(self):
        """Test loading scenarios from non-existent file"""
        with pytest.raises(FileNotFoundError):
            load_scenarios("/nonexistent/path/scenarios.yaml")

    def test_load_scenarios_simple_format(self):
        """Test loading scenarios with simple format (prompt only)"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
scenarios:
  simple-scenario: "Simple prompt"
""")
            temp_path = f.name

        try:
            config = load_scenarios(temp_path)
            assert config.get_prompt("simple-scenario") == "Simple prompt"
        finally:
            os.unlink(temp_path)


class TestSandboxStateLLMMetrics:
    """Tests for SandboxState with LLM metrics"""

    def test_sandbox_state_has_llm_metrics(self):
        """Test that SandboxState has llm_metrics field"""
        state = SandboxState(sandbox_id=1)
        assert hasattr(state, 'llm_metrics')
        assert isinstance(state.llm_metrics, LLMMetrics)

    def test_sandbox_state_llm_metrics_tracking(self):
        """Test LLM metrics tracking in SandboxState"""
        state = SandboxState(sandbox_id=1)
        state.llm_metrics.add_result(1.5, success=True)
        state.llm_metrics.add_result(2.0, success=True)

        assert state.llm_metrics.total_scenarios == 2
        assert state.llm_metrics.success_count == 2


class TestConfigFromDict:
    """Tests for Config._from_dict with LLM config"""

    def test_from_dict_with_llm_config(self):
        """Test loading Config from dict with LLM config"""
        data = {
            "task_mode": "llm",
            "llm": {
                "enabled": True,
                "endpoint": "http://localhost:5199/v1",
                "model": "test-scenario",
                "timeout": 300,
            }
        }
        config = Config._from_dict(data)

        assert config.task_mode == "llm"
        assert config.llm.enabled is True
        assert config.llm.endpoint == "http://localhost:5199/v1"
        assert config.llm.model == "test-scenario"
        assert config.llm.timeout == 300

    def test_from_dict_without_llm_config(self):
        """Test loading Config from dict without LLM config"""
        data = {}
        config = Config._from_dict(data)

        assert config.task_mode == "browser"  # default
        assert config.llm.enabled is False  # default


class TestHealthCheck:
    """Tests for MockLLM health check"""

    @mock.patch('requests.get')
    def test_check_mockllm_health_success(self, mock_get):
        """Test successful health check"""
        from e2b_bench.llm_runner import check_mockllm_health

        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        result = check_mockllm_health("http://localhost:5199/v1")
        assert result is True
        mock_get.assert_called_once()

    @mock.patch('requests.get')
    def test_check_mockllm_health_failure(self, mock_get):
        """Test failed health check"""
        from e2b_bench.llm_runner import check_mockllm_health

        mock_response = mock.Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        result = check_mockllm_health("http://localhost:5199/v1")
        assert result is False

    @mock.patch('requests.get')
    def test_check_mockllm_health_exception(self, mock_get):
        """Test health check with exception"""
        from e2b_bench.llm_runner import check_mockllm_health

        mock_get.side_effect = Exception("Connection refused")

        result = check_mockllm_health("http://localhost:5199/v1")
        assert result is False


class TestGatewayRequest:
    """Tests for Gateway HTTP request building"""

    def test_build_gateway_request(self):
        """Test Gateway request command building"""
        from e2b_bench.llm_runner import LLMScenarioRunner

        # Create mock objects
        config = Config(
            llm=LLMConfig(
                model="test-scenario",
                request_timeout=30,
            )
        )
        state = SandboxState(sandbox_id=1)
        stop_event = mock.Mock()

        runner = LLMScenarioRunner(state, config, "Test prompt", stop_event)
        cmd = runner._build_gateway_request()

        # Verify command structure
        assert "curl" in cmd
        assert "127.0.0.1:18789/v1/chat/completions" in cmd
        assert "test-token-123" in cmd
        assert "test-scenario" in cmd
        assert "Test prompt" in cmd
        assert "--max-time 30" in cmd

    def test_build_gateway_request_escapes_quotes(self):
        """Test Gateway request escapes quotes in prompt"""
        from e2b_bench.llm_runner import LLMScenarioRunner

        config = Config(llm=LLMConfig(model="test-scenario"))
        state = SandboxState(sandbox_id=1)
        stop_event = mock.Mock()

        runner = LLMScenarioRunner(state, config, "It's a test", stop_event)
        cmd = runner._build_gateway_request()

        # Single quotes should be escaped
        assert "'\\''" in cmd