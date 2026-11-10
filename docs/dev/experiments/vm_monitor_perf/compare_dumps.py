"""Compare two dump_vm_metrics.py outputs for value-identical parsing.

The fake fleet is spawned heavy-first, so pairing by pid order within each
command-line class is deterministic. Reports every field mismatch.
"""
from __future__ import annotations

import json
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    old = load(sys.argv[1])
    new = load(sys.argv[2])
    print(f"candidates old={len(old['candidates'])} new={len(new['candidates'])}")
    print(f"vms old={len(old['vms'])} new={len(new['vms'])}")

    # Pair by index in pid order (same spawn order, pids ascending per class)
    old_vms = sorted(old["vms"], key=lambda v: v["pid"])
    new_vms = sorted(new["vms"], key=lambda v: v["pid"])

    def class_of(rec, dump):
        cand = next(c for c in dump["candidates"] if c["pid"] == rec["pid"])
        return len(cand["cmdline"].split())

    old_sorted = sorted(old_vms, key=lambda v: (class_of(v, old), v["pid"]))
    new_sorted = sorted(new_vms, key=lambda v: (class_of(v, new), v["pid"]))
    assert len(old_sorted) == len(new_sorted)

    mismatches = 0
    for o, n in zip(old_sorted, new_sorted):
        for field in ("numa_maps", "pss_mb"):
            a, b = o[field], n[field]
            if field == "numa_maps":
                a = {k: v for k, v in a.items() if k != "_parsed"}
                b = {k: v for k, v in b.items() if k != "_parsed"}
            if a != b:
                mismatches += 1
                print(f"MISMATCH pid-class {class_of(o, old)} field={field}\n  old={a}\n  new={b}")

    rec_fields = (
        "name",
        "memory_mb",
        "memory_huge_mb",
        "memory_private_mb",
        "memory_heap_mb",
        "memory_per_numa",
        "memory_swapcache_mb",
        "memory_swapcache_per_numa",
        "pss_mb",
        "status",
    )
    for o, n in zip(sorted(old["records"], key=lambda r: r["pid"]), sorted(new["records"], key=lambda r: r["pid"])):
        for field in rec_fields:
            if o.get(field) != n.get(field):
                mismatches += 1
                print(f"RECORD MISMATCH pid {o['pid']} field={field}: old={o.get(field)} new={n.get(field)}")

    print("IDENTICAL" if mismatches == 0 else f"{mismatches} mismatches")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
