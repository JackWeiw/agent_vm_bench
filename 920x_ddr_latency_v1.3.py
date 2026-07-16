#!/usr/bin/env python3
"""
DDR Latency and Frequency Monitoring Tool for 920x Platform (v1.3)

Key Design Principle:
    SERIAL COLLECTION - not parallel!

    Reason: Uncore (L3C) frequency measurement is extremely sensitive to
    CPU activity. Even running multiple perf processes simultaneously
    generates L3C traffic that interferes with uncore frequency measurement.

    Solution: Sequential collection ensures each measurement is accurate:
    1. DDR latency/bandwidth (1 second)
    2. Uncore frequency (1 second, AFTER DDR collection ends)
    3. Core frequency (1 second)

Architecture:
    Phase 1: DDR Collection (all NUMAs)
        ├── Wait for completion
    Phase 2: Uncore Collection (all NUMAs, system now quiet)
        ├── Wait for completion
    Phase 3: Core Collection

    Total cycle time: 3 seconds (1 second per phase)

Usage:
    python 920x_ddr_latency_v1.3.py -d 60 -n 1
    python 920x_ddr_latency_v1.3.py --all
"""

import argparse
import os
import subprocess
import sys
import time
from collections import defaultdict


def calculate_cpu_range_from_numa(numa_nodes: list) -> str:
    """Calculate CPU core range from NUMA node IDs"""
    all_cores = []

    for node in numa_nodes:
        try:
            cpulist_path = f"/sys/devices/system/node/node{node}/cpulist"
            with open(cpulist_path) as f:
                cpulist = f.read().strip()

            for part in cpulist.split(","):
                if "-" in part:
                    start, end = part.split("-")
                    all_cores.extend(range(int(start), int(end) + 1))
                else:
                    all_cores.append(int(part))
        except Exception as e:
            print(f"Warning: Failed to read CPU list for NUMA node {node}: {e}")

    if not all_cores:
        return "0"

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

    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def get_numa_from_ddr_device(ddr_device):
    """Get NUMA node ID from DDR device name"""
    try:
        if "hisi_sccl" in ddr_device:
            parts = ddr_device.split("_")
            if len(parts) >= 2:
                sccl_id = parts[1].replace("sccl", "")
                return int(sccl_id)
    except Exception:
        pass
    return None


def get_l3c_device_for_numa(numa_node):
    """Get L3C device name for a NUMA node"""
    l3c_device = f"hisi_sccl{numa_node}_l3c0_0"
    if os.path.exists(f"/sys/devices/{l3c_device}"):
        return l3c_device
    return None


def find_ddr_devices_for_numa(numa_node=None):
    """Find DDR devices for specific NUMA node or all"""
    all_ddr = []
    try:
        for item in os.listdir("/sys/devices/"):
            if "ddrc" in item:
                all_ddr.append(str(item))
    except Exception:
        pass

    all_ddr.sort()

    if numa_node is not None:
        filtered = []
        for ddr in all_ddr:
            ddr_numa = get_numa_from_ddr_device(ddr)
            if ddr_numa == numa_node:
                filtered.append(ddr)
        return filtered

    return all_ddr


def group_ddr_devices_by_numa(ddr_devices):
    """Group DDR devices by NUMA node"""
    grouped = defaultdict(list)
    for ddr in ddr_devices:
        numa_id = get_numa_from_ddr_device(ddr)
        if numa_id is not None:
            grouped[numa_id].append(ddr)
    return grouped


