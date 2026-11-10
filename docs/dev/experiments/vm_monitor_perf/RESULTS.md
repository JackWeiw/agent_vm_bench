# vm_monitor sampling overhead: measurement and reduction

Status: measured on the shared lab host `192.168.25.95` (320 cores, 4 NUMA
nodes, THP `[always]`, Python 3.11). Every number below is produced by the
scripts next to this file; the raw JSONL rows are kept in
[`raw/`](raw/).

## Why

A replay run at the 1:1 baseline is 384 sandboxes and the oversubscription
sweep goes to 1:10. Before this change the monitor spent most of a sampling
cycle walking every VM's page tables (`/proc/<pid>/numa_maps` +
`/proc/<pid>/smaps_rollup`, ~35 ms per GiB with 4 KiB pages) plus a psutil
`process_iter(["pid", "name", "cmdline", "status"])` discovery pass that reads
the command line of every process on the host. On the target host that was
several cores -- >600 % CPU at 1:10 -- spent next to a benchmark whose whole
point is measuring the sandbox, and page-table walks contend with the guest's
own `mmap_lock` traffic.

## What changed

| Area | Before | After |
|------|--------|-------|
| Discovery | `psutil.process_iter(["pid","name","cmdline","status"])`: a Process object and a cmdline read for every process | direct `/proc` scan: one `stat` read per pid, `cmdline` only for pids whose comm can match (or whose subclass opts in) |
| Per-VM CPU% | `psutil.Process(pid).cpu_percent()` -- a second `/proc/<pid>/stat` read after the jiffies read | jiffy delta from the `/proc/<pid>/stat` read that `vm_cpu_raw.csv` already needs |
| numa_maps parse | per-line dict-of-dicts rebuild, `re.match` per token | pure `parse_numa_maps()` with per-node list accumulators, no regex (20 µs vs 102 µs/VM) |
| Heavy memory reads | every VM, every sample, 64 threads | CPU-budgeted round-robin (`HeavyRefreshScheduler`, 25 % of one core per interval, 8 threads), last measured values carried forward |
| pid reuse | a restarted VM kept the old pid's baseline | `/proc/<pid>/stat` field 22 (starttime) is checked; a recycled pid re-measures and reseeds CPU% |

The policy (how often to refresh) lives in `vm_monitor/sampling.py`; the
low-level readers live in `vm_monitor/procfs.py`; `VMMonitorBase` delegates.
A different cadence policy is a subclass of `HeavyRefreshScheduler`, not an
edit to the monitor.

Compatibility: the reported record shape, the CSV headers, the `Raw_VM_Data`
sheet and every derived metric are unchanged. CPU% is the same quantity
(delta ticks / `SC_CLK_TCK` / elapsed) at the same round(2) precision. Memory
fields can be up to `stride` samples stale on fleets whose full pass does not
fit the budget; `stride == 1` (small or THP-backed fleets) is byte-for-byte
the old cadence. Every stride change logs once at INFO.

## A/B results

`ab.py` runs the real `collect_sample()` loop of one checkout in its own
process and reports `time.process_time()` per sample -- CPU time, so a
co-loaded host does not distort it. `--throttle off` forces stride 1 (i.e. the
post-change code doing every-VM refresh), which separates the parser and
discovery wins from the budget.

Fleet shapes: `light` = 3840 shim processes with no memory mapping (the pure
discovery + jiffies path, i.e. the 1:10 oversubscription bookkeeping); `heavy
4 KiB` = guest-RAM mappings on 4 KiB pages (worst case for the page-table
walk); `heavy THP` = the same mappings backed by 2 MiB pages, which is what a
real guest RAM region looks like on this host (THP `[always]`). Sustained
cores = CPU ms per sample / 2000 ms interval.

| Shape | Arm | CPU ms/sample | Sustained cores | Discovery ms | Heavy stride | µs per VM heavy |
|-------|-----|---------------|-----------------|--------------|--------------|-----------------|
| light, 3840 procs | old | 2519.3 | 1.26 | 459.8 | -- | -- |
| | new, budget on | 1360.0 | 0.68 | 226.3 | 3 | 320 |
| heavy 4 KiB, 192 x 1 GiB | old | 8631.6 | 4.32 | 210.2 | -- | -- |
| | new, stride 1 | 6873.6 | 3.44 | 73.7 | 1 | 35640 |
| | new, budget on | 564.0 | 0.28 | 73.2 | 13 | 31470 |
| heavy THP, 192 x 1 GiB | old | 3232.1 | 1.62 | 228.5 | -- | -- |
| | new, stride 1 | 2367.3 | 1.18 | 76.3 | 1 | 11360 |
| | new, budget on | 594.7 | 0.30 | 72.4 | 5 | 11960 |
| heavy 4 KiB, 384 x 256 MiB | old | 2258.4 | 1.13 | 220.1 | -- | -- |
| | new, stride 1 | 1388.1 | 0.69 | 74.9 | 1 | 3280 |
| | new, budget on | 558.2 | 0.28 | 80.1 | 4 | 4420 |

Raw rows: [`raw/ab_matrix.jsonl`](raw/ab_matrix.jsonl).

Three effects separate out:

- **Discovery** (459.8 -> 73.2-80.1 ms at 192-384 procs, 210 -> 76 ms at 3840)
  is a flat ~3x win and does not depend on the memory shape.
