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

from .bench import main, run_benchmark
from .config import Config
from .schemas import (
    BrowserMetrics,
    ConnectionMetrics,
    CreationMetrics,
    OOMType,
    QAMetrics,
    StressMetrics,
    TestSnapshot,
    VMHealth,
    VMState,
    VMStatus,
)
from .vm_manager import VMConnection, VMManager

# Import version from project root
try:
    from ..version import __version__
except ImportError:
    __version__ = "0.1.0-alpha"  # Fallback for standalone testing

__all__ = [
    "Config",
    "VMStatus",
    "OOMType",
    "CreationMetrics",
    "ConnectionMetrics",
    "QAMetrics",
    "BrowserMetrics",
    "StressMetrics",
    "VMHealth",
    "VMState",
    "TestSnapshot",
    "VMManager",
    "VMConnection",
    "run_benchmark",
    "main",
    "__version__",
]
