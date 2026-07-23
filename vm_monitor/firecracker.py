# vm_monitor/firecracker.py
"""
FirecrackerMonitor - Firecracker microVM Monitor

Monitors firecracker processes (used in E2B, containerd environments).
"""

from typing import Dict, List, Tuple

from .base import VMMonitorBase


class FirecrackerMonitor(VMMonitorBase):
    """Firecracker microVM Monitor

    Monitors firecracker processes.
    """

    # Process names to match
    PROCESS_NAMES = ("firecracker",)

    def get_process_names(self) -> Tuple[str, ...]:
        """Return Firecracker process names to match"""
        return self.PROCESS_NAMES

    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract Sandbox ID from Firecracker process

        Simple implementation: use PID as fallback.
        Can be extended to parse --id or --api-sock path.

        Args:
            pid: Process ID
            cmdline: Full command line string

        Returns:
            Sandbox ID string (e.g., 'fc-123')
        """
        # TODO: Can extend to parse --id parameter or socket path
        return f"fc-{pid}"

    def get_vms_realtime(self) -> List[Dict]:
        """Get real-time information for all Firecracker microVMs.

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
        """Return Firecracker monitoring title"""
        return "Firecracker VM Real-time Monitoring"

    def get_no_vm_message(self) -> str:
        """Return message when no Firecracker microVMs detected"""
        return "No running Firecracker microVMs detected"

    def get_csv_filename_prefix(self) -> str:
        """Return CSV filename prefix"""
        return "firecracker_monitor"
