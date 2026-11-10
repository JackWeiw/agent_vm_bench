"""Independent re-computation of the NUMA sheet, straight from the raw CSVs.

Recomputes node busy% and node memory with a plain groupby (no vm_monitor
code) and compares against host_resources.xlsx. Fails loudly on any mismatch.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

CPU = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
TOL = 1e-6


def main(raw_dir: str, xlsx: str) -> None:
    sheet = pd.read_excel(xlsx, sheet_name="NUMA")
    pc = pd.read_csv(f"{raw_dir}/host_percpu_raw.csv")
    mp = pd.read_csv(f"{raw_dir}/numa_cpu_map.csv")
    nm = pd.read_csv(f"{raw_dir}/numa_mem_raw.csv")
    node_of = dict(zip(mp["cpu"], mp["node"]))

    stamps = sorted(pc["timestamp"].unique())
    checked = 0
    for row in (1, 5, len(stamps) // 2, len(stamps) - 1):
        prev, cur = stamps[row - 1], stamps[row]
        sub = pc[pc["timestamp"].isin([prev, cur])]
        grouped = sub.groupby(["timestamp", "cpu_id"])[CPU].sum()
        delta = (grouped.loc[cur] - grouped.loc[prev]).copy()
        delta["node"] = [node_of[c] for c in delta.index]
        per_node = delta.groupby("node").sum()
        for node in sorted(per_node.index):
            total = per_node.loc[node].sum()
            busy = 100.0 * (1.0 - (per_node.loc[node, "idle"] + per_node.loc[node, "iowait"]) / total)
            got = float(sheet.loc[row, f"NUMA{node} CPU (%)"])
            if abs(got - busy) > 1e-3:
                sys.exit(f"NUMA{node} CPU mismatch at row {row}: sheet={got} recomputed={busy}")
            checked += 1

    for node, group in nm.groupby("node"):
        group = group.reset_index(drop=True)
        for row in (0, 5, len(group) - 1):
            r = group.iloc[row]
            expect = {
                f"NUMA{node} Used (GB)": r["used_mb"] / 1024.0,
                f"NUMA{node} Free (GB)": r["free_mb"] / 1024.0,
                f"NUMA{node} Available (GB)": r["available_mb"] / 1024.0,
                f"NUMA{node} Anon (GB)": r["anon_pages_mb"] / 1024.0,
                f"NUMA{node} File (GB)": r["file_pages_mb"] / 1024.0,
                f"NUMA{node} SwapCache (MB)": r["swap_cached_mb"],
                f"NUMA{node} Usage (%)": r["usage_pct"],
            }
            for column, want in expect.items():
                got = float(sheet.loc[row, column])
                if abs(got - want) > max(1e-3, abs(want) * TOL):
                    sys.exit(f"{column} mismatch at row {row}: sheet={got} recomputed={want}")
                checked += 1

    print(f"OK: {checked} NUMA values match an independent re-computation")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
