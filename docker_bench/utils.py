"""
Utility Functions Module

Common helper functions for statistics and formatting
"""

import statistics
from typing import Dict, List


def calc_percentiles(values: List[float]) -> Dict[str, float]:
    """Calculate percentile statistics

    Args:
        values: List of numeric values

    Returns:
        Dict with min, max, avg, p50, p95, p99
    """
    if not values:
        return {
            "min": 0.0,
            "max": 0.0,
            "avg": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    return {
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": statistics.mean(values),
        "p50": sorted_vals[int(n * 0.50)] if n >= 2 else sorted_vals[0],
        "p95": sorted_vals[int(n * 0.95)] if n >= 20 else sorted_vals[-1],
        "p99": sorted_vals[int(n * 0.99)] if n >= 100 else sorted_vals[-1],
    }


def calc_p99(values: List[float]) -> float:
    """Calculate P99 value

    Args:
        values: List of numeric values

    Returns:
        P99 value
    """
    if not values:
        return 0.0

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    if n >= 100:
        return sorted_vals[int(n * 0.99)]
    return sorted_vals[-1]


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string (e.g., "1m 30s" or "45.2s")
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"
