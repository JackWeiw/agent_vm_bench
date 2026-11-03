"""Measure how much vm_monitor perturbs the workload it observes.

Each fake VM is a page-fault churn loop (touch every page, MADV_DONTNEED,
repeat) -- the same mmap work a sandbox does, and the work the monitor's
numa_maps / smaps_rollup walks contend with via mmap_lock. The same fleet is
run with no monitor, with the pre-change monitor, and with the new monitor
with its CPU budget on and off; rounds completed, victim CPU per round and
worst-case round latency are the interference metrics. Arms are interleaved
round-robin, because this host's co-tenancy drifts by tens of percent between
back-to-back arms -- a sequential sweep measures the neighbours' load.

    python3 perturb.py --arms baseline,old,new-on,new-off --rounds 3 \
        --old-repo /home/w00943531/workspace/avbperf \
        --new-repo /home/w00943531/workspace/avbperf-new
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = "/var/tmp/vmperf/churn"
CHURN_BIN = os.path.join(BIN_DIR, "containerd-shim-cube-rs")
CLK_TCK = os.sysconf("SC_CLK_TCK")

CHURN_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    long mb = argc > 1 ? atol(argv[1]) : 32;
    double secs = argc > 2 ? atof(argv[2]) : 60;
    size_t sz = (size_t)mb << 20;
    char *p = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) return 2;
    /* Pin 4 KiB pages: with THP=always the mapping randomly collapses into
       2 MiB pages (khugepaged), which changes the per-round cost ~5x and
       dominates any interference signal between arms. */
    madvise(p, sz, MADV_NOHUGEPAGE);
    double end = now() + secs, worst = 0.0;
    unsigned long rounds = 0;
    while (now() < end) {
        double t0 = now();
        for (size_t i = 0; i < sz; i += 4096) p[i] = (char)(i + rounds);
        double d = now() - t0;
        if (d > worst) worst = d;
        madvise(p, sz, MADV_DONTNEED);   /* drop the PTEs: next round re-faults */
        rounds++;
    }
    /* own CPU seconds: the victim is reaped before the parent could read
       /proc/<pid>/stat, so it reports its own clock() instead. */
    printf("%lu %.3f %.6f\n", rounds, worst * 1000.0, (double)clock() / CLOCKS_PER_SEC);
    fflush(stdout);
    return 0;
}
"""


def ensure_churn_bin() -> None:
    os.makedirs(BIN_DIR, exist_ok=True)
    src = "/var/tmp/vmperf/churn.c"
    with open(src, "w") as f:
        f.write(CHURN_C)
    subprocess.run(["gcc", "-O2", "-o", CHURN_BIN, src], check=True)
    os.chmod(CHURN_BIN, 0o755)


def proc_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return int(rest[11]) + int(rest[12])
    except (OSError, IndexError, ValueError):
        return None


def run_arm(name, args, repo=None, throttle="on"):
    victims = [
        subprocess.Popen([CHURN_BIN, str(args.mem_mb), str(args.seconds)], stdout=subprocess.PIPE, text=True)
        for _ in range(args.n)
    ]
    time.sleep(args.warm)
    mon = None
    if repo is not None:
        mon = subprocess.Popen(
            [
                sys.executable,
                os.path.join(HERE, "monitor_proc.py"),
                "--repo",
                repo,
                "--interval",
                str(args.interval),
                "--throttle",
                throttle,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    t0 = time.time()
    mon_ticks0 = proc_ticks(mon.pid) if mon else None

    for p in victims:
        p.wait()
    wall = time.time() - t0
    mon_ticks1 = proc_ticks(mon.pid) if mon else None
    if mon:
        mon.send_signal(signal.SIGTERM)
        mon.wait()

    rounds = 0
    worst = victim_cpu = 0.0
    for p in victims:
        out = p.stdout.read().split()
        p.stdout.close()
        if len(out) == 3:
            rounds += int(out[0])
            worst = max(worst, float(out[1]))
            victim_cpu += float(out[2])
    return {
        "arm": name,
        "n": args.n,
        "mem_mb": args.mem_mb,
        "rounds": rounds,
        "rounds_per_s": round(rounds / wall, 1),
        "victim_cpu_s": round(victim_cpu, 2),
        "cpu_ms_per_round": round(1000 * victim_cpu / rounds, 2) if rounds else None,
        "max_round_ms": round(worst, 1),
        "monitor_cores": round((mon_ticks1 - mon_ticks0) / CLK_TCK / wall, 2) if mon and mon_ticks1 else 0.0,
        "wall_s": round(wall, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--mem-mb", type=int, default=32)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--warm", type=float, default=3)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--arms", default="baseline,old,new-on,new-off")
    ap.add_argument("--old-repo", default="/home/w00943531/workspace/avbperf")
    ap.add_argument("--new-repo", default="/home/w00943531/workspace/avbperf-new")
    ap.add_argument("--rounds", type=int, default=3, help="interleave every arm once per round")
    args = ap.parse_args()

    ensure_churn_bin()
    arms = {
        "baseline": (None, "on"),
        "old": (args.old_repo, "on"),
        "new-on": (args.new_repo, "on"),
        "new-off": (args.new_repo, "off"),
    }
    order = args.arms.split(",")
    # Interleaved rounds: this host's co-tenancy drifts by tens of percent
    # between back-to-back arms, so a sequential sweep measures the neighbours'
    # load, not the monitor. Every round runs every arm once, and the order
    # rotates so no arm is always first or always last in the drift.
    rows = []
    for round_no in range(args.rounds):
        rotation = round_no % len(order)
        for arm in order[rotation:] + order[:rotation]:
            repo, throttle = arms[arm]
            row = run_arm(arm, args, repo, throttle)
            row["round"] = round_no
            rows.append(row)
            print(json.dumps(row), flush=True)

    print(json.dumps({"summary": _medians(rows, order)}), flush=True)
    return 0


def _medians(rows: list[dict], order: list[str]) -> dict:
    """Median of each interference metric per arm, plus its spread.

    Also reports the within-round ratio against the no-monitor baseline: that
    pairing cancels most of the host's drift and is the number to trust when
    the absolute rates disagree between rounds.
    """
    by_round = {}
    for r in rows:
        by_round.setdefault(r["round"], {})[r["arm"]] = r
    out = {}
    for arm in order:
        arm_rows = [r for r in rows if r["arm"] == arm]
        if not arm_rows:
            continue
        entry = {
            "rounds_per_s_median": statistics.median(r["rounds_per_s"] for r in arm_rows),
            "rounds_per_s_min": min(r["rounds_per_s"] for r in arm_rows),
            "rounds_per_s_max": max(r["rounds_per_s"] for r in arm_rows),
            "cpu_ms_per_round_median": statistics.median(r["cpu_ms_per_round"] for r in arm_rows),
            "max_round_ms_max": max(r["max_round_ms"] for r in arm_rows),
            "monitor_cores_median": statistics.median(r["monitor_cores"] for r in arm_rows),
            "n": len(arm_rows),
        }
        paired = [
            arm_arm["rounds_per_s"] / base["rounds_per_s"]
            for by_arm in by_round.values()
            if (base := by_arm.get("baseline")) and (arm_arm := by_arm.get(arm))
        ]
        if paired:
            entry["paired_vs_baseline_median"] = round(statistics.median(paired), 3)
        out[arm] = entry
    return out


if __name__ == "__main__":
    raise SystemExit(main())
