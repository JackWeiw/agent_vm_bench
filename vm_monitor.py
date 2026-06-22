#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM Monitor Entry Script

Unified entry point for VM monitoring tool.
Supports multiple VMM types via --vmm argument.

Usage:
    python vm_monitor.py -t 600 -i 2 --vmm qemu
    python vm_monitor.py --stress-file /tmp/bench_running.lock --vmm firecracker
    python vm_monitor.py -t 60 --enable-capture --vmm qemu

For package usage:
    from vm_monitor import QEMUMonitor, FirecrackerMonitor
"""

from vm_monitor.cli import main

if __name__ == '__main__':
    main()