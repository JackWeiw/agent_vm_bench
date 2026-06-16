"""
Utility Functions Module

Provides logging formatting, time handling, percentile calculation and other helper functions
"""

import logging
import statistics
from datetime import datetime
from typing import List, Dict


def setup_logging(level: int = logging.INFO) -> None:
    """Setup logging format"""
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def format_timestamp(ts: float) -> str:
    """Format timestamp to HH:MM:SS"""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    """Format duration to readable format"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def calc_percentiles(values: List[float]) -> Dict[str, float]:
    """Calculate percentile statistics

    Returns: {"min": x, "max": x, "avg": x, "p50": x, "p95": x, "p99": x}
    """
    if not values:
        return {"min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def percentile(p: float) -> float:
        idx = int(n * p / 100)
        idx = min(idx, n - 1)
        return sorted_vals[idx]

    return {
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": statistics.mean(values),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99)
    }


def calc_p99(values: List[float]) -> float:
    """Calculate P99 latency"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) >= 100:
        return sorted_vals[int(len(sorted_vals) * 0.99)]
    return sorted_vals[-1]