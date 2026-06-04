#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-VM Agent Batch Test Scheduler

Orchestrates multiple test runs with different parameter combinations:
1. Define test parameter matrix (VM count, ratio, active percent)
2. Generate test task list
3. For each task: generate temp config, call auto_vm_test.py, collect results
4. Generate summary report comparing all test results

Usage:
    python batch_test_scheduler.py --config batch_config.yaml
"""

import os
import sys
import time
import subprocess
import argparse
import yaml
import json
import shutil
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple
from dataclasses import dataclass


@dataclass
class TestTask:
    """Single test task definition"""
    task_id: str
    vm_count: int
    ratio: float
    active_percent: float
    config_file: str = ""
    result_dir: str = ""
    success: bool = False
    error_msg: str = ""


def load_batch_config(config_path: str) -> Dict:
    """Load batch config YAML file"""
    with open(config_path) as f:
        return yaml.safe_load(f)


def generate_task_id(vm_count: int, ratio: float, active_percent: float) -> str:
    """Generate unique task ID from parameters"""
    return f"vm{vm_count}_ratio{ratio}_active{active_percent}"


def generate_test_tasks(batch_config: Dict) -> List[TestTask]:
    """Generate all test tasks from parameter matrix"""
    matrix = batch_config["test_matrix"]
    fixed = batch_config["fixed_params"]

    vm_counts = matrix["vm_counts"]
    ratios = matrix["ratios"]
    active_percentages = matrix["active_percentages"]

    tasks = []

    for vm_count in vm_counts:
        for ratio in ratios:
            for active_percent in active_percentages:
                task = TestTask(
                    task_id=generate_task_id(vm_count, ratio, active_percent),
                    vm_count=vm_count,
                    ratio=ratio,
                    active_percent=active_percent
                )
                tasks.append(task)

    return tasks


def generate_temp_config(template_path: str, task: TestTask, fixed_params: Dict, base_dir: str, output_dir: str) -> str:
    """Generate temporary config file for a test task"""
    # Load template
    with open(template_path) as f:
        template_content = f.read()

    # Replace dynamic parameters
    replacements = {
        "{{VM_COUNT}}": str(task.vm_count),
        "{{START_IP}}": fixed_params["start_ip"],
        "{{SWAP_SIZE_GB}}": str(fixed_params["swap_size_gb"]),
        "{{RATIO}}": str(task.ratio),
        "{{ACTIVE_PERCENT}}": str(task.active_percent),
        "{{DURATION}}": str(fixed_params["duration"]),
        "{{BASE_DIR}}": base_dir
    }

    for key, value in replacements.items():
        template_content = template_content.replace(key, value)

    # Write to temp file
    os.makedirs(output_dir, exist_ok=True)
    temp_config_path = os.path.join(output_dir, f"config_{task.task_id}.yaml")

    with open(temp_config_path, "w") as f:
        f.write(template_content)

    return temp_config_path


def run_single_test(task: TestTask, config_file: str, batch_log_file: str) -> Tuple[bool, str]:
    """Run single test via auto_vm_test.py"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(batch_log_file, "a") as f:
        f.write(f"\n[{timestamp}] Starting test: {task.task_id}\n")
        f.write(f"  VM count: {task.vm_count}\n")
        f.write(f"  Ratio: {task.ratio}\n")
        f.write(f"  Active percent: {task.active_percent}\n")
        f.write(f"  Config file: {config_file}\n")

    print(f"\n{'=' * 60}")
    print(f"Running test: {task.task_id}")
    print(f"{'=' * 60}")

    # Execute auto_vm_test.py
    cmd = ["python", "auto_vm_test.py", "--config", config_file]

    print(f"[DEBUG] Command: {' '.join(cmd)}")
    print(f"[DEBUG] Working dir: {os.getcwd()}")

    with open(batch_log_file, "a") as f:
        f.write(f"[DEBUG] Command: {' '.join(cmd)}\n")
        f.write(f"[DEBUG] Working dir: {os.getcwd()}\n")

    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    # Print and log stdout
    print(f"[DEBUG] Return code: {result.returncode}")
    print(f"[DEBUG] STDOUT ({len(result.stdout)} chars):")
    print(result.stdout)

    with open(batch_log_file, "a") as f:
        f.write(f"[DEBUG] Return code: {result.returncode}\n")
        f.write(f"[DEBUG] STDOUT:\n{result.stdout}\n")

    # Print and log stderr
    if result.stderr:
        print(f"[DEBUG] STDERR:")
        print(result.stderr)
        with open(batch_log_file, "a") as f:
            f.write(f"[DEBUG] STDERR:\n{result.stderr}\n")

    # Parse result directory from output
    result_dir = ""
    print(f"[DEBUG] Parsing result directory from output...")
    for line in result.stdout.split("\n"):
        print(f"[DEBUG] Line: '{line}'")
        if "Result directory:" in line or "Result dir:" in line or "Test result saved to:" in line:
            # Try different parsing patterns
            if "Result directory:" in line or "Result dir:" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    result_dir = parts[-1].strip()
            elif "Test result saved to:" in line:
                parts = line.split("saved to:")
                if len(parts) >= 2:
                    result_dir = parts[-1].strip()
            print(f"[DEBUG] Found result_dir: {result_dir}")
            break

    success = result.returncode == 0

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(batch_log_file, "a") as f:
        f.write(f"\n[{timestamp}] Test completed: {task.task_id}\n")
        f.write(f"  Success: {success}\n")
        f.write(f"  Result dir: {result_dir}\n")

    print(f"\nTest {task.task_id}: {'SUCCESS' if success else 'FAILED'}")
    print(f"Result: {result_dir}")

    return success, result_dir


