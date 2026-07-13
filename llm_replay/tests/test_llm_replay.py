#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_llm_replay.py - Unit tests for LLM Replay Server.

Tests cover:
- Session parsing (browser-session format)
- Prebuilt response generation
- Turn inference logic
- Configuration management
- Session pool management
"""

import json
import os
import sys
import tempfile
import unittest
import shutil
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from llm_replay.models import Turn, ToolCall, SessionMetadata, PrebuiltTurn
from llm_replay.parser import BrowserSessionParser, parse_session, _iso_to_ms
from llm_replay.config import Config, create_default_config, merge_cli_args, LoggingConfig
from llm_replay.prebuilt import prebuild_turn, SessionPool


# Sample session data for testing
# Note: event.timestamp (outer) should be > message.timestamp (inner) for positive LLM duration
SAMPLE_BROWSER_SESSION = [
    {
        "type": "message",
        "timestamp": "2024-01-01T00:00:05Z",  # outer timestamp (event)
        "message": {
            "role": "assistant",
            "timestamp": "2024-01-01T00:00:00Z",  # inner timestamp (message start)
            "model": "GLM-4.7-W8A8",
            "content": [
                {"type": "text", "text": "I'll help you browse the page."},
                {
                    "type": "toolCall",
                    "id": "tool-001",
                    "name": "browser_open",
                    "arguments": {"url": "https://example.com"},
                },
            ],
            "stopReason": "toolUse",
            "responseId": "chatcmpl-001",
            "usage": {"input": 100, "output": 50, "total": 150},
        },
    },
    {
        "type": "message",
        "timestamp": "2024-01-01T00:00:35Z",  # outer timestamp
        "message": {
            "role": "assistant",
            "timestamp": "2024-01-01T00:00:30Z",  # inner timestamp
            "model": "GLM-4.7-W8A8",
            "content": [
                {
                    "type": "toolCall",
                    "id": "tool-002",
                    "name": "browser_click",
                    "arguments": {"element": "e218"},
                },
            ],
            "stopReason": "toolUse",
            "responseId": "chatcmpl-002",
            "usage": {"input": 200, "output": 30, "total": 230},
        },
    },
    {
        "type": "message",
        "timestamp": "2024-01-01T00:01:05Z",  # outer timestamp
        "message": {
            "role": "assistant",
            "timestamp": "2024-01-01T00:01:00Z",  # inner timestamp
            "model": "GLM-4.7-W8A8",
            "content": [{"type": "text", "text": "Task completed successfully."}],
            "stopReason": "endTurn",
            "responseId": "chatcmpl-003",
            "usage": {"input": 300, "output": 20, "total": 320},
        },
    },
]


class TestModels(unittest.TestCase):
    """Test data models."""

    def test_tool_call_creation(self):
        """Test ToolCall model creation."""
        tc = ToolCall(id="tool-001", name="browser_open", arguments='{"url": "https://example.com"}')
        self.assertEqual(tc.id, "tool-001")
        self.assertEqual(tc.name, "browser_open")
        self.assertIn("url", tc.arguments)

    def test_tool_call_to_openai_dict(self):
        """Test OpenAI format conversion."""
        tc = ToolCall(id="tool-001", name="browser_open", arguments='{"url": "https://example.com"}')
        result = tc.to_openai_dict()
        self.assertEqual(result["id"], "tool-001")
        self.assertEqual(result["type"], "function")
        self.assertEqual(result["function"]["name"], "browser_open")
        self.assertIn("url", result["function"]["arguments"])

    def test_turn_creation(self):
        """Test Turn model creation."""
        turn = Turn(
            index=0,
            text="Hello",
            tool_calls=[],
            llm_duration_ms=5000,
            model="GLM-4.7-W8A8",
            response_id="chatcmpl-001",
            stop_reason="toolUse",
        )
        self.assertEqual(turn.index, 0)
        self.assertEqual(turn.text, "Hello")
        self.assertEqual(turn.llm_duration_ms, 5000)
        self.assertEqual(turn.finish_reason, "tool_calls")

    def test_turn_finish_reason_mapping(self):
        """Test finish_reason mapping."""
        cases = [
            ("toolUse", "tool_calls"),
            ("endTurn", "stop"),
            ("stop", "stop"),
            ("max_tokens", "length"),
            ("unknown", "stop"),
        ]
        for stop_reason, expected in cases:
            turn = Turn(index=0, stop_reason=stop_reason)
            self.assertEqual(turn.finish_reason, expected)

    def test_turn_has_tool_calls(self):
        """Test tool_calls check."""
        turn_with_tools = Turn(
            index=0,
            tool_calls=[ToolCall(id="t1", name="test", arguments="{}")],
        )
        turn_without_tools = Turn(index=0, tool_calls=[])

        self.assertTrue(turn_with_tools.has_tool_calls)
        self.assertFalse(turn_without_tools.has_tool_calls)

    def test_turn_tool_names(self):
        """Test tool_names property."""
        turn = Turn(
            index=0,
            tool_calls=[
                ToolCall(id="t1", name="browser_open", arguments="{}"),
                ToolCall(id="t2", name="browser_click", arguments="{}"),
            ],
        )
        self.assertEqual(turn.tool_names, ["browser_open", "browser_click"])


class TestParser(unittest.TestCase):
    """Test session parsing."""

    def setUp(self):
        """Create temporary session file."""
        self.temp_dir = tempfile.mkdtemp()
        self.session_path = os.path.join(self.temp_dir, "test-session.jsonl")

        with open(self.session_path, "w", encoding="utf-8") as f:
            for event in SAMPLE_BROWSER_SESSION:
                f.write(json.dumps(event) + "\n")

    def tearDown(self):
        """Clean up temporary files."""
        shutil.rmtree(self.temp_dir)

    def test_iso_to_ms_conversion(self):
        """Test timestamp conversion."""
        # ISO string
        result = _iso_to_ms("2024-01-01T00:00:00Z")
        self.assertIsInstance(result, int)

        # Integer
        result = _iso_to_ms(1704067200000)
        self.assertEqual(result, 1704067200000)

        # None
        result = _iso_to_ms(None)
        self.assertIsNone(result)

    def test_browser_session_parser_can_parse(self):
        """Test format detection."""
        self.assertTrue(BrowserSessionParser.can_parse(self.session_path))

        # Non-existent file
        self.assertFalse(BrowserSessionParser.can_parse("/nonexistent/path.jsonl"))

        # Wrong extension
        txt_path = os.path.join(self.temp_dir, "test.txt")
        with open(txt_path, "w") as f:
            f.write("not jsonl")
        self.assertFalse(BrowserSessionParser.can_parse(txt_path))

    def test_browser_session_parser_parse(self):
        """Test session parsing."""
        turns = BrowserSessionParser.parse(self.session_path)

        self.assertEqual(len(turns), 3)

        # First turn
        self.assertEqual(turns[0].index, 0)
        self.assertEqual(turns[0].text, "I'll help you browse the page.")
        self.assertEqual(len(turns[0].tool_calls), 1)
        self.assertEqual(turns[0].tool_calls[0].name, "browser_open")
        self.assertEqual(turns[0].llm_duration_ms, 5000)
        self.assertEqual(turns[0].stop_reason, "toolUse")

        # Second turn
        self.assertEqual(turns[1].index, 1)
        self.assertIsNone(turns[1].text)
        self.assertEqual(len(turns[1].tool_calls), 1)
        self.assertEqual(turns[1].tool_calls[0].name, "browser_click")

        # Third turn (end)
        self.assertEqual(turns[2].index, 2)
        self.assertEqual(turns[2].text, "Task completed successfully.")
        self.assertEqual(len(turns[2].tool_calls), 0)
        self.assertEqual(turns[2].stop_reason, "endTurn")

    def test_parse_session_metadata(self):
        """Test metadata generation."""
        metadata = parse_session(self.session_path)

        self.assertEqual(metadata.name, "test-session")
        self.assertEqual(metadata.total_turns, 3)
        self.assertEqual(metadata.total_tool_calls, 2)
        self.assertEqual(metadata.model, "GLM-4.7-W8A8")
        self.assertTrue(metadata.is_valid)
        self.assertEqual(len(metadata.errors), 0)

    def test_parse_empty_session(self):
        """Test parsing empty file."""
        empty_path = os.path.join(self.temp_dir, "empty.jsonl")
        with open(empty_path, "w") as f:
            f.write("")

        with self.assertRaises(ValueError):
            BrowserSessionParser.parse(empty_path)


class TestPrebuilt(unittest.TestCase):
    """Test prebuilt response generation."""

    def setUp(self):
        """Create test turn."""
        self.turn = Turn(
            index=0,
            text="Hello world",
            tool_calls=[
                ToolCall(id="tool-001", name="browser_open", arguments='{"url": "https://example.com"}')
            ],
            llm_duration_ms=5000,
            model="GLM-4.7-W8A8",
            response_id="chatcmpl-001",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            stop_reason="toolUse",
        )

    def test_prebuild_turn_completion(self):
        """Test non-streaming response prebuild."""
        prebuilt = prebuild_turn(self.turn)

        self.assertEqual(prebuilt.turn, self.turn)
        self.assertEqual(prebuilt.llm_duration_ms, 5000)
        self.assertIsInstance(prebuilt.completion_json, bytes)
        self.assertGreater(len(prebuilt.completion_json), 0)

        # Verify JSON structure
        response = json.loads(prebuilt.completion_json)
        self.assertEqual(response["id"], "chatcmpl-001")
        self.assertEqual(response["object"], "chat.completion")
        self.assertEqual(response["model"], "GLM-4.7-W8A8")
        self.assertEqual(len(response["choices"]), 1)
        self.assertEqual(response["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(response["choices"][0]["finish_reason"], "tool_calls")

    def test_prebuild_turn_stream_chunks(self):
        """Test streaming response prebuild."""
        prebuilt = prebuild_turn(self.turn)

        self.assertIsInstance(prebuilt.stream_chunks_json, list)
        self.assertGreater(len(prebuilt.stream_chunks_json), 0)

        # Each chunk should be valid JSON
        for chunk_bytes in prebuilt.stream_chunks_json:
            chunk = json.loads(chunk_bytes)
            self.assertEqual(chunk["object"], "chat.completion.chunk")
            self.assertIn("choices", chunk)

    def test_prebuild_turn_without_text(self):
        """Test prebuild with only tool calls."""
        turn_no_text = Turn(
            index=0,
            text=None,
            tool_calls=[ToolCall(id="t1", name="test", arguments="{}")],
            llm_duration_ms=1000,
            model="test-model",
            response_id="test-id",
            stop_reason="toolUse",
        )

        prebuilt = prebuild_turn(turn_no_text)
        response = json.loads(prebuilt.completion_json)

        self.assertIsNone(response["choices"][0]["message"]["content"])
        self.assertEqual(len(response["choices"][0]["message"]["tool_calls"]), 1)


class TestSessionPool(unittest.TestCase):
    """Test session pool management."""

    def setUp(self):
        """Create temporary session directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.session_path = os.path.join(self.temp_dir, "test-session.jsonl")

        with open(self.session_path, "w", encoding="utf-8") as f:
            for event in SAMPLE_BROWSER_SESSION:
                f.write(json.dumps(event) + "\n")

    def tearDown(self):
        """Clean up."""
        shutil.rmtree(self.temp_dir)

    def test_session_pool_load_session(self):
        """Test single session loading."""
        pool = SessionPool()
        session = pool.load_session(self.session_path)

        self.assertEqual(len(pool), 1)
        self.assertIn("test-session", pool)
        self.assertEqual(len(session), 3)

    def test_session_pool_load_directory(self):
        """Test directory loading."""
        # Create another session
        session2_path = os.path.join(self.temp_dir, "test-session2.jsonl")
        with open(session2_path, "w", encoding="utf-8") as f:
            for event in SAMPLE_BROWSER_SESSION[:2]:
                f.write(json.dumps(event) + "\n")

        pool = SessionPool()
        pool.load_directory(self.temp_dir)

        self.assertEqual(len(pool), 2)
        self.assertIn("test-session", pool)
        self.assertIn("test-session2", pool)

    def test_session_pool_get_session(self):
        """Test session retrieval."""
        pool = SessionPool()
        pool.load_session(self.session_path)

        session = pool.get_session("test-session")
        self.assertIsNotNone(session)
        self.assertEqual(len(session), 3)

        # Non-existent session
        session = pool.get_session("nonexistent")
        self.assertIsNone(session)

    def test_session_pool_get_turn(self):
        """Test turn retrieval by index."""
        pool = SessionPool()
        pool.load_session(self.session_path)

        session = pool.get_session("test-session")

        turn0 = session.get_turn(0)
        self.assertIsNotNone(turn0)
        self.assertEqual(turn0.turn.index, 0)

        turn_invalid = session.get_turn(999)
        self.assertIsNone(turn_invalid)

    def test_session_pool_summary(self):
        """Test summary generation."""
        pool = SessionPool()
        pool.load_session(self.session_path)

        summary = pool.summary()
        # Summary contains counts, not session names
        self.assertIn("1 sessions", summary)
        self.assertIn("3 total turns", summary)

    def test_session_pool_list_names(self):
        """Test session name listing."""
        pool = SessionPool()
        pool.load_session(self.session_path)

        names = pool.list_session_names()
        self.assertEqual(names, ["test-session"])


