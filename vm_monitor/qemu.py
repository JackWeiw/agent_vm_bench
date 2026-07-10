# vm_monitor/qemu.py
"""
QEMUMonitor - QEMU Virtual Machine Monitor

Monitors qemu-kvm and qemu-system processes.
"""

import re
from typing import Dict, List, Tuple

import psutil

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

        Returns:
            List of dicts with VM info including:
            - pid: Process ID
            - name: VM name
            - cpu_percent: CPU usage percentage
            - memory_mb: Total memory in MB
            - memory_huge_mb: Hugepage memory in MB
            - memory_private_mb: Private memory in MB
            - memory_heap_mb: Heap memory in MB
            - memory_per_numa: Per-NUMA memory breakdown
            - status: Process status
        """
        vms = []
        current_pids = set()
        current_total_mem = 0.0
        current_total_cpu = 0.0

        # Get process names from abstract method
        process_names = self.get_process_names()

        for proc in psutil.process_iter(["pid", "name", "cmdline", "status"]):
            try:
                proc_name = proc.info["name"] or ""
                # Check if process name matches any QEMU variant
                if not any(qemu_name in proc_name for qemu_name in process_names):
                    continue

                pid = proc.info["pid"]
                current_pids.add(pid)
                cmdline = " ".join(proc.info["cmdline"] or [])

                # Use abstract method to extract VM ID
                vm_name = self.extract_vm_id(pid, cmdline)

                # Real Physical Memory: numastat
                numastat_mem = self.get_vm_memory_from_numastat(pid)
                memory_mb = numastat_mem.get("total_mb", 0.0)
                memory_huge_mb = numastat_mem.get("huge_mb", 0.0)
                memory_private_mb = numastat_mem.get("private_mb", 0.0)
                memory_heap_mb = numastat_mem.get("heap_mb", 0.0)
                memory_per_numa = numastat_mem.get("per_node", {})

                # If numastat fails, fall back to psutil
                if memory_mb <= 0:
                    try:
                        mem_info = proc.memory_info()
                        memory_mb = round(mem_info.pss / 1024 / 1024, 2)
                    except:
                        mem_info = proc.memory_info()
                        memory_mb = round(mem_info.rss / 1024 / 1024, 2)

                current_total_mem += memory_mb

                # CPU Statistics
                if pid not in self.process_cache:
                    try:
                        p = psutil.Process(pid)
                        p.cpu_percent()
                        self.process_cache[pid] = p
                        cpu = 0.0
                    except:
                        cpu = 0.0
                else:
                    try:
                        p = self.process_cache[pid]
                        cpu = p.cpu_percent()
                    except psutil.NoSuchProcess:
                        self.process_cache.pop(pid, None)
                        cpu = 0.0

                cpu = round(max(0, min(cpu, 10000)), 2)
                current_total_cpu += cpu

                vms.append(
                    {
                        "pid": pid,
                        "name": vm_name,
                        "cpu_percent": cpu,
                        "memory_mb": memory_mb,
                        "memory_huge_mb": memory_huge_mb,
                        "memory_private_mb": memory_private_mb,
                        "memory_heap_mb": memory_heap_mb,
                        "memory_per_numa": memory_per_numa,
                        "status": proc.info["status"],
                    }
                )

            except:
                continue

        if current_total_mem > self.peak_total_memory_mb:
            self.peak_total_memory_mb = current_total_mem
        if current_total_cpu > self.peak_total_cpu:
            self.peak_total_cpu = current_total_cpu

        dead_pids = [p for p in self.process_cache if p not in current_pids]
        for p in dead_pids:
            self.process_cache.pop(p, None)

        return vms

    def get_monitor_title(self) -> str:
        """Return QEMU monitoring title"""
        return "QEMU VM Real-time Monitoring"

    def get_no_vm_message(self) -> str:
        """Return message when no QEMU VMs detected"""
        return "No running QEMU virtual machines detected"

    def get_csv_filename_prefix(self) -> str:
        """Return CSV filename prefix"""
        return "qemu_monitor"
