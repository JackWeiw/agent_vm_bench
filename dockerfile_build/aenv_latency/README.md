# AgentENV snapshot lazy-load latency benchmark

Replicates the customer's four-metric AgentENV table (VM cold start, snapshot
start, 10-concurrent snapshot start, snapshot lazy-load memory latency) and lets
you diff x86 vs arm.

## Methodology (read this — it's the part that's easy to get wrong)

The lazy-load row only measures *snapshot lazy-load* if the 256MiB working set is
**populated inside the sandbox before pause**. Then the snapshot stores real page
contents; on resume those pages are restored on demand (lazy), so:

- **first touch** = page-in from snapshot (slow) — the lazy-load debt
- **second touch** = resident (fast)

If you skip populate and just `mmap` after resume, first touch degenerates to
zero-page mapping and you measure nothing (the customer's 197–338ms first-touch is
only explainable by fetching real snapshot contents; and first-*write* > first-*read*
is the COW-from-snapshot signature).

Per-mode independence: after the first traversal loads every page, later modes' first
touch is no longer lazy. So the harness runs each of the 4 modes in its own
`populate → pause → resume → measure` cycle (4 cycles, 256MiB each — peak memory
stays at 256MiB; the 4 resumes also feed the snapshot-start row).

## Files

- `latbench.c` — in-guest probe. `populate` / `measure <mode>` / `stress <mode> <iters>` / `cleanup`.
- `Dockerfile` (arm) + `Dockerfile.x86` — lean Ubuntu + `latbench`, no workload.
- `../../scripts/aenv_latency_bench.py` — standalone harness (e2b SDK only).

## Build the image

```bash
# arm
docker build --build-arg HTTP_PROXY=http://your-proxy:8888 \
    -t ubuntu-aenv-latency-bench:24.04-linuxarm64 -f Dockerfile .
# x86
docker build --build-arg HTTP_PROXY=http://your-proxy:8888 \
    -t ubuntu-aenv-latency-bench:24.04-x86_64 -f Dockerfile.x86 .
```

`HTTP_PROXY` is optional (empty by default); pass it behind a corp proxy. The builder
stage skips apt TLS verification because the corp proxy MITMs with a self-signed
cert; the final image is proxy-free so the sandbox doesn't inherit a build-time proxy.

Then register the template with AgentENV via `aenv pull` (handled outside this repo).

## Run the benchmark

```bash
export E2B_API_URL=http://127.0.0.1:8000
export E2B_API_KEY=e2b_000000

# single arch
python scripts/aenv_latency_bench.py --template arm=aenv-latency-arm

# x86 vs arm
python scripts/aenv_latency_bench.py \
    --template arm=aenv-latency-arm --template x86=aenv-latency-x86

# also run a control (populate+measure with NO pause/resume) to prove the
# lazy-load delta is snapshot-induced: control first touch should ~= second touch
python scripts/aenv_latency_bench.py --template arm=... --control
```

## Variance handling (read if your numbers swing between runs)

The startup and lazy-load numbers are single-digit-ms to hundreds-of-ms — easily
perturbed by host noise. The harness is built to suppress the known jitter sources:

- **Warmup discard (`--warmup`, default 1).** The first cold create / snapshot
  resume after a fresh template load runs cold (template cache, host scheduler
  state) and is a consistent outlier. It is sampled on top of `--cold-samples` /
  `--snapshot-samples` and dropped from every summary. Raw samples still land in
  `report.json`.
- **Median headline, not mean.** cold/snapshot/lazy headlines are the **median**
  of the kept samples; the mean was pulled around by the long tail. A `p99` row
  follows each startup metric in the TSV/Markdown as a noise ceiling, and the
  console prints a `min..max` spread so you can *see* remaining jitter.
- **Lazy repeats (`--lazy-repeats`, default 3).** Each lazy mode runs its
  `populate → pause → resume → measure` cycle 3 times; first/second reported are
  the median across repeats (with the raw per-repeat samples in JSON). A single
  lazy observation had no variance estimate — this is the biggest lever.
- **Bigger n by default.** `--cold-samples` and `--snapshot-samples` default to 10
  (kept); bump higher for a tighter p99.

If jitter is still large after these, the remaining cause is the host, not the
harness: lock CPU governor to `performance`, fix/disable turbo frequency,
isolate cores for the sandbox vCPUs, and run with no neighbor load. On a shared
host you cannot control, the median + spread is the honest summary.

