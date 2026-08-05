"""
Test SandboxManager Module

Tests for sandbox creation, detection, and ID file filtering
"""

import os
import tempfile
from threading import Event
from unittest.mock import Mock, patch, call

import pytest

from e2b_bench.config import Config
from e2b_bench.sandbox_manager import SandboxManager
from e2b_bench.schemas import SandboxState, SandboxStatus


class TestDetectFromFile:
    """Tests for detect_from_file method"""

    def _create_mock_sandbox(self, sandbox_id):
        """Helper to create mock sandbox object"""
        mock = Mock()
        mock.sandbox_id = sandbox_id
        mock.commands = Mock()
        mock.commands.run = Mock(return_value=Mock(exit_code=0, stdout="LISTENING"))
        return mock

    def test_file_not_found_raises_error(self):
        """File not found should raise FileNotFoundError"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        with pytest.raises(FileNotFoundError):
            manager.detect_from_file("nonexistent_file.txt")

    def test_empty_file_returns_empty_dict(self):
        """Empty file returns empty dict with warning"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("")  # Empty file
            f.flush()
            temp_path = f.name

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        assert result == {}

    def test_file_with_whitespace_only_returns_empty(self):
        """File with only whitespace/empty lines returns empty dict"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("\n  \n\n")  # Only whitespace
            f.flush()
            temp_path = f.name

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        assert result == {}

    def _make_paginator(self, items_list):
        """Helper to create a mock paginator with proper has_next property"""
        mock_paginator = Mock()
        # Set up next_items to return the list on first call, empty on subsequent
        call_count = [0]

        def next_items_side_effect():
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(items_list):
                return items_list[idx]
            return []

        mock_paginator.next_items = Mock(side_effect=next_items_side_effect)

        # Set up has_next as a property that changes based on call count
        has_next_count = [0]

        def get_has_next():
            idx = has_next_count[0]
            has_next_count[0] += 1
            return idx < len(items_list)

        type(mock_paginator).has_next = property(lambda self: get_has_next())

        return mock_paginator

    @patch("e2b_bench.sandbox_manager.Sandbox.list")
    @patch("e2b_bench.sandbox_manager.Sandbox.connect")
    def test_matches_ids_from_file(self, mock_connect, mock_list):
        """Only sandboxes in file are connected"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file with 2 IDs
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("sbx_id_1\nsbx_id_2\n")
            f.flush()
            temp_path = f.name

        # Mock Sandbox.list() to return 3 running sandboxes
        mock_paginator = self._make_paginator(
            [[Mock(sandbox_id="sbx_id_1"), Mock(sandbox_id="sbx_id_2"), Mock(sandbox_id="sbx_id_3")]]
        )
        mock_list.return_value = mock_paginator

        # Mock Sandbox.connect() to return mock sandbox
        mock_connect.return_value = self._create_mock_sandbox("connected")

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        # Should only connect sbx_id_1 and sbx_id_2 (2 sandboxes)
        assert len(result) == 2
        assert mock_connect.call_count == 2

    @patch("e2b_bench.sandbox_manager.Sandbox.list")
    @patch("e2b_bench.sandbox_manager.Sandbox.connect")
    def test_ids_not_running_shown_as_warning(self, mock_connect, mock_list):
        """IDs in file but not running should be warned"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file with 3 IDs
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("sbx_id_1\nsbx_id_2\nsbx_missing\n")
            f.flush()
            temp_path = f.name

        # Mock Sandbox.list() to return only 2 running sandboxes
        mock_paginator = self._make_paginator([[Mock(sandbox_id="sbx_id_1"), Mock(sandbox_id="sbx_id_2")]])
        mock_list.return_value = mock_paginator

        mock_connect.return_value = self._create_mock_sandbox("connected")

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        # sbx_missing should be warned, not connected
        assert len(result) == 2
        assert mock_connect.call_count == 2

    @patch("e2b_bench.sandbox_manager.Sandbox.list")
    def test_no_matching_sandboxes_returns_empty(self, mock_list):
        """No matches returns empty dict"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("sbx_not_running_1\nsbx_not_running_2\n")
            f.flush()
            temp_path = f.name

        # Mock Sandbox.list() to return different sandboxes
        mock_paginator = self._make_paginator([[Mock(sandbox_id="sbx_other_1"), Mock(sandbox_id="sbx_other_2")]])
        mock_list.return_value = mock_paginator

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        assert result == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestNumaBinding:
    """Tests for NUMA binding during sandbox creation"""

    def test_create_single_with_numa_bind(self):
        """Sandbox.create is called with envs containing FC_BIND when numa_bind is set"""
        config = Config(numa_bind=2)
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)

        with patch("e2b_bench.sandbox_manager.Sandbox.create") as mock_create:
            # Mock Sandbox.create to return a sandbox object
            mock_sandbox = Mock()
            mock_sandbox.sandbox_id = "test_sandbox"
            mock_create.return_value = mock_sandbox

            result = manager._create_single(state)

            # Verify Sandbox.create was called with correct envs
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args

            # Check envs parameter
            assert "envs" in kwargs
            assert kwargs["envs"] == {"FC_BIND": "2"}

            # Verify result
            assert result["success"] is True
            assert state.sandbox_obj == mock_sandbox
            assert state.creation_metrics.status == SandboxStatus.CREATED

    def test_create_single_with_custom_numa_bind(self):
        """Sandbox.create uses custom numa_bind value"""
        config = Config(numa_bind=5)
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)

        with patch("e2b_bench.sandbox_manager.Sandbox.create") as mock_create:
            mock_sandbox = Mock()
            mock_create.return_value = mock_sandbox

            result = manager._create_single(state)

            # Verify correct NUMA node is passed
            args, kwargs = mock_create.call_args
            assert kwargs["envs"] == {"FC_BIND": "5"}
            assert result["success"] is True

    def test_create_single_without_numa_bind(self):
        """Sandbox.create is called without envs when numa_bind is None"""
        config = Config(numa_bind=None)
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)

        with patch("e2b_bench.sandbox_manager.Sandbox.create") as mock_create:
            mock_sandbox = Mock()
            mock_create.return_value = mock_sandbox

            result = manager._create_single(state)

            # Verify Sandbox.create was called with envs=None (or omitted)
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args

            # envs should be None when numa_bind is None
            assert kwargs.get("envs") is None or "envs" not in kwargs

            assert result["success"] is True

    def test_create_single_handles_exception(self):
        """_create_single handles exceptions and returns error"""
        config = Config(numa_bind=2)
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)

        with patch("e2b_bench.sandbox_manager.Sandbox.create") as mock_create:
            mock_create.side_effect = Exception("Connection failed")

            result = manager._create_single(state)

            assert result["success"] is False
            assert "Connection failed" in result["error"]
            assert state.creation_metrics.status == SandboxStatus.CREATING


