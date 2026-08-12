"""Tests for the host-agnostic percentile / formatting helpers."""
from __future__ import annotations

from bench_core.utils import (
    calc_p99,
    calc_percentiles,
    calc_tail_ratio,
    classify_tail_latency,
    format_duration,
    format_latency_distribution,
)


class TestCalcPercentiles:
    def test_empty(self):
        s = calc_percentiles([])
        assert s == {"min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}

    def test_basic(self):
        s = calc_percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
        assert s["min"] == 1.0
        assert s["max"] == 5.0
        assert s["avg"] == 3.0
        assert s["p50"] == 3.0


class TestCalcP99:
    def test_empty(self):
        assert calc_p99([]) == 0.0

    def test_small_sample_returns_max(self):
        # <100 samples: P99 falls back to the largest value.
        assert calc_p99([1.0, 2.0, 3.0]) == 3.0


class TestTailRatio:
    def test_insufficient_data(self):
        assert calc_tail_ratio([1.0, 2.0]) == 1.0

    def test_flat_distribution(self):
        assert calc_tail_ratio([1.0] * 10) == 1.0


class TestClassifyTailLatency:
    def test_thresholds(self):
        assert classify_tail_latency(1.1) == "minimal"
        assert classify_tail_latency(1.3) == "moderate"
        assert classify_tail_latency(2.0) == "significant"


class TestFormatDuration:
    def test_seconds(self):
        assert format_duration(12.5) == "12.5s"

    def test_minutes(self):
        assert format_duration(125) == "2m 5s"

    def test_hours(self):
        assert format_duration(3720) == "1h 2m"


class TestFormatLatencyDistribution:
    def test_empty(self):
        assert format_latency_distribution([]) == "no data"

    def test_values_ms(self):
        out = format_latency_distribution([1.0] * 10, unit="ms")
        assert "P50=" in out
        assert "tail=" in out
        assert "ms" in out
