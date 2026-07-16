#!/usr/bin/env python3
"""
DDR Latency and Frequency Monitoring Tool for 920x Platform (v1.2)

Key Improvements over v1.1:
- Separate perf processes for DDR latency, uncore frequency, and core frequency
- Parallel collection across NUMA nodes
- Avoids self-interference in uncore frequency measurement
- Precise timing alignment across all collectors

Architecture:
    Main Process
    ├── DDR Latency Collectors (one per NUMA, parallel)
    ├── Uncore Frequency Collectors (one per NUMA, parallel)
    └── Core Frequency Collector (one total)

Usage:
    python 920x_ddr_latency_v1.2.py -d 60 -i 3 -n 1
    python 920x_ddr_latency_v1.2.py --all
"""

import argparse
import os
import subprocess
import sys
import time
from collections import defaultdict


def calculate_cpu_range_from_numa(numa_nodes: list) -> str:
    """Calculate CPU core range from NUMA node IDs

    Args:
        numa_nodes: list of NUMA node IDs (e.g., [0, 1])

    Returns:
        CPU range string like "0-95" or "96-191"
    """
    all_cores = []

    for node in numa_nodes:
        try:
            cpulist_path = f"/sys/devices/system/node/node{node}/cpulist"
            with open(cpulist_path) as f:
                cpulist = f.read().strip()

            # Parse cpulist (e.g., "0-95" or "0,1,2,3-10")
            for part in cpulist.split(","):
                if "-" in part:
                    start, end = part.split("-")
                    all_cores.extend(range(int(start), int(end) + 1))
                else:
                    all_cores.append(int(part))
        except Exception as e:
            print(f"Warning: Failed to read CPU list for NUMA node {node}: {e}")

    if not all_cores:
        return "0"  # Fallback default

    # Sort and merge into ranges
    all_cores.sort()
    ranges = []
    start = all_cores[0]
    end = all_cores[0]

    for core in all_cores[1:]:
        if core == end + 1:
            end = core
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = core
            end = core

    # Add last range
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def get_numa_from_ddr_device(ddr_device):
    """Get NUMA node ID from DDR device name

    DDR device name format: hisi_sccl{x}_ddrc{y}_{z}
    x represents the NUMA node (SCCL ID)

    Args:
        ddr_device: DDR device name like 'hisi_sccl1_ddrc0_0'

    Returns:
        NUMA node ID (int) or None if cannot determine
    """
    try:
        # Parse SCCL ID from device name
        # hisi_sccl1_ddrc0_0 -> extract '1'
        if "hisi_sccl" in ddr_device:
            parts = ddr_device.split("_")
            if len(parts) >= 2:
                sccl_id = parts[1].replace("sccl", "")
                return int(sccl_id)
    except Exception:
        pass
    return None


def get_l3c_device_for_numa(numa_node):
    """Get L3C device name for a NUMA node

    Args:
        numa_node: NUMA node ID

    Returns:
        L3C device name like 'hisi_sccl1_l3c0_0' or None
    """
    l3c_device = f"hisi_sccl{numa_node}_l3c0_0"
    # Verify the device exists
    if os.path.exists(f"/sys/devices/{l3c_device}"):
        return l3c_device
    return None


def find_ddr_devices_for_numa(numa_node=None):
    """Find DDR devices for specific NUMA node or all

    Args:
        numa_node: NUMA node ID (int) or None for all

    Returns:
        List of DDR device names
    """
    all_ddr = []
    try:
        for item in os.listdir("/sys/devices/"):
            if "ddrc" in item:
                all_ddr.append(str(item))
    except Exception:
        pass

    all_ddr.sort()

    if numa_node is not None:
        # Filter by NUMA node
        filtered = []
        for ddr in all_ddr:
            ddr_numa = get_numa_from_ddr_device(ddr)
            if ddr_numa == numa_node:
                filtered.append(ddr)
        return filtered

    return all_ddr


