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

# Import shared metrics definitions
from metrics import EXCEL_METRIC_KEY_MAP, BROWSER_METRIC_KEY_MAP, DISPLAY_NAME_MAP


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


def generate_temp_config(template_path: str, task: TestTask, fixed_params: Dict, output_dir: str) -> str:
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
        "{{DURATION}}": str(fixed_params["duration"])
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
    """Extract browser metrics from vm_bench_lite bench_report file

    Uses shared BROWSER_METRIC_KEY_MAP from metrics_definitions.py
    """
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

    # Parse metrics using regex with shared mapping
    metrics = {}
    for source_name, internal_key in BROWSER_METRIC_KEY_MAP.items():
        # Success Rate: X.X%
        if source_name == "Success Rate":
            match = re.search(r"Success Rate:\s+([\d.]+)%", content)
            if match:
                metrics[internal_key] = float(match.group(1))
        # Avg Latency: X.Xms
        elif source_name == "Avg Latency":
            match = re.search(r"Avg Latency:\s+([\d.]+)ms", content)
            if match:
                metrics[internal_key] = float(match.group(1))
        # P99 Latency: X.Xms
        elif source_name == "P99 Latency":
            match = re.search(r"P99 Latency:\s+([\d.]+)ms", content)
            if match:
                metrics[internal_key] = float(match.group(1))
        # Total Tasks
        elif source_name == "Total Tasks":
            match = re.search(r"Total Tasks:\s+(\d+)", content)
            if match:
                metrics[internal_key] = int(match.group(1))

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

        # Process each sheet using shared EXCEL_METRIC_KEY_MAP
        for sheet_name, sheet_key_map in EXCEL_METRIC_KEY_MAP.items():
            if sheet_name in ["-", "bench_report", "config.yaml", "NUMA_Bandwidth"]:
                continue  # Skip non-Excel sources and special handling sheets

            try:
                df = pd.read_excel(excel_path, sheet_name=sheet_name)
                for idx, row in df.iterrows():
                    metric = str(row["Metric"]).strip() if pd.notna(row.get("Metric", "")) else ""
                    value = row.get("Value")
                    if metric in sheet_key_map and pd.notna(value):
                        try:
                            metrics[sheet_key_map[metric]] = float(value)
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass

        # Special handling for NUMA_Bandwidth (different table structure)
        try:
            df_numa = pd.read_excel(excel_path, sheet_name="NUMA_Bandwidth")
            total_read = 0.0
            total_write = 0.0
            for idx, row in df_numa.iterrows():
                node = str(row["NUMA Node"]).strip() if pd.notna(row.get("NUMA Node", "")) else ""
                read = float(row["Read (MB/s)"]) if pd.notna(row.get("Read (MB/s)")) else 0
                write = float(row["Write (MB/s)"]) if pd.notna(row.get("Write (MB/s)")) else 0
                if node:
                    metrics[f"numa{node}_read"] = read
                    metrics[f"numa{node}_write"] = write
                    total_read += read
                    total_write += write
            metrics["numa_total_read"] = total_read
            metrics["numa_total_write"] = total_write
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
                        if metric in key_map and row[1]:
                            try:
                                result[key_map[metric]] = float(row[1])
                            except (ValueError, TypeError):
                                pass
                return result

            # Process each sheet using shared EXCEL_METRIC_KEY_MAP
            for sheet_name, sheet_key_map in EXCEL_METRIC_KEY_MAP.items():
                if sheet_name in ["-", "bench_report", "config.yaml", "NUMA_Bandwidth"]:
                    continue
                if sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    metrics.update(extract_sheet_metrics(ws, sheet_key_map))

            # Special handling for NUMA_Bandwidth
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
    """Generate Excel summary report with ALL metrics from all sheets

    Uses shared metrics definitions from metrics_definitions.py
    """
    try:
        import pandas as pd

        # Build DataFrame with all metrics
        rows = []
        for task_data in results["tasks"]:
            params = task_data.get("parameters", {})
            browser = task_data.get("browser_metrics", {})
            qemu = task_data.get("qemu_metrics", {})

            # Build row dynamically using metrics definitions
            row = {}

            # Basic info (from task_data and params)
            row["test_id"] = task_data.get("task_id", "")
            row["vm_count"] = params.get("vm_count", 0)
            row["ratio"] = params.get("ratio", 0)
            row["active_percent"] = params.get("active_percent", 0)
            row["active_vm_count"] = int(params.get("vm_count", 0) * params.get("active_percent", 0))
            row["success"] = task_data.get("success", False)

            # Browser metrics (use internal keys from metrics_definitions)
            for key in ["browser_success_rate", "browser_avg_latency_ms", "browser_p99_latency_ms", "browser_total_tasks"]:
                # Map from old keys to new keys if needed
                old_key_map = {
                    "browser_success_rate": "browser_success_rate",
                    "browser_avg_latency_ms": "browser_avg_latency_ms",
                    "browser_p99_latency_ms": "browser_p99_latency_ms",
                    "browser_total_tasks": "browser_total_tasks",
                }
                row[key] = browser.get(key, browser.get(old_key_map.get(key, key), 0))

            # All QEMU/Excel metrics - just copy them directly
            for key, value in qemu.items():
                row[key] = value

            rows.append(row)

        df = pd.DataFrame(rows)

        # Rename columns to display names using DISPLAY_NAME_MAP
        rename_map = {}
        for col in df.columns:
            if col in DISPLAY_NAME_MAP:
                rename_map[col] = DISPLAY_NAME_MAP[col]
            # Handle dynamic NUMA columns
            elif col.startswith("numa") and "_read" in col:
                node = col.replace("numa", "").replace("_read", "")
                rename_map[col] = f"NUMA{node} Read (MB/s)"
            elif col.startswith("numa") and "_write" in col:
                node = col.replace("numa", "").replace("_write", "")
                rename_map[col] = f"NUMA{node} Write (MB/s)"

        df = df.rename(columns=rename_map)

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
            template_path, task, fixed_params, temp_config_dir
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