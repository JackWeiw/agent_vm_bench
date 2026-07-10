# vm_monitor/base.py
"""
VMMonitorBase - Abstract Base Class for VM Monitor

Provides system-level monitoring infrastructure independent of VMM type:
- NUMA memory statistics
- Hugepage memory tracking
- NUMA CPU usage
- Host CPU/memory statistics
- Swap usage
- Process memory via numastat
- Sampling loop framework
- Export/analysis infrastructure
"""

import csv
import os
import re
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import psutil


class VMMonitorBase(ABC):
    """Abstract Base Class for VM Real-time Monitor

    Provides system-level monitoring infrastructure independent of VMM type.
    Subclasses must implement abstract methods for VMM-specific logic.

    Attributes:
        running: Monitoring state flag
        data: List of collected sample records
        target_numa_nodes: NUMA nodes to monitor for CPU stats
        numa_memory_history: NUMA memory usage timeline
        hugepage_per_numa: Per-NUMA hugepage statistics
        host_cpu_history: Host total CPU timeline
        swap_history: Swap usage timeline
    """

    def __init__(self):
        """Initialize monitor with empty data containers"""
        self.running = False
        self.data = []
        self.stop_event = threading.Event()
        self.process_cache = {}
        self.numa_memory_history = []
        self.peak_total_memory_mb = 0.0
        self.peak_total_cpu = 0.0
        self.hugepage_total_mb = 0.0
        self.hugepage_free_mb = 0.0
        self.hugepage_used_mb = 0.0
        self.hugepage_used_history = []
        self.peak_hugepage_used_mb = 0.0
        self.last_vm_count = 0

        # Per-NUMA Node Hugepage Statistics
        self.hugepage_per_numa = {}
        self.hugepage_per_numa_history = []

        # Host Machine Total Resource Statistics
        self.host_cpu_history = []
        self.host_mem_history = []
        self.peak_host_cpu = 0.0
        self.peak_host_mem_mb = 0.0

        # Swap Statistics
        self.swap_history = []
        self.peak_swap_used_mb = 0.0

        # Specified NUMA Node CPU Statistics
        self.target_numa_nodes = [0]
        self.numa_cpu_history = defaultdict(list)
        self.numa_cpu_peak = defaultdict(float)
        self.available_numa_nodes = self.get_available_numa_nodes()

    # ==================== Abstract Methods ====================
    @abstractmethod
    def get_vms_realtime(self) -> List[Dict]:
        """Get real-time information for all VMs of this VMM type.

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
        pass

    @abstractmethod
    def get_process_names(self) -> Tuple[str, ...]:
        """Get the process names to monitor for this VMM type.

        Returns:
            Tuple of process name strings (e.g., ('qemu-kvm', 'qemu-system'))
        """
        pass

    @abstractmethod
    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract VM identifier from process command line.

        Args:
            pid: Process ID
            cmdline: Full command line string

        Returns:
            VM identifier string (e.g., VM name)
        """
        pass

    @abstractmethod
    def get_monitor_title(self) -> str:
        """Get the title for this monitor type.

        Returns:
            Title string (e.g., "QEMU VM Real-time Monitoring")
        """
        pass

    @abstractmethod
    def get_no_vm_message(self) -> str:
        """Get the message to display when no VMs are detected.

        Returns:
            Message string (e.g., "No running QEMU virtual machines detected")
        """
        pass

    @abstractmethod
    def get_csv_filename_prefix(self) -> str:
        """Get the CSV filename prefix for this monitor type.

        Returns:
            Prefix string (e.g., "qemu_monitor")
        """
        pass

    # ==================== NUMA Memory Statistics ====================
    def get_numa_nodes_memory(self):
        numa_nodes = []
        try:
            node_dirs = [d for d in os.listdir("/sys/devices/system/node/") if d.startswith("node") and d[4:].isdigit()]
            for node in sorted(node_dirs, key=lambda x: int(x[4:])):
                node_id = int(node[4:])
                path = f"/sys/devices/system/node/{node}/meminfo"
                with open(path) as f:
                    lines = f.read().splitlines()
                total = free = 0
                for l in lines:
                    if "MemTotal" in l:
                        total = int(l.split()[3]) * 1024
                    if "MemFree" in l:
                        free = int(l.split()[3]) * 1024
                used = total - free
                total_mb = round(total / 1024 / 1024, 2)
                used_mb = round(used / 1024 / 1024, 2)
                free_mb = round(free / 1024 / 1024, 2)
                usage = round(used / total * 100, 2) if total > 0 else 0.0
                numa_nodes.append(
                    {"node": node_id, "total": total_mb, "used": used_mb, "free": free_mb, "usage": usage}
                )
        except:
            pass
        self.numa_memory_history.append({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "nodes": numa_nodes})
        return numa_nodes

    def collect_hugepage_stats(self):
        """Collect hugepage memory statistics (total and per-NUMA node)

        Note: /proc/meminfo only reports default hugepage size statistics.
        To get accurate totals across all hugepage sizes, we calculate from per-NUMA stats.
        """
        # First collect per-NUMA stats (this correctly handles multiple hugepage sizes)
        self.collect_hugepage_per_numa_stats()

        # Sum from per-NUMA stats for accurate total (handles 2MB + 1GB etc.)
        self.hugepage_total_mb = 0.0
        self.hugepage_free_mb = 0.0
        for node_id, stats in self.hugepage_per_numa.items():
            self.hugepage_total_mb += stats["total_mb"]
            self.hugepage_free_mb += stats["free_mb"]

        self.hugepage_used_mb = self.hugepage_total_mb - self.hugepage_free_mb
        self.hugepage_used_history.append(self.hugepage_used_mb)

        if self.hugepage_used_mb > self.peak_hugepage_used_mb:
            self.peak_hugepage_used_mb = self.hugepage_used_mb

        # Fallback to /proc/meminfo if per-NUMA collection failed
        if self.hugepage_total_mb == 0 and self.hugepage_per_numa == {}:
            try:
                with open("/proc/meminfo") as f:
                    lines = f.read()
                huge_total = int(re.search(r"HugePages_Total:\s+(\d+)", lines).group(1))
                huge_free = int(re.search(r"HugePages_Free:\s+(\d+)", lines).group(1))
                huge_size = int(re.search(r"Hugepagesize:\s+(\d+)", lines).group(1))  # kB
                self.hugepage_total_mb = (huge_total * huge_size) / 1024
                self.hugepage_free_mb = (huge_free * huge_size) / 1024
                self.hugepage_used_mb = self.hugepage_total_mb - self.hugepage_free_mb
            except:
                pass

    def collect_hugepage_per_numa_stats(self):
        """Collect hugepage memory usage for each NUMA node"""
        self.hugepage_per_numa = {}
        try:
            node_dirs = [d for d in os.listdir("/sys/devices/system/node/") if d.startswith("node") and d[4:].isdigit()]
            for node in sorted(node_dirs, key=lambda x: int(x[4:])):
                node_id = int(node[4:])
                hugepages_dir = f"/sys/devices/system/node/{node}/hugepages"
                if not os.path.exists(hugepages_dir):
                    # Record the node even if hugepages directory doesn't exist (value is 0)
                    self.hugepage_per_numa[node_id] = {
                        "total_mb": 0.0,
                        "free_mb": 0.0,
                        "used_mb": 0.0,
                        "usage_pct": 0.0,
                    }
                    continue

                # Calculate MB for each hugepage size independently, then sum
                total_mb = 0.0
                free_mb = 0.0

                for subdir in os.listdir(hugepages_dir):
                    if subdir.startswith("hugepages-") and subdir.endswith("kB"):
                        size_match = re.search(r"hugepages-(\d+)kB", subdir)
                        if size_match:
                            huge_size_kb = int(size_match.group(1))
                            nr_path = os.path.join(hugepages_dir, subdir, "nr_hugepages")
                            free_path = os.path.join(hugepages_dir, subdir, "free_hugepages")
                            pages = 0
                            free_pages = 0
                            if os.path.exists(nr_path):
                                with open(nr_path) as f:
                                    pages = int(f.read().strip())
                            if os.path.exists(free_path):
                                with open(free_path) as f:
                                    free_pages = int(f.read().strip())
                            # Convert pages x size_kb to MB for this hugepage size
                            total_mb += (pages * huge_size_kb) / 1024
                            free_mb += (free_pages * huge_size_kb) / 1024

                used_mb = total_mb - free_mb
                self.hugepage_per_numa[node_id] = {
                    "total_mb": round(total_mb, 2),
                    "free_mb": round(free_mb, 2),
                    "used_mb": round(used_mb, 2),
                    "usage_pct": round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0.0,
                }
        except Exception:
            pass

        self.hugepage_per_numa_history.append(
            {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "nodes": dict(self.hugepage_per_numa)}
        )

    def print_numa_real_time(self):
        nodes = self.get_numa_nodes_memory()
        if not nodes:
            return
        print("=" * 100)
        print("NUMA Node Memory Real-time Usage")
        for n in nodes:
            print(
                f"    NUMA Node {n['node']:>2d} | Total Memory {n['total']:>8.2f} MB | Used {n['used']:>8.2f} MB | Free {n['free']:>8.2f} MB | Usage {n['usage']:>5.1f}%"
            )
        print("=" * 100)

    def print_final_numa_stats(self):
        if not self.numa_memory_history:
            return

        summary = defaultdict(lambda: {"used": [], "usage": []})

        for entry in self.numa_memory_history:
            for n in entry["nodes"]:
                node_id = n["node"]

                summary[node_id]["used"].append(n["used"])
                summary[node_id]["usage"].append(n["usage"])

        print("\n[ NUMA Node Memory Statistics Summary ]")

        for node_id in sorted(summary.keys()):
            data = summary[node_id]
            avg_used = sum(data["used"]) / len(data["used"])
            max_used = max(data["used"])
            avg_usage = sum(data["usage"]) / len(data["usage"])
            max_usage = max(data["usage"])
            print(
                f"NUMA Node {node_id:>2d} | Avg Used {avg_used:>8.2f} MB | Peak {max_used:>8.2f} MB | Avg Usage {avg_usage:>5.1f}% | Peak Usage {max_usage:.1f}%"
            )

    # ===================== Get System NUMA Nodes =====================
    def get_available_numa_nodes(self):
        try:
            nodes = []
            for f in os.listdir("/sys/devices/system/node/"):
                if f.startswith("node") and f[4:].isdigit():
                    nodes.append(int(f[4:]))
            return sorted(nodes)
        except:
            return [0]

    # ===================== Collect Specified NUMA Node CPU Usage =====================
    def collect_numa_cpu(self):
        try:
            all_cpu = psutil.cpu_percent(interval=None, percpu=True)

            for node in self.target_numa_nodes:
                with open(f"/sys/devices/system/node/node{node}/cpulist") as f:
                    cpulist = f.read().strip()

                cores = []
                for part in cpulist.split(","):
                    if "-" in part:
                        s, e = part.split("-")
                        cores.extend(range(int(s), int(e) + 1))
                    else:
                        cores.append(int(part))

                total = 0.0
                valid = 0
                for c in cores:
                    try:
                        total += all_cpu[c]
                        valid += 1
                    except:
                        pass
                avg = round(total / valid, 1) if valid > 0 else 0.0

                self.numa_cpu_history[node].append(avg)
                if avg > self.numa_cpu_peak[node]:
                    self.numa_cpu_peak[node] = avg
        except:
            pass

    # ===================== Collect Host Machine Total CPU/Memory =====================
    def collect_host_stats(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            mem_used_mb = round(mem.used / 1024 / 1024, 2)
            mem_total_mb = round(mem.total / 1024 / 1024, 2)
            mem_usage = round(mem.percent, 1)
            self.host_cpu_history.append(cpu)
            self.host_mem_history.append({"used_mb": mem_used_mb, "total_mb": mem_total_mb, "usage": mem_usage})
            if cpu > self.peak_host_cpu:
                self.peak_host_cpu = cpu
            if mem_used_mb > self.peak_host_mem_mb:
                self.peak_host_mem_mb = mem_used_mb
        except:
            pass

    # ===================== Collect Swap Usage =====================
    def collect_swap_stats(self):
        try:
            swap = psutil.swap_memory()
            swap_used_mb = round(swap.used / 1024 / 1024, 2)
            swap_total_mb = round(swap.total / 1024 / 1024, 2)
            swap_free_mb = round(swap.free / 1024 / 1024, 2)
            swap_usage = round(swap.percent, 1)
            self.swap_history.append(
                {"used_mb": swap_used_mb, "total_mb": swap_total_mb, "free_mb": swap_free_mb, "usage": swap_usage}
            )
            if swap_used_mb > self.peak_swap_used_mb:
                self.peak_swap_used_mb = swap_used_mb
        except:
            pass

    def get_vm_memory_from_numastat(self, pid):
        """Use numastat -p PID to get process memory (including hugepages)"""
        result = {"total_mb": 0.0, "huge_mb": 0.0, "private_mb": 0.0, "heap_mb": 0.0, "stack_mb": 0.0, "per_node": {}}
        try:
            output = subprocess.run(["numastat", "-p", str(pid)], capture_output=True, text=True, timeout=5)
            if output.returncode != 0:
                return result

            lines = output.stdout.strip().split("\n")
            node_ids = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("Node"):
                    for p in line.split():
                        if p.startswith("Node"):
                            try:
                                node_ids.append(int(p.replace("Node", "")))
                            except:
                                pass

            for line in lines:
                vals = re.findall(r"[\d.]+", line)
                if not vals or len(vals) < len(node_ids) + 1:
                    continue
                total_val = float(vals[-1])
                if line.startswith("Huge"):
                    result["huge_mb"] = total_val
                    for i, n in enumerate(node_ids):
                        result["per_node"][n] = result["per_node"].get(n, {})
                        result["per_node"][n]["huge_mb"] = float(vals[i])
                elif line.startswith("Heap"):
                    result["heap_mb"] = total_val
                    for i, n in enumerate(node_ids):
                        result["per_node"][n] = result["per_node"].get(n, {})
                        result["per_node"][n]["heap_mb"] = float(vals[i])
                elif line.startswith("Stack"):
                    result["stack_mb"] = total_val
                    for i, n in enumerate(node_ids):
                        result["per_node"][n] = result["per_node"].get(n, {})
                        result["per_node"][n]["stack_mb"] = float(vals[i])
                elif line.startswith("Private"):
                    result["private_mb"] = total_val
                    for i, n in enumerate(node_ids):
                        result["per_node"][n] = result["per_node"].get(n, {})
                        result["per_node"][n]["private_mb"] = float(vals[i])
                elif line.startswith("Total") and "---" not in line:
                    result["total_mb"] = total_val
                    for i, n in enumerate(node_ids):
                        result["per_node"][n] = result["per_node"].get(n, {})
                        result["per_node"][n]["total_mb"] = float(vals[i])
        except:
            pass
        return result

    # ==================== Template Methods ====================
    def collect_sample(self):
        """Collect one sample (full refresh each time)"""
        self.collect_hugepage_stats()
        self.collect_numa_cpu()
        self.collect_host_stats()
        self.collect_swap_stats()
        vms = self.get_vms_realtime()
        self.last_vm_count = len(vms)

        timestamp = datetime.now()
        sample_data = []

        if not vms:
            return []

        for vm in vms:
            record = {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "vm_name": vm["name"],
                "pid": vm["pid"],
                "cpu_percent": vm["cpu_percent"],
                "memory_mb": vm["memory_mb"],
                "memory_huge_mb": vm.get("memory_huge_mb", 0),
                "memory_private_mb": vm.get("memory_private_mb", 0),
                "memory_heap_mb": vm.get("memory_heap_mb", 0),
                "memory_per_numa": vm.get("memory_per_numa", {}),
                "status": vm["status"],
            }
            sample_data.append(record)
            self.data.append(record)
        return sample_data

    def display_realtime_table(self, sample_data, elapsed_time, duration, check_method=""):
        """Display real-time table"""
        # Use more reliable clear screen method
        print("\033[2J\033[H\033[?25h", end="", flush=True)
        width = 100
        print("=" * width, flush=True)
        print(f"{self.get_monitor_title()} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        print(
            f"Elapsed: {elapsed_time} | Target: {duration if duration else 'Infinite'} | Detection Method: {check_method}",
            flush=True,
        )
        print("=" * width, flush=True)

        # Real-time display of specified NUMA node CPU
        numa_str = ""
        for node in self.target_numa_nodes:
            hist = self.numa_cpu_history[node]
            current = hist[-1] if hist else 0.0
            peak = self.numa_cpu_peak[node]
            numa_str += f"NUMA{node} CPU: {current:.1f}% (Peak {peak:.1f}%)  "
        print(numa_str.strip(), flush=True)

        # Total Hugepages
        huge_usage = (
            round((self.hugepage_used_mb / self.hugepage_total_mb * 100), 1) if self.hugepage_total_mb > 0 else 0.0
        )
        print(
            f"Hugepage Memory: Total {self.hugepage_total_mb:.0f} MB  Used {self.hugepage_used_mb:.0f} MB ({huge_usage:.1f}%)  Free {self.hugepage_free_mb:.0f} MB",
            flush=True,
        )

        # Per-NUMA node hugepage statistics
        if self.hugepage_per_numa:
            print("Per-NUMA Node Hugepages:", flush=True)
            for node_id in sorted(self.hugepage_per_numa.keys()):
                hp = self.hugepage_per_numa[node_id]
                print(
                    f"    NUMA {node_id:>2d} | Total {hp['total_mb']:>8.0f} MB | Used {hp['used_mb']:>8.0f} MB ({hp['usage_pct']:>5.1f}%) | Free {hp['free_mb']:>8.0f} MB",
                    flush=True,
                )

        # Host machine total resources
        if self.host_cpu_history:
            host_cpu = self.host_cpu_history[-1]
            host_mem = self.host_mem_history[-1]
            print(
                f"Host Machine: CPU {host_cpu:.1f}% (Peak {self.peak_host_cpu:.1f}%) | Memory {host_mem['used_mb']:.0f}/{host_mem['total_mb']:.0f} MB ({host_mem['usage']:.1f}%, Peak {self.peak_host_mem_mb:.0f} MB)",
                flush=True,
            )

        # Swap
        if self.swap_history:
            s = self.swap_history[-1]
            if s["total_mb"] > 0:
                print(
                    f"Swap:      Used {s['used_mb']:.0f}/{s['total_mb']:.0f} MB ({s['usage']:.1f}%) | Peak {self.peak_swap_used_mb:.0f} MB",
                    flush=True,
                )
            else:
                print("Swap:      Not enabled", flush=True)

        self.print_numa_real_time()

        if not sample_data:
            print(self.get_no_vm_message(), flush=True)
            return

        # Table header
        header = f"{'VM Name':<28} {'PID':<10} {'CPU%':<10} {'Memory(MB)':<12} {'Hugepage(MB)':<12} {'Status':<10}"
        print(header, flush=True)
        print("-" * width, flush=True)

        sorted_vms = sorted(sample_data, key=lambda x: x["cpu_percent"], reverse=True)
        for vm in sorted_vms[:15]:
            name = vm["vm_name"][:27] if len(vm["vm_name"]) > 27 else vm["vm_name"]
            huge_mb = vm.get("memory_huge_mb", 0.0)
            row = (
                f"{name:<28} {vm['pid']:<10} {vm['cpu_percent']:<10.2f} "
                f"{vm['memory_mb']:<12.2f} {huge_mb:<12.2f} {vm['status']:<10}"
            )
            print(row, flush=True)

        if len(sorted_vms) > 15:
            print(f"... {len(sorted_vms) - 15} more virtual machines ...", flush=True)

        print("-" * width, flush=True)
        print(f"Total: {len(sample_data)} virtual machines | Data points: {len(self.data)}", flush=True)
        print("Press Ctrl+C to stop monitoring", flush=True)

    def check_stress_process(self, stress_pattern):
        try:
            for proc in psutil.process_iter(["cmdline", "name"]):
                try:
                    cl = " ".join(proc.info["cmdline"] or [])
                    if stress_pattern in cl or stress_pattern in (proc.info["name"] or ""):
                        return True
                except:
                    continue
            return False
        except:
            return False

    def check_stress_file(self, file_path):
        return os.path.exists(file_path)

    def wait_for_stress_and_monitor(self, check_type, check_target, interval_seconds=5, duration_seconds=None):
        """Wait for stress test to start, then monitor for specified duration.

        Args:
            check_type: 'process' or 'file'
            check_target: process name or file path
            interval_seconds: sampling interval
            duration_seconds: optional max duration (monitor stops after this even if stress still running)
        """
        print(f"Waiting for stress test to start... (Detection method: {check_type}={check_target})")
        if duration_seconds:
            print(f"Duration limit: {duration_seconds}s (will stop after this time)")
        stress_started = False
        while not stress_started:
            stress_started = (
                self.check_stress_process(check_target)
                if check_type == "process"
                else self.check_stress_file(check_target)
            )
            if not stress_started:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for stress test to start...")
                time.sleep(2)

        print("Stress test detected! Starting monitoring...")
        self.running = True
        start_time = time.time()

        def handler(sig, frame):
            print("\n\nStop signal received, ending monitoring...")
            self.running = False

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

        sys.stdout.flush()
        time.sleep(0.5)

        try:
            while self.running:
                loop_start = time.time()

                elapsed = time.time() - start_time
                if duration_seconds and elapsed >= duration_seconds:
                    print(f"\nDuration limit ({duration_seconds}s) reached, stopping monitoring")
                    self.running = False
                    break

                if check_type == "process" and not self.check_stress_process(check_target):
                    print("\nStress process ended, stopping monitoring")
                    self.running = False
                    break
                if check_type == "file" and not self.check_stress_file(check_target):
                    print("\nStress file removed, stopping monitoring")
                    self.running = False
                    break

                sample = self.collect_sample()
                elapsed_str = str(timedelta(seconds=int(elapsed)))
                dur_str = str(timedelta(seconds=int(duration_seconds))) if duration_seconds else "infinity"
                self.display_realtime_table(sample, elapsed_str, dur_str, f"Stress Sync: {check_type}={check_target}")

                sl = max(0, interval_seconds - (time.time() - loop_start))
                if sl > 0 and self.running:
                    time.sleep(sl)
        except KeyboardInterrupt:
            pass
        return self.data

    def start_monitoring(self, duration_seconds=None, interval_seconds=5):
        self.running = True
        start_time = time.time()

        def handler(sig, frame):
            print("\nStopping monitoring...")
            self.running = False

        signal.signal(signal.SIGINT, handler)

        print("Starting VM monitoring...")
        print(
            f"Sampling interval: {interval_seconds}s | {'Run indefinitely' if not duration_seconds else f'Duration: {duration_seconds}s'}"
        )

        sys.stdout.flush()
        time.sleep(0.5)

        try:
            while self.running:
                loop_start = time.time()
                sample = self.collect_sample()
                elapsed = time.time() - start_time
                elapsed_str = str(timedelta(seconds=int(elapsed)))
                dur_str = str(timedelta(seconds=int(duration_seconds))) if duration_seconds else "infinity"
                self.display_realtime_table(sample, elapsed_str, dur_str, "Timer Mode")

                if duration_seconds and elapsed >= duration_seconds:
                    print("\nTimer duration reached, monitoring complete")
                    self.running = False
                    break

                sl = max(0, interval_seconds - (time.time() - loop_start))
                if sl > 0 and self.running:
                    time.sleep(sl)
        except KeyboardInterrupt:
            pass
        return self.data

    # ==================== Export and Analysis Methods ====================
    def export_raw_csv(self, filename=None):
        if not filename:
            filename = f"{self.get_csv_filename_prefix()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            fieldnames = [
                "timestamp",
                "vm_name",
                "pid",
                "cpu_percent",
                "memory_mb",
                "memory_huge_mb",
                "memory_private_mb",
                "memory_heap_mb",
                "status",
            ]
            w = csv.DictWriter(f, fieldnames, extrasaction="ignore")
            w.writeheader()
            if self.data:
                w.writerows(self.data)
        print(f"\n[OK] Raw data: {filename} ({len(self.data)} rows)")
        return filename

    def calculate_vm_stats(self):
        vm_data = defaultdict(list)
        for r in self.data:
            vm_data[r["vm_name"]].append(r)
        stats = []
        for name, recs in sorted(vm_data.items()):
            cpus = [r["cpu_percent"] for r in recs]
            mems = [r["memory_mb"] for r in recs]
            huge = [r.get("memory_huge_mb", 0) for r in recs]
            private = [r.get("memory_private_mb", 0) for r in recs]
            heap = [r.get("memory_heap_mb", 0) for r in recs]
            stats.append(
                {
                    "vm_name": name,
                    "pid": recs[0]["pid"],
                    "sample_count": len(recs),
                    "avg_cpu": round(sum(cpus) / len(cpus), 2),
                    "max_cpu": round(max(cpus), 2),
                    "avg_memory_mb": round(sum(mems) / len(mems), 2),
                    "max_memory_mb": round(max(mems), 2),
                    "min_memory_mb": round(min(mems), 2),
                    "last_memory_mb": mems[-1],
                    "avg_huge_mb": round(sum(huge) / len(huge), 2) if huge else 0,
                    "max_huge_mb": round(max(huge), 2) if huge else 0,
                    "avg_private_mb": round(sum(private) / len(private), 2) if private else 0,
                    "max_private_mb": round(max(private), 2) if private else 0,
                    "avg_heap_mb": round(sum(heap) / len(heap), 2) if heap else 0,
                    "max_heap_mb": round(max(heap), 2) if heap else 0,
                }
            )
        return stats

    def calculate_overall_stats(self, vm_stats):
        ac = [v["avg_cpu"] for v in vm_stats]
        am = [v["avg_memory_mb"] for v in vm_stats]
        return {
            "total_vms": len(vm_stats),
            "overall_avg_cpu": round(sum(ac) / len(ac), 2) if ac else 0,
            "overall_max_cpu": round(max([v["max_cpu"] for v in vm_stats]), 2) if vm_stats else 0,
            "overall_avg_memory_mb": round(sum(am) / len(am), 2) if am else 0,
            "overall_max_memory_mb": round(max([v["max_memory_mb"] for v in vm_stats]), 2) if vm_stats else 0,
            "total_avg_memory_mb": round(sum(am), 2),
            "total_avg_memory_gb": round(sum(am) / 1024, 2),
        }

    def export_summary_csv(self, vm_stats, overall_stats, filename=None):
        if not filename:
            filename = f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["=== Host Machine Statistics ===", ""])
            w.writerow(["Total VMs", f"{overall_stats['total_vms']}"])
            w.writerow(["Alive VM Count", self.last_vm_count])

            host_cpu_avg = (
                round(sum(self.host_cpu_history) / len(self.host_cpu_history), 1) if self.host_cpu_history else 0
            )
            w.writerow(["Host Avg CPU%", host_cpu_avg])
            w.writerow(["Host Peak CPU%", round(self.peak_host_cpu, 1)])
            host_mem_avg_mb = (
                round(sum(h["used_mb"] for h in self.host_mem_history) / len(self.host_mem_history), 2)
                if self.host_mem_history
                else 0
            )
            w.writerow(["Host Avg Memory MB", host_mem_avg_mb])
            w.writerow(["Host Peak Memory MB", round(self.peak_host_mem_mb, 2)])

            for node in sorted(self.numa_cpu_history.keys()):
                hist = self.numa_cpu_history[node]
                avg = round(sum(hist) / len(hist), 1) if hist else 0
                peak = self.numa_cpu_peak[node]
                w.writerow([f"NUMA{node} Avg CPU%", avg])
                w.writerow([f"NUMA{node} Peak CPU%", peak])

            w.writerows(
                [
                    ["VM Avg CPU%", overall_stats["overall_avg_cpu"]],
                    ["VM Max CPU%", overall_stats["overall_max_cpu"]],
                    ["Peak Total CPU", round(self.peak_total_cpu, 1)],
                    ["VM Avg Memory MB", overall_stats["overall_avg_memory_mb"]],
                    ["VM Max Memory MB", overall_stats["overall_max_memory_mb"]],
                    ["Total Memory MB (Avg)", overall_stats["total_avg_memory_mb"]],
                ]
            )
            w.writerow(["Hugepage Total MB", round(self.hugepage_total_mb, 0)])
            w.writerow(
                [
                    "Hugepage Avg Usage MB",
                    round(sum(self.hugepage_used_history) / len(self.hugepage_used_history), 0)
                    if self.hugepage_used_history
                    else 0,
                ]
            )
            w.writerow(["Hugepage Peak Usage MB", round(self.peak_hugepage_used_mb, 0)])
            huge_usage_final = (
                round((self.peak_hugepage_used_mb / self.hugepage_total_mb * 100), 1)
                if self.hugepage_total_mb > 0
                else 0
            )
            w.writerow(["Hugepage Peak Usage %", huge_usage_final])

            if self.hugepage_per_numa_history:
                w.writerow([])
                w.writerow(["=== Per-NUMA Node Hugepage Statistics ==="])
                numa_huge_summary = defaultdict(lambda: {"total": [], "used": [], "free": []})
                for entry in self.hugepage_per_numa_history:
                    for node_id, data in entry["nodes"].items():
                        numa_huge_summary[node_id]["total"].append(data["total_mb"])
                        numa_huge_summary[node_id]["used"].append(data["used_mb"])
                        numa_huge_summary[node_id]["free"].append(data["free_mb"])

                for node_id in sorted(numa_huge_summary.keys()):
                    data = numa_huge_summary[node_id]
                    avg_total = round(sum(data["total"]) / len(data["total"]), 0) if data["total"] else 0
                    avg_used = round(sum(data["used"]) / len(data["used"]), 0) if data["used"] else 0
                    avg_free = round(sum(data["free"]) / len(data["free"]), 0) if data["free"] else 0
                    avg_usage = round(avg_used / avg_total * 100, 1) if avg_total > 0 else 0
                    w.writerow([f"NUMA{node_id} Hugepage Total MB", avg_total])
                    w.writerow([f"NUMA{node_id} Hugepage Avg Used MB", avg_used])
                    w.writerow([f"NUMA{node_id} Hugepage Avg Free MB", avg_free])
                    w.writerow([f"NUMA{node_id} Hugepage Avg Usage %", avg_usage])

            swap_avg_mb = (
                round(sum(s["used_mb"] for s in self.swap_history) / len(self.swap_history), 0)
                if self.swap_history
                else 0
            )
            w.writerow(
                ["Swap Total Capacity MB", round(self.swap_history[0]["total_mb"], 0) if self.swap_history else 0]
            )
            w.writerow(["Swap Avg Usage MB", swap_avg_mb])
            w.writerow(["Swap Peak Usage MB", round(self.peak_swap_used_mb, 0)])
            swap_total = self.swap_history[0]["total_mb"] if self.swap_history else 0
            swap_peak_pct = round(self.peak_swap_used_mb / swap_total * 100, 1) if swap_total > 0 else 0
            w.writerow(["Swap Peak Usage %", swap_peak_pct])

            w.writerow([])
            w.writerow(["=== Single VM Statistics ==="])
            w.writerow(
                [
                    "VM",
                    "PID",
                    "Samples",
                    "avgCPU",
                    "maxCPU",
                    "avgMem",
                    "maxMem",
                    "minMem",
                    "lastMem",
                    "avgHuge",
                    "maxHuge",
                    "avgPrivate",
                    "maxPrivate",
                    "avgHeap",
                    "maxHeap",
                ]
            )
            for v in vm_stats:
                w.writerow(
                    [
                        v["vm_name"],
                        v["pid"],
                        v["sample_count"],
                        v["avg_cpu"],
                        v["max_cpu"],
                        v["avg_memory_mb"],
                        v["max_memory_mb"],
                        v["min_memory_mb"],
                        v["last_memory_mb"],
                        v.get("avg_huge_mb", 0),
                        v.get("max_huge_mb", 0),
                        v.get("avg_private_mb", 0),
                        v.get("max_private_mb", 0),
                        v.get("avg_heap_mb", 0),
                        v.get("max_heap_mb", 0),
                    ]
                )
        print(f"[OK] Summary report: {filename}")
        return filename

    def print_summary_report(self, vm_stats, overall_stats):
        print("\n" + "=" * 85)
        print(f"{self.get_monitor_title()} Summary Report")
        print("=" * 85)
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if self.data:
            print(f"Period: {self.data[0]['timestamp']} ~ {self.data[-1]['timestamp']}")

        self.print_final_numa_stats()

        print("\n[Specified NUMA Node CPU Statistics]")
        for node in sorted(self.numa_cpu_history.keys()):
            hist = self.numa_cpu_history[node]
            avg = round(sum(hist) / len(hist), 1) if hist else 0
            peak = self.numa_cpu_peak[node]
            print(f"  NUMA {node:>2d} | Avg CPU: {avg:>5.1f}% | Peak CPU: {peak:>5.1f}%")

        print("\n[Host Machine Total Resources]")
        host_cpu_avg = round(sum(self.host_cpu_history) / len(self.host_cpu_history), 1) if self.host_cpu_history else 0
        host_mem_avg_mb = (
            round(sum(h["used_mb"] for h in self.host_mem_history) / len(self.host_mem_history), 2)
            if self.host_mem_history
            else 0
        )
        host_mem_total_mb = round(self.host_mem_history[0]["total_mb"], 0) if self.host_mem_history else 0
        print(f"  Host Avg CPU:  {host_cpu_avg:.1f}% (Peak {self.peak_host_cpu:.1f}%)")
        print(
            f"  Host Avg Memory: {host_mem_avg_mb:.0f}/{host_mem_total_mb:.0f} MB (Peak {self.peak_host_mem_mb:.0f} MB)"
        )
        print("")
        print(f"  Total VMs:    {overall_stats['total_vms']}")
        print(f"  Alive VMs:     {self.last_vm_count}")
        print(f"  Avg CPU:       {overall_stats['overall_avg_cpu']}%")
        print(f"  Max CPU:       {overall_stats['overall_max_cpu']}%")
        print(f"  Peak Total CPU:     {round(self.peak_total_cpu, 1)}%")
        print(f"  Avg Memory:      {overall_stats['overall_avg_memory_mb']} MB")
        print(
            f"  Total Memory (Avg):   {overall_stats['total_avg_memory_mb']} MB ({overall_stats['total_avg_memory_gb']} GB)"
        )
        print("[Hugepage Memory Info]")
        print(f"  Hugepage Total:     {self.hugepage_total_mb:.0f} MB")
        avg_huge = (
            round(sum(self.hugepage_used_history) / len(self.hugepage_used_history), 0)
            if self.hugepage_used_history
            else 0
        )
        huge_usage = (
            round((self.peak_hugepage_used_mb / self.hugepage_total_mb * 100), 1) if self.hugepage_total_mb > 0 else 0
        )
        print(f"  Hugepage Avg Usage:    {avg_huge} MB")
        print(f"  Hugepage Peak Usage:    {self.peak_hugepage_used_mb:.0f} MB ({huge_usage:.1f}%)")

        if self.hugepage_per_numa_history:
            print("\n[Per-NUMA Node Hugepage Statistics]")
            numa_huge_summary = defaultdict(lambda: {"total": [], "used": [], "free": []})
            for entry in self.hugepage_per_numa_history:
                for node_id, data in entry["nodes"].items():
                    numa_huge_summary[node_id]["total"].append(data["total_mb"])
                    numa_huge_summary[node_id]["used"].append(data["used_mb"])
                    numa_huge_summary[node_id]["free"].append(data["free_mb"])

            for node_id in sorted(numa_huge_summary.keys()):
                data = numa_huge_summary[node_id]
                avg_total = round(sum(data["total"]) / len(data["total"]), 0) if data["total"] else 0
                avg_used = round(sum(data["used"]) / len(data["used"]), 0) if data["used"] else 0
                avg_free = round(sum(data["free"]) / len(data["free"]), 0) if data["free"] else 0
                avg_usage = round(avg_used / avg_total * 100, 1) if avg_total > 0 else 0
                print(
                    f"  NUMA {node_id:>2d} | Total {avg_total:>8.0f} MB | Used {avg_used:>8.0f} MB ({avg_usage:>5.1f}%) | Free {avg_free:>8.0f} MB"
                )

        if self.swap_history:
            swap_avg_mb = (
                round(sum(s["used_mb"] for s in self.swap_history) / len(self.swap_history), 0)
                if self.swap_history
                else 0
            )
            swap_total_mb = self.swap_history[0]["total_mb"] if self.swap_history else 0
            swap_peak_pct = round(self.peak_swap_used_mb / swap_total_mb * 100, 1) if swap_total_mb > 0 else 0
            print("[Swap Partition]")
            print(f"  Total Capacity:     {swap_total_mb:.0f} MB")
            print(f"  Avg Usage:    {swap_avg_mb:.0f} MB")
            print(f"  Peak Usage:    {self.peak_swap_used_mb:.0f} MB ({swap_peak_pct:.1f}%)")

        if vm_stats:
            print("\n[TOP10 CPU]")
            for i, v in enumerate(sorted(vm_stats, key=lambda x: x["avg_cpu"], reverse=True)[:10], 1):
                n = v["vm_name"][:29]
                print(f"{i:2}. {n:<30} {v['avg_cpu']:6.2f}% (max {v['max_cpu']:.2f})")

            print("\n[TOP10 Memory]")
            for i, v in enumerate(sorted(vm_stats, key=lambda x: x["avg_memory_mb"], reverse=True)[:10], 1):
                n = v["vm_name"][:29]
                huge_mb = v.get("avg_huge_mb", 0)
                print(f"{i:2}. {n:<30} {v['avg_memory_mb']:8.2f} MB (Hugepage {huge_mb:.2f} MB)")
        print("=" * 85)

    def analyze_and_export(self, raw=None, summary=None):
        """Analyze collected data and export reports

        Note: Imports exporters module lazily to avoid circular dependency.
        """
        vs = self.calculate_vm_stats()
        os = self.calculate_overall_stats(vs)
        rf = self.export_raw_csv(raw)
        sf = self.export_summary_csv(vs, os, summary)
        self.print_summary_report(vs, os)
        return rf, sf
