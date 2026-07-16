#!/usr/bin/env python3
"""
DDR Latency and Frequency Monitoring Tool for 920x Platform

Supports:
- DDR latency (RD/WR in cycles)
- DDR bandwidth (RD/WR in GB/s)
- Uncore frequency (L3C frequency)
- Core frequency (per NUMA node)

Usage:
    python 920x_ddr_latency_v1.1.py -d 60 -i 3 -n 1
    python 920x_ddr_latency_v1.1.py --all
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
        if "hisi_sccl" in ddr_device or "hisi_sccl" in ddr_device:
            parts = ddr_device.split("_")
            if len(parts) >= 2:
                sccl_id = parts[1].replace("sccl", "")
                return int(sccl_id)
    except Exception:
        pass
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


def collect_all_stats(ddr_devices, cpu_range=None, interval=1, duration=0):
    """Main collection loop

    Args:
        ddr_devices: List of DDR devices to monitor
        cpu_range: CPU range for core frequency (None to skip)
        interval: Sampling interval in seconds
        duration: Total duration in seconds (0 for indefinite)

    Returns:
        Generator yielding (sample_num, numa_stats, core_stats)
    """
    start_time = time.time()
    sample_count = 0

    # Use absolute paths for perf output files
    ddr_perf_output = os.path.abspath("perf_output_ddr.txt")
    core_perf_output = os.path.abspath("perf_output_core.txt")

    while True:
        if duration > 0 and (time.time() - start_time) >= duration:
            break

        sample_start = time.time()

        # Start both perf processes in parallel using Popen
        ddr_process = None
        core_process = None

        # Build DDR perf command
        events = []
        uncore_events = set()

        for ddr_device in ddr_devices:
            numa_node = get_numa_from_ddr_device(ddr_device)
            events.append(f"{ddr_device}/config=0x00/")
            events.append(f"{ddr_device}/config=0x41/")
            events.append(f"{ddr_device}/config=0x44/")
            events.append(f"{ddr_device}/config=0x80/")
            events.append(f"{ddr_device}/config=0x81/")
            events.append(f"{ddr_device}/config=0x83/")
            events.append(f"{ddr_device}/config=0x84/")
            if numa_node is not None:
                l3c_device = f"hisi_sccl{numa_node}_l3c0_0"
                uncore_key = f"{l3c_device}/config=0x7f/"
                if uncore_key not in uncore_events:
                    uncore_events.add(uncore_key)
                    events.append(uncore_key)

        event_str = ",".join(events)
        ddr_cmd = ["perf", "stat", "-e", event_str, "-o", ddr_perf_output, "sleep", str(interval)]

        # Start DDR perf (perf stat -o already handles output redirection)
        ddr_process = subprocess.Popen(ddr_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Start core frequency perf in parallel (perf stat -o handles output)
        core_process = None
        if cpu_range:
            core_cmd = [
                "perf",
                "stat",
                "-e",
                "task-clock,cycles",
                "-C",
                cpu_range,
                "-o",
                core_perf_output,
                "sleep",
                str(interval),
            ]
            core_process = subprocess.Popen(core_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for both processes to complete
        if ddr_process:
            ddr_process.wait()
        if core_process:
            core_process.wait()

        # Parse DDR results
        numa_stats = parse_ddr_output(ddr_devices, ddr_perf_output)

        # Parse core frequency results
        core_stats = None
        if cpu_range:
            core_freq_ghz = parse_core_frequency_output(core_perf_output)
            if core_freq_ghz:
                core_stats = {"cpu_range": cpu_range, "freq_ghz": core_freq_ghz}

        sample_count += 1

        yield sample_count, numa_stats, core_stats

        # Wait for next interval (accounting for collection time)
        elapsed = time.time() - sample_start
        if elapsed < interval:
            time.sleep(interval - elapsed)


def parse_ddr_output(ddr_devices, perf_output_path="perf_output_ddr.txt"):
    """Parse perf output file and return numa_stats

    Args:
        ddr_devices: List of DDR device names
        perf_output_path: Path to perf output file (absolute path recommended)
    """
    numa_stats = defaultdict(lambda: {"ddr_devices": [], "uncore_cycles": None})

    try:
        with open(perf_output_path) as f:
            lines = f.readlines()

        device_data = defaultdict(dict)
        uncore_data = {}

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
                    elif "l3c" in device_part and config_part == "0x7f":
                        numa_id = get_numa_from_ddr_device(device_part.replace("l3c0_0", "ddrc0_0"))
                        uncore_data[numa_id] = value

            except (ValueError, IndexError):
                continue

        for ddr_device in ddr_devices:
            numa_id = get_numa_from_ddr_device(ddr_device)
            data = device_data.get(ddr_device, {})

            if "0x00" in data:
                numa_stats[numa_id]["ddr_devices"].append(
                    {
                        "name": ddr_device,
                        "ddr_cycles": data.get("0x00", 0),
                        "ddr_rd": data.get("0x41", 0) + 1,
                        "ddr_wr": data.get("0x44", 0) + 1,
                        "ddr_rd_time": data.get("0x80", 0),
                        "ddr_wr_time": data.get("0x81", 0),
                        "ddr_rd_data": data.get("0x84", 0),
                        "ddr_wr_data": data.get("0x83", 0),
                    }
                )
                numa_stats[numa_id]["uncore_cycles"] = uncore_data.get(numa_id)

    except Exception as e:
        print(f"Error parsing DDR output: {e}")

    return numa_stats


def parse_core_frequency_output(perf_output_path="perf_output_core.txt"):
    """Parse perf output file and return core frequency in GHz

    Args:
        perf_output_path: Path to perf output file (absolute path recommended)
    """
    try:
        with open(perf_output_path) as f:
            lines = f.readlines()

        core_freq_ghz = None

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
                                core_freq_ghz = ghz / 1000
                            else:
                                core_freq_ghz = ghz
                            break
                        except ValueError:
                            continue

        return core_freq_ghz

    except Exception as e:
        print(f"Error parsing core frequency output: {e}")
        return None


def print_output(sample_num, numa_stats, core_stats, ddr_devices, output_format="tsv"):
    """Print collected stats in specified format

    Args:
        sample_num: Sample number
        numa_stats: Dict of NUMA ID -> stats
        core_stats: Core frequency stats dict or None
        ddr_devices: List of DDR device names
        output_format: 'tsv' or 'json'

    Output format for qemu_monitor parsing:
    - DDR device lines: NUMA{id}\tDDR\t{device}\t{freq}\t{rd_bw}\t{wr_bw}\t{rd_lat}\t{wr_lat}\t{uncore_ghz}
    - NUMA aggregate line: NUMA{id}\tAGGREGATE\t-\t{total_rd_bw}\t{total_wr_bw}\t{avg_rd_lat}\t{avg_wr_lat}\t{uncore_ghz}
    - Core frequency line: NUMA{id}\tCORE\t{freq_ghz}\t{cpu_range}\t-\t-\t{freq_ghz}
    """
    if output_format == "tsv":
        # Print DDR stats per NUMA
        for numa_id in sorted(numa_stats.keys()):
            stats = numa_stats[numa_id]
            uncore_cycles = stats["uncore_cycles"] or 0

            # Calculate uncore frequency (cycles -> GHz)
            # uncore_cycles is collected over interval seconds, need to divide by interval for rate
            uncore_freq_ghz = uncore_cycles / 1e9 if uncore_cycles else 0

            # Calculate NUMA-level DDR latency average
            total_rd_latency = 0
            total_wr_latency = 0
            total_rd_bw = 0
            total_wr_bw = 0
            device_count = 0

            # Print per-device DDR stats
            for dev in stats["ddr_devices"]:
                frequency = dev["ddr_cycles"] * 4 / 1e6  # DDR frequency in MHz
                rd_bw = dev["ddr_rd_data"] * 32 / 1024 / 1024 / 1024  # GB/s
                wr_bw = dev["ddr_wr_data"] * 32 / 1024 / 1024 / 1024  # GB/s
                rd_latency = dev["ddr_rd_time"] / dev["ddr_rd"] if dev["ddr_rd"] > 0 else 0  # cycles
                wr_latency = dev["ddr_wr_time"] / dev["ddr_wr"] if dev["ddr_wr"] > 0 else 0  # cycles

                # Format: NUMA{id} DDR {device} {freq} {rd_bw} {wr_bw} {rd_lat} {wr_lat} {uncore_ghz}
                print(
                    f"NUMA{numa_id}\tDDR\t{dev['name']}\t{frequency:.0f}\t"
                    f"{rd_bw:.2f}\t{wr_bw:.2f}\t{rd_latency:.2f}\t{wr_latency:.2f}\t"
                    f"{uncore_freq_ghz:.3f}"
                )

                # Accumulate for NUMA average
                total_rd_latency += rd_latency
                total_wr_latency += wr_latency
                total_rd_bw += rd_bw
                total_wr_bw += wr_bw
                device_count += 1

            # Print NUMA-level aggregate (average latency, total bandwidth)
            if device_count > 0:
                avg_rd_latency = total_rd_latency / device_count
                avg_wr_latency = total_wr_latency / device_count
                # Format: NUMA{id} AGGREGATE {total_rd_bw} {total_wr_bw} {avg_rd_lat} {avg_wr_lat} {uncore_ghz}
                print(
                    f"NUMA{numa_id}\tAGGREGATE\t{device_count}\t"
                    f"{total_rd_bw:.2f}\t{total_wr_bw:.2f}\t{avg_rd_latency:.2f}\t{avg_wr_latency:.2f}\t"
                    f"{uncore_freq_ghz:.3f}"
                )

        # Print core stats
        if core_stats:
            # Get NUMA id from first DDR device
            numa_ids = sorted(
                set(get_numa_from_ddr_device(d) for d in ddr_devices if get_numa_from_ddr_device(d) is not None)
            )
            for numa_id in numa_ids:
                # Format: NUMA{id} CORE {freq_ghz} {cpu_range}
                print(
                    f"NUMA{numa_id}\tCORE\t{core_stats['freq_ghz']:.3f}\t{core_stats['cpu_range']}\t-\t-\t{core_stats['freq_ghz']:.3f}"
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DDR Latency and Frequency Monitoring Tool for 920x Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  1) Collect for specific NUMA node:
     python 920x_ddr_latency_v1.1.py -d 60 -i 3 -n 1

  2) Collect for all NUMA nodes:
     python 920x_ddr_latency_v1.1.py -d 60 -i 3 --all

  3) Collect DDR latency only (no core frequency):
     python 920x_ddr_latency_v1.1.py -d 60 -i 3 -n 1 --no-core

Output format (TSV - tab separated, for qemu_monitor parsing):
  Header: NUMA  Type    Device        Freq(MHz)  RD_BW(GB/s)  WR_BW(GB/s)  RD_Lat(cycle)  WR_Lat(cycle)  Uncore(GHz)

  DDR device line:    NUMA{id}  DDR       {device_name}    {freq}      {rd_bw}      {wr_bw}      {rd_lat}        {wr_lat}        {uncore_ghz}
  NUMA aggregate:     NUMA{id}  AGGREGATE {device_count}   -           {total_rd_bw} {total_wr_bw} {avg_rd_lat}    {avg_wr_lat}    {uncore_ghz}
  Core frequency:     NUMA{id}  CORE      {freq_ghz}       {cpu_range}  -            -            -               -               {freq_ghz}

Key columns for parsing:
  - Type: DDR (per-device), AGGREGATE (NUMA-level summary), CORE (core frequency)
  - RD_Lat/WR_Lat: DDR read/write latency in cycles
  - Uncore(GHz): Uncore (L3C) frequency in GHz
  - Core frequency shown twice (in Freq column and Uncore column for consistency)

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

    args = parser.parse_args()

    duration = args.duration
    interval = args.interval
    numa_node = args.numa
    collect_all = args.all
    no_core = args.no_core

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

    # Determine CPU range for core frequency using calculate_cpu_range_from_numa
    cpu_range = None
    if not no_core:
        numa_nodes_to_monitor = set()
        for ddr in ddr_devices:
            numa_id = get_numa_from_ddr_device(ddr)
            if numa_id is not None:
                numa_nodes_to_monitor.add(numa_id)

        if numa_nodes_to_monitor:
            cpu_range = calculate_cpu_range_from_numa(sorted(numa_nodes_to_monitor))

    print(f"Collection duration: {duration if duration > 0 else 'indefinite'} seconds")
    print(f"Sampling interval: {interval} seconds")
    print(f"DDR devices: {ddr_devices}")
    if cpu_range:
        print(f"Core frequency CPU range: {cpu_range}")
    print()

    # Print header - updated format for easier parsing
    print("NUMA\tType\tDevice\tFreq(MHz)\tRD_BW(GB/s)\tWR_BW(GB/s)\tRD_Lat(cycle)\tWR_Lat(cycle)\tUncore(GHz)")

    # Main collection loop
    for sample_num, numa_stats, core_stats in collect_all_stats(ddr_devices, cpu_range, interval, duration):
        print_output(sample_num, numa_stats, core_stats, ddr_devices)

    print(f"\nCollection completed. Total samples: {sample_num}")