def group_ddr_devices_by_numa(ddr_devices):
    """Group DDR devices by NUMA node

    Args:
        ddr_devices: List of DDR device names

    Returns:
        Dict mapping NUMA node ID -> list of DDR devices
    """
    grouped = defaultdict(list)
    for ddr in ddr_devices:
        numa_id = get_numa_from_ddr_device(ddr)
        if numa_id is not None:
            grouped[numa_id].append(ddr)
    return grouped


class PerfCollector:
    """Base class for perf collectors"""

    def __init__(self, output_file):
        self.output_file = output_file
        self.process = None
        self.events = []

    def build_command(self, interval):
        """Build the perf stat command"""
        raise NotImplementedError

    def start(self, interval):
        """Start the perf collection process"""
        cmd = self.build_command(interval)
        self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.process

    def wait(self):
        """Wait for the process to complete"""
        if self.process:
            return self.process.wait()
        return None

    def parse_output(self):
        """Parse the perf output file"""
        raise NotImplementedError


class DDRCollector(PerfCollector):
    """Collects DDR latency and bandwidth events for a NUMA node"""

    # DDR events to collect
    # 0x00: DDR cycles
    # 0x41: Read count
    # 0x44: Write count
    # 0x80: Read time (cycles)
    # 0x81: Write time (cycles)
    # 0x83: Write data (bytes)
    # 0x84: Read data (bytes)
    DDR_EVENTS = ["0x00", "0x41", "0x44", "0x80", "0x81", "0x83", "0x84"]

    def __init__(self, numa_id, ddr_devices, output_file):
        super().__init__(output_file)
        self.numa_id = numa_id
        self.ddr_devices = ddr_devices

    def build_command(self, interval):
        """Build perf stat command for DDR events"""
        events = []
        for ddr_device in self.ddr_devices:
            for event_config in self.DDR_EVENTS:
                events.append(f"{ddr_device}/config={event_config}/")

        event_str = ",".join(events)
        return ["perf", "stat", "-e", event_str, "-o", self.output_file, "sleep", str(interval)]

    def parse_output(self):
        """Parse DDR perf output and return device stats"""
        device_data = defaultdict(dict)

        try:
            with open(self.output_file) as f:
                lines = f.readlines()

            for line in lines:
                line = line.strip()
                if not line or "Performance counter stats" in line or "seconds" in line:
                    continue

                parts = line.split()
                if len(parts) < 2:
                    continue

                try:
                    value = int(float(parts[0].replace(",", "")))
                    event_spec = parts[-1]

                    if "/config=" in event_spec:
                        device_part = event_spec.split("/")[0]
                        config_part = event_spec.split("config=")[1].rstrip("/").rstrip(",")

                        if "ddrc" in device_part:
                            device_data[device_part][config_part] = value

                except (ValueError, IndexError):
                    continue

        except Exception as e:
            print(f"Error parsing DDR output for NUMA {self.numa_id}: {e}")

        return device_data


class UncoreCollector(PerfCollector):
    """Collects uncore (L3C) frequency for a NUMA node

    Key: This runs in a SEPARATE process from DDR collection
    to avoid perf self-interference in frequency measurement.
    """

    def __init__(self, numa_id, output_file):
        super().__init__(output_file)
        self.numa_id = numa_id
        self.l3c_device = get_l3c_device_for_numa(numa_id)

        if not self.l3c_device:
            print(f"Warning: No L3C device found for NUMA {numa_id}")

    def build_command(self, interval):
        """Build perf stat command for uncore frequency"""
        if not self.l3c_device:
            return None

        # L3C config 0x7f measures uncore cycles
        event_str = f"{self.l3c_device}/config=0x7f/"
        return ["perf", "stat", "-e", event_str, "-o", self.output_file, "sleep", str(interval)]

    def start(self, interval):
        """Start uncore collection (may skip if no L3C device)"""
        cmd = self.build_command(interval)
        if cmd:
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.process

    def parse_output(self):
        """Parse uncore frequency output"""
        if not self.l3c_device:
            return None

        try:
            with open(self.output_file) as f:
                lines = f.readlines()

            for line in lines:
                line = line.strip()
                if not line or "Performance counter stats" in line or "seconds" in line:
                    continue

                parts = line.split()
                if len(parts) < 2:
                    continue

                try:
                    value = int(float(parts[0].replace(",", "")))
                    event_spec = parts[-1]

                    if "l3c" in event_spec and "config=0x7f" in event_spec:
                        # uncore_cycles collected over interval seconds
                        # Convert to GHz: cycles / 1e9
                        return value

                except (ValueError, IndexError):
                    continue

        except Exception as e:
            print(f"Error parsing uncore output for NUMA {self.numa_id}: {e}")

        return None


