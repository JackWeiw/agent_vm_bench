"""Dump per-VM memory metrics from one checkout, for old-vs-new diffing.

    python3 dump_vm_metrics.py --repo /home/w00943531/workspace/avbperf     --out /tmp/old.json
    python3 dump_vm_metrics.py --repo /home/w00943531/workspace/avbperf-new --out /tmp/new.json

Spawns a deterministic fake fleet (light shims + 192 MiB shims so numa_maps
has real per-node content), waits until the pages are faulted in, then dumps
the numa_maps parser output, PSS and the full per-VM record for every pid.

With --pids-file it reads an already-running fleet instead (see spawn_fleet.py):
both checkouts must be dumped against the *same* pids, or pid/NUMA-placement
drift between runs shows up as phantom parser mismatches.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, "/home/w00943531/workspace/perf")
import ab  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--light", type=int, default=6)
    ap.add_argument("--heavy", type=int, default=6)
    ap.add_argument("--mem-mb", type=int, default=192)
    ap.add_argument("--pids-file", default=None, help="dump this already-running fleet instead of spawning one")
    args = ap.parse_args()

    sys.path.insert(0, args.repo)
    from vm_monitor.vmm.cubesandbox import CubeSandboxMonitor  # noqa: E402

    heavy_procs: list = []
    light_procs: list = []
    if args.pids_file is None:
        ab.ensure_fake_bin(args.mem_mb)
        heavy_procs = [ab.subprocess.Popen([ab.FAKE_BIN, str(args.mem_mb), "600"]) for _ in range(args.heavy)]
        light_procs = [ab.subprocess.Popen([ab.FAKE_BIN, "600"]) for _ in range(args.light)]
        ab.wait_faulted(heavy_procs, args.mem_mb)
    try:
        if args.pids_file is not None:
            with open(args.pids_file, encoding="utf-8") as f:
                live = set(json.load(f))
        time.sleep(2.0)
        mon = CubeSandboxMonitor()
        mon.log_dir = "/var/tmp/vmperf/dump"
        candidates = [c for c in mon._discover_vm_processes() if args.pids_file is None or c["pid"] in live]
        dump = {"candidates": candidates, "vms": [], "racy": []}

        for candidate in candidates:
            pid = candidate["pid"]
            dump["vms"].append(
                {
                    "pid": pid,
                    "numa_maps": mon.get_vm_memory_from_numa_maps(pid),
                    "pss_mb": mon._read_pss_mb(pid),
                    "jiffies": mon._read_pid_jiffies(pid),
                }
            )
        # Full record path for the same fleet (heavy fields + status/name)
        dump["records"] = mon._collect_vm_metrics_parallel(candidates)

        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=1, sort_keys=True, default=str)
        print(f"wrote {args.out}: {len(candidates)} candidates")
    finally:
        for p in heavy_procs + light_procs:
            p.kill()
        for p in heavy_procs + light_procs:
            p.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