def move_config_to_result(config_file: str, result_dir: str, task_id: str):
    """Move temp config file to result directory with task_id suffix"""
    if not result_dir or not os.path.exists(result_dir):
        return

    # Use task_id in filename to avoid collision with auto_vm_test.py's config.yaml
    dest = os.path.join(result_dir, f"config_{task_id}.yaml")
    shutil.move(config_file, dest)


def extract_browser_metrics_from_report(result_dir: str) -> Dict:
    """Extract browser metrics from vm_bench_lite bench_report file"""
    vm_bench_dir = os.path.join(result_dir, "vm_bench_lite")

    if not os.path.exists(vm_bench_dir):
        return {}

    # Find bench_report file
    bench_reports = list(Path(vm_bench_dir).glob("bench_report_*.txt"))
    if not bench_reports:
        return {}

    # Read the most recent report
    report_path = str(sorted(bench_reports)[-1])
    with open(report_path, encoding="utf-8") as f:
        content = f.read()

    # Parse metrics using regex
    metrics = {}

    # Success Rate: X.X%
    match = re.search(r"Success Rate:\s+([\d.]+)%", content)
    if match:
        metrics["success_rate"] = float(match.group(1))

    # Avg Latency: X.Xms
    match = re.search(r"Avg Latency:\s+([\d.]+)ms", content)
    if match:
        metrics["avg_latency"] = float(match.group(1))

    # P99 Latency: X.Xms
    match = re.search(r"P99 Latency:\s+([\d.]+)ms", content)
    if match:
        metrics["p99_latency"] = float(match.group(1))

    # Total Tasks
    match = re.search(r"Total Tasks:\s+(\d+)", content)
    if match:
        metrics["total_tasks"] = int(match.group(1))

    return metrics