class TestCheckReady:
    """Tests for _check_ready method with workflow-specific behavior"""

    def test_check_ready_coding_workflow(self):
        """Coding workflow uses _check_command_ready"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="Linux sandbox 5.15.0")
        state.sandbox_obj = mock_sandbox

        result = manager._check_ready(state)

        assert result["success"] is True
        assert result["wait_elapsed"] >= 0
        mock_sandbox.commands.run.assert_called_with("uname -a", timeout=10, user="root")

    def test_check_ready_browser_workflow(self):
        """Browser workflow uses _check_ports"""
        config = Config(workflow_type="browser")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        # Mock successful port check for both ports
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="LISTEN 0 0 0.0.0.0:18789")
        state.sandbox_obj = mock_sandbox

        result = manager._check_ready(state)

        assert result["success"] is True
        assert result["wait_elapsed"] >= 0

    def test_check_ready_default_workflow_is_browser(self):
        """Default workflow (unspecified) uses _check_ports"""
        config = Config()  # Default workflow_type is "browser"
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="LISTEN 0 0 0.0.0.0:18789")
        state.sandbox_obj = mock_sandbox

        result = manager._check_ready(state)

        assert result["success"] is True


class TestCheckCommandReady:
    """Tests for _check_command_ready method (coding workflow)"""

    def test_command_ready_success_immediate(self):
        """Sandbox is ready when uname -a returns immediately"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="Linux sandbox 5.15.0")
        state.sandbox_obj = mock_sandbox

        result = manager._check_command_ready(state)

        assert result["success"] is True
        assert result["error"] == ""

    def test_command_ready_empty_stdout_fails(self):
        """Empty stdout should fail the check (after timeout)"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="")
        state.sandbox_obj = mock_sandbox

        # Patch READY_CHECK_MAX_WAIT to make test fast
        with patch("e2b_bench.sandbox_manager.READY_CHECK_MAX_WAIT", 0.1), patch(
            "e2b_bench.sandbox_manager.READY_CHECK_INTERVAL", 0.05
        ):
            result = manager._check_command_ready(state)

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_command_ready_nonzero_exit_fails(self):
        """Non-zero exit code should fail the check (after timeout)"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        mock_sandbox.commands.run.return_value = Mock(exit_code=1, stdout="")
        state.sandbox_obj = mock_sandbox

        with patch("e2b_bench.sandbox_manager.READY_CHECK_MAX_WAIT", 0.1), patch(
            "e2b_bench.sandbox_manager.READY_CHECK_INTERVAL", 0.05
        ):
            result = manager._check_command_ready(state)

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_command_ready_no_sandbox_handle(self):
        """No sandbox handle should return failure"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        state.sandbox_obj = None

        result = manager._check_command_ready(state)

        assert result["success"] is False
        assert "No sandbox handle" in result["error"]

    def test_command_ready_stop_event_interrupts(self):
        """Stop event should interrupt the check"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        stop_event.set()  # Already stopped
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        state.sandbox_obj = mock_sandbox

        result = manager._check_command_ready(state)

        assert result["success"] is False
        assert "Stop event" in result["error"]

    def test_command_ready_exception_continues_waiting(self):
        """Exception during command execution should not immediately fail"""
        config = Config(workflow_type="coding")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        # First call raises exception, second succeeds
        mock_sandbox.commands.run.side_effect = [
            Exception("Connection error"),
            Mock(exit_code=0, stdout="Linux sandbox 5.15.0"),
        ]
        state.sandbox_obj = mock_sandbox

        result = manager._check_command_ready(state)

        # Should succeed after retry
        assert result["success"] is True