class TestConfig(unittest.TestCase):
    """Test configuration management."""

    def test_create_default_config(self):
        """Test default config creation."""
        config = create_default_config()

        self.assertEqual(config.server.host, "0.0.0.0")
        self.assertEqual(config.server.port, 5199)
        self.assertEqual(config.timing.scale, 1.0)
        self.assertEqual(config.sessions.directory, "./sessions")
        self.assertTrue(config.stats.enabled)

    def test_merge_cli_args(self):
        """Test CLI args merging."""
        config = create_default_config()

        # Override server
        config = merge_cli_args(config, host="127.0.0.1", port=8000)
        self.assertEqual(config.server.host, "127.0.0.1")
        self.assertEqual(config.server.port, 8000)

        # Override timing
        config = merge_cli_args(config, scale=0.5, no_sleep=True)
        self.assertEqual(config.timing.scale, 0.5)
        self.assertTrue(config.timing.no_sleep)

        # Override sessions
        config = merge_cli_args(config, sessions_dir="/custom/path")
        self.assertEqual(config.sessions.directory, "/custom/path")

    def test_config_from_yaml(self):
        """Test YAML loading."""
        # Create temporary YAML file
        temp_dir = tempfile.mkdtemp()
        yaml_path = os.path.join(temp_dir, "config.yaml")

        yaml_content = """
server:
  host: "192.168.1.1"
  port: 9999

timing:
  scale: 0.1
  no_sleep: true

sessions:
  directory: "/test/sessions"
"""
        with open(yaml_path, "w") as f:
            f.write(yaml_content)

        config = Config.from_yaml(yaml_path)
        self.assertEqual(config.server.host, "192.168.1.1")
        self.assertEqual(config.server.port, 9999)
        self.assertEqual(config.timing.scale, 0.1)
        self.assertTrue(config.timing.no_sleep)
        self.assertEqual(config.sessions.directory, "/test/sessions")

        # Cleanup
        shutil.rmtree(temp_dir)

    def test_config_to_yaml(self):
        """Test YAML saving."""
        temp_dir = tempfile.mkdtemp()
        yaml_path = os.path.join(temp_dir, "output.yaml")

        config = create_default_config()
        config.server.port = 12345
        config.timing.scale = 0.2

        config.to_yaml(yaml_path)

        # Read back
        config2 = Config.from_yaml(yaml_path)
        self.assertEqual(config2.server.port, 12345)
        self.assertEqual(config2.timing.scale, 0.2)

        # Cleanup
        shutil.rmtree(temp_dir)

    def test_logging_config(self):
        """Test logging config."""
        log_config = LoggingConfig(
            level="DEBUG",
            file_enabled=True,
            file_path="/var/log/test.log",
        )
        self.assertEqual(log_config.level, "DEBUG")
        self.assertTrue(log_config.file_enabled)


