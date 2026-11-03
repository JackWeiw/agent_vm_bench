"""Runs one vm_monitor checkout's sampling loop as its own process.

    python3 monitor_proc.py --repo /path/to/checkout --interval 2 [--throttle on|off]

Exits on SIGTERM/SIGINT. Kept in its own process so the perturbation
experiment can attribute CPU to the monitor exactly as the benchmark does
(vm_monitor runs as a separate process next to the workload).
"""
from __future__ import annotations

import argparse
import signal
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--log-dir", default="/var/tmp/vmperf/perturb_mon")
    ap.add_argument("--throttle", choices=("on", "off"), default="on")
    args = ap.parse_args()

    sys.path.insert(0, args.repo)
    from vm_monitor.vmm.cubesandbox import CubeSandboxMonitor

    mon = CubeSandboxMonitor()
    mon.interval = args.interval
    mon.log_dir = args.log_dir
    if args.throttle == "off":
        mon.heavy_refresh.cpu_fraction = 0  # stride 1: refresh every VM every sample

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    while not stop:
        started = time.perf_counter()
        mon.collect_sample()
        time.sleep(max(0.0, args.interval - (time.perf_counter() - started)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
