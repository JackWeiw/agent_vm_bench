# vm_monitor/parsers.py
"""
Log Parser Module

Parses output logs from various collection tools (devkit, ksys, ub_watch,
smap_bw, getfre) and extracts structured metrics data.
"""

import os
import re

# Internal dependency


def parse_devkit_top_down(log_path: str) -> dict:
    """Parse devkit_top_down.log to extract key metrics

    Returns:
        {
            'cycles_avg': average cycles across all reports,
            'ipc_avg': average IPC,
            ... (other averages),
            'timeline': [list of per-report data with timestamps],
            'report_count': number of reports parsed
        }
    """
    result = {
        "cycles": [],
        "instructions": [],
        "ipc": [],
        "bad_speculation": [],
        "frontend_bound": [],
        "retiring": [],
        "backend_bound": [],
        "l3_bound": [],
        "mem_bound": [],
        "mem_latency_bound": [],
        "mem_bandwidth_bound": [],
        "timestamps": [],
        "report_count": 0,
    }

    if not os.path.exists(log_path):
        return {"error": "File not found", "report_count": 0}

    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Split by report sections and keep the header with time
        report_headers = re.findall(
            r"TOP-DOWN Summary Report-\d+\s+Time:(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})", content
        )
        reports = re.split(r"TOP-DOWN Summary Report-\d+\s+Time:\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}", content)

        # Process each report (skip first empty split)
        report_idx = 0
        for report in reports[1:]:
            if not report.strip():
                continue

            # Get timestamp from header
            if report_idx < len(report_headers):
                result["timestamps"].append(report_headers[report_idx])
            else:
                result["timestamps"].append(f"Report-{report_idx + 1}")

            # Parse Cycles, Instructions, IPC (always append, use 0 if not found)
            cycles_match = re.search(r"Cycles\s+([\d,]+)", report)
            inst_match = re.search(r"Instructions\s+([\d,]+)", report)
            ipc_match = re.search(r"IPC\s+([\d.]+)", report)

            result["cycles"].append(float(cycles_match.group(1).replace(",", "")) if cycles_match else 0)
            result["instructions"].append(float(inst_match.group(1).replace(",", "")) if inst_match else 0)
            result["ipc"].append(float(ipc_match.group(1)) if ipc_match else 0)

            # Parse top-down metrics (always append)
            bad_spec_match = re.search(r"Bad Speculation\s+([\d.]+)", report)
            frontend_match = re.search(r"Frontend Bound\s+([\d.]+)", report)
            retiring_match = re.search(r"Retiring\s+([\d.]+)", report)
            backend_match = re.search(r"Backend Bound\s+([\d.]+)", report)

            result["bad_speculation"].append(float(bad_spec_match.group(1)) if bad_spec_match else 0)
            result["frontend_bound"].append(float(frontend_match.group(1)) if frontend_match else 0)
            result["retiring"].append(float(retiring_match.group(1)) if retiring_match else 0)
            result["backend_bound"].append(float(backend_match.group(1)) if backend_match else 0)

            # L3 Bound, Mem Bound (always append)
            l3_match = re.search(r"L3 Bound\s+([\d.]+)", report)
            mem_match = re.search(r"Mem Bound\s+([\d.]+)", report)
            latency_match = re.search(r"Latency bound\s+([\d.]+)", report)
            bw_match = re.search(r"Bandwidth bound\s+([\d.]+)", report)

            result["l3_bound"].append(float(l3_match.group(1)) if l3_match else 0)
            result["mem_bound"].append(float(mem_match.group(1)) if mem_match else 0)
            result["mem_latency_bound"].append(float(latency_match.group(1)) if latency_match else 0)
            result["mem_bandwidth_bound"].append(float(bw_match.group(1)) if bw_match else 0)

            result["report_count"] += 1
            report_idx += 1

        # Ensure all arrays have same length (truncate timestamps if needed)
        expected_len = result["report_count"]
        for key in [
            "cycles",
            "instructions",
            "ipc",
            "bad_speculation",
            "frontend_bound",
            "retiring",
            "backend_bound",
            "l3_bound",
            "mem_bound",
            "mem_latency_bound",
            "mem_bandwidth_bound",
        ]:
            # Pad with 0 if shorter
            while len(result[key]) < expected_len:
                result[key].append(0)
            # Truncate if longer
            result[key] = result[key][:expected_len]
        result["timestamps"] = result["timestamps"][:expected_len]

        # Calculate averages
        avg_result = {"report_count": result["report_count"]}
        for key in [
            "cycles",
            "instructions",
            "bad_speculation",
            "frontend_bound",
            "retiring",
            "backend_bound",
            "l3_bound",
            "mem_bound",
            "mem_latency_bound",
            "mem_bandwidth_bound",
        ]:
            if result[key]:
                avg_result[f"{key}_avg"] = sum(result[key]) / len(result[key])
                avg_result[f"{key}_max"] = max(result[key])
                avg_result[f"{key}_min"] = min(result[key])
            else:
                avg_result[f"{key}_avg"] = 0.0
                avg_result[f"{key}_max"] = 0.0
                avg_result[f"{key}_min"] = 0.0

        # IPC average: correct formula is sum(instructions) / sum(cycles)
        total_instructions = sum(result["instructions"]) if result["instructions"] else 0
        total_cycles = sum(result["cycles"]) if result["cycles"] else 0
        avg_result["ipc_avg"] = total_instructions / total_cycles if total_cycles > 0 else 0.0
        avg_result["ipc_max"] = max(result["ipc"]) if result["ipc"] else 0.0
        avg_result["ipc_min"] = min(result["ipc"]) if result["ipc"] else 0.0

        # IMPORTANT: Keep original IPC array for comparison in fix_ipc_offline.py
        avg_result["ipc"] = result["ipc"]  # Store raw IPC values for OLD formula calculation

        # Add timeline data
        avg_result["timestamps"] = result["timestamps"]
        avg_result["timeline"] = {
            "timestamp": result["timestamps"],
            "ipc": result["ipc"],
            "bad_speculation": result["bad_speculation"],
            "frontend_bound": result["frontend_bound"],
            "retiring": result["retiring"],
            "backend_bound": result["backend_bound"],
            "l3_bound": result["l3_bound"],
            "mem_bound": result["mem_bound"],
            "mem_latency_bound": result["mem_latency_bound"],
            "mem_bandwidth_bound": result["mem_bandwidth_bound"],
        }

        return avg_result

    except Exception as e:
        return {"error": str(e), "report_count": 0}