class TestCheckPorts:
    """Tests for _check_ports method (browser workflow)"""

    def test_ports_ready_both_ports_listening(self):
        """Both ports listening should succeed"""
        config = Config(workflow_type="browser")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        # Both ports are listening
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="LISTEN 0 0 0.0.0.0:18789")
        state.sandbox_obj = mock_sandbox

        result = manager._check_ports(state)

        assert result["success"] is True
        assert result["error"] == ""

    def test_ports_ready_missing_port_fails(self):
        """Missing port should fail the check (after timeout)"""
        config = Config(workflow_type="browser")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        # Port not listening
        mock_sandbox.commands.run.return_value = Mock(exit_code=0, stdout="PORT_NOT_LISTENING")
        state.sandbox_obj = mock_sandbox

        with patch("e2b_bench.sandbox_manager.READY_CHECK_MAX_WAIT", 0.1), patch(
            "e2b_bench.sandbox_manager.READY_CHECK_INTERVAL", 0.05
        ):
            result = manager._check_ports(state)

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_ports_ready_no_sandbox_handle(self):
        """No sandbox handle should return failure"""
        config = Config(workflow_type="browser")
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        state.sandbox_obj = None

        result = manager._check_ports(state)

        assert result["success"] is False
        assert "No sandbox handle" in result["error"]

    def test_ports_ready_stop_event_interrupts(self):
        """Stop event should interrupt the check"""
        config = Config(workflow_type="browser")
        stop_event = Event()
        stop_event.set()  # Already stopped
        manager = SandboxManager(config, stop_event)

        state = SandboxState(sandbox_id=1)
        mock_sandbox = Mock()
        state.sandbox_obj = mock_sandbox

        result = manager._check_ports(state)

        assert result["success"] is False
        assert "Stop event" in result["error"]