class CoreCollector(PerfCollector):
    """Collects core frequency for specified CPU range"""

    def __init__(self, cpu_range, output_file):
        super().__init__(output_file)
        self.cpu_range = cpu_range

    def build_command(self, interval):
        """Build perf stat command for core frequency"""
        return [
            "perf",
            "stat",
            "-e",
            "task-clock,cycles",
            "-C",
            self.cpu_range,
            "-o",
            self.output_file,
            "sleep",
            str(interval),
        ]

    def parse_output(self):
        """Parse core frequency output and return frequency in GHz"""
        try:
            with open(self.output_file) as f:
                lines = f.readlines()

            for line in lines:
                if "cycles" in line and "GHz" in line:
                    parts = line.split("#")
                    if len(parts) >= 2:
                        comment = parts[1].strip()
                        freq_parts = comment.split()
                        for p in freq_parts:
                            try:
                                ghz = float(p.replace(",", "").replace("GHz", "").replace("MHz", ""))
                                if "MHz" in comment:
                                    return ghz / 1000
                                else:
                                    return ghz
                            except ValueError:
                                continue

        except Exception as e:
            print(f"Error parsing core frequency output: {e}")

        return None


class ParallelCollector:
    """Orchestrates parallel collection of DDR, uncore, and core metrics

    Architecture:
        - One DDR collector per NUMA node (parallel)
        - One uncore collector per NUMA node (parallel, SEPARATE from DDR)
        - One core collector for all monitored CPUs

    This separation ensures:
        1. Uncore frequency measurement is not affected by DDR collection overhead
        2. Different NUMA nodes are collected in parallel (efficiency)
        3. All collectors use the same time window (synchronization)
    """

    def __init__(self, ddr_devices_by_numa, cpu_range=None, temp_dir=None):
        """
        Args:
            ddr_devices_by_numa: Dict mapping NUMA ID -> list of DDR devices
            cpu_range: CPU range for core frequency (None to skip)
            temp_dir: Directory for output files (None for current dir)
        """
        self.ddr_devices_by_numa = ddr_devices_by_numa
        self.cpu_range = cpu_range
        self.temp_dir = temp_dir or "."

        # Create collectors
        self.ddr_collectors = {}
        self.uncore_collectors = {}
        self.core_collector = None

        # Output file paths
        self._output_files = []

        self._setup_collectors()

    def _setup_collectors(self):
        """Initialize all collector instances"""
        # DDR collectors (one per NUMA)
        for numa_id, ddr_devices in self.ddr_devices_by_numa.items():
            output_file = os.path.join(self.temp_dir, f"perf_ddr_numa{numa_id}.txt")
            self._output_files.append(output_file)
            self.ddr_collectors[numa_id] = DDRCollector(numa_id, ddr_devices, output_file)

        # Uncore collectors (one per NUMA, SEPARATE from DDR)
        for numa_id in self.ddr_devices_by_numa.keys():
            output_file = os.path.join(self.temp_dir, f"perf_uncore_numa{numa_id}.txt")
            self._output_files.append(output_file)
            self.uncore_collectors[numa_id] = UncoreCollector(numa_id, output_file)

        # Core collector (one total)
        if self.cpu_range:
            output_file = os.path.join(self.temp_dir, "perf_core.txt")
            self._output_files.append(output_file)
            self.core_collector = CoreCollector(self.cpu_range, output_file)

    def collect_once(self, interval):
        """Run one collection cycle with all collectors in parallel

        Args:
            interval: Collection interval in seconds

        Returns:
            Tuple of (ddr_stats, uncore_stats, core_freq_ghz)
            - ddr_stats: Dict[numa_id, device_data]
            - uncore_stats: Dict[numa_id, uncore_cycles]
            - core_freq_ghz: Core frequency in GHz or None
        """
        # Start all collectors simultaneously
        processes = []

        # Start DDR collectors
        for numa_id, collector in self.ddr_collectors.items():
            proc = collector.start(interval)
            if proc:
                processes.append(proc)

        # Start uncore collectors (SEPARATE processes from DDR)
        for numa_id, collector in self.uncore_collectors.items():
            proc = collector.start(interval)
            if proc:
                processes.append(proc)

        # Start core collector
        if self.core_collector:
            proc = self.core_collector.start(interval)
            if proc:
                processes.append(proc)

        # Wait for all processes to complete
        for proc in processes:
            proc.wait()

        # Parse all results
        ddr_stats = {}
        uncore_stats = {}
        core_freq_ghz = None

        # Parse DDR results
        for numa_id, collector in self.ddr_collectors.items():
            ddr_stats[numa_id] = collector.parse_output()

        # Parse uncore results
        for numa_id, collector in self.uncore_collectors.items():
            uncore_cycles = collector.parse_output()
            if uncore_cycles is not None:
                uncore_stats[numa_id] = uncore_cycles

        # Parse core results
        if self.core_collector:
            core_freq_ghz = self.core_collector.parse_output()

        return ddr_stats, uncore_stats, core_freq_ghz

    def cleanup(self):
        """Clean up temporary output files"""
        for output_file in self._output_files:
            try:
                if os.path.exists(output_file):
                    os.remove(output_file)
            except Exception:
                pass