def parse_ksys(log_path: str) -> dict:
    """Parse ksys.log to extract Miss Latency metrics

    Returns:
        {
            'l2_miss_latency': {'cycles_max': x, 'cycles_min': y, 'cycles_avg': z},
            'l3_miss_latency': {'cycles_max': x, 'cycles_min': y, 'cycles_avg': z},
            'ipc': IPC value,
            'topdown': top-down summary data
        }
    """
    result = {"l2_miss_latency": {}, "l3_miss_latency": {}, "ipc": None, "topdown": {}}

    if not os.path.exists(log_path):
        return {"error": "File not found"}

    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Parse Miss Latency Summary Data
        miss_latency_section = re.search(
            r"Miss Latency Summary Data.*?\n.*?\n"
            r"\|.*?latency.*?cycles_max.*?cycles_min.*?cycles_avg.*?\n"
            r"\|.*?L2 Miss Latency.*?\|.*?(\d+).*?\|.*?(\d+).*?\|.*?(\d+).*?\n"
            r"\|.*?L3 Miss Latency.*?\|.*?(\d+).*?\|.*?(\d+).*?\|.*?(\d+).*?\n",
            content,
            re.DOTALL,
        )

        if miss_latency_section:
            result["l2_miss_latency"] = {
                "cycles_max": int(miss_latency_section.group(1)),
                "cycles_min": int(miss_latency_section.group(2)),
                "cycles_avg": int(miss_latency_section.group(3)),
            }
            result["l3_miss_latency"] = {
                "cycles_max": int(miss_latency_section.group(4)),
                "cycles_min": int(miss_latency_section.group(5)),
                "cycles_avg": int(miss_latency_section.group(6)),
            }

        # Alternative simpler parsing if above fails
        if not result["l2_miss_latency"]:
            l2_match = re.search(r"L2 Miss Latency.*?\|.*?(\d+).*?\|.*?(\d+).*?\|.*?(\d+)", content)
            if l2_match:
                result["l2_miss_latency"] = {
                    "cycles_max": int(l2_match.group(1)),
                    "cycles_min": int(l2_match.group(2)),
                    "cycles_avg": int(l2_match.group(3)),
                }

            l3_match = re.search(r"L3 Miss Latency.*?\|.*?(\d+).*?\|.*?(\d+).*?\|.*?(\d+)", content)
            if l3_match:
                result["l3_miss_latency"] = {
                    "cycles_max": int(l3_match.group(1)),
                    "cycles_min": int(l3_match.group(2)),
                    "cycles_avg": int(l3_match.group(3)),
                }

        # Parse IPC
        ipc_match = re.search(r"IPC\s*\|\s*([\d.]+)", content)
        if ipc_match:
            result["ipc"] = float(ipc_match.group(1))

        # Parse Topdown Summary
        retiring_match = re.search(r"Retiring\(%?\)\s*\|\s*([\d.]+)", content)
        frontend_match = re.search(r"Frontend Bound\(%?\)\s*\|\s*([\d.]+)", content)
        bad_spec_match = re.search(r"Bad Speculation\(%?\)\s*\|\s*([\d.]+)", content)
        backend_match = re.search(r"Backend Bound\(%?\)\s*\|\s*([\d.]+)", content)

        result["topdown"] = {
            "retiring": float(retiring_match.group(1)) if retiring_match else None,
            "frontend_bound": float(frontend_match.group(1)) if frontend_match else None,
            "bad_speculation": float(bad_spec_match.group(1)) if bad_spec_match else None,
            "backend_bound": float(backend_match.group(1)) if backend_match else None,
        }

        return result

    except Exception as e:
        return {"error": str(e)}


