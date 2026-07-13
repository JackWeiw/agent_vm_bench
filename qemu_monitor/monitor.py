#!/usr/bin/env python3
"""
QEMU Monitor Core Module - DEPRECATED

This module is deprecated. All functionality has been migrated to vm_monitor.qemu module.
This module provides backward compatibility forwarding.

DEPRECATION WARNING:
    This module will be removed in a future version.
    Please migrate to vm_monitor.qemu module:

    OLD: from qemu_monitor.monitor import QEMUMonitor
    NEW: from vm_monitor import QEMUMonitor

Migration Guide:
    - QEMUMonitor class is now in vm_monitor/qemu.py
    - All methods and attributes unchanged
"""

import warnings

# Emit deprecation warning on import
warnings.warn(
    "qemu_monitor.monitor module is deprecated. Please use vm_monitor.QEMUMonitor instead.",
    DeprecationWarning,
    stacklevel=2,
)

# Forward QEMUMonitor from vm_monitor
from vm_monitor.qemu import QEMUMonitor

__all__ = ["QEMUMonitor"]