def extract_qemu_metrics_from_excel(result_dir: str) -> Dict:
    """Extract QEMU and performance metrics from qemu_monitor analysis_report.xlsx

    Extracts ALL metrics from:
    - Summary sheet (VM CPU stats)
    - DevKit_TopDown sheet (13 metrics)
    - DevKit_Memory sheet (6 metrics + NUMA bandwidth)
    - KSys sheet (latency, IPC, topdown)
    - UBWatch_Latency sheet (8 metrics)
    """
    qemu_dir = os.path.join(result_dir, "qemu_monitor")
    excel_path = os.path.join(qemu_dir, "analysis_report.xlsx")

    if not os.path.exists(excel_path):
        return {}

    metrics = {}

    try:
        import pandas as pd

        # ========== Summary sheet ==========
        try:
            df_summary = pd.read_excel(excel_path, sheet_name="Summary")
            for idx, row in df_summary.iterrows():
                metric = str(row["Metric"]).strip() if pd.notna(row["Metric"]) else ""
                value = row["Value"]
                if metric == "VM Avg CPU":
                    metrics["avg_cpu_percent"] = float(value) if pd.notna(value) else 0
                elif metric == "VM Peak Total CPU":
                    metrics["max_cpu_percent"] = float(value) if pd.notna(value) else 0
        except Exception:
            pass

        # ========== DevKit_TopDown sheet (13 metrics) ==========
        try:
            df_topdown = pd.read_excel(excel_path, sheet_name="DevKit_TopDown")
            for idx, row in df_topdown.iterrows():
                metric = str(row["Metric"]).strip() if pd.notna(row["Metric"]) else ""
                value = row["Value"]
                # Map metric names to keys
                key_map = {
                    "Cycles Avg": "td_cycles_avg",
                    "Instructions Avg": "td_instructions_avg",
                    "IPC Avg": "td_ipc_avg",
                    "IPC Max": "td_ipc_max",
                    "IPC Min": "td_ipc_min",
                    "Bad Speculation (%)": "td_bad_speculation",
                    "Frontend Bound (%)": "td_frontend_bound",
                    "Retiring (%)": "td_retiring",
                    "Backend Bound (%)": "td_backend_bound",
                    "L3 Bound (%)": "td_l3_bound",
                    "Mem Bound (%)": "td_mem_bound",
                    "Latency Bound (%)": "td_latency_bound",
                    "Bandwidth Bound (%)": "td_bandwidth_bound",
                }
                if metric in key_map:
                    metrics[key_map[metric]] = float(value) if pd.notna(value) else 0
        except Exception:
            pass

        # ========== DevKit_Memory sheet (6 metrics) ==========
        try:
            df_mem = pd.read_excel(excel_path, sheet_name="DevKit_Memory")
            for idx, row in df_mem.iterrows():
                metric = str(row["Metric"]).strip() if pd.notna(row["Metric"]) else ""
                value = row["Value"]
                key_map = {
                    "L1D Miss (%)": "mem_l1d_miss",
                    "L1I Miss (%)": "mem_l1i_miss",
                    "L2D Miss (%)": "mem_l2d_miss",
                    "L2I Miss (%)": "mem_l2i_miss",
                    "DDR Read (MB/s)": "mem_ddr_read",
                    "DDR Write (MB/s)": "mem_ddr_write",
                }
                if metric in key_map:
                    metrics[key_map[metric]] = float(value) if pd.notna(value) else 0
        except Exception:
            pass

        # ========== NUMA_Bandwidth sheet ==========
        try:
            df_numa = pd.read_excel(excel_path, sheet_name="NUMA_Bandwidth")
            # Store per-node bandwidth, also compute total
            total_read = 0.0
            total_write = 0.0
            for idx, row in df_numa.iterrows():
                node = str(row["NUMA Node"]).strip() if pd.notna(row["NUMA Node"]) else ""
                read = float(row["Read (MB/s)"]) if pd.notna(row["Read (MB/s)"]) else 0
                write = float(row["Write (MB/s)"]) if pd.notna(row["Write (MB/s)"]) else 0
                if node:
                    metrics[f"numa{node}_read"] = read
                    metrics[f"numa{node}_write"] = write
                    total_read += read
                    total_write += write
            metrics["numa_total_read"] = total_read
            metrics["numa_total_write"] = total_write
        except Exception:
            pass

        # ========== KSys sheet ==========
        try:
            df_ksys = pd.read_excel(excel_path, sheet_name="KSys")
            for idx, row in df_ksys.iterrows():
                metric = str(row["Metric"]).strip() if pd.notna(row["Metric"]) else ""
                value = row["Value"]
                key_map = {
                    "L2 Miss Latency Max": "ksys_l2_latency_max",
                    "L2 Miss Latency Min": "ksys_l2_latency_min",
                    "L2 Miss Latency Avg": "ksys_l2_latency_avg",
                    "L3 Miss Latency Max": "ksys_l3_latency_max",
                    "L3 Miss Latency Min": "ksys_l3_latency_min",
                    "L3 Miss Latency Avg": "ksys_l3_latency_avg",
                    "IPC": "ksys_ipc",
                    "Retiring (%)": "ksys_retiring",
                    "Frontend Bound (%)": "ksys_frontend_bound",
                    "Bad Speculation (%)": "ksys_bad_speculation",
                    "Backend Bound (%)": "ksys_backend_bound",
                }
                if metric in key_map:
                    metrics[key_map[metric]] = float(value) if pd.notna(value) else 0
        except Exception:
            pass

        # ========== UBWatch_Latency sheet ==========
        try:
            df_ub = pd.read_excel(excel_path, sheet_name="UBWatch_Latency")
            for idx, row in df_ub.iterrows():
                metric = str(row["Metric"]).strip() if pd.notna(row["Metric"]) else ""
                value = row["Value"]
                key_map = {
                    "Samples": "ub_samples",
                    "Avg Read (ns)": "ub_avg_read_ns",
                    "Avg Write (ns)": "ub_avg_write_ns",
                    "Min Read (ns)": "ub_min_read_ns",
                    "Min Write (ns)": "ub_min_write_ns",
                    "Max Read (ns)": "ub_max_read_ns",
                    "Max Write (ns)": "ub_max_write_ns",
                }
                if metric in key_map:
                    # Handle numeric values, skip "Latency Path" which is a string
                    try:
                        metrics[key_map[metric]] = float(value) if pd.notna(value) else 0
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    except ImportError:
        # pandas not available, try openpyxl
        try:
            from openpyxl import load_workbook
            wb = load_workbook(excel_path, data_only=True)

            # Helper function to extract metrics from a sheet
            def extract_sheet_metrics(ws, key_map):
                result = {}
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row[0]:
                        metric = str(row[0]).strip()
                        if metric in key_map:
                            try:
                                result[key_map[metric]] = float(row[1]) if row[1] else 0
                            except (ValueError, TypeError):
                                pass
                return result

            # Summary sheet
            if "Summary" in wb.sheetnames:
                ws = wb["Summary"]
                key_map = {"VM Avg CPU": "avg_cpu_percent", "VM Peak Total CPU": "max_cpu_percent"}
                metrics.update(extract_sheet_metrics(ws, key_map))

            # DevKit_TopDown
            if "DevKit_TopDown" in wb.sheetnames:
                ws = wb["DevKit_TopDown"]
                key_map = {
                    "Cycles Avg": "td_cycles_avg",
                    "Instructions Avg": "td_instructions_avg",
                    "IPC Avg": "td_ipc_avg",
                    "IPC Max": "td_ipc_max",
                    "IPC Min": "td_ipc_min",
                    "Bad Speculation (%)": "td_bad_speculation",
                    "Frontend Bound (%)": "td_frontend_bound",
                    "Retiring (%)": "td_retiring",
                    "Backend Bound (%)": "td_backend_bound",
                    "L3 Bound (%)": "td_l3_bound",
                    "Mem Bound (%)": "td_mem_bound",
                    "Latency Bound (%)": "td_latency_bound",
                    "Bandwidth Bound (%)": "td_bandwidth_bound",
                }
                metrics.update(extract_sheet_metrics(ws, key_map))

            # DevKit_Memory
            if "DevKit_Memory" in wb.sheetnames:
                ws = wb["DevKit_Memory"]
                key_map = {
                    "L1D Miss (%)": "mem_l1d_miss",
                    "L1I Miss (%)": "mem_l1i_miss",
                    "L2D Miss (%)": "mem_l2d_miss",
                    "L2I Miss (%)": "mem_l2i_miss",
                    "DDR Read (MB/s)": "mem_ddr_read",
                    "DDR Write (MB/s)": "mem_ddr_write",
                }
                metrics.update(extract_sheet_metrics(ws, key_map))

            # NUMA_Bandwidth
            if "NUMA_Bandwidth" in wb.sheetnames:
                ws = wb["NUMA_Bandwidth"]
                total_read = 0.0
                total_write = 0.0
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row[0]:
                        node = str(row[0]).strip()
                        try:
                            read = float(row[1]) if row[1] else 0
                            write = float(row[2]) if row[2] else 0
                            metrics[f"numa{node}_read"] = read
                            metrics[f"numa{node}_write"] = write
                            total_read += read
                            total_write += write
                        except (ValueError, TypeError):
                            pass
                metrics["numa_total_read"] = total_read
                metrics["numa_total_write"] = total_write

            # KSys
            if "KSys" in wb.sheetnames:
                ws = wb["KSys"]
                key_map = {
                    "L2 Miss Latency Max": "ksys_l2_latency_max",
                    "L2 Miss Latency Min": "ksys_l2_latency_min",
                    "L2 Miss Latency Avg": "ksys_l2_latency_avg",
                    "L3 Miss Latency Max": "ksys_l3_latency_max",
                    "L3 Miss Latency Min": "ksys_l3_latency_min",
                    "L3 Miss Latency Avg": "ksys_l3_latency_avg",
                    "IPC": "ksys_ipc",
                    "Retiring (%)": "ksys_retiring",
                    "Frontend Bound (%)": "ksys_frontend_bound",
                    "Bad Speculation (%)": "ksys_bad_speculation",
                    "Backend Bound (%)": "ksys_backend_bound",
                }
                metrics.update(extract_sheet_metrics(ws, key_map))

            # UBWatch_Latency
            if "UBWatch_Latency" in wb.sheetnames:
                ws = wb["UBWatch_Latency"]
                key_map = {
                    "Samples": "ub_samples",
                    "Avg Read (ns)": "ub_avg_read_ns",
                    "Avg Write (ns)": "ub_avg_write_ns",
                    "Min Read (ns)": "ub_min_read_ns",
                    "Min Write (ns)": "ub_min_write_ns",
                    "Max Read (ns)": "ub_max_read_ns",
                    "Max Write (ns)": "ub_max_write_ns",
                }
                metrics.update(extract_sheet_metrics(ws, key_map))

        except ImportError:
            print(f"WARNING: Neither pandas nor openpyxl available for {result_dir}")
        except Exception as e:
            print(f"WARNING: Failed to read Excel for {result_dir}: {e}")

    return metrics


