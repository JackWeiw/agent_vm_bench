#!/usr/bin/env python3
"""
Offline Fix Script: Add L3 Hit Rate to existing analysis_report.xlsx

Usage:
    # Fix single result directory
    python fix_l3_hit_rate.py --result-dir results/test01

    # Fix all result directories in a batch
    python fix_l3_hit_rate.py --batch-dir results

    # Specify NUMA nodes
    python fix_l3_hit_rate.py --batch-dir results --numa 0,1
"""

import argparse
import os
import sys

from vm_monitor.exporters import add_l3_hit_rate_to_excel


def fix_single_result(result_dir: str, numa_nodes: list = None):
    """Fix single result directory"""
    print(f"\nProcessing: {result_dir}")
    success = add_l3_hit_rate_to_excel(result_dir, numa_nodes)
    return success


def fix_batch_results(batch_dir: str, numa_nodes: list = None):
    """Fix all result directories in batch"""
    print(f"\nScanning: {batch_dir}")

    # Find all subdirectories that look like test results
    result_dirs = []
    for entry in os.listdir(batch_dir):
        entry_path = os.path.join(batch_dir, entry)
        if os.path.isdir(entry_path):
            # Check if it has vm_monitor or qemu_monitor subdirectory
            if os.path.exists(os.path.join(entry_path, "vm_monitor")) or os.path.exists(
                os.path.join(entry_path, "qemu_monitor")
            ):
                result_dirs.append(entry_path)

    if not result_dirs:
        print(f"[WARN] No valid result directories found in {batch_dir}")
        return

    print(f"Found {len(result_dirs)} result directories")

    success_count = 0
    fail_count = 0

    for result_dir in sorted(result_dirs):
        if fix_single_result(result_dir, numa_nodes):
            success_count += 1
        else:
            fail_count += 1

    print(f"\n{'=' * 60}")
    print(f"Summary: {success_count} success, {fail_count} failed")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Add L3 Hit Rate to existing Excel reports")
    parser.add_argument("--result-dir", help="Single result directory to fix")
    parser.add_argument("--batch-dir", help="Batch directory containing multiple result dirs")
    parser.add_argument("--numa", help="NUMA nodes to filter (e.g., 0,1)", default=None)

    args = parser.parse_args()

    numa_nodes = None
    if args.numa:
        numa_nodes = [int(x) for x in args.numa.split(",")]

    if args.result_dir:
        fix_single_result(args.result_dir, numa_nodes)
    elif args.batch_dir:
        fix_batch_results(args.batch_dir, numa_nodes)
    else:
        print("Please specify --result-dir or --batch-dir")
        sys.exit(1)


if __name__ == "__main__":
    main()
