"""Structural + arithmetic checks on a built host_resources.xlsx.

Verifies the workbook opens cleanly under openpyxl (the strict reader), that
each sheet carries the expected number of native LineCharts, and that the new
swap / VM-memory / FC-PSS columns agree with the raw CSVs.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from openpyxl import load_workbook

EXPECTED_CHARTS = {"Host resources": 15, "NUMA": 4, "Per-VM": 2, "Disk IO": 5, "HostProc": 2}


def main(raw_dir: str, xlsx: str) -> None:
    book = load_workbook(xlsx)
    print("sheets:", book.sheetnames)
    for name, sheet in zip(book.sheetnames, book.worksheets):
        title_chart = getattr(sheet, "_charts", [])
        titles = [c.title.tx.rich.p[0].r[0].t if c.title and c.title.tx else "?" for c in title_chart]
        print(f"{name}: {sheet.max_row} rows x {sheet.max_column} cols, {len(title_chart)} charts")
        for t in titles:
            print("   -", t)
        want = EXPECTED_CHARTS.get(name)
        if want is not None and len(title_chart) != want:
            sys.exit(f"{name}: expected {want} charts, found {len(title_chart)}")

    host = pd.read_excel(xlsx, sheet_name="Host resources")
    mem = pd.read_csv(f"{raw_dir}/host_mem_raw.csv")
    used = (mem["SwapTotal"] - mem["SwapFree"]) / 1024.0
    if not np.allclose(host["Swap used (GB)"], used, atol=1e-6):
        sys.exit("swap_used_gb does not match (SwapTotal - SwapFree) / 1024")
    ratio = (100.0 * mem["SwapCached"] / (mem["SwapTotal"] - mem["SwapFree"])).replace([np.inf, -np.inf], np.nan)
    if not np.allclose(host["Swap cache ratio (%)"], ratio, atol=1e-6):
        sys.exit("swap_cache_ratio_pct does not match 100*SwapCached/(SwapTotal-SwapFree)")

    vm = pd.read_csv(f"{raw_dir}/vm_cpu_raw.csv", usecols=["timestamp", "memory_mb", "pss_mb"])
    per_sample = vm.groupby("timestamp").agg(mem=("memory_mb", "sum"), pss=("pss_mb", "sum"))
    stamps = mem["timestamp"]
    got = host["VM memory (GB)"].to_numpy()
    want = per_sample["mem"].reindex(stamps).to_numpy() / 1024.0
    if not np.allclose(got, want, atol=1e-6):
        sys.exit("vm_mem_gb does not match sum(memory_mb)/1024 per sample")
    want_pss = per_sample["pss"].reindex(stamps).to_numpy() / 1024.0
    if not np.allclose(host["FC total PSS (GB)"].to_numpy(), want_pss, atol=1e-6):
        sys.exit("fc_pss_gb does not match sum(pss_mb)/1024 per sample")

    print("OK: workbook opens, chart counts match, swap / VM-memory / PSS columns recompute exactly")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
