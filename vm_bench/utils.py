"""
Utilities Module

Helper functions for percentile calculation, timing, etc.
"""

import math
import statistics
from typing import Dict, List


def calc_percentiles(values: List[float]) -> Dict[str, float]:
    """Calculate percentile statistics for a list of values

    Returns: {"min", "max", "avg", "p50", "p95", "p99", "count"}
    """
    if not values:
        return {
            "min": 0.0,
            "max": 0.0,
            "avg": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "count": 0,
        }

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def percentile(p: float) -> float:
        idx = min(int(math.ceil(p / 100.0 * n)) - 1, n - 1)
        return sorted_vals[max(idx, 0)]

    return {
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": statistics.mean(values),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "count": n,
    }


def calc_p99(values: List[float]) -> float:
    """Calculate P99 latency"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) >= 100:
        return sorted_vals[int(len(sorted_vals) * 0.99)]
    return sorted_vals[-1]


def calc_avg(values: List[float]) -> float:
    """Calculate average"""
    return statistics.mean(values) if values else 0.0