def calculate_ddr_metrics(device_stats):
    """Calculate DDR metrics from raw counter values

    Args:
        device_stats: Dict of config -> value for a DDR device

    Returns:
        Dict with calculated metrics or None if data incomplete
    """
    try:
        ddr_cycles = device_stats.get("0x00", 0)
        ddr_rd = device_stats.get("0x41", 0) + 1  # Avoid division by zero
        ddr_wr = device_stats.get("0x44", 0) + 1
        ddr_rd_time = device_stats.get("0x80", 0)
        ddr_wr_time = device_stats.get("0x81", 0)
        ddr_rd_data = device_stats.get("0x84", 0)
        ddr_wr_data = device_stats.get("0x83", 0)

        frequency_mhz = ddr_cycles * 4 / 1e6  # DDR frequency in MHz
        rd_bw = ddr_rd_data * 32 / 1024 / 1024 / 1024  # GB/s
        wr_bw = ddr_wr_data * 32 / 1024 / 1024 / 1024  # GB/s
        rd_latency = ddr_rd_time / ddr_rd if ddr_rd > 0 else 0  # cycles
        wr_latency = ddr_wr_time / ddr_wr if ddr_wr > 0 else 0  # cycles

        return {
            "frequency_mhz": frequency_mhz,
            "rd_bw": rd_bw,
            "wr_bw": wr_bw,
            "rd_latency": rd_latency,
            "wr_latency": wr_latency,
            "raw": {
                "ddr_cycles": ddr_cycles,
                "ddr_rd": ddr_rd,
                "ddr_wr": ddr_wr,
                "ddr_rd_time": ddr_rd_time,
                "ddr_wr_time": ddr_wr_time,
                "ddr_rd_data": ddr_rd_data,
                "ddr_wr_data": ddr_wr_data,
            },
        }
    except Exception:
        return None


