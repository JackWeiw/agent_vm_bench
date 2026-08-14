"""Host-agnostic helper functions: logging setup, time formatting, percentiles.

These are pure functions with no provider dependency; the stats collector and
task runners use them to compute latency statistics and format output.
"""
from __future__ import annotations

import logging
import statistics
import sys
from datetime import datetime


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging with a timestamped format.

    ``logging.basicConfig`` is a no-op once the root logger already has handlers
    (e.g. when the package is embedded in a host that configured logging first).
    Explicitly force the root level afterwards so the default INFO level -- and
    the progress messages emitted at it -- is honored even in that case.

    Output goes to stdout (not the logging default of stderr) to preserve the
    pre-refactor ``print`` destination: shell redirects and ``tee`` keep
    capturing the report echo and progress output.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger().setLevel(level)


def format_timestamp(ts: float) -> str:
    """Format a timestamp to HH:MM:SS."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    """Format a duration to a readable form (e.g. ``12.3s``, ``3m 4s``)."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.0f}s"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    return f"{hours}h {mins}m"


def calc_percentiles(values: list[float]) -> dict[str, float]:
    """Calculate percentile statistics.

    Returns: ``{"min", "max", "avg", "p50", "p95", "p99"}`` (all 0.0 when empty).
    """
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def percentile(p: float) -> float:
        idx = min(int(n * p / 100), n - 1)
        return sorted_vals[idx]

    return {
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": statistics.mean(values),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
    }


def calc_p99(values: list[float]) -> float:
    """Calculate the P99 latency."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) >= 100:
        return sorted_vals[int(len(sorted_vals) * 0.99)]
    return sorted_vals[-1]


def calc_tail_ratio(values: list[float]) -> float:
    """Calculate the tail latency ratio (P99 / P50).

    - < 1.2x: minimal tail latency (well-behaved distribution)
    - 1.2x ~ 1.5x: moderate tail latency
    - > 1.5x: significant tail latency (outliers present)

    Returns 1.0 when there is insufficient data.
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
    """Classify tail latency severity from the P99/P50 ratio."""
    if tail_ratio < 1.2:
        return "minimal"
    if tail_ratio < 1.5:
        return "moderate"
    return "significant"


def format_latency_distribution(values: list[float], unit: str = "ms") -> str:
    """Format a latency distribution as a compact string.

    Shows P50, P95, P99 and tail ratio for quick analysis.

    Args:
        values: List of latency values in seconds.
        unit: Output unit (``"ms"`` or ``"s"``).

    Returns:
        e.g. ``"P50=20ms, P95=45ms, P99=50ms, tail=2.5x (significant)"``.
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
