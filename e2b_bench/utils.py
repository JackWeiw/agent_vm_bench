"""
工具函数模块

提供日志格式化、时间处理、百分位计算等辅助函数
"""

import logging
import statistics
from datetime import datetime
from typing import List, Dict


def setup_logging(level: int = logging.INFO) -> None:
    """设置日志格式"""
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def format_timestamp(ts: float) -> str:
    """格式化时间戳为 HH:MM:SS"""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    """格式化时长为易读格式"""
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
    """计算百分位统计

    返回: {"min": x, "max": x, "avg": x, "p50": x, "p95": x, "p99": x}
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
    """计算P99延迟"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) >= 100:
        return sorted_vals[int(len(sorted_vals) * 0.99)]
    return sorted_vals[-1]