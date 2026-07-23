# vm_monitor/qemu.py
"""
QEMUMonitor - QEMU Virtual Machine Monitor

Monitors qemu-kvm and qemu-system processes.
"""

import re
from typing import Dict, List, Tuple

from .base import VMMonitorBase


class QEMUMonitor(VMMonitorBase):
    """QEMU Virtual Machine Monitor

    Monitors qemu-kvm and qemu-system processes.
    """

    # Process names to match
    PROCESS_NAMES = ("qemu-kvm", "qemu-system")

    def get_process_names(self) -> Tuple[str, ...]:
        """Return QEMU process names to match"""
        return self.PROCESS_NAMES

    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract VM name from -name parameter

        Args:
            pid: Process ID
            cmdline: Full command line string

        Returns:
            VM name string (e.g., 'vm-123' or parsed name)
        """
        name_match = re.search(r"-name\s+([^,\s]+)", cmdline)
        if name_match:
            return name_match.group(1)
        return f"vm-{pid}"

    def get_vms_realtime(self) -> List[Dict]:
        """Get real-time information for all QEMU VMs.

        Uses two-phase collection:
        Phase 1: Discover VM processes (serial psutil scan)
        Phase 2: Collect per-VM metrics (parallel for large counts)

        Returns:
            List of dicts with VM info including pid, name, cpu_percent,
            memory_mb, memory_huge_mb, memory_private_mb, memory_heap_mb,
            memory_per_numa, memory_swapcache_mb, memory_swapcache_per_numa,
            status.
        """
        vm_candidates = self._discover_vm_processes()
        return self._collect_vm_metrics_parallel(vm_candidates)

    def get_monitor_title(self) -> str:
        """Return QEMU monitoring title"""
        return "QEMU VM Real-time Monitoring"

    def get_no_vm_message(self) -> str:
        """Return message when no QEMU VMs detected"""
        return "No running QEMU virtual machines detected"

    def get_csv_filename_prefix(self) -> str:
        """Return CSV filename prefix"""
        return "qemu_monitor"