Output goes to `results/aenv_latency/<timestamp>/` (`report.json`, `report.tsv`,
`report.md`) plus a printed table. `report.tsv` is paste-ready for Excel — rows
are fixed-order so arm and x86 runs from separate machines paste-align row-by-row.

## Metric definitions (what the startup numbers actually measure)

- **VM cold start** = `Sandbox.create()` call + ready probe (`true` runs). This is
  "time until a command can execute in the sandbox", NOT pure kernel-boot time —
  it includes the E2B/AgentENV command-plane (websocat/ssh) handshake. Fine for
  x86-vs-arm (same ruler); not directly comparable to a customer number that may
  measure pure VM boot or include image pull/template build.
- **VM snapshot start** = `connect()` resume + ready probe, after `beta_pause()`.
- **Lazy-load first/second** = in-guest `latbench` first-touch vs second-touch of
  the 256MiB working set, at nanosecond source precision (printed to 1ns, table to
  1µs). The first-minus-second delta is the snapshot lazy-load debt.

## If shm doesn't survive resume

`latbench` defaults to POSIX shm (`/dev/shm`), which is tmpfs (guest RAM) and should
be captured + lazy-restored by the snapshot. If your AgentENV deploy clears tmpfs on
resume, `measure` will error (`shm_open: No such file`). Retry with file backing:

```bash
python scripts/aenv_latency_bench.py --template arm=... --backing file
```

Note: with `--backing file`, first-touch reflects page-cache/disk load, not pure
snapshot lazy-load — use it only as a fallback to confirm the mechanism.

## Profiling the resident path (L2/L3, EPT/TLB) with devkit / ksys / perf

The lazy-load first/second touches are 0.3–0.7ms — too short for a hardware
counter tool to sample. `latbench stress` populates the working set, warms it
with one traversal, then **loops the resident traversal N times** to give a
multi-second steady-state window. Pin a profiler to the **host VMM process**
(one firecracker/aenv vmm per sandbox) during that window to inspect the guest
resident-touch path: EPT/TLB walk, L2/L3 cache behavior. This is how you tell
whether the host backs guest RAM with 2M hugepages (→ EPT 2M, fast second-touch)
vs 4KiB (→ EPT thrash, slow — a candidate explanation for a slow sequential
second vs a fast one).

The harness drives this (it owns sandbox creation — there is no direct shell
into the sandbox):

```bash
# in one terminal: start the stress loop (prints the sandbox_id + VMM guidance)
python scripts/aenv_latency_bench.py --template arm=lat \
    --stress seq_read --stress-iters 3000

# on the AgentENV HOST, find the VMM PID for that live sandbox
FC_PID=$(pgrep -f 'firecracker|aenv|vmm' | head -1)

# verify the guest-RAM backing page size for that process
grep -E 'KernelPageSize|MMUPageSize|AnonHugePages' /proc/$FC_PID/smaps
#   MMUPageSize: 2048 kB  => host backs guest RAM with 2M hugepages => EPT 2M
#   MMUPageSize: 4 kB     => 4K => EPT 4K

# pin + lock frequency, then sample during the loop window
taskset -pc 8 $FC_PID
cpupower -c 8 frequency-set -g performance
# devkit_mem   -> L3 cache (mem_l3d_miss_percent), cache hierarchy
# ksys         -> ksys_l3_latency_avg (L3 latency)
# perf (lightweight alternative):
perf stat -p $FC_PID -I 1000 \
    -e cycles,cache-misses,dtlb_load_misses.walks,LLC-load-misses
# arm: `perf list | grep -iE 'tlb|walk|cache|llc'` for arch event names
```

**A/B to confirm a hugepage/EPT hypothesis:** disable host hugepages/THP and re-run
the stress profile. If `per_iter_ms` jumps (or the cache-miss / TLB-walk rate
climbs), host hugepage backing (EPT 2M) was what made the resident touch cheap.

```bash
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/shmem_enabled
# if /proc/meminfo HugePages_Total > 0, those are explicit hugepages the
# AgentENV/Firecracker mem-backend requested — reconfigure it to 4K;
# toggling THP alone will not change those.
```

Modes: `seq_read` / `seq_write` (full 4KiB per page — bandwidth-bound, the
case where hugepage/EPT 2M matters most) and `rand_read` / `rand_write` (one
8-byte per page — latency-bound, stresses TLB/EPT walk per page).