class SerialCollector:
    """Serial (sequential) collection to avoid interference

    Key insight: Uncore frequency measurement is sensitive to CPU activity.
    Running DDR perf process simultaneously interferes with uncore measurement.

    Solution: Collect in phases:
    Phase 1: DDR latency (all NUMAs in parallel, but separate from uncore)
    Phase 2: Uncore frequency (after DDR collection ends, system quiet)
    Phase 3: Core frequency
    """

    # Fixed 1-second collection per phase
    COLLECTION_INTERVAL = 1

    # DDR events
    DDR_EVENTS = ["0x00", "0x41", "0x44", "0x80", "0x81", "0x83", "0x84"]

    def __init__(self, ddr_devices_by_numa, cpu_range=None, temp_dir=None):
        self.ddr_devices_by_numa = ddr_devices_by_numa
        self.cpu_range = cpu_range
        self.temp_dir = temp_dir or "."

        # Output files
        self.ddr_output = os.path.join(self.temp_dir, "perf_ddr.txt")
        self.uncore_outputs = {}
        self.core_output = os.path.join(self.temp_dir, "perf_core.txt")

        for numa_id in ddr_devices_by_numa.keys():
            self.uncore_outputs[numa_id] = os.path.join(self.temp_dir, f"perf_uncore_numa{numa_id}.txt")

    def collect_cycle(self):
        """Run one complete collection cycle (3 phases, 3 seconds total)

        Returns:
            Tuple of (ddr_stats, uncore_stats, core_freq_ghz)
        """
        # Phase 1: DDR latency/bandwidth collection
        ddr_stats = self._collect_ddr_phase()

        # Phase 2: Uncore frequency collection (DDR process has ended)
        uncore_stats = self._collect_uncore_phase()

        # Phase 3: Core frequency collection
        core_freq_ghz = self._collect_core_phase() if self.cpu_range else None

        return ddr_stats, uncore_stats, core_freq_ghz

    def _collect_ddr_phase(self):
        """Phase 1: Collect DDR latency and bandwidth for all NUMAs

        All NUMA DDR devices are collected in a SINGLE perf process.
        This is efficient and doesn't interfere with uncore measurement
        (which happens in the next phase).
        """
        # Build event list for all DDR devices
        events = []
        for numa_id, ddr_devices in self.ddr_devices_by_numa.items():
            for ddr_device in ddr_devices:
                for event_config in self.DDR_EVENTS:
                    events.append(f"{ddr_device}/config={event_config}/")

        event_str = ",".join(events)

        # Run DDR collection
        cmd = ["perf", "stat", "-e", event_str, "-o", self.ddr_output, "sleep", str(self.COLLECTION_INTERVAL)]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Parse DDR results
        return self._parse_ddr_output()

    def _collect_uncore_phase(self):
        """Phase 2: Collect uncore frequency for all NUMAs

        CRITICAL: This runs AFTER DDR collection has completed.
        The system is now relatively quiet, with minimal L3C traffic
        from perf processes. This ensures accurate uncore frequency measurement.

        All NUMA uncore measurements are collected in parallel (single perf process)
        since they don't interfere with each other at this point.
        """
        # Build event list for all uncore devices
        events = []
        l3c_devices = {}

        for numa_id in self.ddr_devices_by_numa.keys():
            l3c_device = get_l3c_device_for_numa(numa_id)
            if l3c_device:
                events.append(f"{l3c_device}/config=0x7f/")
                l3c_devices[numa_id] = l3c_device

        if not events:
            return {}

        event_str = ",".join(events)

        # Run uncore collection (system is quiet now)
        cmd = [
            "perf",
            "stat",
            "-e",
            event_str,
            "-o",
            self.uncore_outputs[list(self.uncore_outputs.keys())[0]],
            "sleep",
            str(self.COLLECTION_INTERVAL),
        ]

        # Use a single output file for all uncore events
        uncore_output = os.path.join(self.temp_dir, "perf_uncore.txt")
        cmd = ["perf", "stat", "-e", event_str, "-o", uncore_output, "sleep", str(self.COLLECTION_INTERVAL)]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Parse uncore results
        return self._parse_uncore_output(uncore_output, l3c_devices)

    def _collect_core_phase(self):
        """Phase 3: Collect core frequency

        Core frequency measurement is less sensitive to interference,
        but we still do it after DDR and uncore for consistency.
        """
        cmd = [
            "perf",
            "stat",
            "-e",
            "task-clock,cycles",
            "-C",
            self.cpu_range,
            "-o",
            self.core_output,
            "sleep",
            str(self.COLLECTION_INTERVAL),
        ]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return self._parse_core_output()

    def _parse_ddr_output(self):
        """Parse DDR perf output"""
        device_data = defaultdict(dict)

        try:
            with open(self.ddr_output) as f:
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
            print(f"Error parsing DDR output: {e}")

        return device_data

    def _parse_uncore_output(self, output_file, l3c_devices):
        """Parse uncore perf output"""
        uncore_stats = {}

        try:
            with open(output_file) as f:
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
                        # Extract NUMA ID from L3C device name
                        device_part = event_spec.split("/")[0]
                        numa_id = get_numa_from_ddr_device(device_part.replace("l3c0_0", "ddrc0_0"))
                        if numa_id is not None:
                            uncore_stats[numa_id] = value

                except (ValueError, IndexError):
                    continue

        except Exception as e:
            print(f"Error parsing uncore output: {e}")

        return uncore_stats

    def _parse_core_output(self):
        """Parse core frequency output"""
        try:
            with open(self.core_output) as f:
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

    def cleanup(self):
        """Clean up temporary output files"""
        files_to_clean = [self.ddr_output, os.path.join(self.temp_dir, "perf_uncore.txt"), self.core_output]

        for f in files_to_clean:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass


def calculate_ddr_metrics(device_stats):
    """Calculate DDR metrics from raw counter values"""
    try:
        ddr_cycles = device_stats.get("0x00", 0)
        ddr_rd = device_stats.get("0x41", 0) + 1
        ddr_wr = device_stats.get("0x44", 0) + 1
        ddr_rd_time = device_stats.get("0x80", 0)
        ddr_wr_time = device_stats.get("0x81", 0)
        ddr_rd_data = device_stats.get("0x84", 0)
        ddr_wr_data = device_stats.get("0x83", 0)

        frequency_mhz = ddr_cycles * 4 / 1e6
        rd_bw = ddr_rd_data * 32 / 1024 / 1024 / 1024
        wr_bw = ddr_wr_data * 32 / 1024 / 1024 / 1024
        rd_latency = ddr_rd_time / ddr_rd if ddr_rd > 0 else 0
        wr_latency = ddr_wr_time / ddr_wr if ddr_wr > 0 else 0

        return {
            "frequency_mhz": frequency_mhz,
            "rd_bw": rd_bw,
            "wr_bw": wr_bw,
            "rd_latency": rd_latency,
            "wr_latency": wr_latency,
        }
    except Exception:
        return None


