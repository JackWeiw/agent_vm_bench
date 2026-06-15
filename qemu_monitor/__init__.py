# qemu_monitor/__init__.py
"""
QEMU Monitor Package

Real-time monitoring and log collection for QEMU virtual machines.
Provides modular components for configuration, log capture, parsing,
data export, and VM monitoring.

Usage:
    # Package-level import (recommended)
    from qemu_monitor import QEMUMonitor, LogCapture

    # Module-level import (for specific functions)
    from qemu_monitor.parsers import parse_devkit_top_down

    # CLI entry point
    python -m qemu_monitor.cli -t 60 -i 3
"""

# Core classes
from .monitor import QEMUMonitor
from .log_capture import LogCapture

# Configuration management
from .config import (
    load_env_config,
    save_env_config,
    validate_and_prompt_missing,
    load_getfre_config,
)

# Parser functions
from .parsers import (
    parse_devkit_top_down,
    parse_ksys,
    parse_devkit_mem,
    parse_getfre,
    parse_ub_watch,
    parse_smap_bw,
    parse_all_logs,
)

# Export utilities
from .exporters import (
    export_to_excel,
    print_capture_summary,
)

# Version marker
__version__ = '1.0.0'

__all__ = [
    'QEMUMonitor',
    'LogCapture',
    'load_env_config',
    'save_env_config',
    'validate_and_prompt_missing',
    'load_getfre_config',
    'parse_devkit_top_down',
    'parse_ksys',
    'parse_devkit_mem',
    'parse_getfre',
    'parse_ub_watch',
    'parse_smap_bw',
    'parse_all_logs',
    'export_to_excel',
    'print_capture_summary',
]