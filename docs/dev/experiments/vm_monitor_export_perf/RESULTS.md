# host_resources.xlsx — 2-hour export: performance + accuracy validation

**Date:** 2026-11-03 · **Host:** `192.168.25.95` (320 cores, 502 GiB, Python 3.11.6,
pandas 2.2.2, xlsxwriter 3.3.1)

## What changed

`resource_report.xlsx` used to carry every per-sample timeline (7 timeline sheets at
up to 7,200 rows × 161 columns, plus a `Raw_VM_Data` sheet that could reach 5.4 M rows)
and was built with `pandas.ExcelWriter(engine="openpyxl")` + a full
`load_workbook`/re-save for charts. It now carries **summary sheets only**.

Every per-sample timeline moved to `host_resources.xlsx`, derived post-hoc from the
always-on `raw_data/*.csv` snapshots with a rewritten, vectorized pipeline
(`raw_frames.py` → `raw_metrics.py` → `raw_xlsx.py` → `raw_report.py`) and written
natively with `xlsxwriter(constant_memory=True)`.

## Fixture

`gen_fixture.py` tiles a real 20 s capture from the same host to 3,600 samples × 2 s
(**2 h**) × 384 VMs × 320 CPUs × 20 disks (16 kept, see the sheet cap) × 8 ublk daemons
× 1 host process, advancing every cumulative counter monotonically:

```
276 MB raw CSVs:
  host_cpu_raw.csv        1.0 MB    host_percpu_raw.csv  121.7 MB
  host_mem_raw.csv        1.5 MB    host_vmstat_raw.csv    4.1 MB
  numa_mem_raw.csv        2.8 MB    numa_cpu_map.csv       1.8 KB
  vm_cpu_raw.csv        137.6 MB    disk_io_raw.csv       16.7 MB
  ublk_cpu_raw.csv        2.5 MB    hostproc_cpu_raw.csv   0.4 MB
  api_state_raw.csv       0.1 MB
```

## Results

| Path | Wall | Peak RSS | Output |
|---|---|---|---|
| `host_resources.xlsx` — **before** (4 sheets) | **134.8 s** | 569 MB | 3.8 MB |
| `host_resources.xlsx` — **after** (5 sheets) | **17.6 s** | 1002 MB | 5.3 MB |

**7.7× faster** while *adding* the `NUMA` sheet (4 nodes × 8 columns + 4 charts),
four swap gauge columns and `vm_mem_gb` — i.e. strictly more work, not less.

`resource_report.xlsx` is now sample-count-independent by construction: on the same
capture it is `Summary` (40 rows × 3 cols) + `NUMA_Overview` (5 rows × 9 cols) only.
Nothing in it grows with the sampling window, so a 2 h (or 8 h) run cannot inflate it.

### Where the 17.6 s goes (cProfile, 2 h fixture)

| Phase | Time | Note |
|---|---|---|
| Render/write | 14.4 s | 1.5 M cells; `Disk IO` (7,200 × 161) alone is 1.16 M cells |
| Derive | 7.0 s | all five timelines, fully vectorized |
| CSV parse | 5.5 s | 11 files, 276 MB, dtypes pinned |

No pathological hotspot: the remainder is xlsxwriter's per-cell write, which is the
price of a native charted workbook. Rendering is the next lever (not taken: a 2 h
report at 17.6 s is inside target).

## Accuracy

### 1. Old vs new, same fixture, all shared columns

All **29** columns present in both workbooks are **bit-identical** (max relative
deviation 0.0 over 3,599 rows):

```
CPU busy/iowait/user/sys, Mem used/cache, Buffers, Dirty, Writeback, Anon pages,
File cache, SReclaimable, Swap in/out, Page scan/reclaim/refault, ublk cores,
VM cores, FC total PSS, Direct reclaim, Alloc stalls, Concurrent VMs,
Procs running/blocked, Sandboxes running/paused (API), t_s
```

One intentional difference: **row 0 gauges**. The old build NaN'd the first sample of
every gauge column; the new one reports it (`Mem used (GB)` row 0 = 248.22 vs `nan`,
`Procs running` row 0 = 153 vs `nan`). Gauges need no predecessor — this is the
documented contract ("gauge 首行即有值"), and the rate columns still start at NaN.

`Per-VM` (7,203 rows × 10 cols), `Disk IO` (7,200 × 161) and `HostProc` (7,200 × 15)
are shape-identical to the old build.

