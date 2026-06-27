"""
VM Bench - OpenStack VM Batch Performance Testing Suite

Features:
- Batch create OpenStack VMs, collect creation performance (time, success rate, P50/P95/P99)
- SSH connection to VMs, collect connection metrics
- Execute tasks (QA, Stress, Browser), collect execution performance
- Monitor VM alive status via SSH and OpenStack
- Support batched creation/connection/startup
- Real-time statistics snapshot + final report
- YAML config file support

Usage Examples:
    # Create VMs only (Phase 0)
    python -m vm_bench --config config/vm_bench.yaml --create-only

    # Full workflow: Create + Connect + Benchmark
    python -m vm_bench --config config/vm_bench.yaml

    # Browser benchmark with warmup
    python -m vm_bench --config config/vm_bench.yaml --warmup-only
    python -m vm_bench --config config/vm_bench.yaml -bp 0.5 -t 300
"""

from .config import Config
from .schemas import (
    VMStatus,
    OOMType,
    CreationMetrics,
    ConnectionMetrics,
    QAMetrics,
    BrowserMetrics,
    StressMetrics,
    VMHealth,
    VMState,
    TestSnapshot,
)
from .vm_manager import VMManager, VMConnection
from .bench import run_benchmark, main

__version__ = "1.0.0"

__all__ = [
    'Config',
    'VMStatus',
    'OOMType',
    'CreationMetrics',
    'ConnectionMetrics',
    'QAMetrics',
    'BrowserMetrics',
    'StressMetrics',
    'VMHealth',
    'VMState',
    'TestSnapshot',
    'VMManager',
    'VMConnection',
    'run_benchmark',
    'main',
]