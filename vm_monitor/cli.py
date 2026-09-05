# vm_monitor/cli.py
"""
Command Line Interface Entry Point

Main entry point for VM monitor tool. Handles argparse parsing,
initialization of monitor and log capture, and coordinates execution.
Supports multiple VMM types: QEMU, Firecracker.
"""

import argparse
import os
import sys
import time
from datetime import datetime

from .base import _discover_block_devices

# Internal dependencies - all modules
from .config import load_env_config, validate_and_prompt_missing
from .exporters import export_to_excel, print_capture_summary
from .firecracker import FirecrackerMonitor
from .log_capture import LogCapture
from .qemu import QEMUMonitor
from .svg_exporter import export_svg_reports

# Try to import pandas for Excel availability check
try:
    import pandas as pd

    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


def resolve_numa_nodes(numa_arg: str, available_nodes: list[int]) -> list[int]:
    """Resolve a --numa argument to a concrete list of NUMA node IDs.

    "all" -> every node the host exposes (``available_nodes``); falls back to
    [0] when the host is non-NUMA (no /sys/devices/system/node nodeN dirs) so a
    default "all" never yields an empty focus set. A comma-separated int list
    ("0,1") is parsed verbatim; an unparseable value falls back to [0].
    """
    if numa_arg.strip().lower() == "all":
        return list(available_nodes) if available_nodes else [0]
    try:
        return list(map(int, numa_arg.split(",")))
    except ValueError:
        return [0]


def main():
    """Main entry point for VM monitoring tool"""
    parser = argparse.ArgumentParser(
        description="VM Monitoring Tool (supports QEMU and Firecracker)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
[Mode 1: Stress Sync Monitoring]
  sudo python3 vm_monitor.py --stress-file /tmp/bench_running.lock --vmm qemu
    -> Wait for lock file to appear then start monitoring

[Mode 2: Timer Monitoring]
  sudo python3 vm_monitor.py -t 60 -i 2 --vmm qemu
    -> Monitor for 60 seconds

[Mode 3: With Log Collection]
  sudo python3 vm_monitor.py -t 60 -i 2 --enable-capture --vmm qemu
    -> Monitor for 60 seconds with parallel log collection

[VMM Types]
  --vmm qemu         Monitor QEMU VMs (qemu-kvm, qemu-system)
  --vmm firecracker  Monitor Firecracker microVMs
        """,
    )

    # VMM type selection
    parser.add_argument(
        "--vmm", type=str, choices=["qemu", "firecracker"], default="qemu", help="VMM type to monitor (default: qemu)"
    )

    # Stress sync modes
    sync = parser.add_mutually_exclusive_group()
    sync.add_argument("--stress-process", type=str, help="Stress process name")
    sync.add_argument("--stress-file", type=str, help="Stress marker file (e.g., /tmp/bench_running.lock)")

    # Timing parameters
    parser.add_argument("-t", "--time", type=int, default=60, help="Timer duration seconds (default 60)")
    parser.add_argument("-i", "--interval", type=int, default=2, help="Sampling interval (default 2 seconds)")

    # Output parameters
    parser.add_argument("-o", "--output", type=str, help="Output prefix")
    parser.add_argument(
        "--numa",
        type=str,
        default="all",
        help="NUMA nodes to monitor: 'all' (default) = every node, or comma-separated 0,1",
    )
    parser.add_argument(
        "--remote-numa",
        type=int,
        default=5,
        help="Designated 'remote borrowing' NUMA node (the node this platform uses to "
        "borrow memory from a remote socket; NUMA5 on the reference 4-socket box). "
        "Added to the focus set for free-memory monitoring. Use a negative value to "
        "disable injecting any remote node (default 5).",
    )
    parser.add_argument(
        "--disks",
        type=str,
        default="all",
        help="Block devices to monitor for I/O, comma-separated (e.g. sda,nvme0n1). "
        "Use 'all' (default) to auto-discover every physical block device on the host; "
        "virtual/software layers (loop, ram, sr, zram, md, dm) are excluded.",
    )
    parser.add_argument("--log-dir", type=str, help="Log output directory (default: logs_${timestamp}/ in current dir)")

    # SVG time-curve export
    parser.add_argument(
        "--no-svg",
        action="store_true",
        help="Skip writing dark-themed SVG time-curve reports (disk_io / host_resources / swap / numa / vm_total .svg)",
    )

    # Log capture options
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

    # Create appropriate Monitor instance based on --vmm argument
    if args.vmm == "qemu":
        m = QEMUMonitor()
        csv_prefix = "qemu_monitor"
    elif args.vmm == "firecracker":
        m = FirecrackerMonitor()
        csv_prefix = "firecracker_monitor"
    else:
        print(f"[ERROR] Unknown VMM type: {args.vmm}")
        sys.exit(1)

    m.target_numa_nodes = resolve_numa_nodes(args.numa, m.available_numa_nodes)

    # Override the platform-default remote borrowing node; a negative value
    # disables injecting any remote node.
    m.remote_numa_id = args.remote_numa if args.remote_numa >= 0 else None

    if args.disks.strip().lower() == "all":
        m.target_disks = _discover_block_devices()
    else:
        m.target_disks = [d.strip() for d in args.disks.split(",") if d.strip()]

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

    # Start VM monitoring
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
    raw = os.path.join(log_dir, f"{args.output}.csv" if args.output else f"{csv_prefix}.csv")
    sumf = os.path.join(log_dir, f"summary_{args.output}.csv" if args.output else "summary.csv")
    m.analyze_and_export(raw, sumf)

    # Print capture summary
    capture_results = None
    if capture:
        capture_results = capture.get_results()
        print_capture_summary(capture_results, log_dir, m.target_numa_nodes)

    # Export dark-themed SVG time-curve reports FIRST. The xlsx report must be
    # the LAST artifact written: orchestrators (bench-core MonitorController)
    # poll for resource_report.xlsx as the "all artifacts written" signal and
    # reap the subprocess the moment it appears. If SVG ran after xlsx, that
    # reap would drop every SVG file.
    if not args.no_svg:
        svg_files = export_svg_reports(m, log_dir)
        if svg_files:
            print(f"[OK] SVG time-curve reports: {', '.join(os.path.basename(p) for p in svg_files)}")

    # Export to Excel (if pandas available) -- LAST so its appearance signals
    # that CSV + SVG + xlsx are all written and the subprocess is essentially
    # done (safe to reap).
    if PANDAS_AVAILABLE:
        excel_file = os.path.join(log_dir, "resource_report.xlsx")
        export_to_excel(m, log_dir, m.target_numa_nodes, excel_file, capture_results)

    print(f"\nComplete! All outputs saved to: {log_dir}/")