class TestTurnInference(unittest.TestCase):
    """Test turn inference logic."""

    def test_assistant_count_inference(self):
        """Test turn index inference from assistant count."""
        # Scenario: 2 assistant messages completed, requesting turn 2
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},  # turn 0 done
            {"role": "user", "content": "Open browser"},
            {"role": "assistant", "tool_calls": [...]},  # turn 1 done
            {"role": "tool", "content": "..."},
            {"role": "user", "content": "Click"},
        ]

        assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
        turn_index = assistant_count

        self.assertEqual(turn_index, 2)  # Requesting turn 2

    def test_assistant_count_empty_messages(self):
        """Test inference with empty messages."""
        messages = []
        assistant_count = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")
        self.assertEqual(assistant_count, 0)


class TestIntegration(unittest.TestCase):
    """Integration tests."""

    def setUp(self):
        """Create full test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.session_path = os.path.join(self.temp_dir, "integration-test.jsonl")

        with open(self.session_path, "w", encoding="utf-8") as f:
            for event in SAMPLE_BROWSER_SESSION:
                f.write(json.dumps(event) + "\n")

    def tearDown(self):
        """Cleanup."""
        shutil.rmtree(self.temp_dir)

    def test_full_workflow(self):
        """Test complete workflow: load -> prebuild -> serve."""
        # Load session
        pool = SessionPool()
        session = pool.load_session(self.session_path)

        # Verify loaded
        self.assertEqual(len(session), 3)

        # Get turn 0
        turn0 = session.get_turn(0)
        self.assertIsNotNone(turn0)

        # Verify prebuilt response
        response_json = json.loads(turn0.completion_json)
        self.assertEqual(response_json["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(response_json["choices"][0]["message"]["content"], "I'll help you browse the page.")

        # Get turn 2 (end)
        turn2 = session.get_turn(2)
        self.assertIsNotNone(turn2)

        response_json = json.loads(turn2.completion_json)
        self.assertEqual(response_json["choices"][0]["finish_reason"], "stop")
        self.assertEqual(response_json["choices"][0]["message"]["content"], "Task completed successfully.")

    def test_out_of_range_turn(self):
        """Test handling out-of-range turn request."""
        pool = SessionPool()
        session = pool.load_session(self.session_path)

        # Request turn beyond session length
        turn_invalid = session.get_turn(999)
        self.assertIsNone(turn_invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)