def collect_all_results(tasks: List[TestTask], base_dir: str) -> Dict[str, Any]:
    """Collect results from all test runs by parsing raw result files"""
    all_metrics = []

    for task in tasks:
        if not task.result_dir:
            continue

        if not os.path.exists(task.result_dir):
            continue

        # Extract browser metrics from vm_bench_lite report
        browser_metrics = extract_browser_metrics_from_report(task.result_dir)

        # Extract ALL QEMU and performance metrics from analysis_report.xlsx
        qemu_metrics = extract_qemu_metrics_from_excel(task.result_dir)

        # Try to get parameters from config.yaml in result dir
        params = {}
        config_path = os.path.join(task.result_dir, "config.yaml")
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f)
                params = {
                    "vm_count": int(config.get("vm", {}).get("count", 0)),
                    "ratio": float(config.get("smap_tool", {}).get("ratio", 0)),
                    "active_percent": float(config.get("test", {}).get("active_percent", 0)),
                }
            except Exception:
                # Fallback to task parameters
                params = {
                    "vm_count": task.vm_count,
                    "ratio": task.ratio,
                    "active_percent": task.active_percent,
                }
        else:
            params = {
                "vm_count": task.vm_count,
                "ratio": task.ratio,
                "active_percent": task.active_percent,
            }

        # Build metrics dict - pass ALL extracted metrics
        metrics = {
            "task_id": task.task_id,
            "success": task.success,
            "parameters": params,
            "browser_metrics": browser_metrics,
            "qemu_metrics": qemu_metrics,  # All metrics from Excel
        }

        all_metrics.append(metrics)

    return {
        "tasks": all_metrics,
        "total_tests": len(tasks),
        "successful_tests": sum(1 for t in tasks if t.success),
        "failed_tests": sum(1 for t in tasks if not t.success),
    }


