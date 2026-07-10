# qemu_monitor/cli.py
"""
Command Line Interface Entry Point

Main entry point for QEMU monitor tool. Handles argparse parsing,
initialization of monitor and log capture, and coordinates execution.
"""

import argparse
import os
import sys
import time
from datetime import datetime

# Internal dependencies - all modules
from .config import load_env_config, validate_and_prompt_missing
from .exporters import export_to_excel, print_capture_summary
from .log_capture import LogCapture
from .monitor import QEMUMonitor

# Try to import pandas for Excel availability check
try:
    import pandas as pd

    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


def main():
    """Main entry point for QEMU monitoring tool"""
    parser = argparse.ArgumentParser(
        description="QEMU Monitoring Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
[Mode 1: Stress Sync Monitoring]
  sudo python3 qemu_monitor.py --stress-file /tmp/bench_running.lock
    -> Wait for lock file to appear then start monitoring

[Mode 2: Timer Monitoring]
  sudo python3 qemu_monitor.py -t 60 -i 2
    -> Monitor for 60 seconds

[Mode 3: With Log Collection]
  sudo python3 qemu_monitor.py -t 60 -i 2 --enable-capture
    -> Monitor for 60 seconds with parallel log collection
        """,
    )
    sync = parser.add_mutually_exclusive_group()
    sync.add_argument("--stress-process", type=str, help="Stress process name")
    sync.add_argument("--stress-file", type=str, help="Stress marker file (e.g., /tmp/bench_running.lock)")
    parser.add_argument("-t", "--time", type=int, default=60, help="Timer duration seconds (default 60)")
    parser.add_argument("-i", "--interval", type=int, default=3, help="Sampling interval (default 3 seconds)")
    parser.add_argument("-o", "--output", type=str, help="Output prefix")
    parser.add_argument("--numa", type=str, default="1", help="Specify NUMA nodes to monitor, comma-separated 0,1")
    parser.add_argument("--log-dir", type=str, help="Log output directory (default: logs_${timestamp}/ in current dir)")
    parser.add_argument(
        "--enable-capture", action="store_true", help="Enable parallel log collection with devkit/ksys/ub_watch/smap_bw"
    )
    parser.add_argument(
        "--auto-skip", action="store_true", help="Auto-skip missing log capture tools (for automated testing)"
    )
    parser.add_argument(
        "--ksys-parse-timeout",
        type=int,
        default=600,
        help="Timeout for ksys data parsing phase in seconds (default: 600s, increase for large VM counts)",
    )
    args = parser.parse_args()

    # Check root permission
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("[WARN] Recommended to run as root, otherwise some processes cannot be read")
        time.sleep(1)

    # Setup log directory
    log_dir = args.log_dir or f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(log_dir, exist_ok=True)
    print(f"[OK] Log directory: {log_dir}")

    # Load .env config if capture enabled
    capture = None
    config = None
    if args.enable_capture:
        print("\nLoading log collection configuration...")
        config = load_env_config()
        config = validate_and_prompt_missing(config, non_interactive=args.auto_skip)

    # Create QEMUMonitor instance
    m = QEMUMonitor()
    try:
        m.target_numa_nodes = list(map(int, args.numa.split(",")))
    except:
        m.target_numa_nodes = [0]

    # Start log capture (parallel with monitor)
    if args.enable_capture:
        print("\nStarting log collection tools...")
        capture = LogCapture(
            config, args.time, log_dir, m.target_numa_nodes, ksys_parse_timeout=args.ksys_parse_timeout
        )
        capture.start()
        print(f"[OK] Log collection tools started in background (duration={args.time}s)")
        print(f"  ksys parse timeout: {args.ksys_parse_timeout}s")
        sys.stdout.flush()

    # Start QEMU monitoring
    if args.stress_process:
        m.wait_for_stress_and_monitor("process", args.stress_process, args.interval, args.time)
    elif args.stress_file:
        m.wait_for_stress_and_monitor("file", args.stress_file, args.interval, args.time)
    else:
        m.start_monitoring(args.time, args.interval)

    # Wait for capture to finish
    if capture:
        print("\nWaiting for log collection tools to finish...")
        capture.wait()
        print("[OK] Log collection complete")

    # Export results to log_dir
    raw = os.path.join(log_dir, f"{args.output}.csv" if args.output else "qemu_monitor.csv")
    sumf = os.path.join(log_dir, f"summary_{args.output}.csv" if args.output else "summary.csv")
    m.analyze_and_export(raw, sumf)

    # Print capture summary
    capture_results = None
    if capture:
        capture_results = capture.get_results()
        print_capture_summary(capture_results, log_dir, m.target_numa_nodes)

    # Export to Excel (if pandas available)
    if PANDAS_AVAILABLE:
        excel_file = os.path.join(log_dir, "analysis_report.xlsx")
        export_to_excel(m, log_dir, m.target_numa_nodes, excel_file, capture_results)

    print(f"\nComplete! All outputs saved to: {log_dir}/")
