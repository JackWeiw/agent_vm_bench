"""
E2B Sandbox Bench - E2B Sandbox Batch Performance Testing Suite

Features:
- Batch create E2B sandboxes, collect startup performance (time, success rate, P50/P95/P99 latency)
- Execute browser tasks, collect execution performance (latency, throughput)
- Monitor sandbox alive status
- Support batched startup and random task interval
- Real-time statistics snapshot + final report

Usage Examples:
    python -m e2b_bench --config config/e2b_bench.yaml
    python -m e2b_bench --config config/e2b_bench.yaml --total 50 --duration 300
"""

from .config import Config
from .schemas import (
    SandboxState,
    SandboxStatus,
    CreationMetrics,
    BrowserMetrics,
    TestSnapshot,
)
from .bench import run_benchmark, main

__version__ = "1.0.0"

__all__ = [
    'Config',
    'SandboxState',
    'SandboxStatus',
    'CreationMetrics',
    'BrowserMetrics',
    'TestSnapshot',
    'run_benchmark',
    'main',
]