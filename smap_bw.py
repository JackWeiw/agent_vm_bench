#!/usr/bin/env python3
"""
smap_bw.py — SMAP_migrate per-cycle bandwidth tool

Each migration cycle:
  Phase 1: from 1 to 5  (migration starts)
  Phase 2: from 5 to 1  (migration ends)
  When the direction switches back from 5->1 to 1->5, report the previous cycle's bandwidth.

Formula: bandwidth = sum(nr) * 2 / 1024 / dt  GB/s

Usage:
    sudo python3 smap_bw.py [options]

Options:
    --file FILE       Use a log file as the input source; the script reads and analyzes it.
                      Without this option, the script runs `dmesg -w` to monitor the kernel log live.

    --clear           Run `dmesg -C` before monitoring to clear the kernel log buffer.
                      Only meaningful in live mode; ensures only new events are captured.

    --timeout TIMEOUT In live mode, stop automatically if no new SMAP_migrate event arrives
                      for TIMEOUT consecutive seconds. Default 10 seconds. Ignored in file mode
                      (file mode ends when the file is fully consumed).

    --duration SECS   Start timing from the first SMAP_migrate event and stop automatically
                      after SECS seconds. Used to bound the capture window; complements --timeout:
                      --timeout caps "idle wait", --duration caps "total capture length".

    --debug           Print every matched raw line and parsing step, useful for diagnosing
                      whether the regex matches correctly and whether the format is abnormal.

Examples:
    # Offline analysis of an existing file
    python3 smap_bw.py --file dmesg_log.txt

    # Live monitoring: clear old logs first, exit after 30s with no new events
    sudo python3 smap_bw.py --clear --timeout 30

    # Live monitoring: stop after capturing for 60 seconds
    sudo python3 smap_bw.py --clear --duration 60

    # Capture for 60s, also exit early if no new event for 15s
    sudo python3 smap_bw.py --clear --duration 60 --timeout 15

    # Live monitoring with debug: inspect per-line parsing details
    sudo python3 smap_bw.py --clear --timeout 30 --debug

    # Read from a pipe
    dmesg -w | python3 smap_bw.py
"""

import argparse
import re
import subprocess
import sys
import threading
import time

G = "\033[32m"
Y = "\033[33m"
C = "\033[36m"
R = "\033[31m"
B = "\033[1m"
D = "\033[2m"
E = "\033[0m"

RE_MIGRATE = re.compile(
    r"$$\s*(\d+\.\d+)$$\s+SMAP_migrate:\s+$$(\d+)$$\s+" r"pid\s+(\d+)\s+from\s+(\d+)\s+to\s+(\d+)\s+nr\s+(\d+)"
)


def parse_line(line):
    line = line.strip()
    if not line:
        return None
    m = RE_MIGRATE.search(line)
    if m:
        return dict(
            ts=float(m.group(1)),
            seq=int(m.group(2)),
            pid=int(m.group(3)),
            frm=int(m.group(4)),
            to=int(m.group(5)),
            nr=int(m.group(6)),
        )
    # Fallback parser for slightly different formatting
    if "SMAP_migrate" not in line:
        return None
    try:
        bracket_ts, rest = line.split("]", 1)
        ts = float(bracket_ts.lstrip("[").strip())
        parts = rest.split()
        idx = 0
        for i, p in enumerate(parts):
            if p.startswith("SMAP_migrate"):
                idx = i + 1
                break
        seq = 0
        for i in range(idx, len(parts)):
            if parts[i].startswith("["):
                seq = int(parts[i].strip("[]"))
                idx = i + 1
                break

        def find_val(kw):
            for j in range(idx, len(parts) - 1):
                if parts[j] == kw:
                    return int(parts[j + 1])
            return 0

        pid = find_val("pid")
        frm = find_val("from")
        to = find_val("to")
        nr = find_val("nr")
        if nr > 0:
            return dict(ts=ts, seq=seq, pid=pid, frm=frm, to=to, nr=nr)
    except (ValueError, IndexError):
        pass
    return None


def report_cycle(cycle_no, records):
    n = len(records)
    total_nr = sum(r["nr"] for r in records)
    t0 = records[0]["ts"]
    t1 = records[-1]["ts"]
    dt = t1 - t0
    if dt <= 0:
        dt = 0.000001

    gb = total_nr * 2 / 1024
    bw = gb / dt

    nodes = {}
    for r in records:
        key = (r["frm"], r["to"])
        nodes[key] = nodes.get(key, 0) + r["nr"]

    print(
        f"""
{B}╔══════════════════════════════════════════════╗
║        Cycle {cycle_no:>3d} Migration Bandwidth Report       ║
╠══════════════════════════════════════════════╣{E}
  Event count:      {n}
  Start time:        {t0:.6f} s
  End time:          {t1:.6f} s
  Duration:          {dt:.6f} s
  Total pages:       {total_nr}
  Data volume:       {gb:.4f} GB
{B}  ────────────────────────────────────────────{E}
  Direction stats:"""
    )
    for (f, t_), nr in sorted(nodes.items()):
        print(f"    node {f} → {t_}:  {nr} pages")
    print(
        f"""{B}  ────────────────────────────────────────────{E}
{Y}{B}  Bandwidth:        {bw:.4f} GB/s{E}
{B}╚══════════════════════════════════════════════╝{E}"""
    )
    return total_nr, dt, bw