class TestMultiNumaBinding:
    """Tests for multi-NUMA round-robin binding during sandbox creation"""

    def _create_single_and_capture_envs(self, config, sandbox_id):
        """Helper: run _create_single on a sandbox_id, return the envs passed to Sandbox.create"""
        manager = SandboxManager(config, Event())
        state = SandboxState(sandbox_id=sandbox_id)
        with patch("e2b_bench.sandbox_manager.Sandbox.create") as mock_create:
            mock_sandbox = Mock()
            mock_sandbox.sandbox_id = f"sbx_{sandbox_id}"
            mock_create.return_value = mock_sandbox
            manager._create_single(state)
            _, kwargs = mock_create.call_args
            return kwargs.get("envs")

    def test_two_nodes_round_robin_20_sandboxes(self):
        """20 sandboxes on [2,3]: odd IDs -> node 2, even IDs -> node 3"""
        config = Config(numa_bind=[2, 3])
        for sandbox_id in range(1, 21):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            expected_node = 2 if sandbox_id % 2 == 1 else 3
            assert envs == {
                "FC_BIND": str(expected_node)
            }, f"sandbox_id={sandbox_id} expected FC_BIND={expected_node}, got {envs}"

    def test_two_nodes_21_sandboxes_remainder_to_first(self):
        """21 sandboxes on [2,3]: 11 on node 2, 10 on node 3"""
        config = Config(numa_bind=[2, 3])
        node_counts = {2: 0, 3: 0}
        for sandbox_id in range(1, 22):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            # 0-based index = sandbox_id - 1; even index -> node 2, odd -> node 3
            index = sandbox_id - 1
            expected_node = 2 if index % 2 == 0 else 3
            assert envs == {"FC_BIND": str(expected_node)}
            node_counts[expected_node] += 1
        assert node_counts == {2: 11, 3: 10}

    def test_single_node_list_equivalent_to_int(self):
        """[2] produces FC_BIND=2 for every sandbox (same as old numa_bind=2)"""
        config = Config(numa_bind=[2])
        for sandbox_id in [1, 2, 5, 20]:
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            assert envs == {"FC_BIND": "2"}

    def test_three_nodes_round_robin(self):
        """[2, 3, 5] round-robins across three nodes"""
        config = Config(numa_bind=[2, 3, 5])
        nodes = [2, 3, 5]
        for sandbox_id in range(1, 10):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            expected_node = nodes[(sandbox_id - 1) % 3]
            assert envs == {"FC_BIND": str(expected_node)}

    def test_none_numa_bind_no_envs(self):
        """numa_bind=None produces envs=None for every sandbox"""
        config = Config(numa_bind=None)
        envs = self._create_single_and_capture_envs(config, sandbox_id=1)
        assert envs is None

    def test_nodes_including_zero_round_robin(self):
        """[0, 1] round-robins across nodes 0 and 1 (node 0 is valid)"""
        config = Config(numa_bind=[0, 1])
        for sandbox_id in range(1, 21):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            index = sandbox_id - 1
            expected_node = 0 if index % 2 == 0 else 1
            assert envs == {
                "FC_BIND": str(expected_node)
            }, f"sandbox_id={sandbox_id} expected FC_BIND={expected_node}, got {envs}"
