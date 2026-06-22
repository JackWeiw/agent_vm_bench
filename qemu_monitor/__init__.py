# qemu_monitor/__init__.py
"""
QEMU Monitor Package - DEPRECATED

This package is deprecated. All functionality has been migrated to vm_monitor package.
This module provides backward compatibility forwarding.

DEPRECATION WARNING:
    This package will be removed in a future version.
    Please migrate to vm_monitor package:

    OLD: from qemu_monitor import QEMUMonitor, LogCapture
    NEW: from vm_monitor import QEMUMonitor, LogCapture

    OLD: python qemu_monitor.py -t 60 -i 2
    NEW: python vm_monitor.py -t 60 -i 2 --vmm qemu

Migration Guide:
    - All imports work the same, just change 'qemu_monitor' to 'vm_monitor'
    - CLI arguments unchanged, add --vmm qemu to use QEMU monitor
    - All parsers and exporters migrated unchanged
"""

import warnings

# Emit deprecation warning on import
warnings.warn(
    "qemu_monitor package is deprecated. Please migrate to vm_monitor package. "
    "Change: from qemu_monitor import X -> from vm_monitor import X",
    DeprecationWarning,
    stacklevel=2
)

# Forward all imports from vm_monitor
from vm_monitor import (
    # Core classes
    QEMUMonitor,
    LogCapture,
    # Configuration
    load_env_config,
    save_env_config,
    validate_and_prompt_missing,
    load_getfre_config,
    # Parsers
    parse_devkit_top_down,
    parse_ksys,
    parse_devkit_mem,
    parse_getfre,
    parse_ub_watch,
    parse_smap_bw,
    parse_all_logs,
    # Exporters
    export_to_excel,
    print_capture_summary,
)

# Also export FirecrackerMonitor for users who want to try new functionality
from vm_monitor import FirecrackerMonitor, VMMonitorBase

# Version marker (same as vm_monitor for compatibility)
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
    # New exports for forward compatibility
    'VMMonitorBase',
    'FirecrackerMonitor',
]