def print_output(sample_num, ddr_stats, uncore_stats, core_freq_ghz, ddr_devices_by_numa, output_format="tsv"):
    """Print collected stats in specified format

    Output format for qemu_monitor parsing:
    - DDR device lines: NUMA{id}\tDDR\t{device}\t{freq}\t{rd_bw}\t{wr_bw}\t{rd_lat}\t{wr_lat}\t{uncore_ghz}
    - NUMA aggregate line: NUMA{id}\tAGGREGATE\t-\t{total_rd_bw}\t{total_wr_bw}\t{avg_rd_lat}\t{avg_wr_lat}\t{uncore_ghz}
    - Core frequency line: NUMA{id}\tCORE\t{freq_ghz}\t{cpu_range}\t-\t-\t{freq_ghz}
    """
    if output_format == "tsv":
        # Print DDR stats per NUMA
        for numa_id in sorted(ddr_devices_by_numa.keys()):
            ddr_devices = ddr_devices_by_numa[numa_id]
            device_data = ddr_stats.get(numa_id, {})

            # Get uncore frequency for this NUMA
            uncore_cycles = uncore_stats.get(numa_id, 0)
            uncore_freq_ghz = uncore_cycles / 1e9 if uncore_cycles else 0

            # Calculate NUMA-level DDR latency average
            total_rd_latency = 0
            total_wr_latency = 0
            total_rd_bw = 0
            total_wr_bw = 0
            device_count = 0

            # Print per-device DDR stats
            for ddr_device in ddr_devices:
                stats = device_data.get(ddr_device, {})
                metrics = calculate_ddr_metrics(stats)

                if metrics:
                    # Format: NUMA{id} DDR {device} {freq} {rd_bw} {wr_bw} {rd_lat} {wr_lat} {uncore_ghz}
                    print(
                        f"NUMA{numa_id}\tDDR\t{ddr_device}\t{metrics['frequency_mhz']:.0f}\t"
                        f"{metrics['rd_bw']:.2f}\t{metrics['wr_bw']:.2f}\t"
                        f"{metrics['rd_latency']:.2f}\t{metrics['wr_latency']:.2f}\t"
                        f"{uncore_freq_ghz:.3f}"
                    )

                    # Accumulate for NUMA average
                    total_rd_latency += metrics["rd_latency"]
                    total_wr_latency += metrics["wr_latency"]
                    total_rd_bw += metrics["rd_bw"]
                    total_wr_bw += metrics["wr_bw"]
                    device_count += 1

            # Print NUMA-level aggregate (average latency, total bandwidth)
            if device_count > 0:
                avg_rd_latency = total_rd_latency / device_count
                avg_wr_latency = total_wr_latency / device_count
                # Format: NUMA{id} AGGREGATE {device_count} {total_rd_bw} {total_wr_bw} {avg_rd_lat} {avg_wr_lat} {uncore_ghz}
                print(
                    f"NUMA{numa_id}\tAGGREGATE\t{device_count}\t"
                    f"{total_rd_bw:.2f}\t{total_wr_bw:.2f}\t"
                    f"{avg_rd_latency:.2f}\t{avg_wr_latency:.2f}\t"
                    f"{uncore_freq_ghz:.3f}"
                )

        # Print core stats
        if core_freq_ghz:
            for numa_id in sorted(ddr_devices_by_numa.keys()):
                # Format: NUMA{id} CORE {freq_ghz} {cpu_range}
                # Note: cpu_range is printed in header or stored elsewhere
                print(f"NUMA{numa_id}\tCORE\t{core_freq_ghz:.3f}\t-\t-\t-\t-\t{core_freq_ghz:.3f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DDR Latency and Frequency Monitoring Tool for 920x Platform (v1.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  1) Collect for specific NUMA node:
     python 920x_ddr_latency_v1.2.py -d 60 -i 3 -n 1

  2) Collect for all NUMA nodes:
     python 920x_ddr_latency_v1.2.py -d 60 -i 3 --all

  3) Collect DDR latency only (no core frequency):
     python 920x_ddr_latency_v1.2.py -d 60 -i 3 -n 1 --no-core

Architecture (v1.2):
  - DDR latency collectors: One per NUMA node (parallel)
  - Uncore frequency collectors: One per NUMA node (parallel, SEPARATE from DDR)
  - Core frequency collector: One total

  This separation ensures uncore frequency measurement is not affected by
  DDR collection overhead.

