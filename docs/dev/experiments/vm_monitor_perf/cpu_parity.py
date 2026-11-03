"""Per-VM CPU% parity: psutil cpu_percent() (old) vs jiffy delta (new).

Both checkouts sample the SAME busy fleet from separate processes; each reports
CPU% on its second sample (the first seeds the baseline, just like psutil).
A fleet of full-core burners should read ~100 % in both.

    python3 cpu_parity.py --old-repo /home/w00943531/workspace/avbperf \
                          --new-repo /home/w00943531/workspace/avbperf-new --n 8

Each checkout runs in its own process (two versions of the same package cannot
be imported into one interpreter).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = "/var/tmp/vmperf/churn"
CHURN_BIN = os.path.join(BIN_DIR, "containerd-shim-cube-rs")


def sample(repo: str, pids_file: str, out: str, interval: float) -> None:
    """Second-sample CPU% of every pid in --pids-file, as JSON."""
    sys.path.insert(0, repo)
    from vm_monitor.vmm.cubesandbox import CubeSandboxMonitor

    with open(pids_file, encoding="utf-8") as f:
        wanted = set(json.load(f))
    mon = CubeSandboxMonitor()
    mon.log_dir = None
    mon.interval = interval
    mon.collect_sample()  # seed: same first-sample-zero behavior as psutil
    time.sleep(interval)
    records = mon.collect_sample()
    json.dump({r["pid"]: r["cpu_percent"] for r in records if r["pid"] in wanted}, open(out, "w"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-repo", default="/home/w00943531/workspace/avbperf")
    ap.add_argument("--new-repo", default="/home/w00943531/workspace/avbperf-new")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--sample-repo", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--pids-file", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--out", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.sample_repo:
        sample(args.sample_repo, args.pids_file, args.out, args.interval)
        return 0

    import perturb  # shares the churn binary with the perturbation experiment

    perturb.ensure_churn_bin()
    victims = [subprocess.Popen([CHURN_BIN, "32", "120"], stdout=subprocess.DEVNULL) for _ in range(args.n)]
    pids = [p.pid for p in victims]
    tmp = tempfile.mkdtemp(prefix="cpu_parity_")
    pids_file = os.path.join(tmp, "pids.json")
    with open(pids_file, "w", encoding="utf-8") as f:
        json.dump(pids, f)
    try:
        time.sleep(3)  # let every victim spin
        results = {}
        for tag, repo in (("old", args.old_repo), ("new", args.new_repo)):
            out = os.path.join(tmp, f"{tag}.json")
            subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--sample-repo",
                    repo,
                    "--pids-file",
                    pids_file,
                    "--out",
                    out,
                    "--interval",
                    str(args.interval),
                ],
                check=True,
            )
            with open(out, encoding="utf-8") as f:
                results[tag] = json.load(f)
        common = sorted(set(results["old"]) & set(results["new"]))
        old = [results["old"][p] for p in common]
        new = [results["new"][p] for p in common]
        print(
            json.dumps(
                {
                    "pids": len(common),
                    "old_mean_pct": round(statistics.mean(old), 2),
                    "new_mean_pct": round(statistics.mean(new), 2),
                    "max_abs_diff_pct": round(max(abs(a - b) for a, b in zip(old, new)), 2),
                    "old": {p: results["old"][p] for p in common},
                    "new": {p: results["new"][p] for p in common},
                }
            )
        )
        return 0
    finally:
        for p in victims:
            p.send_signal(signal.SIGKILL)
        for p in victims:
            p.wait()


if __name__ == "__main__":
    raise SystemExit(main())