def generate_summary_report(results: Dict, output_path: str):
    """Generate Excel summary report with ALL metrics from all sheets"""
    try:
        import pandas as pd

        # Build DataFrame with all metrics
        rows = []
        for task_data in results["tasks"]:
            params = task_data.get("parameters", {})
            browser = task_data.get("browser_metrics", {})
            qemu = task_data.get("qemu_metrics", {})

            # Basic test info
            row = {
                "test_id": task_data.get("task_id", ""),
                "vm_count": params.get("vm_count", 0),
                "ratio": params.get("ratio", 0),
                "active_percent": params.get("active_percent", 0),
                "active_vm_count": int(params.get("vm_count", 0) * params.get("active_percent", 0)),
                "success": task_data.get("success", False),
            }

            # Browser metrics
            row["browser_success_rate"] = browser.get("success_rate", 0)
            row["browser_avg_latency_ms"] = browser.get("avg_latency", 0)
            row["browser_p99_latency_ms"] = browser.get("p99_latency", 0)

            # VM CPU metrics (from Summary sheet)
            row["vm_avg_cpu_percent"] = qemu.get("avg_cpu_percent", 0)
            row["vm_max_cpu_percent"] = qemu.get("max_cpu_percent", 0)

            # DevKit TopDown metrics (13 metrics)
            row["td_cycles_avg"] = qemu.get("td_cycles_avg", 0)
            row["td_instructions_avg"] = qemu.get("td_instructions_avg", 0)
            row["td_ipc_avg"] = qemu.get("td_ipc_avg", 0)
            row["td_ipc_max"] = qemu.get("td_ipc_max", 0)
            row["td_ipc_min"] = qemu.get("td_ipc_min", 0)
            row["td_bad_speculation_percent"] = qemu.get("td_bad_speculation", 0)
            row["td_frontend_bound_percent"] = qemu.get("td_frontend_bound", 0)
            row["td_retiring_percent"] = qemu.get("td_retiring", 0)
            row["td_backend_bound_percent"] = qemu.get("td_backend_bound", 0)
            row["td_l3_bound_percent"] = qemu.get("td_l3_bound", 0)
            row["td_mem_bound_percent"] = qemu.get("td_mem_bound", 0)
            row["td_latency_bound_percent"] = qemu.get("td_latency_bound", 0)
            row["td_bandwidth_bound_percent"] = qemu.get("td_bandwidth_bound", 0)

            # DevKit Memory metrics (6 metrics)
            row["mem_l1d_miss_percent"] = qemu.get("mem_l1d_miss", 0)
            row["mem_l1i_miss_percent"] = qemu.get("mem_l1i_miss", 0)
            row["mem_l2d_miss_percent"] = qemu.get("mem_l2d_miss", 0)
            row["mem_l2i_miss_percent"] = qemu.get("mem_l2i_miss", 0)
            row["mem_ddr_read_mb_s"] = qemu.get("mem_ddr_read", 0)
            row["mem_ddr_write_mb_s"] = qemu.get("mem_ddr_write", 0)

            # NUMA Bandwidth (total)
            row["numa_total_read_mb_s"] = qemu.get("numa_total_read", 0)
            row["numa_total_write_mb_s"] = qemu.get("numa_total_write", 0)
            # Per-node NUMA bandwidth (dynamically add if exists)
            for key, value in qemu.items():
                if key.startswith("numa") and "_read" in key and key != "numa_total_read":
                    row[key] = value
                elif key.startswith("numa") and "_write" in key and key != "numa_total_write":
                    row[key] = value

            # KSys metrics
            row["ksys_l2_latency_max"] = qemu.get("ksys_l2_latency_max", 0)
            row["ksys_l2_latency_min"] = qemu.get("ksys_l2_latency_min", 0)
            row["ksys_l2_latency_avg"] = qemu.get("ksys_l2_latency_avg", 0)
            row["ksys_l3_latency_max"] = qemu.get("ksys_l3_latency_max", 0)
            row["ksys_l3_latency_min"] = qemu.get("ksys_l3_latency_min", 0)
            row["ksys_l3_latency_avg"] = qemu.get("ksys_l3_latency_avg", 0)
            row["ksys_ipc"] = qemu.get("ksys_ipc", 0)
            row["ksys_retiring_percent"] = qemu.get("ksys_retiring", 0)
            row["ksys_frontend_bound_percent"] = qemu.get("ksys_frontend_bound", 0)
            row["ksys_bad_speculation_percent"] = qemu.get("ksys_bad_speculation", 0)
            row["ksys_backend_bound_percent"] = qemu.get("ksys_backend_bound", 0)

            # UBWatch Latency metrics
            row["ub_samples"] = qemu.get("ub_samples", 0)
            row["ub_avg_read_ns"] = qemu.get("ub_avg_read_ns", 0)
            row["ub_avg_write_ns"] = qemu.get("ub_avg_write_ns", 0)
            row["ub_min_read_ns"] = qemu.get("ub_min_read_ns", 0)
            row["ub_min_write_ns"] = qemu.get("ub_min_write_ns", 0)
            row["ub_max_read_ns"] = qemu.get("ub_max_read_ns", 0)
            row["ub_max_write_ns"] = qemu.get("ub_max_write_ns", 0)

            rows.append(row)

        df = pd.DataFrame(rows)

        # Sort by vm_count, ratio, active_percent ascending
        df = df.sort_values(by=['vm_count', 'ratio', 'active_percent'], ascending=[True, True, True])

        # Save to Excel
        df.to_excel(output_path, index=False, sheet_name="Summary")

        print(f"\nSummary report saved to: {output_path}")

    except ImportError:
        print("WARNING: pandas not available, saving as JSON instead")
        json_path = output_path.replace(".xlsx", ".json")
        with open(json_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Summary saved to: {json_path}")


def scan_existing_results(result_base_dir: str) -> List[TestTask]:
    """Scan existing test result directories and create TestTask list"""
    tasks = []

    if not os.path.exists(result_base_dir):
        print(f"ERROR: Result directory not found: {result_base_dir}")
        return tasks

    # Scan subdirectories matching pattern: vm{n}_ratio{ratio}_active{percent}_timestamp
    pattern = re.compile(r"^vm(\d+)_ratio([\d.]+)_active([\d.]+)_\d{8}_\d{6}$")

    for entry in os.listdir(result_base_dir):
        entry_path = os.path.join(result_base_dir, entry)

        if not os.path.isdir(entry_path):
            continue

        # Try to match pattern
        match = pattern.match(entry)
        if match:
            vm_count = int(match.group(1))
            ratio = float(match.group(2))
            active_percent = float(match.group(3))

            # Check if result files exist
            has_qemu = os.path.exists(os.path.join(entry_path, "qemu_monitor", "analysis_report.xlsx"))
            has_bench = os.path.exists(os.path.join(entry_path, "vm_bench_lite"))

            # Consider success if both result directories exist
            success = has_qemu or has_bench

            task = TestTask(
                task_id=entry,
                vm_count=vm_count,
                ratio=ratio,
                active_percent=active_percent,
                result_dir=entry_path,
                success=success
            )
            tasks.append(task)
            print(f"  Found: {entry} (vm={vm_count}, ratio={ratio}, active={active_percent})")

    # Sort by task_id
    tasks.sort(key=lambda t: t.task_id)

    return tasks


def offline_summary(result_base_dir: str, output_path: str = None):
    """Generate batch summary from existing test results (offline mode)"""
    print("=" * 60)
    print("Offline Batch Summary Generator")
    print("=" * 60)
    print(f"Scanning result directory: {result_base_dir}")

    # Scan existing results
    tasks = scan_existing_results(result_base_dir)

    if not tasks:
        print("ERROR: No valid test result directories found")
        return

    print(f"\nFound {len(tasks)} test results")

    # Summary of what we found
    success_count = sum(1 for t in tasks if t.success)
    print(f"  Complete results (has data files): {success_count}")
    print(f"  Incomplete results: {len(tasks) - success_count}")

    # Collect results
    print("\n" + "=" * 60)
    print("Extracting metrics from result files...")
    print("=" * 60)

    results = collect_all_results(tasks, result_base_dir)

    # Generate output path if not specified
    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(result_base_dir, f"batch_summary_{timestamp}.xlsx")

    # Generate summary report
    print("\nGenerating summary report...")
    generate_summary_report(results, output_path)

    # Print summary
    print("\n" + "=" * 60)
    print("Offline Summary Complete")
    print("=" * 60)
    print(f"Total results processed: {len(tasks)}")
    print(f"Successful: {results['successful_tests']}")
    print(f"Failed/Incomplete: {results['failed_tests']}")
    print(f"Summary report: {output_path}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Multi-VM Agent Batch Test Scheduler")
    parser.add_argument("--config", default="batch_config.yaml", help="Batch config YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Print tasks without executing")
    parser.add_argument("--offline", action="store_true", help="Generate summary from existing results (offline mode)")
    parser.add_argument("--result-dir", help="Result directory for offline mode (default: from config)")
    parser.add_argument("--output", help="Output Excel path for offline mode")

    args = parser.parse_args()

    # Offline mode: generate summary from existing results
    if args.offline:
        # Determine result directory
        if args.result_dir:
            result_dir = args.result_dir
        else:
            # Try to load from config
            try:
                batch_config = load_batch_config(args.config)
                result_dir = batch_config["result"]["base_dir"]
            except Exception:
                print("ERROR: Cannot determine result directory. Use --result-dir or provide valid --config")
                return

        offline_summary(result_dir, args.output)
        return

    # Load config
    batch_config = load_batch_config(args.config)

    # Generate tasks
    tasks = generate_test_tasks(batch_config)

    print("=" * 60)
    print("Multi-VM Agent Batch Test Scheduler")
    print("=" * 60)
    print(f"Total tasks: {len(tasks)}")
    print(f"VM counts: {batch_config['test_matrix']['vm_counts']}")
    print(f"Ratios: {batch_config['test_matrix']['ratios']}")
    print(f"Active percentages: {batch_config['test_matrix']['active_percentages']}")
    print("=" * 60)

    # Print task list
    print("\nTask list:")
    for i, task in enumerate(tasks):
        print(f"  [{i+1}] {task.task_id}")

    if args.dry_run:
        print("\n[Dry run mode] Tasks listed but not executed")
        return

    # Create directories
    base_dir = batch_config["result"]["base_dir"]
    temp_config_dir = os.path.join(base_dir, "temp_configs")
    os.makedirs(temp_config_dir, exist_ok=True)

    # Batch log file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_log_file = os.path.join(base_dir, f"batch_log_{timestamp}.txt")

    with open(batch_log_file, "w") as f:
        f.write("=" * 60 + "\n")
        f.write("Multi-VM Agent Batch Test Log\n")
        f.write(f"Started: {timestamp}\n")
        f.write(f"Total tasks: {len(tasks)}\n")
        f.write("=" * 60 + "\n")

    print(f"\nBatch log: {batch_log_file}")

    # Template path
    template_path = batch_config["result"]["template_path"]
    fixed_params = batch_config["fixed_params"]
    scheduler_config = batch_config["scheduler"]

    # Execute tests
    start_time = time.time()

    for i, task in enumerate(tasks):
        print(f"\n{'=' * 60}")
        print(f"Task [{i+1}/{len(tasks)}]: {task.task_id}")
        print(f"{'=' * 60}")

        # Generate temp config
        task.config_file = generate_temp_config(
            template_path, task, fixed_params, base_dir, temp_config_dir
        )

        # Run test
        success, result_dir = run_single_test(task, task.config_file, batch_log_file)

        task.success = success
        task.result_dir = result_dir

        # Move config to result directory
        if result_dir:
            move_config_to_result(task.config_file, result_dir, task.task_id)
        else:
            # Keep failed config for debugging in temp_configs
            pass

        # Handle failure
        if not success and not scheduler_config["continue_on_failure"]:
            print(f"\nTest failed, stopping batch execution")
            break

    elapsed = time.time() - start_time

    # Collect results
    print("\n" + "=" * 60)
    print("Collecting results...")
    print("=" * 60)

    results = collect_all_results(tasks, base_dir)

    # Generate summary report
    summary_path = os.path.join(base_dir, f"batch_summary_{timestamp}.xlsx")
    generate_summary_report(results, summary_path)

    # Final summary
    with open(batch_log_file, "a") as f:
        f.write("\n" + "=" * 60 + "\n")
        f.write("Batch Test Completed\n")
        f.write(f"Total time: {elapsed:.1f} seconds\n")
        f.write(f"Successful: {results['successful_tests']}\n")
        f.write(f"Failed: {results['failed_tests']}\n")
        f.write("=" * 60 + "\n")

    print("\n" + "=" * 60)
    print("Batch Test Summary")
    print("=" * 60)
    print(f"Total time: {elapsed:.1f} seconds")
    print(f"Successful tests: {results['successful_tests']}/{len(tasks)}")
    print(f"Failed tests: {results['failed_tests']}/{len(tasks)}")
    print(f"Batch log: {batch_log_file}")
    print(f"Summary report: {summary_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()