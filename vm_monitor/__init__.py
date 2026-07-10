# vm_monitor/__init__.py
"""VM Monitor Package

Provides abstract base class and implementations for VM monitoring.
Supports multiple VMM types: QEMU, Firecracker.

Usage:
    # Package-level import (recommended)
    from vm_monitor import VMMonitorBase, QEMUMonitor, FirecrackerMonitor
    from vm_monitor import LogCapture

    # Module-level import (for specific functions)
    from vm_monitor.parsers import parse_devkit_top_down

    # CLI entry point
    python -m vm_monitor.cli -t 60 -i 3 --vmm qemu
"""

# Core classes
from .base import VMMonitorBase

# Configuration management
from .config import (
    load_env_config,
    load_getfre_config,
    save_env_config,
    validate_and_prompt_missing,
)

# Export utilities
from .exporters import (
    export_to_excel,
    print_capture_summary,
)
from .firecracker import FirecrackerMonitor
from .log_capture import LogCapture

# Parser functions
from .parsers import (
    parse_all_logs,
    parse_devkit_mem,
    parse_devkit_top_down,
    parse_getfre,
    parse_ksys,
    parse_smap_bw,
    parse_ub_watch,
)
from .qemu import QEMUMonitor

# Version marker
__version__ = "1.0.0"

__all__ = [
    # Base class and implementations
    "VMMonitorBase",
    "QEMUMonitor",
    "FirecrackerMonitor",
    "LogCapture",
    # Configuration
    "load_env_config",
    "save_env_config",
    "validate_and_prompt_missing",
    "load_getfre_config",
    # Parsers
    "parse_devkit_top_down",
    "parse_ksys",
    "parse_devkit_mem",
    "parse_getfre",
    "parse_ub_watch",
    "parse_smap_bw",
    "parse_all_logs",
    # Exporters
    "export_to_excel",
    "print_capture_summary",
]