def print_output(sample_num, ddr_stats, uncore_stats, core_freq_ghz, ddr_devices_by_numa):
    """Print collected stats"""

    for numa_id in sorted(ddr_devices_by_numa.keys()):
        ddr_devices = ddr_devices_by_numa[numa_id]
        device_data = ddr_stats

        # Get uncore frequency for this NUMA
        uncore_cycles = uncore_stats.get(numa_id, 0)
        uncore_freq_ghz = uncore_cycles / 1e9 if uncore_cycles else 0

        # Accumulate for NUMA average
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
                print(
                    f"NUMA{numa_id}\tDDR\t{ddr_device}\t{metrics['frequency_mhz']:.0f}\t"
                    f"{metrics['rd_bw']:.2f}\t{metrics['wr_bw']:.2f}\t"
                    f"{metrics['rd_latency']:.2f}\t{metrics['wr_latency']:.2f}\t"
                    f"{uncore_freq_ghz:.3f}"
                )

                total_rd_latency += metrics["rd_latency"]
                total_wr_latency += metrics["wr_latency"]
                total_rd_bw += metrics["rd_bw"]
                total_wr_bw += metrics["wr_bw"]
                device_count += 1

        # Print NUMA aggregate
        if device_count > 0:
            avg_rd_latency = total_rd_latency / device_count
            avg_wr_latency = total_wr_latency / device_count
            print(
                f"NUMA{numa_id}\tAGGREGATE\t{device_count}\t"
                f"{total_rd_bw:.2f}\t{total_wr_bw:.2f}\t"
                f"{avg_rd_latency:.2f}\t{avg_wr_latency:.2f}\t"
                f"{uncore_freq_ghz:.3f}"
            )

    # Print core stats
    if core_freq_ghz:
        for numa_id in sorted(ddr_devices_by_numa.keys()):
            print(f"NUMA{numa_id}\tCORE\t{core_freq_ghz:.3f}\t-\t-\t-\t-\t{core_freq_ghz:.3f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DDR Latency and Frequency Monitoring Tool for 920x Platform (v1.3 - Serial Collection)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  1) Collect for specific NUMA node:
     python 920x_ddr_latency_v1.3.py -d 60 -n 1

  2) Collect for all NUMA nodes:
     python 920x_ddr_latency_v1.3.py -d 60 --all

Design (v1.3):
  Serial collection to ensure accurate uncore frequency:

  Phase 1 (1s): DDR latency/bandwidth (all NUMAs)
      └─ Wait for completion
  Phase 2 (1s): Uncore frequency (system now quiet)
      └─ DDR process ended, L3C traffic minimal
  Phase 3 (1s): Core frequency

  Total cycle: 3 seconds

  Why serial? Uncore (L3C) frequency is extremely sensitive to CPU activity.
  Running DDR perf simultaneously generates L3C traffic that interferes with
  uncore measurement. Serial collection ensures accurate readings.

Output format (TSV):
  NUMA    Type    Device          Freq(MHz)  RD_BW(GB/s)  WR_BW(GB/s)  RD_Lat  WR_Lat  Uncore(GHz)
""",
    )
    parser.add_argument(
        "-d", "--duration", type=int, default=0, help="Total collection duration in seconds (0 = indefinite)"
    )
    parser.add_argument("-n", "--numa", type=int, default=None, help="NUMA node ID to monitor (e.g., 1)")
    parser.add_argument("--all", action="store_true", help="Monitor all NUMA nodes")
    parser.add_argument("--no-core", action="store_true", help="Skip core frequency collection")
    parser.add_argument("--temp-dir", type=str, default=None, help="Directory for temporary perf output files")

    args = parser.parse_args()

    duration = args.duration
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
    print(f"DDR devices by NUMA: {dict(ddr_devices_by_numa)}")
    if cpu_range:
        print(f"Core frequency CPU range: {cpu_range}")
    print("Collection mode: SERIAL (3 phases, 3 seconds per cycle)")
    print()

    # Print header
    print("NUMA\tType\tDevice\tFreq(MHz)\tRD_BW(GB/s)\tWR_BW(GB/s)\tRD_Lat(cycle)\tWR_Lat(cycle)\tUncore(GHz)")

    # Create serial collector
    collector = SerialCollector(ddr_devices_by_numa, cpu_range, temp_dir)

    try:
        sample_count = 0
        start_time = time.time()

        while True:
            if duration > 0 and (time.time() - start_time) >= duration:
                break

            # Collect one cycle (3 phases, 3 seconds total)
            ddr_stats, uncore_stats, core_freq_ghz = collector.collect_cycle()

            sample_count += 1

            # Print results
            print_output(sample_count, ddr_stats, uncore_stats, core_freq_ghz, ddr_devices_by_numa)

    finally:
        collector.cleanup()

    print(f"\nCollection completed. Total samples: {sample_count}")
