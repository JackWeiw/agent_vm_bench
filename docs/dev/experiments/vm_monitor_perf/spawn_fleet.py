"""Spawn the fake fleet once, print its pids, and hold it alive.

    python3 spawn_fleet.py --light 6 --heavy 6 --mem-mb 192 --pids-file /tmp/fleet.json &

Then dump the same pids against each checkout (dump_vm_metrics.py --pids-file)
so pid identity and NUMA placement are held constant across the comparison.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, "/home/w00943531/workspace/perf")
import ab  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--light", type=int, default=6)
    ap.add_argument("--heavy", type=int, default=6)
    ap.add_argument("--mem-mb", type=int, default=192)
    ap.add_argument("--pids-file", required=True)
    args = ap.parse_args()

    ab.ensure_fake_bin(args.mem_mb)
    heavy = [ab.subprocess.Popen([ab.FAKE_BIN, str(args.mem_mb), "3600"]) for _ in range(args.heavy)]
    light = [ab.subprocess.Popen([ab.FAKE_BIN, "3600"]) for _ in range(args.light)]
    ab.wait_faulted(heavy, args.mem_mb)
    pids = [p.pid for p in heavy + light]
    with open(args.pids_file, "w", encoding="utf-8") as f:
        json.dump(pids, f)
    print(f"fleet up: {len(pids)} pids -> {args.pids_file}")

    def stop(*_):
        for p in heavy + light:
            p.send_signal(signal.SIGKILL)
        for p in heavy + light:
            p.wait()
        try:
            os.unlink(args.pids_file)
        except OSError:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    time.sleep(3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