def parse_devkit_mem(log_path: str, numa_nodes: list = None) -> dict:
    """Parse devkit_mem.log to extract cache miss and bandwidth metrics

    Args:
        log_path: path to devkit_mem.log
        numa_nodes: list of NUMA nodes to filter bandwidth (e.g., [0, 1])

    Returns:
        {
            'cache_miss': {'L1D': avg, 'L1I': avg, 'L2D': avg, 'L2I': avg},
            'ddr_bandwidth_system': {'write': avg, 'read': avg},
            'numa_bandwidth': {node_id: {'read': avg, 'write': avg}},
            'l3_hit_rate': {node_id: avg_hit_rate},  # L3 Read Hit Rate per NUMA node
            'timeline': {per-report data with timestamps},
            'report_count': number of reports
        }
    """
    result = {
        "cache_miss": {"L1D": [], "L1I": [], "L2D": [], "L2I": []},
        "ddr_bandwidth_system": {"write": [], "read": []},
        "numa_bandwidth": {},
        "l3_hit_rate": {},  # L3 Read Hit Rate per NUMA node (CCL=-- rows)
        "timestamps": [],
        "report_count": 0,
    }

    if not os.path.exists(log_path):
        return {"error": "File not found", "report_count": 0}

    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Extract timestamps from report headers
        report_headers = re.findall(
            r"Memory Summary Report-\d+\s+Time:(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})", content
        )
        reports = re.split(r"Memory Summary Report-\d+\s+Time:\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}", content)

        # Process each report (skip first empty split)
        for i, report in enumerate(reports[1:], 0):
            if not report.strip():
                continue

            # Get timestamp
            if i < len(report_headers):
                result["timestamps"].append(report_headers[i])
            else:
                result["timestamps"].append(f"Report-{i + 1}")

            # Parse Cache miss
            l1d_match = re.search(r"L1D\s+([\d.]+)%", report)
            l1i_match = re.search(r"L1I\s+([\d.]+)%", report)
            l2d_match = re.search(r"L2D\s+([\d.]+)%", report)
            l2i_match = re.search(r"L2I\s+([\d.]+)%", report)

            result["cache_miss"]["L1D"].append(float(l1d_match.group(1)) if l1d_match else 0)
            result["cache_miss"]["L1I"].append(float(l1i_match.group(1)) if l1i_match else 0)
            result["cache_miss"]["L2D"].append(float(l2d_match.group(1)) if l2d_match else 0)
            result["cache_miss"]["L2I"].append(float(l2i_match.group(1)) if l2i_match else 0)

            # Parse DDR Bandwidth (system wide)
            write_match = re.search(r"ddrc_write\s+([\d.]+)MB/s", report)
            read_match = re.search(r"ddrc_read\s+([\d.]+)MB/s", report)

            result["ddr_bandwidth_system"]["write"].append(float(write_match.group(1)) if write_match else 0)
            result["ddr_bandwidth_system"]["read"].append(float(read_match.group(1)) if read_match else 0)

            # Parse NUMA bandwidth per node - track which nodes found in this report
            found_nodes = set()
            for line in report.split("\n"):
                if re.match(r"\s+[0-3]\s+[\d.]+MB/s", line):
                    node_match = re.match(r"\s+([0-3])\s+", line)
                    if node_match:
                        node_id = int(node_match.group(1))
                        matches = re.findall(r"([\d.]+)MB/s", line)
                        if len(matches) >= 2:
                            read_val = float(matches[-2])
                            write_val = float(matches[-1])
                            if node_id not in result["numa_bandwidth"]:
                                result["numa_bandwidth"][node_id] = {"read": [], "write": []}
                            result["numa_bandwidth"][node_id]["read"].append(read_val)
                            result["numa_bandwidth"][node_id]["write"].append(write_val)
                            found_nodes.add(node_id)

            # For nodes not found in this report, append 0 to maintain consistent length
            for node_id in result["numa_bandwidth"]:
                if node_id not in found_nodes:
                    result["numa_bandwidth"][node_id]["read"].append(0)
                    result["numa_bandwidth"][node_id]["write"].append(0)

            # Parse L3 Read Hit Rate (CCL=-- rows are NUMA-level aggregates)
            # Format: NODE  CCL  Read Hit Bandwidth  Read Bandwidth  Read Hit Rate
            # Example: 0     --         4092.77MB/s     6371.99MB/s         64.23%
            # Only collect for NUMA nodes specified in numa_nodes parameter
            found_l3_nodes = set()
            l3_section_match = re.search(
                r"2\. L3 Read Bandwidth and Hit Rate.*?───────────.*?\n.*?\n.*?\n(.*?)(?:───────────|$)",
                report,
                re.DOTALL,
            )
            if l3_section_match:
                l3_lines = l3_section_match.group(1).strip().split("\n")
                for line in l3_lines:
                    # Match lines with NODE and CCL=-- (NUMA aggregate)
                    # Pattern: NODE (digit) + whitespace + "--" + numbers + percentage
                    l3_match = re.match(r"\s*([0-3])\s+--\s+[\d.]+MB/s\s+[\d.]+MB/s\s+([\d.]+)%", line.strip())
                    if l3_match:
                        node_id = int(l3_match.group(1))
                        # Filter: only collect if node_id is in numa_nodes parameter
                        if numa_nodes and node_id not in numa_nodes:
                            continue
                        hit_rate = float(l3_match.group(2))
                        if node_id not in result["l3_hit_rate"]:
                            result["l3_hit_rate"][node_id] = []
                        result["l3_hit_rate"][node_id].append(hit_rate)
                        found_l3_nodes.add(node_id)

            # For L3 nodes not found in this report, append 0 to maintain consistent length
            for node_id in result["l3_hit_rate"]:
                if node_id not in found_l3_nodes:
                    result["l3_hit_rate"][node_id].append(0)

            result["report_count"] += 1

        # Ensure all main arrays have same length
        expected_len = result["report_count"]
        result["timestamps"] = result["timestamps"][:expected_len]
        for key in ["L1D", "L1I", "L2D", "L2I"]:
            while len(result["cache_miss"][key]) < expected_len:
                result["cache_miss"][key].append(0)
            result["cache_miss"][key] = result["cache_miss"][key][:expected_len]
        for key in ["write", "read"]:
            while len(result["ddr_bandwidth_system"][key]) < expected_len:
                result["ddr_bandwidth_system"][key].append(0)
            result["ddr_bandwidth_system"][key] = result["ddr_bandwidth_system"][key][:expected_len]

        # Ensure NUMA bandwidth arrays have consistent length
        for node_id in result["numa_bandwidth"]:
            for key in ["read", "write"]:
                while len(result["numa_bandwidth"][node_id][key]) < expected_len:
                    result["numa_bandwidth"][node_id][key].append(0)
                result["numa_bandwidth"][node_id][key] = result["numa_bandwidth"][node_id][key][:expected_len]

        # Ensure L3 hit rate arrays have consistent length
        for node_id in result["l3_hit_rate"]:
            while len(result["l3_hit_rate"][node_id]) < expected_len:
                result["l3_hit_rate"][node_id].append(0)
            result["l3_hit_rate"][node_id] = result["l3_hit_rate"][node_id][:expected_len]

        # Calculate averages
        avg_result = {"report_count": result["report_count"]}

        # Cache miss averages
        avg_result["cache_miss"] = {}
        for key in ["L1D", "L1I", "L2D", "L2I"]:
            if result["cache_miss"][key]:
                avg_result["cache_miss"][key] = sum(result["cache_miss"][key]) / len(result["cache_miss"][key])
            else:
                avg_result["cache_miss"][key] = 0.0

        # DDR bandwidth averages
        avg_result["ddr_bandwidth_system"] = {}
        for key in ["write", "read"]:
            if result["ddr_bandwidth_system"][key]:
                avg_result["ddr_bandwidth_system"][key] = sum(result["ddr_bandwidth_system"][key]) / len(
                    result["ddr_bandwidth_system"][key]
                )
            else:
                avg_result["ddr_bandwidth_system"][key] = 0.0

        # NUMA bandwidth averages
        avg_result["numa_bandwidth"] = {}
        for node_id, data in result["numa_bandwidth"].items():
            if numa_nodes and node_id not in numa_nodes:
                continue
            avg_result["numa_bandwidth"][node_id] = {
                "read": sum(data["read"]) / len(data["read"]) if data["read"] else 0.0,
                "write": sum(data["write"]) / len(data["write"]) if data["write"] else 0.0,
            }

        # L3 hit rate averages
        avg_result["l3_hit_rate"] = {}
        for node_id, data in result["l3_hit_rate"].items():
            avg_result["l3_hit_rate"][node_id] = sum(data) / len(data) if data else 0.0

        # Add timeline data
        avg_result["timestamps"] = result["timestamps"]
        avg_result["timeline"] = {
            "timestamp": result["timestamps"],
            "L1D_miss": result["cache_miss"]["L1D"],
            "L1I_miss": result["cache_miss"]["L1I"],
            "L2D_miss": result["cache_miss"]["L2D"],
            "L2I_miss": result["cache_miss"]["L2I"],
            "ddr_write": result["ddr_bandwidth_system"]["write"],
            "ddr_read": result["ddr_bandwidth_system"]["read"],
        }
        # Add NUMA bandwidth timeline
        for node_id, data in result["numa_bandwidth"].items():
            avg_result["timeline"][f"NUMA{node_id}_read"] = data["read"]
            avg_result["timeline"][f"NUMA{node_id}_write"] = data["write"]

        # Add L3 hit rate timeline
        for node_id, data in result["l3_hit_rate"].items():
            avg_result["timeline"][f"NUMA{node_id}_l3_hit_rate"] = data

        return avg_result

    except Exception as e:
        return {"error": str(e), "report_count": 0}


