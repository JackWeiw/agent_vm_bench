"""A/B harness: per-cycle CPU of the real vm_monitor loop at fleet scale.

Runs against whichever checkout is given by --repo (so the pre-change and
post-change trees are measured by the same code path in separate processes).

    python3 ab.py --repo /home/w00943531/workspace/avbperf     --shape heavy --n 192 --mem-mb 1024 --throttle off
    python3 ab.py --repo /home/w00943531/workspace/avbperf-new --shape heavy --n 192 --mem-mb 1024 --throttle on

CPU is time.process_time() of this process (user+sys of every thread), so a
co-loaded host does not distort it; wall time is context only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

BIN_DIR = "/var/tmp/vmperf/bin"
FAKE_BIN = os.path.join(BIN_DIR, "containerd-shim-cube-rs")


def ensure_fake_bin(mem_mb: int) -> None:
    os.makedirs(BIN_DIR, exist_ok=True)
    if mem_mb <= 0:
        shutil.copy("/bin/sleep", FAKE_BIN)
        return
    src = "/var/tmp/vmperf/vmsim.c"
    with open(src, "w") as f:
        f.write(
            """
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
int main(int argc, char **argv) {
    long mb = argc > 1 ? atol(argv[1]) : 64;
    long secs = argc > 2 ? atol(argv[2]) : 3600;
    size_t sz = (size_t)mb * 1024 * 1024;
    /* mmap, not malloc: the mapping must be 2 MiB-aligned for VMSIM_THP to
       collapse (a misaligned malloc'd mapping barely collapses at all). */
    char *p = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) return 2;
    /* VMSIM_THP: back the mapping with 2 MiB pages, as a real guest RAM
       mapping is under THP=always -- the realistic numa_maps cost. Faults
       every 4 KiB page either way; RSS then reflects the whole mapping. */
    if (getenv("VMSIM_THP")) madvise(p, sz, MADV_HUGEPAGE);
    for (size_t i = 0; i < sz; i += 4096) p[i] = (char)(i & 0xff);
    fflush(stdout);
    sleep((unsigned)secs);
    return 0;
}
"""
        )
    subprocess.run(["gcc", "-O2", "-o", FAKE_BIN, src], check=True)
    os.chmod(FAKE_BIN, 0o755)


def spawn(n: int, mem_mb: int, thp: bool = False) -> list[subprocess.Popen]:
    args_base = [FAKE_BIN, "3600"] if mem_mb <= 0 else [FAKE_BIN, str(mem_mb), "3600"]
    env = dict(os.environ, VMSIM_THP="1") if thp else None
    return [
        subprocess.Popen(args_base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env) for _ in range(n)
    ]


def rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def wait_faulted(procs, mem_mb: int, timeout: float = 180.0) -> None:
    if mem_mb <= 0:
        time.sleep(2.0)
        return
    target = mem_mb * 0.9
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(rss_mb(p.pid) >= target for p in procs):
            return
        time.sleep(0.5)
    raise SystemExit("fake VMs did not fault in their memory within timeout")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--shape", default="light")
    ap.add_argument("--n", type=int, default=384)
    ap.add_argument("--mem-mb", type=int, default=1024)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--throttle", choices=("on", "off"), default="on")
    ap.add_argument("--thp", action="store_true", help="back the fake VMs with 2 MiB pages")
    ap.add_argument("--log-dir", default="/var/tmp/vmperf/ab")
    args = ap.parse_args()

    sys.path.insert(0, args.repo)
    from vm_monitor.cubesandbox import CubeSandboxMonitor  # noqa: E402

    mem = args.mem_mb if args.shape == "heavy" else 0
    ensure_fake_bin(mem)
    procs = spawn(args.n, mem, args.thp)
    try:
        wait_faulted(procs, mem)
        mon = CubeSandboxMonitor()
        mon.log_dir = args.log_dir
        shutil.rmtree(args.log_dir, ignore_errors=True)
        # The pre-change tree has no heavy-refresh scheduler: it refreshes
        # every VM every cycle, which is what --throttle off emulates on the
        # post-change tree. Guard so one harness serves both trees.
        scheduler = getattr(mon, "heavy_refresh", None)
        if args.throttle == "off" and scheduler is not None:
            scheduler.cpu_fraction = 0
        mon.interval = args.interval

        # Uneven process start: VMs created in waves get different heavy costs.
        mon.collect_sample()  # cold cycle: seeds jiffies, measures every VM
        time.sleep(args.interval)

        # Separate discovery cost (one clean call)
        d0 = time.process_time()
        candidates = mon._discover_vm_processes()
        discovery_ms = (time.process_time() - d0) * 1000

        samples = []
        for _ in range(args.samples):
            c0, w0 = time.process_time(), time.perf_counter()
            mon.collect_sample()
            samples.append(
                {
                    "cpu": time.process_time() - c0,
                    "wall": time.perf_counter() - w0,
                }
            )
            time.sleep(args.interval)

        cpu = sum(s["cpu"] for s in samples) / len(samples)
        wall = sum(s["wall"] for s in samples) / len(samples)
        result = {
            "repo": args.repo,
            "shape": args.shape,
            "n": args.n,
            "mem_mb": mem,
            "thp": args.thp,
            "interval": args.interval,
            "throttle": args.throttle,
            "n_vms": len(candidates),
            "discovery_ms": round(discovery_ms, 1),
            "cpu_ms_per_sample": round(cpu * 1000, 1),
            "wall_ms_per_sample": round(wall * 1000, 1),
            "sustained_cores": round(cpu / args.interval, 2),
            "heavy_stride": scheduler.stride if scheduler is not None else None,
            "heavy_cost_per_vm_ms": round(scheduler.cost_per_vm * 1000, 2) if scheduler is not None else None,
        }
        print(json.dumps(result))
    finally:
        for p in procs:
            p.send_signal(signal.SIGKILL)
        for p in procs:
            p.wait()


if __name__ == "__main__":
    raise SystemExit(main())
