#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QEMU Virtual Machine Real-time Monitoring Tool

Backward compatible entry point - All functionality migrated to qemu_monitor/ package.

Usage remains unchanged:
    python qemu_monitor.py -t 600 -i 2
    python qemu_monitor.py --stress-file /tmp/bench_running.lock
    python qemu_monitor.py -t 60 --enable-capture

For package usage:
    from qemu_monitor import QEMUMonitor
"""

from qemu_monitor.cli import main

if __name__ == '__main__':
    main()