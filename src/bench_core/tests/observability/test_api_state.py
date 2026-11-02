"""Tests for ApiStateSampler: provider state counts -> raw_data/api_state_raw.csv."""
from __future__ import annotations

import csv
import time
from pathlib import Path

from bench_core.observability.api_state import ApiStateSampler


class _StubProvider:
    name = "stub"

    def __init__(self, states):
        self.states = states
        self.calls = 0

    def list_sandbox_states(self):
        self.calls += 1
        if isinstance(self.states, Exception):
            raise self.states
        return self.states


def _read_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_sample_writes_counts(tmp_path):
    path = tmp_path / "raw_data" / "api_state_raw.csv"
    sampler = ApiStateSampler(_StubProvider({"running": 3, "paused": 7, "failed": 1}), path, 2)
    assert sampler._sample() is True
    sampler.stop()
    rows = _read_rows(path)
    assert len(rows) == 1
    assert rows[0]["sandboxes_running"] == "3"
    assert rows[0]["sandboxes_paused"] == "7"
    assert rows[0]["sandboxes_other"] == "1"  # failed is neither running nor paused
    assert rows[0]["sandboxes_total"] == "11"
    assert rows[0]["timestamp"]  # wall-clock string, parseable by _ts_to_epoch


def test_no_list_api_ends_series_without_a_file(tmp_path):
    path = tmp_path / "raw_data" / "api_state_raw.csv"
    sampler = ApiStateSampler(_StubProvider(None), path, 2)
    assert sampler._sample() is False
    sampler.stop()
    assert not path.exists()


def test_poll_error_is_swallowed_and_retried(tmp_path):
    path = tmp_path / "raw_data" / "api_state_raw.csv"
    provider = _StubProvider(RuntimeError("api down"))
    sampler = ApiStateSampler(provider, path, 2)
    assert sampler._sample() is True  # transient -> keep polling
    provider.states = {"running": 2}
    assert sampler._sample() is True
    sampler.stop()
    assert [r["sandboxes_running"] for r in _read_rows(path)] == ["2"]


def test_start_stop_samples_immediately(tmp_path):
    path = tmp_path / "raw_data" / "api_state_raw.csv"
    sampler = ApiStateSampler(_StubProvider({"running": 4, "paused": 0}), path, 60)
    sampler.start()
    time.sleep(0.05)
    sampler.stop()
    assert _read_rows(path)[0]["sandboxes_running"] == "4"
