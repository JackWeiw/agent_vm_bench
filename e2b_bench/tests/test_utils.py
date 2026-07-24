"""
Test Utils Module

Tests for utility functions: format_timestamp, format_duration, calc_percentiles, calc_p99,
tail latency analysis functions
"""

import pytest

from e2b_bench.utils import (
    calc_p99,
    calc_percentiles,
    calc_tail_ratio,
    classify_tail_latency,
    format_duration,
    format_latency_distribution,
    format_timestamp,
)


class TestFormatTimestamp:
    """Tests for format_timestamp"""

    def test_format_returns_hhmmss(self):
        """Result should be HH:MM:SS format"""
        ts = 1704067200.0  # 2024-01-01 00:00:00 UTC
        result = format_timestamp(ts)
        assert len(result) == 8
        assert ":" in result

    def test_format_different_timestamps(self):
        """Test different timestamps"""
        # These will show local time, just verify format
        for ts in [0.0, 1000000.0, 1704067200.0]:
            result = format_timestamp(ts)
            assert len(result) == 8


class TestFormatDuration:
    """Tests for format_duration"""

    def test_seconds(self):
        """Duration under 60 seconds - actual implementation uses .1f"""
        assert format_duration(5.5) == "5.5s"
        assert format_duration(30.0) == "30.0s"
        assert format_duration(59.9) == "59.9s"

    def test_minutes(self):
        """Duration between 60 and 3600 seconds"""
        assert format_duration(60.0) == "1m 0s"
        assert format_duration(90.5) == "1m 30s"  # 90.5 % 60 = 30.5, int(30.5) = 30
        assert format_duration(3599.0) == "59m 59s"

    def test_hours(self):
        """Duration over 3600 seconds"""
        assert format_duration(3600.0) == "1h 0m"
        assert format_duration(3660.0) == "1h 1m"
        assert format_duration(7200.0) == "2h 0m"


class TestCalcPercentiles:
    """Tests for calc_percentiles - matches actual implementation"""

    def test_empty_list(self):
        """Empty input returns zeros"""
        result = calc_percentiles([])
        assert result["min"] == 0
        assert result["max"] == 0
        assert result["avg"] == 0
        assert result["p50"] == 0
        assert result["p95"] == 0
        assert result["p99"] == 0

    def test_single_value(self):
        """Single value returns that value for all"""
        result = calc_percentiles([100.0])
        assert result["min"] == 100.0
        assert result["max"] == 100.0
        assert result["avg"] == 100.0
        assert result["p50"] == 100.0

    def test_ten_values(self):
        """Test with 10 values - implementation uses int(n * p / 100)"""
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        result = calc_percentiles(values)
        assert result["min"] == 1
        assert result["max"] == 10
        assert result["avg"] == 5.5
        # p50: int(10 * 50 / 100) = 5 -> index 5 -> value 6
        assert result["p50"] == 6

    def test_hundred_values(self):
        """Test with 100 values for accurate percentiles"""
        values = list(range(1, 101))
        result = calc_percentiles(values)
        assert result["min"] == 1
        assert result["max"] == 100
        assert result["avg"] == 50.5
        # p50: int(100 * 50 / 100) = 50 -> index 50 -> value 51
        assert result["p50"] == 51
        # p95: int(100 * 95 / 100) = 95 -> index 95 -> value 96
        assert result["p95"] == 96
        # p99: int(100 * 99 / 100) = 99 -> index 99 -> value 100
        assert result["p99"] == 100


class TestCalcP99:
    """Tests for calc_p99 - matches actual implementation"""

    def test_empty(self):
        assert calc_p99([]) == 0.0

    def test_single(self):
        assert calc_p99([100.0]) == 100.0

    def test_small_list_returns_max(self):
        """Small list (< 100 items) returns max"""
        assert calc_p99([1, 2, 3, 4, 5]) == 5

    def test_large_list(self):
        """Large list (>= 100) returns 99th percentile index"""
        values = list(range(1, 101))
        # int(len(values) * 0.99) = 99 -> index 99 -> value 100
        assert calc_p99(values) == 100