### 2. Independent re-computation (no `vm_monitor` code)

`verify_numa.py` recomputes the `NUMA` sheet straight from the raw CSVs with plain
`groupby` arithmetic and compares 100 values (4 nodes × {CPU%, Used, Free, Available,
Anon, File, SwapCache, Usage}) at four sample positions:

```
OK: 100 NUMA values match an independent re-computation
```

`verify_workbook.py` recomputes the new columns from the raw CSVs:

```
OK: workbook opens, chart counts match, swap / VM-memory / PSS columns recompute exactly
```

- `swap_used_gb` = `(SwapTotal − SwapFree) / 1024` — exact
- `swap_cache_ratio_pct` = `100 × SwapCached / (SwapTotal − SwapFree)` — exact
- `vm_mem_gb` = `Σ_vm memory_mb / 1024` per sample — exact (404.02 GB for the fixture's
  384 × ~1.05 GiB fleet)
- `fc_pss_gb` = `Σ_vm pss_mb / 1024` per sample — exact (384.78 GB)

### 3. Workbook structure

Opened with `openpyxl` (the strict reader): 5 sheets, expected native LineCharts, no
corrupt cells.

| Sheet | Rows × cols | Charts |
|---|---|---|
| Host resources | 3,601 × 34 | 15 (`Host memory`, `Buffers / dirty / writeback`, `Anon vs file cache`, `Swap usage`, `Swap cache`, `FC memory (total PSS)`, `VM memory vs FC PSS`, `Page-cache pressure`, `Alloc stalls`, `Swap in/out`, `Host CPU`, `CPU cores (ublk + VM)`, `Procs running/blocked`, `Concurrent VMs`, `Sandbox states (API)`) |
| NUMA | 3,601 × 34 | 4 (`NUMA CPU`, `NUMA memory used/free`, `NUMA anon vs file`, `NUMA swap cache`) |
| Per-VM | 7,204 × 10 | 2 |
| Disk IO | 7,201 × 161 | 5 |
| HostProc | 7,201 × 15 | 2 |

### 4. Degradation (the real timeout/failure modes)

| Case | Result |
|---|---|
| Real minimal capture (no VMs, no ublk, no API state) | 31 columns, 11 charts — VM/API columns and their charts absent, **no blank charts**; `Per-VM`/`HostProc` sheets absent |
| `vm_cpu_raw.csv` truncated mid-file | WARNING, `Per-VM` cut to the readable prefix (112 rows), other sheets complete, exit 0, 6.2 s |
| `disk_io_raw.csv` truncated + `numa_mem_raw.csv` emptied | WARNING, `Disk IO` 45 rows, `NUMA` keeps its CPU columns, exit 0 |
| 20 devices > 16-series cap | WARNING naming the dropped devices, keeps the 16 busiest |
| Fixture's synthetic fleet exceeds host busy cores | one WARNING at build time — the `vm_cores ≤ host_busy_cores` guard firing as designed |
| Any single sheet builder raises | it is dropped and the workbook is rebuilt without it; output is promoted by `os.replace`, so a failed build never leaves a corrupt file |

## Known limits

- **Peak RSS grew 569 MB → 1002 MB.** The whole raw dir is loaded once (276 MB of CSV
  for 2 h × 384 VMs) and the `NUMA` derivation adds a per-CPU pivot. At 8 h the CSVs
  scale ~4× — the upgrade path is chunked reads per frame, not needed at the 2 h target.
- **Charts are skipped above 500,000 points** (the table still carries every sample).
  At a 2 s interval that is ~11 days.
- **Disk IO is capped at 16 devices** (10 columns each); the rest are named in a WARNING.
- `Available (GB)` reads 0 on kernels whose node `meminfo` has no `MemAvailable`
  (this host) — a real zero, not a missing column.

## Reproduce

```bash
# 1. real capture -> template
vm-monitor --vmm firecracker -t 20 -i 2 --log-dir /tmp/vmcap --no-svg

# 2. tile it to 2 h
python3 gen_fixture.py --template /tmp/vmcap/raw_data --out /tmp/fixture2h \
    --samples 3600 --interval 2 --vms 384 --ublk 8 --hostproc 1

# 3. build + verify
vm-monitor raw-report /tmp/fixture2h --out /tmp/hr_new.xlsx
python3 verify_numa.py /tmp/fixture2h /tmp/hr_new.xlsx
python3 verify_workbook.py /tmp/fixture2h /tmp/hr_new.xlsx
```