def parse_getfre(log_path: str) -> dict:
    """Parse getfre_NUMA*.log to extract core frequency metrics

    Args:
        log_path: path to getfre log file (e.g., getfre_NUMA0.log)

    Returns:
        {
            'core_stats': {core_id: {'avg': x, 'max': y, 'min': z}},
            'numa_avg': average frequency for this NUMA,
            'timeline': {timestamp: {core_id: freq}},
            'sample_count': number of samples
        }
    """
    result = {
        "core_stats": {},
        "samples": {},  # {core_id: [freqs]}
        "timeline": {},
        "sample_count": 0,
    }

    if not os.path.exists(log_path):
        return {"error": "File not found", "sample_count": 0}

    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            # Skip header
            lines = f.readlines()[1:]  # Skip 'timestamp,core,freq_mhz\n'

        for line in lines:
            line = line.strip()
            if not line:
                continue

            parts = line.split(",")
            if len(parts) != 3:
                continue

            timestamp, core_id, freq_str = parts

            # Skip ERROR/TIMEOUT entries
            if freq_str in ("ERROR", "TIMEOUT"):
                continue

            try:
                core_id = int(core_id)
                freq_mhz = int(freq_str)
            except ValueError:
                continue

            # Track samples per core
            if core_id not in result["samples"]:
                result["samples"][core_id] = []
            result["samples"][core_id].append(freq_mhz)

            # Track timeline
            if timestamp not in result["timeline"]:
                result["timeline"][timestamp] = {}
            result["timeline"][timestamp][core_id] = freq_mhz

            result["sample_count"] += 1

        # Calculate stats per core
        for core_id, freqs in result["samples"].items():
            if freqs:
                result["core_stats"][core_id] = {
                    "avg": sum(freqs) / len(freqs),
                    "max": max(freqs),
                    "min": min(freqs),
                    "count": len(freqs),
                }

        # Calculate NUMA average (across all cores)
        all_freqs = []
        for freqs in result["samples"].values():
            all_freqs.extend(freqs)

        if all_freqs:
            result["numa_avg"] = sum(all_freqs) / len(all_freqs)
            result["numa_max"] = max(all_freqs)
            result["numa_min"] = min(all_freqs)
        else:
            result["numa_avg"] = 0.0
            result["numa_max"] = 0.0
            result["numa_min"] = 0.0

        return result

    except Exception as e:
        return {"error": str(e), "sample_count": 0}