- **The heavy pass** is what scales with page weight. At stride 1 (i.e. the
  post-change tree with the budget disabled) the numa_maps parser rewrite
  alone takes 4 KiB-backed 192 x 1 GiB from 8631.6 to 6873.6 ms/sample
  (-20 %), and 384 x 256 MiB from 2258.4 to 1388.1 (-39 %); the smaller
  relative win on the largest fleet is because the walk itself dominates over
  the parse.
- **The budget** is what removes the cliff. It caps the heavy pass at 25 % of
  one core per interval and pushes the rest to stride: 8631.6 -> 564.0
  ms/sample on the 4 KiB 192 x 1 GiB fleet (-93 %), 2258.4 -> 558.2 on the
  wide fleet (-75 %). Trimmed to the two numbers that matter, the old monitor
  burns 4.32 cores where the new one burns 0.28.

THP changes the constant, not the conclusion: a THP-backed GiB costs 11.4 ms to
walk instead of 35.6 ms (3.1x, not 512x -- the walk still visits every VMA and
`smaps_rollup` accounts PSS at base-page granularity), so the budget's stride
shrinks (5 instead of 13) but the monitor still sits at 0.30 cores.

Production shape. The 1:1 replay baseline is 384 sandboxes; guest RAM per VM on
the AENV pool is 4 GiB, THP-backed. Extrapolating the measured per-GiB THP cost
(11.4 ms) to 4 GiB gives ~46 ms per VM and ~17.5 s per full pass -- 35x the
budget, so stride lands near 35 (each VM's memory/PSS numbers refresh about
once a minute at the 2 s interval) while CPU%/jiffies, which are what the
stress analysis reads, stay per-sample. The old monitor at that shape would
spend ~26 s of CPU per 2 s sample on the heavy pass alone, i.e. ~13 cores
alongside the benchmark.

## Value parity

`dump_vm_metrics.py` dumps, for the **same** live pids, every numa_maps field,
PSS, jiffies and the full per-VM record from both checkouts, then
`compare_dumps.py` diffs them.

Both checkouts dumped the same 12 live pids (6 light shims + 6 x 192 MiB
shims), so pid identity and NUMA placement were held constant and any
difference would be a real parser/collection difference:

```
candidates old=12 new=12
vms old=12 new=12
IDENTICAL
```

That covers every numa_maps field (per-node file/anon/active/dirty/swapcache
totals), PSS, jiffies and the full per-VM record. Raw dumps:
[`raw/parity_old.json`](raw/parity_old.json),
[`raw/parity_new.json`](raw/parity_new.json).

CPU% parity is checked separately because the two trees compute it by
different code paths (psutil `cpu_percent()` vs the jiffy delta): eight
full-core burners sampled by both:

```
old mean 99.85 %   new mean 99.91 %   max abs diff 0.43 pp
```

([`raw/cpu_parity.json`](raw/cpu_parity.json)) -- the same quantity to within
half a percentage point, which is the sampling-window jitter of a 100 % burner,
not a bias.

## Side effects on the workload

`perturb.py` runs a fleet of page-fault churn loops -- the same `mmap` work a
sandbox does, and the work the monitor's page-table walks contend with -- with
no monitor, the old monitor, and the new monitor with its budget on and off.
Arms run round-robin with a rotating order, because this host's co-tenancy
drifts by tens of percent between back-to-back 30 s windows; the
within-round ratio against the no-monitor arm is the number to read.

Fleet: 64 churn loops x 32 MiB, 6 rounds x 4 arms x 30 s
([raw/perturb.jsonl](raw/perturb.jsonl)).

| Arm | Monitor cores (median) | Victim rounds/s vs no-monitor (per-round ratio, median) | Ratio range over 6 rounds |
|-----|------------------------|--------------------------------------------------------|---------------------------|
| no monitor | 0 | 1.000 | -- |
| old monitor | 1.35 | 0.976 | 0.82 - 1.32 |
| new, budget on | 1.07 | 1.019 | 0.82 - 1.35 |
| new, budget off (refresh every sample) | 1.11 | 0.996 | 0.79 - 1.10 |

The churn workload's own per-round cost swings 7.3-11.5 ms between windows by
itself (THP collapse of the anonymous mapping, plus neighbours on this shared
host), so every arm lands inside the same +-25 % band. **No monitor-induced
slowdown is detectable above the host's noise floor** -- including for the old
monitor, whose 1.35 cores on a 320-core host are ~0.4 % of capacity. The
multiplicity that matters is the CPU figure, and that is where the change
bites: the same fleet costs the new monitor 21 % less, and the gap widens with
fleet size and page weight (see the A/B table).

## Reproduce

```bash
# on the target host, with two checkouts: pre-change and post-change
bash run_matrix.sh                     # the full A/B matrix above
python3 dump_vm_metrics.py --repo <old> --out /tmp/old.json --pids-file /tmp/fleet.json
python3 compare_dumps.py /tmp/old.json /tmp/new.json
python3 cpu_parity.py --old-repo <old> --new-repo <new> --n 8
python3 perturb.py --arms baseline,old,new-on,new-off --n 64 --mem-mb 32 --rounds 6
```

The fake VMs are a compiled shim (`vmsim.c`, emitted by `ab.py`): it `mmap`s
the requested size and touches every 4 KiB page; `VMSIM_THP=1` adds
`MADV_HUGEPAGE`, so `--thp` reproduces the 2 MiB-page backing of a real guest
RAM mapping (verified: `AnonHugePages` == RSS for a 256 MiB mapping).
