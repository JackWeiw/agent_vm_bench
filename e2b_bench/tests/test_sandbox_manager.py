"""
Test SandboxManager Module

Tests for sandbox creation, detection, and ID file filtering
"""

import pytest
import tempfile
import os
from unittest.mock import Mock, patch, MagicMock
from threading import Event

from e2b_bench.sandbox_manager import SandboxManager
from e2b_bench.config import Config
from e2b_bench.schemas import SandboxStatus


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

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
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

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
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

    @patch('e2b_bench.sandbox_manager.Sandbox.list')
    @patch('e2b_bench.sandbox_manager.Sandbox.connect')
    def test_matches_ids_from_file(self, mock_connect, mock_list):
        """Only sandboxes in file are connected"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file with 2 IDs
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("sbx_id_1\nsbx_id_2\n")
            f.flush()
            temp_path = f.name

        # Mock Sandbox.list() to return 3 running sandboxes
        mock_paginator = self._make_paginator([
            [Mock(sandbox_id="sbx_id_1"), Mock(sandbox_id="sbx_id_2"), Mock(sandbox_id="sbx_id_3")]
        ])
        mock_list.return_value = mock_paginator

        # Mock Sandbox.connect() to return mock sandbox
        mock_connect.return_value = self._create_mock_sandbox("connected")

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        # Should only connect sbx_id_1 and sbx_id_2 (2 sandboxes)
        assert len(result) == 2
        assert mock_connect.call_count == 2

    @patch('e2b_bench.sandbox_manager.Sandbox.list')
    @patch('e2b_bench.sandbox_manager.Sandbox.connect')
    def test_ids_not_running_shown_as_warning(self, mock_connect, mock_list):
        """IDs in file but not running should be warned"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file with 3 IDs
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("sbx_id_1\nsbx_id_2\nsbx_missing\n")
            f.flush()
            temp_path = f.name

        # Mock Sandbox.list() to return only 2 running sandboxes
        mock_paginator = self._make_paginator([
            [Mock(sandbox_id="sbx_id_1"), Mock(sandbox_id="sbx_id_2")]
        ])
        mock_list.return_value = mock_paginator

        mock_connect.return_value = self._create_mock_sandbox("connected")

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        # sbx_missing should be warned, not connected
        assert len(result) == 2
        assert mock_connect.call_count == 2

    @patch('e2b_bench.sandbox_manager.Sandbox.list')
    def test_no_matching_sandboxes_returns_empty(self, mock_list):
        """No matches returns empty dict"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("sbx_not_running_1\nsbx_not_running_2\n")
            f.flush()
            temp_path = f.name

        # Mock Sandbox.list() to return different sandboxes
        mock_paginator = self._make_paginator([
            [Mock(sandbox_id="sbx_other_1"), Mock(sandbox_id="sbx_other_2")]
        ])
        mock_list.return_value = mock_paginator

        result = manager.detect_from_file(temp_path)
        os.unlink(temp_path)

        assert result == {}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])