def parse_ub_watch(log_path: str) -> dict:
    """Parse ub_watch.log to extract FINAL PERFORMANCE REPORT

    Returns:
        {
            'latency': {'path': 'N0->N2', 'avg_r': ns, 'avg_w': ns, 'min_r': ns, 'min_w': ns, 'max_r': ns, 'max_w': ns},
            'bandwidth': [{chip, ports, avg_wr, avg_rd, avg_sum, max_wr, max_rd, max_sum}]
        }
    """
    result = {"latency": {}, "bandwidth": []}

    if not os.path.exists(log_path):
        return {"error": "File not found"}

    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Find FINAL PERFORMANCE REPORT section
        final_section = re.search(
            r"FINAL PERFORMANCE REPORT.*?Duration:.*?\n" r"(.*?)" r"\[System\] Cleanup done",
            content,
            re.DOTALL,
        )

        if not final_section:
            return {"error": "Final report section not found"}

        final_content = final_section.group(1)

        # Parse Latency Statistics
        latency_match = re.search(
            r"\[Latency Statistics.*?\n"
            r"Path.*?Samples.*?Avg\(R/W\).*?Min\(R/W\).*?Max\(R/W\).*?\n"
            r"(-+).*?\n"
            r"(N\d+->N\d+)\s+(\d+)\s+([\d]+)/([\d]+)\s+([\d]+)/([\d]+)\s+([\d]+)/([\d]+)",
            final_content,
            re.DOTALL,
        )

        if latency_match:
            result["latency"] = {
                "path": latency_match.group(2),
                "samples": int(latency_match.group(3)),
                "avg_r": int(latency_match.group(4)),
                "avg_w": int(latency_match.group(5)),
                "min_r": int(latency_match.group(6)),
                "min_w": int(latency_match.group(7)),
                "max_r": int(latency_match.group(8)),
                "max_w": int(latency_match.group(9)),
            }

        # Parse Bandwidth Statistics
        bw_section = re.search(
            r"\[Bandwidth Statistics.*?\n"
            r"Chip.*?Ports.*?Avg Wr.*?Avg Rd.*?Avg Sum.*?Max Wr.*?Max Rd.*?Max Sum.*?\n"
            r"(-+).*?\n(.*?)(?=\n-+|$)",
            final_content,
            re.DOTALL,
        )

        if bw_section:
            bw_lines = bw_section.group(2).strip().split("\n")
            for line in bw_lines:
                # Parse: Chip   Ports   Avg Wr   Avg Rd   Avg Sum   Max Wr   Max Rd   Max Sum
                parts = line.strip().split()
                if len(parts) >= 8:
                    try:
                        result["bandwidth"].append(
                            {
                                "chip": int(parts[0]),
                                "ports": parts[1],
                                "avg_wr": float(parts[2]),
                                "avg_rd": float(parts[3]),
                                "avg_sum": float(parts[4]),
                                "max_wr": float(parts[5]),
                                "max_rd": float(parts[6]),
                                "max_sum": float(parts[7]),
                            }
                        )
                    except ValueError:
                        continue

        return result

    except Exception as e:
        return {"error": str(e)}


