"""Phase 2: JSON file handler emits one valid JSON line per record, newline-escaped."""
from __future__ import annotations

import json
import logging

from bench_core.utils import JsonFormatter, setup_logging


def test_json_formatter_emits_valid_json_line():
    rec = logging.LogRecord(
        name="x",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="line1\nline2",
        args=(),
        exc_info=None,
    )
    line = JsonFormatter().format(rec)
    obj = json.loads(line)  # must be valid JSON
    assert obj["level"] == "WARNING"
    assert obj["logger"] == "x"
    assert "line1" in obj["msg"]
    assert "\n" not in line  # newline-escaped, one record per line


def test_setup_logging_json_file_handler(tmp_path):
    # Save root handlers so this test does not pollute other tests.
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    log_path = tmp_path / "run.log"
    try:
        setup_logging(log_path=str(log_path), json_lines=True)
        logging.getLogger("testjson").warning("hello\nworld")
        for h in root.handlers:
            h.flush()
        lines = log_path.read_text().splitlines()
        assert lines
        obj = json.loads(lines[-1])
        assert obj["msg"] == "hello\\nworld"  # newline-escaped inside the JSON string
    finally:
        # Restore the pre-test handler set (remove our file handler, keep originals).
        for h in list(root.handlers):
            if h not in handlers_before:
                root.removeHandler(h)
                h.close()