def main():
    ap = argparse.ArgumentParser(
        description="SMAP_migrate per-cycle bandwidth tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  sudo python3 smap_bw.py --clear --timeout 15\n"
        "  python3 smap_bw.py --file dmesg_log.txt\n"
        "  dmesg -w | python3 smap_bw.py\n",
    )
    ap.add_argument("--timeout", type=float, default=10, help="Idle timeout in seconds (default 10)")
    ap.add_argument("--duration", type=float, default=None, help="Max capture duration from first event (seconds)")
    ap.add_argument("--clear", action="store_true", help="Clear the dmesg buffer before starting")
    ap.add_argument("--file", type=str, default=None, help="Read logs from a file")
    ap.add_argument("--debug", action="store_true", help="Show parsing details")
    args = ap.parse_args()

    print(
        f"""
{C}{B}┌──────────────────────────────────────────────┐
│     SMAP_migrate per-cycle bandwidth tool     │
│     Detect direction switch: 1->5 + 5->1      │
└──────────────────────────────────────────────┘{E}
"""
    )

    if args.clear:
        subprocess.run(["dmesg", "-C"], check=True, capture_output=True)
        print(f"{D}  Cleared dmesg buffer{E}")

    proc = None
    if args.file:
        source = open(args.file, errors="replace")
        label = f"file: {args.file}"
    elif not sys.stdin.isatty():
        source = sys.stdin
        label = "stdin pipe"
    else:
        proc = subprocess.Popen(
            ["dmesg", "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        source = proc.stdout
        label = f"dmesg -w (timeout {args.timeout}s)"

    print(f"{D}  Data source: {label}{E}")
    if args.duration is not None:
        print(f"{D}  Max capture duration: {args.duration}s{E}")
    print()

    # -- State --
    collecting = False
    current_records = []
    prev_direction = None
    cycle_no = 0
    all_bw = []
    wall_start = None  # wall-clock time of the first event
    stopped = threading.Event()
    stop_reason = [None]  # records the stop reason
    timer_lock = threading.Lock()
    timer = [None]

    def on_timeout():
        stop_reason[0] = "timeout"
        stopped.set()

    def reset_timer():
        with timer_lock:
            if timer[0]:
                timer[0].cancel()
            t = threading.Timer(args.timeout, on_timeout)
            t.daemon = True
            t.start()
            timer[0] = t

    def cleanup():
        with timer_lock:
            if timer[0]:
                timer[0].cancel()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if hasattr(source, "close"):
            source.close()

    def finish_cycle():
        nonlocal cycle_no
        if not current_records:
            return
        cycle_no += 1
        t, d, b = report_cycle(cycle_no, current_records)
        all_bw.append((t, d, b))
        current_records.clear()

    need_timer = proc is not None

    try:
        if need_timer:
            reset_timer()

        for raw in source:
            if stopped.is_set():
                break

            rec = parse_line(raw)
            if rec is None:
                continue

            # -- First event: record wall-clock --
            if wall_start is None:
                wall_start = time.monotonic()

            # -- Duration check --
            if args.duration is not None:
                elapsed = time.monotonic() - wall_start
                if elapsed >= args.duration:
                    stop_reason[0] = "duration"
                    # Do not break yet; finish processing the current event first
                    stopped.set()

            direction = (rec["frm"], rec["to"])

            if collecting and prev_direction == (5, 1) and direction == (1, 5):
                finish_cycle()

            if direction == (1, 5):
                collecting = True

            if collecting:
                current_records.append(rec)
                el = current_records[-1]["ts"] - current_records[0]["ts"]
                cum = sum(r["nr"] for r in current_records)
                if args.debug or (cycle_no == 0 and len(current_records) <= 3):
                    print(
                        "  {}[{:4d}]{} pid={:<10d} {}->{} nr={:<6d} │ cycle={:<3d} cum_nr={:<8d} dt={:.6f}s".format(
                            D, rec["seq"], E, rec["pid"], rec["frm"], rec["to"], rec["nr"], cycle_no + 1, cum, el
                        )
                    )

            prev_direction = direction

            if need_timer:
                reset_timer()

            if stopped.is_set():
                break

        else:
            if collecting:
                print(f"\n{D}  (input ended, processing the last incomplete cycle){E}")

        if stop_reason[0] == "timeout":
            print(f"\n{Y}[TIMEOUT]{E} no new event for {args.timeout}s")
        elif stop_reason[0] == "duration":
            wall_dur = time.monotonic() - wall_start if wall_start else 0
            print(f"\n{Y}[DURATION]{E} captured {wall_dur:.1f}s, reached duration limit {args.duration}s")

    except KeyboardInterrupt:
        print(f"\n{Y}[STOP]{E} interrupted by user")
    finally:
        cleanup()

    # -- Process the last cycle --
    if current_records:
        finish_cycle()

    # -- Global summary --
    if all_bw:
        total_pages = sum(x[0] for x in all_bw)
        avg_bw = sum(x[2] for x in all_bw) / len(all_bw)
        print(
            f"""
{C}{B}┌──────────────────────────────────────────────┐
│                  Global summary                │
├──────────────────────────────────────────────┤{E}
  Total cycles:        {len(all_bw)}
  Total pages:         {total_pages}
  Average bandwidth:   {avg_bw:.4f} GB/s
  Cycle BW range:      {min(x[2] for x in all_bw):.4f} ~ {max(x[2] for x in all_bw):.4f} GB/s
{B}└──────────────────────────────────────────────┘{E}"""
        )
    else:
        print(f"\n{R}  No SMAP_migrate migration cycle was captured.{E}")


if __name__ == "__main__":
    main()