def parse_smap_bw(log_path: str) -> dict:
    """Parse smap_bw.log to extract SMAP migration bandwidth metrics

    Returns:
        {
            'cycles': [{'cycle_no': int, 'total_pages': int, 'duration': float,
                       'bandwidth_gb_s': float, 'directions': {(from, to): pages}}],
            'summary': {'total_cycles': int, 'total_pages': int, 'avg_bandwidth_gb_s': float,
                        'min_bandwidth_gb_s': float, 'max_bandwidth_gb_s': float},
            'all_directions': set of (from_node, to_node) tuples for column headers
        }
    """
    result = {"cycles": [], "summary": {}, "all_directions": set()}

    if not os.path.exists(log_path):
        return {"error": "File not found"}

    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Parse each complete cycle report block (including direction stats)
        # Better approach: match cycle block boundary first (from header to bandwidth line)
        # Then extract details from within the block - avoids greedy matching issues
        cycle_boundary_pattern = re.compile(r"周期\s+(\d+)\s+迁移带宽报告(.*?)迁移带宽:\s+([\d.]+)\s+GB/s", re.DOTALL)

        for match in cycle_boundary_pattern.finditer(content):
            cycle_no = int(match.group(1))
            cycle_body = match.group(2)
            bandwidth = float(match.group(3))

            # Extract details from cycle_body
            pages_match = re.search(r"累计页数:\s+(\d+)", cycle_body)
            total_pages = int(pages_match.group(1)) if pages_match else 0

            duration_match = re.search(r"持续时长:\s+([\d.]+)\s+s", cycle_body)
            duration = float(duration_match.group(1)) if duration_match else 0

            # Extract directions from cycle_body
            directions = {}
            dir_pattern = re.compile(r"node\s+(\d+)\s+→\s+(\d+):\s+(\d+)\s+pages")
            for dir_match in dir_pattern.finditer(cycle_body):
                from_node = int(dir_match.group(1))
                to_node = int(dir_match.group(2))
                pages = int(dir_match.group(3))
                directions[(from_node, to_node)] = pages
                result["all_directions"].add((from_node, to_node))

            result["cycles"].append(
                {
                    "cycle_no": cycle_no,
                    "total_pages": total_pages,
                    "duration": duration,
                    "bandwidth_gb_s": bandwidth,
                    "directions": directions,
                }
            )

        # Parse global summary
        # Pattern: 全局汇总
        #   周期总数: N
        #   总页数: X
        #   平均带宽: Y GB/s
        #   周期带宽范围: min ~ max GB/s
        summary_pattern = re.compile(
            r"全局汇总.*?"
            r"周期总数:\s+(\d+).*?"
            r"总页数:\s+(\d+).*?"
            r"平均带宽:\s+([\d.]+)\s+GB/s.*?"
            r"周期带宽范围:\s+([\d.]+)\s+~\s+([\d.]+)\s+GB/s",
            re.DOTALL,
        )

        summary_match = summary_pattern.search(content)
        if summary_match:
            result["summary"] = {
                "total_cycles": int(summary_match.group(1)),
                "total_pages": int(summary_match.group(2)),
                "avg_bandwidth_gb_s": float(summary_match.group(3)),
                "min_bandwidth_gb_s": float(summary_match.group(4)),
                "max_bandwidth_gb_s": float(summary_match.group(5)),
            }

        # If we got cycles but no summary, calculate summary from cycles
        if result["cycles"] and not result["summary"]:
            bandwidths = [c["bandwidth_gb_s"] for c in result["cycles"]]
            result["summary"] = {
                "total_cycles": len(result["cycles"]),
                "total_pages": sum(c["total_pages"] for c in result["cycles"]),
                "avg_bandwidth_gb_s": sum(bandwidths) / len(bandwidths),
                "min_bandwidth_gb_s": min(bandwidths),
                "max_bandwidth_gb_s": max(bandwidths),
            }

        return result

    except Exception as e:
        return {"error": str(e)}


