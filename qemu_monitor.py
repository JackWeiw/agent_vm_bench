#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QEMU Virtual Machine Real-time Monitoring Tool
Supports synchronization with stress tests via signals, file markers, or process detection

python3 qemu_monitor.py -t 600 -i 2
"""

import psutil
import time
import argparse
import csv
import signal
import re
import subprocess
import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict
import threading

class QEMUMonitor:
    def __init__(self):
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

        # ===================== Per-NUMA Node Hugepage Statistics =====================
        self.hugepage_per_numa = {}  # {node_id: {'total_mb': x, 'free_mb': y, 'used_mb': z}}
        self.hugepage_per_numa_history = []  # Record history
        # =====================================================================

        # ===================== Host Machine Total Resource Statistics =====================
        self.host_cpu_history = []       # [(timestamp, cpu%)]
        self.host_mem_history = []       # [(timestamp, used_mb, total_mb, usage%)]
        self.peak_host_cpu = 0.0
        self.peak_host_mem_mb = 0.0

        # ===================== Swap Statistics =====================
        self.swap_history = []           # [(timestamp, used_mb, total_mb, free_mb, usage%)]
        self.peak_swap_used_mb = 0.0
        # =====================================================================

        # ===================== Specified NUMA Node CPU Statistics =====================
        self.target_numa_nodes = [0]  # Default: monitor NUMA 0
        self.numa_cpu_history = defaultdict(list)  # key: node id, value: cpu% list
        self.numa_cpu_peak = defaultdict(float)
        self.available_numa_nodes = self.get_available_numa_nodes()
        # =====================================================================

        # ===================== Baseline Noise =====================
        self.baseline_data = None          # Loaded baseline noise data
        self.baseline_file = None          # Baseline noise file path
        self.baseline_samples = 0          # Baseline noise sample count
        # =====================================================================

    # ==================== NUMA Memory Statistics ====================
    def get_numa_nodes_memory(self):
        numa_nodes = []
        try:
            node_dirs = [d for d in os.listdir('/sys/devices/system/node/') if d.startswith('node') and d[4:].isdigit()]
            for node in sorted(node_dirs, key=lambda x: int(x[4:])):
                node_id = int(node[4:])
                path = f'/sys/devices/system/node/{node}/meminfo'
                with open(path) as f:
                    lines = f.read().splitlines()
                total = free = 0
                for l in lines:
                    if 'MemTotal' in l:
                        total = int(l.split()[3]) * 1024
                    if 'MemFree' in l:
                        free = int(l.split()[3]) * 1024
                used = total - free
                total_mb = round(total / 1024 / 1024, 2)
                used_mb = round(used / 1024 / 1024, 2)
                free_mb = round(free / 1024 / 1024, 2)
                usage = round(used / total * 100, 2) if total > 0 else 0.0
                numa_nodes.append({
                    'node': node_id, 'total': total_mb, 'used': used_mb, 'free': free_mb, 'usage': usage
                })
        except:
            pass
        self.numa_memory_history.append({
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'nodes': numa_nodes
        })
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
            self.hugepage_total_mb += stats['total_mb']
            self.hugepage_free_mb += stats['free_mb']

        self.hugepage_used_mb = self.hugepage_total_mb - self.hugepage_free_mb
        self.hugepage_used_history.append(self.hugepage_used_mb)

        if self.hugepage_used_mb > self.peak_hugepage_used_mb:
            self.peak_hugepage_used_mb = self.hugepage_used_mb

        # Fallback to /proc/meminfo if per-NUMA collection failed
        if self.hugepage_total_mb == 0 and self.hugepage_per_numa == {}:
            try:
                with open("/proc/meminfo", "r") as f:
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
            node_dirs = [d for d in os.listdir('/sys/devices/system/node/') if d.startswith('node') and d[4:].isdigit()]
            for node in sorted(node_dirs, key=lambda x: int(x[4:])):
                node_id = int(node[4:])
                hugepages_dir = f'/sys/devices/system/node/{node}/hugepages'
                if not os.path.exists(hugepages_dir):
                    # Record the node even if hugepages directory doesn't exist (value is 0)
                    self.hugepage_per_numa[node_id] = {
                        'total_mb': 0.0, 'free_mb': 0.0, 'used_mb': 0.0, 'usage_pct': 0.0
                    }
                    continue

                # Calculate MB for each hugepage size independently, then sum
                total_mb = 0.0
                free_mb = 0.0

                for subdir in os.listdir(hugepages_dir):
                    if subdir.startswith('hugepages-') and subdir.endswith('kB'):
                        size_match = re.search(r'hugepages-(\d+)kB', subdir)
                        if size_match:
                            huge_size_kb = int(size_match.group(1))
                            nr_path = os.path.join(hugepages_dir, subdir, 'nr_hugepages')
                            free_path = os.path.join(hugepages_dir, subdir, 'free_hugepages')
                            pages = 0
                            free_pages = 0
                            if os.path.exists(nr_path):
                                with open(nr_path) as f:
                                    pages = int(f.read().strip())
                            if os.path.exists(free_path):
                                with open(free_path) as f:
                                    free_pages = int(f.read().strip())
                            # Convert pages × size_kb to MB for this hugepage size
                            total_mb += (pages * huge_size_kb) / 1024
                            free_mb += (free_pages * huge_size_kb) / 1024

                used_mb = total_mb - free_mb
                self.hugepage_per_numa[node_id] = {
                    'total_mb': round(total_mb, 2),
                    'free_mb': round(free_mb, 2),
                    'used_mb': round(used_mb, 2),
                    'usage_pct': round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0.0
                }
        except Exception as e:
            pass

        self.hugepage_per_numa_history.append({
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'nodes': dict(self.hugepage_per_numa)
        })

    def print_numa_real_time(self):
        nodes = self.get_numa_nodes_memory()
        if not nodes:
            return
        print("=" * 100)
        print(f"📊 Local NUMA Node Memory Real-time Usage")
        for n in nodes:
            print(f"    NUMA Node {n['node']:>2d} | Total Memory {n['total']:>8.2f} MB | Used {n['used']:>8.2f} MB | Free {n['free']:>8.2f} MB | Usage {n['usage']:>5.1f}%")
        print("=" * 100)

    def print_final_numa_stats(self):
        if not self.numa_memory_history:
            return

        summary = defaultdict(lambda: {'used': [], 'usage': []})  # Auto-initialize, supports dynamic nodes

        for entry in self.numa_memory_history:
            for n in entry['nodes']:
                node_id = n['node']

                summary[node_id]['used'].append(n['used'])
                summary[node_id]['usage'].append(n['usage'])

        print("\n[ NUMA Node Memory Statistics Summary ]")

        for node_id in sorted(summary.keys()):
            data = summary[node_id]
            avg_used = sum(data['used']) / len(data['used'])
            max_used = max(data['used'])
            avg_usage = sum(data['usage']) / len(data['usage'])
            max_usage = max(data['usage'])
            print(f"NUMA Node {node_id:>2d} | Avg Used {avg_used:>8.2f} MB | Peak {max_used:>8.2f} MB | Avg Usage {avg_usage:>5.1f}% | Peak Usage {max_usage:.1f}%")

    # ===================== Get System NUMA Nodes =====================
    def get_available_numa_nodes(self):
        try:
            nodes = []
            for f in os.listdir('/sys/devices/system/node/'):
                if f.startswith('node') and f[4:].isdigit():
                    nodes.append(int(f[4:]))
            return sorted(nodes)
        except:
            return [0]

    # ===================== Collect Specified NUMA Node CPU Usage =====================
    def collect_numa_cpu(self):
        try:
            all_cpu = psutil.cpu_percent(interval=None, percpu=True)

            for node in self.target_numa_nodes:
                with open(f'/sys/devices/system/node/node{node}/cpulist') as f:
                    cpulist = f.read().strip()

                cores = []
                for part in cpulist.split(','):
                    if '-' in part:
                        s, e = part.split('-')
                        cores.extend(range(int(s), int(e)+1))
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
            self.host_mem_history.append({
                'used_mb': mem_used_mb, 'total_mb': mem_total_mb, 'usage': mem_usage
            })
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
            self.swap_history.append({
                'used_mb': swap_used_mb, 'total_mb': swap_total_mb,
                'free_mb': swap_free_mb, 'usage': swap_usage
            })
            if swap_used_mb > self.peak_swap_used_mb:
                self.peak_swap_used_mb = swap_used_mb
        except:
            pass

    def get_vm_memory_from_numastat(self, pid):
        """Use numastat -p PID to get process memory (including hugepages)"""
        result = {'total_mb': 0.0, 'huge_mb': 0.0, 'private_mb': 0.0, 'heap_mb': 0.0, 'stack_mb': 0.0, 'per_node': {}}
        try:
            output = subprocess.run(['numastat', '-p', str(pid)], capture_output=True, text=True, timeout=5)
            if output.returncode != 0:
                return result

            lines = output.stdout.strip().split('\n')
            node_ids = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith('Node'):
                    for p in line.split():
                        if p.startswith('Node'):
                            try: node_ids.append(int(p.replace('Node', '')))
                            except: pass

            for line in lines:
                vals = re.findall(r'[\d.]+', line)
                if not vals or len(vals) < len(node_ids) + 1:
                    continue
                total_val = float(vals[-1])
                if line.startswith('Huge'):
                    result['huge_mb'] = total_val
                    for i, n in enumerate(node_ids):
                        result['per_node'][n] = result['per_node'].get(n, {})
                        result['per_node'][n]['huge_mb'] = float(vals[i])
                elif line.startswith('Heap'):
                    result['heap_mb'] = total_val
                    for i, n in enumerate(node_ids):
                        result['per_node'][n] = result['per_node'].get(n, {})
                        result['per_node'][n]['heap_mb'] = float(vals[i])
                elif line.startswith('Stack'):
                    result['stack_mb'] = total_val
                    for i, n in enumerate(node_ids):
                        result['per_node'][n] = result['per_node'].get(n, {})
                        result['per_node'][n]['stack_mb'] = float(vals[i])
                elif line.startswith('Private'):
                    result['private_mb'] = total_val
                    for i, n in enumerate(node_ids):
                        result['per_node'][n] = result['per_node'].get(n, {})
                        result['per_node'][n]['private_mb'] = float(vals[i])
                elif line.startswith('Total') and '---' not in line:
                    result['total_mb'] = total_val
                    for i, n in enumerate(node_ids):
                        result['per_node'][n] = result['per_node'].get(n, {})
                        result['per_node'][n]['total_mb'] = float(vals[i])
        except:
            pass
        return result

    def get_qemu_vms_realtime(self):
        """Real physical memory: Use numastat to get (including hugepage memory)"""
        vms = []
        current_pids = set()
        current_total_mem = 0.0
        current_total_cpu = 0.0

        # Support both qemu-kvm and qemu-system process names
        qemu_process_names = ('qemu-kvm', 'qemu-system')

        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
            try:
                proc_name = proc.info['name'] or ''
                # Check if process name matches any QEMU variant
                if not any(qemu_name in proc_name for qemu_name in qemu_process_names):
                    continue

                pid = proc.info['pid']
                current_pids.add(pid)
                cmdline = ' '.join(proc.info['cmdline'] or [])
                name_match = re.search(r'-name\s+([^,\s]+)', cmdline)
                vm_name = name_match.group(1) if name_match else f"vm-{pid}"

                # ===================== [Real Physical Memory: numastat] =====================
                # Use numastat -p PID to get memory (including hugepages)
                numastat_mem = self.get_vm_memory_from_numastat(pid)
                memory_mb = numastat_mem.get('total_mb', 0.0)
                memory_huge_mb = numastat_mem.get('huge_mb', 0.0)
                memory_private_mb = numastat_mem.get('private_mb', 0.0)
                memory_heap_mb = numastat_mem.get('heap_mb', 0.0)
                memory_per_numa = numastat_mem.get('per_node', {})

                # If numastat fails, fall back to psutil
                if memory_mb <= 0:
                    try:
                        mem_info = proc.memory_info()
                        memory_mb = round(mem_info.pss / 1024 / 1024, 2)
                    except:
                        mem_info = proc.memory_info()
                        memory_mb = round(mem_info.rss / 1024 / 1024, 2)
                # =================================================================

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

                vms.append({
                    'pid': pid,
                    'name': vm_name,
                    'cpu_percent': cpu,
                    'memory_mb': memory_mb,
                    'memory_huge_mb': memory_huge_mb,
                    'memory_private_mb': memory_private_mb,
                    'memory_heap_mb': memory_heap_mb,
                    'memory_per_numa': memory_per_numa,
                    'status': proc.info['status']
                })

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

    def collect_sample(self):
        """Collect one sample (full refresh each time)"""
        self.collect_hugepage_stats()
        self.collect_numa_cpu()
        self.collect_host_stats()  # Collect host machine total resources
        self.collect_swap_stats()  # Collect Swap
        vms = self.get_qemu_vms_realtime()
        self.last_vm_count = len(vms)

        timestamp = datetime.now()
        sample_data = []

        if not vms:
            return []

        for vm in vms:
            record = {
                'timestamp': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                'vm_name': vm['name'],
                'pid': vm['pid'],
                'cpu_percent': vm['cpu_percent'],
                'memory_mb': vm['memory_mb'],
                'memory_huge_mb': vm.get('memory_huge_mb', 0),
                'memory_private_mb': vm.get('memory_private_mb', 0),
                'memory_heap_mb': vm.get('memory_heap_mb', 0),
                'memory_per_numa': vm.get('memory_per_numa', {}),
                'status': vm['status']
            }
            sample_data.append(record)
            self.data.append(record)
        return sample_data

    def display_realtime_table(self, sample_data, elapsed_time, duration, check_method=""):
        """Display real-time table"""
        print('\033[2J\033[H', end='')
        print("=" * 100)
        print(f"QEMU VM Real-time Monitoring - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Elapsed: {elapsed_time} | Target: {duration if duration else 'Infinite'} | Detection Method: {check_method}")
        print("=" * 100)

        # Real-time display of specified NUMA node CPU
        numa_str = ""
        for node in self.target_numa_nodes:
            hist = self.numa_cpu_history[node]
            current = hist[-1] if hist else 0.0
            peak = self.numa_cpu_peak[node]
            numa_str += f"NUMA{node} CPU: {current:.1f}% (Peak {peak:.1f}%)  "
        print(numa_str.strip())

        # Total Hugepages
        huge_usage = round((self.hugepage_used_mb / self.hugepage_total_mb * 100), 1) if self.hugepage_total_mb > 0 else 0.0
        print(f"📄 Hugepage Memory: Total {self.hugepage_total_mb:.0f} MB  Used {self.hugepage_used_mb:.0f} MB ({huge_usage:.1f}%)  Free {self.hugepage_free_mb:.0f} MB")

        # Per-NUMA node hugepage statistics
        if self.hugepage_per_numa:
            print("📄 Per-NUMA Node Hugepages:")
            for node_id in sorted(self.hugepage_per_numa.keys()):
                hp = self.hugepage_per_numa[node_id]
                print(f"    NUMA {node_id:>2d} | Total {hp['total_mb']:>8.0f} MB | Used {hp['used_mb']:>8.0f} MB ({hp['usage_pct']:>5.1f}%) | Free {hp['free_mb']:>8.0f} MB")

        # Host machine total resources
        if self.host_cpu_history:
            host_cpu = self.host_cpu_history[-1]
            host_mem = self.host_mem_history[-1]
            print(f"🖥️  Host Machine: CPU {host_cpu:.1f}% (Peak {self.peak_host_cpu:.1f}%) | Memory {host_mem['used_mb']:.0f}/{host_mem['total_mb']:.0f} MB ({host_mem['usage']:.1f}%, Peak {self.peak_host_mem_mb:.0f} MB)")

        # Swap
        if self.swap_history:
            s = self.swap_history[-1]
            if s['total_mb'] > 0:
                print(f"🔄 Swap:      Used {s['used_mb']:.0f}/{s['total_mb']:.0f} MB ({s['usage']:.1f}%) | Peak {self.peak_swap_used_mb:.0f} MB")
            else:
                print("🔄 Swap:      Not enabled")

        self.print_numa_real_time()

        if not sample_data:
            print("No running QEMU virtual machines detected")
            return

        # Additional display for baseline mode
        mode_tag = ""
        if self.baseline_data:
            mode_tag = " | 📊 Baseline Comparison Mode"

        # Table header: Add hugepage memory column
        header = f"{'VM Name':<28} {'PID':<10} {'CPU%':<10} {'Memory(MB)':<12} {'Hugepage(MB)':<12} {'Status':<10}"
        print(header)
        print("-" * 120)

        sorted_vms = sorted(sample_data, key=lambda x: x['cpu_percent'], reverse=True)
        for vm in sorted_vms[:15]:
            name = vm['vm_name'][:27] if len(vm['vm_name']) > 27 else vm['vm_name']
            huge_mb = vm.get('memory_huge_mb', 0.0)
            row = (f"{name:<28} {vm['pid']:<10} {vm['cpu_percent']:<10.2f} "
                   f"{vm['memory_mb']:<12.2f} {huge_mb:<12.2f} {vm['status']:<10}")
            print(row)

        if len(sorted_vms) > 15:
            print(f"... {len(sorted_vms) - 15} more virtual machines ...")

        print("-" * 120)
        total_tag = f"Total: {len(sample_data)} virtual machines | Data points: {len(self.data)}"
        if mode_tag:
            total_tag += mode_tag
        print(total_tag)
        print("Press Ctrl+C to stop monitoring")

    def check_stress_process(self, stress_pattern):
        try:
            for proc in psutil.process_iter(['cmdline', 'name']):
                try:
                    cl = ' '.join(proc.info['cmdline'] or [])
                    if stress_pattern in cl or stress_pattern in (proc.info['name'] or ''):
                        return True
                except:
                    continue
            return False
        except:
            return False

    def check_stress_file(self, file_path):
        return os.path.exists(file_path)

    def wait_for_stress_and_monitor(self, check_type, check_target, interval_seconds=5):
        print(f"Waiting for stress test to start... (Detection method: {check_type}={check_target})")
        stress_started = False
        while not stress_started:
            stress_started = (self.check_stress_process(check_target) if check_type == 'process'
                              else self.check_stress_file(check_target))
            if not stress_started:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for stress test to start...")
                time.sleep(2)

        print(f"✓ Stress test detected! Starting monitoring...")
        self.running = True
        start_time = time.time()

        def handler(sig, frame):
            print("\n\nStop signal received, ending monitoring...")
            self.running = False

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

        try:
            while self.running:
                loop_start = time.time()
                if check_type == 'process' and not self.check_stress_process(check_target):
                    print("\n✓ Stress process ended, stopping monitoring")
                    self.running = False
                    break
                if check_type == 'file' and not self.check_stress_file(check_target):
                    print("\n✓ Stress file removed, stopping monitoring")
                    self.running = False
                    break

                sample = self.collect_sample()
                elapsed = str(timedelta(seconds=int(time.time() - start_time)))
                self.display_realtime_table(sample, elapsed, "Stress Sync", f"{check_type}={check_target}")

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

        print(f"Starting QEMU VM monitoring...")
        print(f"Sampling interval: {interval_seconds}s | {'Run indefinitely' if not duration_seconds else f'Duration: {duration_seconds}s'}")

        try:
            while self.running:
                loop_start = time.time()
                sample = self.collect_sample()
                elapsed = time.time() - start_time
                elapsed_str = str(timedelta(seconds=int(elapsed)))
                dur_str = str(timedelta(seconds=int(duration_seconds))) if duration_seconds else "∞"
                self.display_realtime_table(sample, elapsed_str, dur_str, "Timer Mode")

                if duration_seconds and elapsed >= duration_seconds:
                    print(f"\n✓ Timer duration reached, monitoring complete")
                    self.running = False
                    break

                sl = max(0, interval_seconds - (time.time() - loop_start))
                if sl > 0 and self.running:
                    time.sleep(sl)
        except KeyboardInterrupt:
            pass
        return self.data

    def export_raw_csv(self, filename=None):
        if not filename:
            filename = f"qemu_monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            fieldnames = ['timestamp','vm_name','pid','cpu_percent','memory_mb','memory_huge_mb','memory_private_mb','memory_heap_mb','status']
            w = csv.DictWriter(f, fieldnames, extrasaction='ignore')
            w.writeheader()
            if self.data:
                w.writerows(self.data)
        print(f"\n✓ Raw data: {filename} ({len(self.data)} rows)")
        return filename

    def calculate_vm_stats(self):
        vm_data = defaultdict(list)
        for r in self.data:
            vm_data[r['vm_name']].append(r)
        stats = []
        for name, recs in sorted(vm_data.items()):
            cpus = [r['cpu_percent'] for r in recs]
            mems = [r['memory_mb'] for r in recs]
            huge = [r.get('memory_huge_mb', 0) for r in recs]
            private = [r.get('memory_private_mb', 0) for r in recs]
            heap = [r.get('memory_heap_mb', 0) for r in recs]
            stats.append({
                'vm_name': name, 'pid': recs[0]['pid'], 'sample_count': len(recs),
                'avg_cpu': round(sum(cpus)/len(cpus),2), 'max_cpu': round(max(cpus),2),
                'avg_memory_mb': round(sum(mems)/len(mems),2), 'max_memory_mb': round(max(mems),2),
                'min_memory_mb': round(min(mems),2), 'last_memory_mb': mems[-1],
                'avg_huge_mb': round(sum(huge)/len(huge),2) if huge else 0, 'max_huge_mb': round(max(huge),2) if huge else 0,
                'avg_private_mb': round(sum(private)/len(private),2) if private else 0, 'max_private_mb': round(max(private),2) if private else 0,
                'avg_heap_mb': round(sum(heap)/len(heap),2) if heap else 0, 'max_heap_mb': round(max(heap),2) if heap else 0,
            })
        return stats

    def calculate_overall_stats(self, vm_stats):
        ac = [v['avg_cpu'] for v in vm_stats]
        am = [v['avg_memory_mb'] for v in vm_stats]
        return {
            'total_vms': len(vm_stats),
            'overall_avg_cpu': round(sum(ac)/len(ac),2) if ac else 0,
            'overall_max_cpu': round(max([v['max_cpu'] for v in vm_stats]),2) if vm_stats else 0,
            'overall_avg_memory_mb': round(sum(am)/len(am),2) if am else 0,
            'overall_max_memory_mb': round(max([v['max_memory_mb'] for v in vm_stats]),2) if vm_stats else 0,
            'total_avg_memory_mb': round(sum(am),2),
            'total_avg_memory_gb': round(sum(am)/1024,2)
        }

    # ===================== Baseline Noise Collection =====================
    def run_baseline_capture(self, duration_seconds, interval_seconds=3):
        """Baseline collection mode: Monitor VM idle state, save as baseline file"""
        print(f"\n{'='*80}")
        print("Baseline Collection Mode - Monitoring VM Idle State")
        print(f"{'='*80}")
        print(f"Collection duration: {duration_seconds}s | Sampling interval: {interval_seconds}s")
        print(f"⚠️ Please ensure no stress test tasks are running on VMs during this period!")
        print(f"{'='*80}\n")

        self.running = True
        start_time = time.time()
        warmup_samples = 3  # Skip first 3 samples (first CPU sample is 0)
        sample_count = 0

        def handler(sig, frame):
            print("\nStop signal received...")
            self.running = False

        signal.signal(signal.SIGINT, handler)

        try:
            while self.running:
                loop_start = time.time()
                elapsed = time.time() - start_time

                sample = self.collect_sample()
                sample_count += 1

                if sample_count > warmup_samples:
                    elapsed_str = str(timedelta(seconds=int(elapsed)))
                    dur_str = str(timedelta(seconds=int(duration_seconds)))
                    self.display_realtime_table(sample, elapsed_str, dur_str, f"Baseline Collection ({sample_count} samples)")

                if duration_seconds and elapsed >= duration_seconds:
                    print(f"\n✓ Baseline collection complete, {sample_count} samples total")
                    self.running = False
                    break

                sl = max(0, interval_seconds - (time.time() - loop_start))
                if sl > 0 and self.running:
                    time.sleep(sl)
        except KeyboardInterrupt:
            pass

        # Save baseline
        self.save_baseline()
        # Print readable baseline report
        self.print_baseline_report()
        return self.data

    def save_baseline(self, filename=None):
        """Save baseline to JSON file"""
        if not filename:
            filename = f"qemu_baseline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        vm_stats = self.calculate_vm_stats()
        overall = self.calculate_overall_stats(vm_stats)

        baseline = {
            'metadata': {
                'created_at': datetime.now().isoformat(),
                'sample_count': len(self.data) // max(len(vm_stats), 1),
                'numa_nodes': self.target_numa_nodes,
            },
            'overall': {
                'total_vms': overall['total_vms'],
                'avg_cpu': overall['overall_avg_cpu'],
                'max_cpu': overall['overall_max_cpu'],
                'peak_total_cpu': round(self.peak_total_cpu, 1),
                'avg_total_pss_mb': overall['total_avg_memory_mb'],
                'avg_total_pss_gb': overall['total_avg_memory_gb'],
                'peak_total_pss_mb': round(self.peak_total_memory_mb, 2),
                'host_avg_cpu': round(sum(self.host_cpu_history)/len(self.host_cpu_history), 1) if self.host_cpu_history else 0,
                'host_peak_cpu': round(self.peak_host_cpu, 1),
                'host_avg_mem_mb': round(sum(h['used_mb'] for h in self.host_mem_history)/len(self.host_mem_history), 2) if self.host_mem_history else 0,
                'host_peak_mem_mb': round(self.peak_host_mem_mb, 2),
                'swap_avg_mb': round(sum(s['used_mb'] for s in self.swap_history)/len(self.swap_history), 2) if self.swap_history else 0,
                'swap_peak_mb': round(self.peak_swap_used_mb, 2),
                'swap_total_mb': self.swap_history[0]['total_mb'] if self.swap_history else 0,
                'hugepage_avg_mb': round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 2) if self.hugepage_used_history else 0,
                'hugepage_peak_mb': round(self.peak_hugepage_used_mb, 2),
                'hugepage_total_mb': round(self.hugepage_total_mb, 2),
            },
            'per_vm': {}
        }

        # Calculate hugepage estimate
        huge_avg = round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 2) if self.hugepage_used_history else 0
        huge_per_vm = round(huge_avg / max(len(vm_stats), 1), 2)

        for v in vm_stats:
            baseline['per_vm'][v['vm_name']] = {
                'pid': v['pid'],
                'avg_cpu': v['avg_cpu'],
                'max_cpu': v['max_cpu'],
                'avg_pss_mb': v['avg_memory_mb'],
                'max_pss_mb': v['max_memory_mb'],
                'min_pss_mb': v['min_memory_mb'],
                'avg_rss_mb': v['avg_rss_mb'],
                'max_rss_mb': v['max_rss_mb'],
                'avg_uss_mb': v['avg_uss_mb'],
                'max_uss_mb': v['max_uss_mb'],
                'hugepage_est_mb': huge_per_vm,
                'avg_pss_plus_huge_mb': round(v['avg_memory_mb'] + huge_per_vm, 2),
                'max_pss_plus_huge_mb': round(v['max_memory_mb'] + huge_per_vm, 2),
            }

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(baseline, f, indent=2, ensure_ascii=False)

        self.baseline_file = filename
        self.baseline_data = baseline
        self.baseline_samples = baseline['metadata']['sample_count']

        print(f"\n✓ Baseline saved: {filename}")
        print(f"  Samples: {self.baseline_samples}")
        print(f"  VM count: {overall['total_vms']}")
        print(f"  Avg total PSS: {overall['total_avg_memory_mb']:.0f} MB ({overall['total_avg_memory_gb']:.2f} GB)")
        return filename

    def print_baseline_report(self):
        """Print readable baseline report"""
        vm_stats = self.calculate_vm_stats()
        overall = self.calculate_overall_stats(vm_stats)

        print(f"\n{'='*100}")
        print("Baseline Collection Report - VM Idle State Resource Usage")
        print(f"{'='*100}")
        print(f"Collection time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if self.data:
            print(f"Data period: {self.data[0]['timestamp']} ~ {self.data[-1]['timestamp']}")

        # Host machine level
        host_cpu_avg = round(sum(self.host_cpu_history)/len(self.host_cpu_history), 1) if self.host_cpu_history else 0
        host_mem_avg_mb = round(sum(h['used_mb'] for h in self.host_mem_history)/len(self.host_mem_history), 2) if self.host_mem_history else 0
        host_mem_total_mb = round(self.host_mem_history[0]['total_mb'], 0) if self.host_mem_history else 0

        print(f"\n[Host Machine Resources]")
        print(f"  CPU average:    {host_cpu_avg:.1f}% (Peak {self.peak_host_cpu:.1f}%)")
        print(f"  Memory:       {host_mem_avg_mb:.0f}/{host_mem_total_mb:.0f} MB (Peak {self.peak_host_mem_mb:.0f} MB)")

        # QEMU total resources
        print(f"\n[QEMU Process Total Resources]")
        print(f"  VM count:     {overall['total_vms']}")
        print(f"  Avg total CPU:  {overall['overall_avg_cpu']:.2f}% (Average of all VMs)")
        print(f"  Peak total CPU:  {self.peak_total_cpu:.1f}% (Sum of all VMs at same time)")
        print(f"  Avg total PSS:  {overall['total_avg_memory_mb']:.0f} MB ({overall['total_avg_memory_gb']:.2f} GB)")
        print(f"  Peak total PSS:  {self.peak_total_memory_mb:.0f} MB ({self.peak_total_memory_mb/1024:.2f} GB)")

        # Hugepage memory (relation with PSS explained)
        if self.hugepage_used_history:
            avg_huge = round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 0)
            peak_huge = round(self.peak_hugepage_used_mb, 0)
            huge_total = round(self.hugepage_total_mb, 0)
            huge_per_vm = round(avg_huge / max(overall['total_vms'], 1), 1) if overall['total_vms'] > 0 else 0
            pss_total = overall['total_avg_memory_mb']
            # Hugepages may be included in PSS, or may be separately counted (depends on mapping method)
            print(f"\n[Hugepage Memory (/dev/hugepages)]")
            print(f"  Total capacity:     {huge_total:.0f} MB")
            print(f"  Avg usage:   {avg_huge:.0f} MB ({avg_huge/max(huge_total,1)*100:.1f}%) | Peak {peak_huge:.0f} MB")
            print(f"  Per-VM average:   {huge_per_vm:.1f} MB (Assuming equal distribution)")
            print(f"  PSS+Hugepage:   ~{round(pss_total + avg_huge):.0f} MB (If PSS doesn't include hugepages, this is real total physical memory)")

        # Swap
        if self.swap_history:
            swap_avg_mb = round(sum(s['used_mb'] for s in self.swap_history)/len(self.swap_history), 0) if self.swap_history else 0
            swap_total_mb = self.swap_history[0]['total_mb'] if self.swap_history else 0
            swap_peak_pct = round(self.peak_swap_used_mb / swap_total_mb * 100, 1) if swap_total_mb > 0 else 0
            print(f"\n[Swap Partition]")
            print(f"  Total capacity:   {swap_total_mb:.0f} MB")
            print(f"  Avg usage: {swap_avg_mb:.0f} MB")
            print(f"  Peak usage: {self.peak_swap_used_mb:.0f} MB ({swap_peak_pct:.1f}%)")

        # NUMA CPU summary
        if self.numa_cpu_history:
            print(f"\n[NUMA Node CPU Idle Baseline]")
            for node in sorted(self.numa_cpu_history.keys()):
                hist = self.numa_cpu_history[node]
                avg = round(sum(hist)/len(hist),1) if hist else 0
                peak = self.numa_cpu_peak[node]
                print(f"  NUMA {node:>2d} | Avg CPU: {avg:>5.1f}% | Peak CPU: {peak:>5.1f}%")

        # Single VM details
        if vm_stats:
            huge_avg = round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 1) if self.hugepage_used_history else 0
            huge_per_vm = round(huge_avg / max(overall['total_vms'], 1), 1) if overall['total_vms'] > 0 else 0
            has_huge = huge_per_vm > 0

            print(f"\n[Single VM Baseline Details]")
            hdr = f"  {'VM Name':<25} {'avgCPU':>8} {'maxCPU':>8} " \
                   f"{'avgPSS':>10} {'maxPSS':>10} {'minPSS':>10}"
            if has_huge:
                hdr += f" | {'PSS+Huge(est)':>14}"
            hdr += f" {'avgRSS':>10} {'maxRSS':>10} " \
                   f"{'avgUSS':>10} {'maxUSS':>10}"
            print(hdr)
            sep = '-' * (115 + 15 if has_huge else 115)
            print(f"  {sep}")

            for v in sorted(vm_stats, key=lambda x: x['avg_memory_mb'], reverse=True):
                n = v['vm_name'][:24]
                row = (f"  {n:<25} {v['avg_cpu']:>7.2f}% {v['max_cpu']:>7.2f}% "
                       f"{v['avg_memory_mb']:>9.2f} {v['max_memory_mb']:>9.2f} {v['min_memory_mb']:>9.2f}")
                if has_huge:
                    pss_plus_hp = round(v['avg_memory_mb'] + huge_per_vm, 1)
                    max_pss_plus_hp = round(v['max_memory_mb'] + huge_per_vm, 1)
                    row += f" | {pss_plus_hp:>13.1f} {max_pss_plus_hp:>9.1f}"
                row += (f" {v['avg_rss_mb']:>9.2f} {v['max_rss_mb']:>9.2f} "
                        f"{v['avg_uss_mb']:>9.2f} {v['max_uss_mb']:>9.2f}")
                print(row)

        print(f"{'='*100}")

    def load_baseline(self, filename):
        """Load baseline file"""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                self.baseline_data = json.load(f)
            self.baseline_file = filename
            meta = self.baseline_data['metadata']
            print(f"✓ Baseline loaded: {filename}")
            print(f"  Collection time: {meta['created_at']}")
            print(f"  Samples: {meta['sample_count']}")
            print(f"  VM count: {self.baseline_data['overall']['total_vms']}")
            return True
        except Exception as e:
            print(f"✗ Failed to load baseline: {e}")
            return False

    def print_baseline_comparison(self, vm_stats, overall_stats):
        """Print baseline vs stress test comparison report"""
        if not self.baseline_data:
            return

        baseline = self.baseline_data
        bl_overall = baseline['overall']

        print("\n" + "=" * 85)
        print("Baseline vs Stress Test Comparison Report")
        print("=" * 85)
        print(f"Baseline file: {self.baseline_file}")
        print(f"Stress test period: {self.data[0]['timestamp']} ~ {self.data[-1]['timestamp']}")
        print("-" * 85)

        # Host machine level comparison
        print("\n[Host Machine Level Comparison]")
        print(f"  {'Metric':<25} {'Baseline':>15} {'Stress':>15} {'Delta':>15}")
        print(f"  {'-'*70}")

        # CPU
        bl_cpu = bl_overall['host_avg_cpu']
        st_cpu = round(sum(self.host_cpu_history)/len(self.host_cpu_history), 1) if self.host_cpu_history else 0
        print(f"  {'Host Avg CPU%':<25} {bl_cpu:>14.1f}% {st_cpu:>14.1f}% {st_cpu-bl_cpu:>+14.1f}%")

        # Memory
        bl_mem = bl_overall['host_avg_mem_mb']
        st_mem = round(sum(h['used_mb'] for h in self.host_mem_history)/len(self.host_mem_history), 2) if self.host_mem_history else 0
        print(f"  {'Host Avg Memory MB':<25} {bl_mem:>14.0f} MB {st_mem:>14.0f} MB {st_mem-bl_mem:>+14.0f} MB")

        # Total PSS
        bl_pss = bl_overall['avg_total_pss_mb']
        st_pss = overall_stats['total_avg_memory_mb']
        print(f"  {'VM Total PSS':<25} {bl_pss:>14.0f} MB {st_pss:>14.0f} MB {st_pss-bl_pss:>+14.0f} MB ({(st_pss-bl_pss)/bl_pss*100 if bl_pss>0 else 0:+.1f}%)")

        # Peak CPU
        bl_peak_cpu = bl_overall['host_peak_cpu']
        st_peak_cpu = round(self.peak_host_cpu, 1)
        print(f"  {'Host Peak CPU%':<25} {bl_peak_cpu:>14.1f}% {st_peak_cpu:>14.1f}% {st_peak_cpu-bl_peak_cpu:>+14.1f}%")

        # Swap
        bl_swap = bl_overall.get('swap_avg_mb', 0)
        bl_swap_total = bl_overall.get('swap_total_mb', 0)
        st_swap = round(sum(s['used_mb'] for s in self.swap_history)/len(self.swap_history), 2) if self.swap_history else 0
        st_swap_total = self.swap_history[0]['total_mb'] if self.swap_history else 0
        if bl_swap_total > 0 or st_swap_total > 0:
            print(f"  {'Swap Usage MB':<25} {bl_swap:>14.0f} {st_swap:>14.0f} {st_swap-bl_swap:>+14.0f} MB")
            print(f"  {'Swap Peak MB':<25} {bl_overall.get('swap_peak_mb', 0):>14.0f} {self.peak_swap_used_mb:>14.0f} {self.peak_swap_used_mb - bl_overall.get('swap_peak_mb', 0):>+14.0f} MB")

        # Hugepage
        bl_huge = bl_overall.get('hugepage_avg_mb', 0)
        bl_huge_total = bl_overall.get('hugepage_total_mb', 0)
        st_huge = round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 2) if self.hugepage_used_history else 0
        st_huge_total = round(self.hugepage_total_mb, 2)
        if bl_huge_total > 0 or st_huge_total > 0:
            print(f"  {'Hugepage Usage MB':<25} {bl_huge:>14.0f} {st_huge:>14.0f} {st_huge-bl_huge:>+14.0f} MB")
            print(f"  {'Hugepage Peak MB':<25} {bl_overall.get('hugepage_peak_mb', 0):>14.0f} {self.peak_hugepage_used_mb:>14.0f} {self.peak_hugepage_used_mb - bl_overall.get('hugepage_peak_mb', 0):>+14.0f} MB")

        # Single VM comparison
        bl_huge_per_vm = bl_overall.get('hugepage_avg_mb', 0) / max(bl_overall.get('total_vms', 1), 1) if bl_overall.get('total_vms', 1) > 0 else 0
        has_huge = bl_huge_per_vm > 0 or bl_overall.get('hugepage_total_mb', 0) > 0

        print(f"\n[Single VM Baseline vs Stress Comparison]")
        hdr = f"  {'VM Name':<25} {'BasePSS':>10} {'StressPSS':>10} {'Delta':>10}"
        if has_huge:
            hdr += f" | {'BasePSS+HP':>12}"
        hdr += f" {'BaseCPU':>8} {'StressCPU':>8} {'Delta':>8}"
        print(hdr)
        print(f"  {'-'*85}")

        bl_per_vm = baseline.get('per_vm', {})
        for v in sorted(vm_stats, key=lambda x: x['avg_memory_mb'], reverse=True):
            name = v['vm_name']
            bl_vm = bl_per_vm.get(name, {})
            bl_pss_vm = bl_vm.get('avg_pss_mb', 0)
            st_pss_vm = v['avg_memory_mb']
            delta_pss = st_pss_vm - bl_pss_vm
            bl_cpu_vm = bl_vm.get('avg_cpu', 0)
            st_cpu_vm = v['avg_cpu']
            delta_cpu = st_cpu_vm - bl_cpu_vm

            marker = ""
            if delta_pss > bl_pss_vm * 0.5 and bl_pss_vm > 0:
                marker = " ⚠️ Delta>50%"
            name_display = name[:24]
            row = f"  {name_display:<25} {bl_pss_vm:>9.1f} MB {st_pss_vm:>9.1f} MB {delta_pss:>+9.1f} MB"
            if has_huge:
                bl_pss_huge = bl_vm.get('avg_pss_plus_huge_mb', round(bl_pss_vm + bl_huge_per_vm, 1))
                row += f" | {bl_pss_huge:>11.1f} MB"
            row += f" {bl_cpu_vm:>7.1f}% {st_cpu_vm:>7.1f}% {delta_cpu:>+7.1f}%{marker}"
            print(row)

        print("=" * 85)

    def export_summary_csv(self, vm_stats, overall_stats, filename=None):
        if not filename:
            filename = f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['=== Host Machine Statistics ===', ''])
            w.writerow(['Total VMs', f"{overall_stats['total_vms']}"])
            w.writerow(['Alive VM Count', self.last_vm_count])

            # Host machine total CPU/Memory
            host_cpu_avg = round(sum(self.host_cpu_history)/len(self.host_cpu_history), 1) if self.host_cpu_history else 0
            w.writerow([f'Host Avg CPU%', host_cpu_avg])
            w.writerow([f'Host Peak CPU%', round(self.peak_host_cpu, 1)])
            host_mem_avg_mb = round(sum(h['used_mb'] for h in self.host_mem_history)/len(self.host_mem_history), 2) if self.host_mem_history else 0
            w.writerow([f'Host Avg Memory MB', host_mem_avg_mb])
            w.writerow([f'Host Peak Memory MB', round(self.peak_host_mem_mb, 2)])

            # Export NUMA CPU statistics
            for node in sorted(self.numa_cpu_history.keys()):
                hist = self.numa_cpu_history[node]
                avg = round(sum(hist)/len(hist),1) if hist else 0
                peak = self.numa_cpu_peak[node]
                w.writerow([f'NUMA{node} Avg CPU%', avg])
                w.writerow([f'NUMA{node} Peak CPU%', peak])

            w.writerows([
                ['VM Avg CPU%', overall_stats['overall_avg_cpu']],
                ['VM Max CPU%', overall_stats['overall_max_cpu']],
                ['Peak Total CPU', round(self.peak_total_cpu, 1)],
                ['VM Avg Memory MB', overall_stats['overall_avg_memory_mb']],
                ['VM Max Memory MB', overall_stats['overall_max_memory_mb']],
                ['Total Memory MB (Avg)', overall_stats['total_avg_memory_mb']],
            ])
            w.writerow(['Hugepage Total MB', round(self.hugepage_total_mb, 0)])
            w.writerow(['Hugepage Avg Usage MB', round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 0) if self.hugepage_used_history else 0])
            w.writerow(['Hugepage Peak Usage MB', round(self.peak_hugepage_used_mb, 0)])
            huge_usage_final = round((self.peak_hugepage_used_mb / self.hugepage_total_mb * 100),1) if self.hugepage_total_mb>0 else 0
            w.writerow(['Hugepage Peak Usage %', huge_usage_final])

            # Per-NUMA node hugepage statistics
            if self.hugepage_per_numa_history:
                w.writerow([])
                w.writerow(['=== Per-NUMA Node Hugepage Statistics ==='])
                # Calculate average for each NUMA node
                numa_huge_summary = defaultdict(lambda: {'total': [], 'used': [], 'free': []})
                for entry in self.hugepage_per_numa_history:
                    for node_id, data in entry['nodes'].items():
                        numa_huge_summary[node_id]['total'].append(data['total_mb'])
                        numa_huge_summary[node_id]['used'].append(data['used_mb'])
                        numa_huge_summary[node_id]['free'].append(data['free_mb'])

                for node_id in sorted(numa_huge_summary.keys()):
                    data = numa_huge_summary[node_id]
                    avg_total = round(sum(data['total'])/len(data['total']), 0) if data['total'] else 0
                    avg_used = round(sum(data['used'])/len(data['used']), 0) if data['used'] else 0
                    avg_free = round(sum(data['free'])/len(data['free']), 0) if data['free'] else 0
                    avg_usage = round(avg_used / avg_total * 100, 1) if avg_total > 0 else 0
                    w.writerow([f'NUMA{node_id} Hugepage Total MB', avg_total])
                    w.writerow([f'NUMA{node_id} Hugepage Avg Used MB', avg_used])
                    w.writerow([f'NUMA{node_id} Hugepage Avg Free MB', avg_free])
                    w.writerow([f'NUMA{node_id} Hugepage Avg Usage %', avg_usage])

            # Swap
            swap_avg_mb = round(sum(s['used_mb'] for s in self.swap_history)/len(self.swap_history), 0) if self.swap_history else 0
            w.writerow(['Swap Total Capacity MB', round(self.swap_history[0]['total_mb'], 0) if self.swap_history else 0])
            w.writerow(['Swap Avg Usage MB', swap_avg_mb])
            w.writerow(['Swap Peak Usage MB', round(self.peak_swap_used_mb, 0)])
            swap_total = self.swap_history[0]['total_mb'] if self.swap_history else 0
            swap_peak_pct = round(self.peak_swap_used_mb / swap_total * 100, 1) if swap_total > 0 else 0
            w.writerow(['Swap Peak Usage %', swap_peak_pct])

            # Baseline comparison
            if self.baseline_data:
                bl = self.baseline_data['overall']
                w.writerow([])
                w.writerow(['=== Baseline vs Stress Comparison ==='])
                w.writerow(['Metric', 'Baseline', 'Stress', 'Delta'])
                bl_pss = bl['avg_total_pss_mb']
                st_pss = overall_stats['total_avg_memory_mb']
                w.writerow(['VM Total PSS(MB)', bl_pss, st_pss, round(st_pss - bl_pss, 2)])
                bl_cpu = bl['host_avg_cpu']
                st_cpu = round(sum(self.host_cpu_history)/len(self.host_cpu_history), 1) if self.host_cpu_history else 0
                w.writerow(['Host CPU%', bl_cpu, st_cpu, round(st_cpu - bl_cpu, 1)])
                bl_mem = bl['host_avg_mem_mb']
                st_mem = round(sum(h['used_mb'] for h in self.host_mem_history)/len(self.host_mem_history), 2) if self.host_mem_history else 0
                w.writerow(['Host Memory MB', bl_mem, st_mem, round(st_mem - bl_mem, 2)])
                bl_swap = bl.get('swap_avg_mb', 0)
                st_swap = round(sum(s['used_mb'] for s in self.swap_history)/len(self.swap_history), 2) if self.swap_history else 0
                w.writerow(['Swap Usage MB', bl_swap, st_swap, round(st_swap - bl_swap, 2)])
                bl_huge = bl.get('hugepage_avg_mb', 0)
                st_huge = round(sum(self.hugepage_used_history)/len(self.hugepage_used_history), 2) if self.hugepage_used_history else 0
                w.writerow(['Hugepage Usage MB', bl_huge, st_huge, round(st_huge - bl_huge, 2)])
            w.writerow([])
            w.writerow(['=== Single VM Statistics ==='])
            w.writerow(['VM','PID','Samples','avgCPU','maxCPU','avgMem','maxMem','minMem','lastMem','avgHuge','maxHuge','avgPrivate','maxPrivate','avgHeap','maxHeap'])
            for v in vm_stats:
                w.writerow([v['vm_name'],v['pid'],v['sample_count'],v['avg_cpu'],v['max_cpu'],
                            v['avg_memory_mb'],v['max_memory_mb'],v['min_memory_mb'],v['last_memory_mb'],
                            v.get('avg_huge_mb',0),v.get('max_huge_mb',0),
                            v.get('avg_private_mb',0),v.get('max_private_mb',0),
                            v.get('avg_heap_mb',0),v.get('max_heap_mb',0)])
        print(f"✓ Summary report: {filename}")
        return filename

    def print_summary_report(self, vm_stats, overall_stats):
        print("\n" + "="*85)
        print("QEMU Monitoring Summary Report")
        print("="*85)
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if self.data:
            print(f"Period: {self.data[0]['timestamp']} ~ {self.data[-1]['timestamp']}")

        self.print_final_numa_stats()

        # NUMA CPU summary report
        print("\n[Specified NUMA Node CPU Statistics]")
        for node in sorted(self.numa_cpu_history.keys()):
            hist = self.numa_cpu_history[node]
            avg = round(sum(hist)/len(hist),1) if hist else 0
            peak = self.numa_cpu_peak[node]
            print(f"  NUMA {node:>2d} | Avg CPU: {avg:>5.1f}% | Peak CPU: {peak:>5.1f}%")

        print("\n[Host Machine Total Resources]")
        host_cpu_avg = round(sum(self.host_cpu_history)/len(self.host_cpu_history), 1) if self.host_cpu_history else 0
        host_mem_avg_mb = round(sum(h['used_mb'] for h in self.host_mem_history)/len(self.host_mem_history), 2) if self.host_mem_history else 0
        host_mem_total_mb = round(self.host_mem_history[0]['total_mb'], 0) if self.host_mem_history else 0
        print(f"  Host Avg CPU:  {host_cpu_avg:.1f}% (Peak {self.peak_host_cpu:.1f}%)")
        print(f"  Host Avg Memory: {host_mem_avg_mb:.0f}/{host_mem_total_mb:.0f} MB (Peak {self.peak_host_mem_mb:.0f} MB)")
        print("")
        print(f"  Total VMs:    {overall_stats['total_vms']}")
        print(f"  Alive VMs:     {self.last_vm_count}")
        print(f"  Avg CPU:       {overall_stats['overall_avg_cpu']}%")
        print(f"  Max CPU:       {overall_stats['overall_max_cpu']}%")
        print(f"  Peak Total CPU:     {round(self.peak_total_cpu, 1)}%")
        print(f"  Avg Memory:      {overall_stats['overall_avg_memory_mb']} MB")
        print(f"  Total Memory (Avg):   {overall_stats['total_avg_memory_mb']} MB ({overall_stats['total_avg_memory_gb']} GB)")
        print("[Hugepage Memory Info]")
        print(f"  Hugepage Total:     {self.hugepage_total_mb:.0f} MB")
        avg_huge = round(sum(self.hugepage_used_history)/len(self.hugepage_used_history),0) if self.hugepage_used_history else 0
        huge_usage = round((self.peak_hugepage_used_mb / self.hugepage_total_mb * 100),1) if self.hugepage_total_mb>0 else 0
        print(f"  Hugepage Avg Usage:    {avg_huge} MB")
        print(f"  Hugepage Peak Usage:    {self.peak_hugepage_used_mb:.0f} MB ({huge_usage:.1f}%)")

        # Per-NUMA node hugepage statistics
        if self.hugepage_per_numa_history:
            print("\n[Per-NUMA Node Hugepage Statistics]")
            numa_huge_summary = defaultdict(lambda: {'total': [], 'used': [], 'free': []})
            for entry in self.hugepage_per_numa_history:
                for node_id, data in entry['nodes'].items():
                    numa_huge_summary[node_id]['total'].append(data['total_mb'])
                    numa_huge_summary[node_id]['used'].append(data['used_mb'])
                    numa_huge_summary[node_id]['free'].append(data['free_mb'])

            for node_id in sorted(numa_huge_summary.keys()):
                data = numa_huge_summary[node_id]
                avg_total = round(sum(data['total'])/len(data['total']), 0) if data['total'] else 0
                avg_used = round(sum(data['used'])/len(data['used']), 0) if data['used'] else 0
                avg_free = round(sum(data['free'])/len(data['free']), 0) if data['free'] else 0
                avg_usage = round(avg_used / avg_total * 100, 1) if avg_total > 0 else 0
                print(f"  NUMA {node_id:>2d} | Total {avg_total:>8.0f} MB | Used {avg_used:>8.0f} MB ({avg_usage:>5.1f}%) | Free {avg_free:>8.0f} MB")

        # Swap
        if self.swap_history:
            swap_avg_mb = round(sum(s['used_mb'] for s in self.swap_history)/len(self.swap_history), 0) if self.swap_history else 0
            swap_total_mb = self.swap_history[0]['total_mb'] if self.swap_history else 0
            swap_peak_pct = round(self.peak_swap_used_mb / swap_total_mb * 100, 1) if swap_total_mb > 0 else 0
            print("[Swap Partition]")
            print(f"  Total Capacity:     {swap_total_mb:.0f} MB")
            print(f"  Avg Usage:    {swap_avg_mb:.0f} MB")
            print(f"  Peak Usage:    {self.peak_swap_used_mb:.0f} MB ({swap_peak_pct:.1f}%)")

        if vm_stats:
            print("\n[TOP10 CPU]")
            for i, v in enumerate(sorted(vm_stats,key=lambda x:x['avg_cpu'],reverse=True)[:10],1):
                n = v['vm_name'][:29]
                print(f"{i:2}. {n:<30} {v['avg_cpu']:6.2f}% (max {v['max_cpu']:.2f})")

            print("\n[TOP10 Memory]")
            for i, v in enumerate(sorted(vm_stats,key=lambda x:x['avg_memory_mb'],reverse=True)[:10],1):
                n = v['vm_name'][:29]
                huge_mb = v.get('avg_huge_mb', 0)
                print(f"{i:2}. {n:<30} {v['avg_memory_mb']:8.2f} MB (Hugepage {huge_mb:.2f} MB)")
        print("="*85)

    def analyze_and_export(self, raw=None, summary=None):
        vs = self.calculate_vm_stats()
        os = self.calculate_overall_stats(vs)
        rf = self.export_raw_csv(raw)
        sf = self.export_summary_csv(vs, os, summary)
        self.print_summary_report(vs, os)
        if self.baseline_data:
            self.print_baseline_comparison(vs, os)
        return rf, sf

def main():
    parser = argparse.ArgumentParser(
        description='QEMU Monitoring Tool (Supports baseline collection + stress test comparison)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
[Mode 1: Baseline Collection]
  sudo python3 qemu_monitor.py --baseline 60 -i 3
    → Collect VM idle state for 60 seconds, save as qemu_baseline_*.json

[Mode 2: Stress Sync + Baseline Comparison]
  sudo python3 qemu_monitor.py --stress-file /tmp/bench_running.lock --baseline-file qemu_baseline_20260414.json
    → Wait for lock file to appear then start monitoring, compare baseline after completion

[Mode 3: Timer Stress + Baseline Comparison]
  sudo python3 qemu_monitor.py -t 300 --baseline-file qemu_baseline_20260414.json
    → Monitor for 300 seconds, compare baseline after completion

[Mode 4: Pure Timer Monitoring]
  sudo python3 qemu_monitor.py -t 60 -i 2
        """
    )
    sync = parser.add_mutually_exclusive_group()
    sync.add_argument('--stress-process', type=str, help='Stress process name')
    sync.add_argument('--stress-file', type=str, help='Stress marker file (e.g., /tmp/bench_running.lock)')
    parser.add_argument('--baseline', type=int, metavar='SEC', help='Baseline collection mode: collect for N seconds')
    parser.add_argument('--baseline-file', type=str, help='Baseline file path (for comparison)')
    parser.add_argument('-t','--time', type=int, help='Timer duration seconds')
    parser.add_argument('-i','--interval', type=int, default=3, help='Sampling interval (default 3 seconds)')
    parser.add_argument('-o','--output', type=str, help='Output prefix')
    parser.add_argument('--numa', type=str, default='1', help='Specify NUMA nodes to monitor, comma-separated 0,1')
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("⚠ Recommended to run as root, otherwise some processes cannot be read")
        time.sleep(1)

    m = QEMUMonitor()
    try:
        m.target_numa_nodes = list(map(int, args.numa.split(',')))
    except:
        m.target_numa_nodes = [0]

    # Baseline collection mode
    if args.baseline:
        m.run_baseline_capture(args.baseline, args.interval)
        print("\n✅ Baseline collection complete!")
        return

    # Load baseline file (for comparison)
    if args.baseline_file:
        m.load_baseline(args.baseline_file)

    # Stress test monitoring
    if args.stress_process:
        m.wait_for_stress_and_monitor('process', args.stress_process, args.interval)
    elif args.stress_file:
        m.wait_for_stress_and_monitor('file', args.stress_file, args.interval)
    else:
        m.start_monitoring(args.time, args.interval)

    raw = f"{args.output}.csv" if args.output else None
    sumf = f"summary_{args.output}.csv" if args.output else None
    m.analyze_and_export(raw, sumf)
    print("\n✅ Complete!")

if __name__ == '__main__':
    main()