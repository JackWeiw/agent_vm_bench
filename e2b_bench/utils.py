"""
Utility Functions Module

Provides logging formatting, time handling, percentile calculation and other helper functions
"""

import logging
import statistics
from datetime import datetime
from typing import Dict, List


def setup_logging(level: int = logging.INFO) -> None:
    """Setup logging format"""
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


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
        "p99": percentile(99),
    }


def calc_p99(values: List[float]) -> float:
    """Calculate P99 latency"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) >= 100:
        return sorted_vals[int(len(sorted_vals) * 0.99)]
    return sorted_vals[-1]


def calc_tail_ratio(values: List[float]) -> float:
    """Calculate tail latency ratio (P99 / P50).

    Tail ratio indicates severity of long-tail latency:
    - < 1.2x: Minimal tail latency (well-behaved distribution)
    - 1.2x ~ 1.5x: Moderate tail latency
    - > 1.5x: Significant tail latency (outliers present)

    Returns:
        Tail ratio (P99 / P50), or 1.0 if insufficient data
    """
    if not values or len(values) < 5:
        return 1.0

    stats = calc_percentiles(values)
    p50 = stats["p50"]
    p99 = stats["p99"]

    if p50 <= 0:
        return 1.0

    return p99 / p50


def classify_tail_latency(tail_ratio: float) -> str:
    """Classify tail latency severity.

    Args:
        tail_ratio: P99/P50 ratio

    Returns:
        Classification string: "minimal", "moderate", or "significant"
    """
    if tail_ratio < 1.2:
        return "minimal"
    elif tail_ratio < 1.5:
        return "moderate"
    else:
        return "significant"


def format_latency_distribution(values: List[float], unit: str = "ms") -> str:
    """Format latency distribution as a compact string.

    Shows P50, P95, P99 and tail ratio for quick analysis.

    Args:
        values: List of latency values in seconds
        unit: Output unit ("ms" or "s")

    Returns:
        Formatted string like "P50=20ms, P95=45ms, P99=50ms, tail=2.5x (significant)"
    """
    if not values:
        return "no data"

    stats = calc_percentiles(values)
    tail_ratio = calc_tail_ratio(values)
    severity = classify_tail_latency(tail_ratio)

    multiplier = 1000 if unit == "ms" else 1

    parts = [
        f"P50={stats['p50'] * multiplier:.0f}{unit}",
        f"P95={stats['p95'] * multiplier:.0f}{unit}",
        f"P99={stats['p99'] * multiplier:.0f}{unit}",
        f"tail={tail_ratio:.2f}x ({severity})",
    ]

    return ", ".join(parts)