class TestCalcTailRatio:
    """Tests for calc_tail_ratio (P99/P50)"""

    def test_empty_list(self):
        """Empty list returns 1.0"""
        assert calc_tail_ratio([]) == 1.0

    def test_insufficient_data(self):
        """Less than 5 values returns 1.0"""
        assert calc_tail_ratio([1.0, 2.0]) == 1.0
        assert calc_tail_ratio([1.0, 2.0, 3.0, 4.0]) == 1.0

    def test_uniform_distribution(self):
        """Uniform values should have ratio near 1.0"""
        values = [1.0] * 10
        ratio = calc_tail_ratio(values)
        assert ratio == 1.0

    def test_minimal_tail(self):
        """Small variance = minimal tail ratio"""
        values = [1.0] * 9 + [1.1]  # P50=1.0, P99=1.1
        ratio = calc_tail_ratio(values)
        assert ratio < 1.2

    def test_moderate_tail(self):
        """Moderate variance = moderate tail ratio"""
        # With 10 values: P50 index = int(10*50/100) = 5 -> value at index 5
        # [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.2, 1.3, 1.4, 1.5]
        values = [1.0] * 6 + [1.2, 1.3, 1.4, 1.5]
        ratio = calc_tail_ratio(values)
        # P50=1.0, P99=1.5 -> ratio=1.5 (borderline moderate/significant)
        assert 1.2 <= ratio <= 1.6

    def test_significant_tail(self):
        """Large variance = significant tail ratio"""
        values = [1.0] * 8 + [5.0, 10.0]  # P50=1.0, P99=10.0
        ratio = calc_tail_ratio(values)
        assert ratio >= 1.5

    def test_zero_p50(self):
        """P50=0 should return 1.0 to avoid division by zero"""
        values = [0.0, 0.0, 0.0, 0.0, 1.0]
        ratio = calc_tail_ratio(values)
        assert ratio == 1.0


class TestClassifyTailLatency:
    """Tests for classify_tail_latency"""

    def test_minimal_classification(self):
        """Ratio < 1.2 is minimal"""
        assert classify_tail_latency(1.0) == "minimal"
        assert classify_tail_latency(1.1) == "minimal"
        assert classify_tail_latency(1.19) == "minimal"

    def test_moderate_classification(self):
        """Ratio 1.2-1.5 is moderate"""
        assert classify_tail_latency(1.2) == "moderate"
        assert classify_tail_latency(1.3) == "moderate"
        assert classify_tail_latency(1.49) == "moderate"

    def test_significant_classification(self):
        """Ratio >= 1.5 is significant"""
        assert classify_tail_latency(1.5) == "significant"
        assert classify_tail_latency(2.0) == "significant"
        assert classify_tail_latency(10.0) == "significant"


class TestFormatLatencyDistribution:
    """Tests for format_latency_distribution"""

    def test_empty_values(self):
        """Empty list returns 'no data'"""
        assert format_latency_distribution([]) == "no data"

    def test_format_with_ms_unit(self):
        """Test with ms unit (default)"""
        # Use values that give clear P50 result
        # With 3 values [0.001, 0.002, 0.003], P50=0.002 (index 1)
        values = [0.001, 0.002, 0.003]  # 1ms, 2ms, 3ms
        result = format_latency_distribution(values, unit="ms")
        assert "P50=2ms" in result  # 0.002 * 1000 = 2ms
        assert "P95=3ms" in result  # 0.003 * 1000 = 3ms
        assert "P99=3ms" in result
        assert "tail=" in result

    def test_format_with_s_unit(self):
        """Test with seconds unit"""
        # With 3 values [1.0, 2.0, 3.0], P50=2.0 (index 1)
        values = [1.0, 2.0, 3.0]
        result = format_latency_distribution(values, unit="s")
        assert "P50=2s" in result
        assert "P95=3s" in result

    def test_includes_severity(self):
        """Should include severity classification"""
        # Create data with significant tail
        values = [1.0] * 8 + [10.0, 15.0]
        result = format_latency_distribution(values)
        assert "(significant)" in result or "(moderate)" in result or "(minimal)" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
