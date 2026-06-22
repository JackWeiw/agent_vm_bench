#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QEMU Virtual Machine Real-time Monitoring Tool - DEPRECATED

This entry script is deprecated. All functionality has been migrated to vm_monitor package.
This script provides backward compatibility forwarding.

DEPRECATION WARNING:
    This script will be removed in a future version.
    Please migrate to vm_monitor.py script:

    OLD: python qemu_monitor.py -t 600 -i 2
    NEW: python vm_monitor.py -t 600 -i 2 --vmm qemu

    OLD: python qemu_monitor.py --stress-file /tmp/bench_running.lock
    NEW: python vm_monitor.py --stress-file /tmp/bench_running.lock --vmm qemu

    OLD: python qemu_monitor.py -t 60 --enable-capture
    NEW: python vm_monitor.py -t 60 --enable-capture --vmm qemu

For package usage:
    OLD: from qemu_monitor import QEMUMonitor
    NEW: from vm_monitor import QEMUMonitor
"""

import warnings

# Emit deprecation warning on run
warnings.warn(
    "qemu_monitor.py is deprecated. Please use vm_monitor.py with --vmm qemu argument.",
    DeprecationWarning,
    stacklevel=2
)

# Forward to vm_monitor.cli with --vmm qemu default
from vm_monitor.cli import main

if __name__ == '__main__':
    # Note: Users should migrate to vm_monitor.py --vmm qemu
    # This wrapper will auto-add --vmm qemu if not provided
    import sys
    if '--vmm' not in sys.argv:
        sys.argv.insert(1, '--vmm')
        sys.argv.insert(2, 'qemu')
    main()