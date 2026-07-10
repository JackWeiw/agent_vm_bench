"""
Tests for Docker Bench utility functions
"""

import pytest
from docker_bench.utils import (
    calc_percentiles,
    calc_p99,
    format_duration,
)


class TestCalcPercentiles:
    """Test calc_percentiles function"""

    def test_empty_list(self):
        """Empty input returns zeros"""
        result = calc_percentiles([])
        assert result["min"] == 0.0
        assert result["max"] == 0.0
        assert result["avg"] == 0.0
        assert result["p50"] == 0.0
        assert result["p95"] == 0.0
        assert result["p99"] == 0.0

    def test_single_value(self):
        """Single value returns that value for all"""
        result = calc_percentiles([100.0])
        assert result["min"] == 100.0
        assert result["max"] == 100.0
        assert result["avg"] == 100.0
        assert result["p50"] == 100.0

    def test_multiple_values(self):
        """Test with multiple values"""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = calc_percentiles(values)
        assert result["min"] == 1.0
        assert result["max"] == 5.0
        assert result["avg"] == 3.0

    def test_large_dataset(self):
        """Test with 100 values for accurate percentiles"""
        values = list(range(1, 101))
        result = calc_percentiles(values)
        assert result["min"] == 1
        assert result["max"] == 100
        assert result["avg"] == 50.5
        # p50: int(100 * 0.50) = 50 -> index 50 -> value 51
        assert result["p50"] == 51
        # p95: int(100 * 0.95) = 95 -> index 95 -> value 96
        assert result["p95"] == 96

    def test_unsorted_input(self):
        """Input unsorted, function should still work"""
        values = [5.0, 1.0, 3.0, 2.0, 4.0]
        result = calc_percentiles(values)
        assert result["min"] == 1.0
        assert result["max"] == 5.0


class TestCalcP99:
    """Test calc_p99 function"""

    def test_empty_list(self):
        """Empty input returns 0"""
        assert calc_p99([]) == 0.0

    def test_single_value(self):
        """Single value returns that value"""
        assert calc_p99([100.0]) == 100.0

    def test_small_sample(self):
        """Small sample (<100 items) returns max"""
        values = [1.0, 2.0, 3.0]
        assert calc_p99(values) == 3.0

    def test_large_sample(self):
        """Large sample (>=100) returns 99th percentile"""
        values = list(range(1, 101))
        # int(100 * 0.99) = 99 -> index 99 -> value 100
        assert calc_p99(values) == 100


class TestFormatDuration:
    """Test format_duration function"""

    def test_seconds(self):
        """Duration under 60 seconds"""
        assert format_duration(5.5) == "5.5s"
        assert format_duration(30.0) == "30.0s"
        assert format_duration(59.9) == "59.9s"

    def test_minutes(self):
        """Duration between 60 and 3600 seconds"""
        assert format_duration(60.0) == "1m 0s"
        assert format_duration(90.5) == "1m 30s"  # 90.5 % 60 = 30.5, int(30.5) = 30
        assert format_duration(3599.0) == "59m 59s"

    def test_hours(self):
        """Duration over 3600 seconds - actual implementation shows minutes, not hours"""
        # Actual implementation: minutes = int(seconds // 60), no hours handling
        assert format_duration(3600.0) == "60m 0s"
        assert format_duration(3660.0) == "61m 0s"
        assert format_duration(7200.0) == "120m 0s"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])