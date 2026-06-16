#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM Bench Lite Package

Modular VM batch stress testing tool.

Backward compatible entry point - All functionality migrated to vm_bench_lite/ package.

Usage remains unchanged:
    python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode -t 180

For package-level import:
    from vm_bench_lite import Config, VMState
"""

# Export commonly used classes for backward compatibility
from .config import Config
from .models import (
    OOMType,
    QAMetrics,
    BrowserMetrics,
    StressMetrics,
    VMHealth,
    VMState,
)

# Package version
__version__ = '2.0.0'

__all__ = [
    'Config',
    'OOMType',
    'QAMetrics',
    'BrowserMetrics',
    'StressMetrics',
    'VMHealth',
    'VMState',
]