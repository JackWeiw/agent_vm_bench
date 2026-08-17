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

- `latbench.c` — in-guest probe. `populate` / `measure <mode>` / `cleanup`.
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
```

Output goes to `results/aenv_latency/<timestamp>/report.json` plus a printed table.

## If shm doesn't survive resume

`latbench` defaults to POSIX shm (`/dev/shm`), which is tmpfs (guest RAM) and should
be captured + lazy-restored by the snapshot. If your AgentENV deploy clears tmpfs on
resume, `measure` will error (`shm_open: No such file`). Retry with file backing:

```bash
python scripts/aenv_latency_bench.py --template arm=... --backing file
```

Note: with `--backing file`, first-touch reflects page-cache/disk load, not pure
snapshot lazy-load — use it only as a fallback to confirm the mechanism.