def parse_all_logs(log_dir: str, numa_nodes: list = None) -> dict:
    """Parse all log files in the directory

    Args:
        log_dir: directory containing log files
        numa_nodes: list of NUMA nodes for filtering devkit_mem bandwidth

    Returns:
        {
            'devkit_top_down': parsed result,
            'devkit_mem': parsed result,
            'ksys': parsed result,
            'ub_watch': parsed result,
            'smap_bw': parsed result,
            'getfre': {numa_id: parsed result}  # getfre results per NUMA
        }
    """
    results = {}

    # Parse devkit_top_down
    top_down_path = os.path.join(log_dir, "devkit_top_down.log")
    if os.path.exists(top_down_path):
        results["devkit_top_down"] = parse_devkit_top_down(top_down_path)

    # Parse devkit_mem
    mem_path = os.path.join(log_dir, "devkit_mem.log")
    if os.path.exists(mem_path):
        results["devkit_mem"] = parse_devkit_mem(mem_path, numa_nodes)

    # Parse ksys
    ksys_path = os.path.join(log_dir, "ksys.log")
    if os.path.exists(ksys_path):
        results["ksys"] = parse_ksys(ksys_path)

    # Parse ub_watch
    ub_path = os.path.join(log_dir, "ub_watch.log")
    if os.path.exists(ub_path):
        results["ub_watch"] = parse_ub_watch(ub_path)

    # Parse smap_bw
    smap_path = os.path.join(log_dir, "smap_bw.log")
    if os.path.exists(smap_path):
        results["smap_bw"] = parse_smap_bw(smap_path)

    # Parse getfre logs (per NUMA)
    getfre_results = {}
    for numa_id in range(4):  # Check NUMA 0-3
        getfre_path = os.path.join(log_dir, f"getfre_NUMA{numa_id}.log")
        if os.path.exists(getfre_path):
            getfre_results[numa_id] = parse_getfre(getfre_path)
    if getfre_results:
        results["getfre"] = getfre_results

    return results