Output format (TSV - tab separated, for qemu_monitor parsing):
  Header: NUMA  Type    Device        Freq(MHz)  RD_BW(GB/s)  WR_BW(GB/s)  RD_Lat(cycle)  WR_Lat(cycle)  Uncore(GHz)

  DDR device line:    NUMA{id}  DDR       {device_name}    {freq}      {rd_bw}      {wr_bw}      {rd_lat}        {wr_lat}        {uncore_ghz}
  NUMA aggregate:     NUMA{id}  AGGREGATE {device_count}   -           {total_rd_bw} {total_wr_bw} {avg_rd_lat}    {avg_wr_lat}    {uncore_ghz}
  Core frequency:     NUMA{id}  CORE      {freq_ghz}       -            -            -            -               -               {freq_ghz}

NUMA mapping (920x example):
  hisi_sccl0_ddrc* -> NUMA 0 (CPU 0-95)
  hisi_sccl1_ddrc* -> NUMA 1 (CPU 96-191)
  hisi_sccl8_ddrc* -> NUMA 8 (CPU 192-287)
  hisi_sccl9_ddrc* -> NUMA 9 (CPU 288-383)
""",
    )
    parser.add_argument(
        "-d", "--duration", type=int, default=0, help="Total collection duration in seconds (0 = indefinite)"
    )
    parser.add_argument("-i", "--interval", type=int, default=3, help="Sampling interval in seconds (default: 3)")
    parser.add_argument("-n", "--numa", type=int, default=None, help="NUMA node ID to monitor (e.g., 1)")
    parser.add_argument("--all", action="store_true", help="Monitor all NUMA nodes")
    parser.add_argument("--no-core", action="store_true", help="Skip core frequency collection")
    parser.add_argument("--output", choices=["tsv", "json"], default="tsv", help="Output format (default: tsv)")
    parser.add_argument(
        "--temp-dir", type=str, default=None, help="Directory for temporary perf output files (default: current dir)"
    )

    args = parser.parse_args()

    duration = args.duration
    interval = args.interval
    numa_node = args.numa
    collect_all = args.all
    no_core = args.no_core
    temp_dir = args.temp_dir or "."

    # Determine DDR devices to monitor
    if collect_all:
        ddr_devices = find_ddr_devices_for_numa(None)
    elif numa_node is not None:
        ddr_devices = find_ddr_devices_for_numa(numa_node)
    else:
        # Default: monitor all
        ddr_devices = find_ddr_devices_for_numa(None)

    if not ddr_devices:
        print("ERROR: No DDR devices found")
        sys.exit(1)

    # Group DDR devices by NUMA node
    ddr_devices_by_numa = group_ddr_devices_by_numa(ddr_devices)

    # Determine CPU range for core frequency
    cpu_range = None
    if not no_core:
        numa_nodes_to_monitor = list(ddr_devices_by_numa.keys())
        if numa_nodes_to_monitor:
            cpu_range = calculate_cpu_range_from_numa(sorted(numa_nodes_to_monitor))

    print(f"Collection duration: {duration if duration > 0 else 'indefinite'} seconds")
    print(f"Sampling interval: {interval} seconds")
    print(f"DDR devices by NUMA: {dict(ddr_devices_by_numa)}")
    if cpu_range:
        print(f"Core frequency CPU range: {cpu_range}")
    print()

    # Print header
    print("NUMA\tType\tDevice\tFreq(MHz)\tRD_BW(GB/s)\tWR_BW(GB/s)\tRD_Lat(cycle)\tWR_Lat(cycle)\tUncore(GHz)")

    # Create parallel collector
    collector = ParallelCollector(ddr_devices_by_numa, cpu_range, temp_dir)

    try:
        sample_count = 0
        start_time = time.time()

        while True:
            if duration > 0 and (time.time() - start_time) >= duration:
                break

            sample_start = time.time()

            # Collect all metrics in parallel
            ddr_stats, uncore_stats, core_freq_ghz = collector.collect_once(interval)

            sample_count += 1

            # Print results
            print_output(sample_count, ddr_stats, uncore_stats, core_freq_ghz, ddr_devices_by_numa, args.output)

            # Wait for next interval (accounting for collection time)
            elapsed = time.time() - sample_start
            if elapsed < interval:
                time.sleep(interval - elapsed)

    finally:
        collector.cleanup()

    print(f"\nCollection completed. Total samples: {sample_count}")
