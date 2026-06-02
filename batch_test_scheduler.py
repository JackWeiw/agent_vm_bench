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
        "{{ACTIVE_PERCENT}}": str(task.active_percent)
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


def move_config_to_result(config_file: str, result_dir: str):
    """Move temp config file to result directory"""
    if not result_dir or not os.path.exists(result_dir):
        return

    dest = os.path.join(result_dir, "config.yaml")
    if os.path.exists(dest):
        # Already has config copy from auto_vm_test.py
        os.remove(config_file)
    else:
        shutil.move(config_file, dest)


def collect_all_results(tasks: List[TestTask], base_dir: str) -> Dict[str, Any]:
    """Collect results from all test runs"""
    all_metrics = []

    for task in tasks:
        if not task.result_dir:
            continue

        metrics_path = os.path.join(task.result_dir, "summary", "metrics_summary.json")

        if not os.path.exists(metrics_path):
            continue

        with open(metrics_path) as f:
            metrics = json.load(f)

        # Add task info
        metrics["task_id"] = task.task_id
        metrics["success"] = task.success

        all_metrics.append(metrics)

    return {
        "tasks": all_metrics,
        "total_tests": len(tasks),
        "successful_tests": sum(1 for t in tasks if t.success),
        "failed_tests": sum(1 for t in tasks if not t.success)
    }


def generate_summary_report(results: Dict, output_path: str):
    """Generate Excel summary report"""
    try:
        import pandas as pd

        # Build DataFrame
        rows = []
        for task_data in results["tasks"]:
            params = task_data.get("parameters", {})
            browser = task_data.get("browser_metrics", {})
            qemu = task_data.get("qemu_metrics", {})
            perf = task_data.get("performance_metrics", {})

            row = {
                "test_id": task_data.get("task_id", ""),
                "vm_count": params.get("vm_count", 0),
                "ratio": params.get("ratio", 0),
                "active_percent": params.get("active_percent", 0),
                "active_vm_count": int(params.get("vm_count", 0) * params.get("active_percent", 0)),
                "success": task_data.get("success", False),
                "success_rate": browser.get("success_rate", 0),
                "avg_latency": browser.get("avg_latency", 0),
                "p99_latency": browser.get("p99_latency", 0),
                "avg_cpu": qemu.get("avg_cpu_percent", 0),
                "max_cpu": qemu.get("max_cpu_percent", 0),
                "ipc": perf.get("ipc_avg", 0),
                "ddr_read": perf.get("ddr_bandwidth_read_avg", 0),
                "ddr_write": perf.get("ddr_bandwidth_write_avg", 0)
            }
            rows.append(row)

        df = pd.DataFrame(rows)

        # Save to Excel
        df.to_excel(output_path, index=False, sheet_name="Summary")

        print(f"\nSummary report saved to: {output_path}")

    except ImportError:
        print("WARNING: pandas not available, saving as JSON instead")
        json_path = output_path.replace(".xlsx", ".json")
        with open(json_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Summary saved to: {json_path}")


def main():
    parser = argparse.ArgumentParser(description="Multi-VM Agent Batch Test Scheduler")
    parser.add_argument("--config", default="batch_config.yaml", help="Batch config YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Print tasks without executing")

    args = parser.parse_args()

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
        if success and result_dir:
            move_config_to_result(task.config_file, result_dir)
        else:
            # Keep failed config for debugging
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