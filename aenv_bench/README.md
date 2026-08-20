# AgentENV benchmark suite

Two standalone benchmarks for AgentENV sandboxes, sharing one image
(`ubuntu-aenv-latency-bench`, built under `dockerfile_build/aenv_latency/`):

| Script | Measures | Transport |
|---|---|---|
| `aenv_cold_boot_bench.py` | real cold boot (kernel boot from an OCI image) | `curl` (subprocess) |
| `aenv_latency_bench.py` | snapshot start + snapshot lazy-load memory latency | e2b SDK |

## Testing principles — why each bench exists and what it measures

The three things these benches measure correspond to three **different**
AgentENV launch paths. They are easy to conflate; this section pins them down
(verified against the AgentENV source).

### 1. Real cold boot — `POST /sandboxes-cold` → `start_fresh`

A cold boot pulls an OCI image and boots a fresh kernel. The path is:

```
POST /sandboxes-cold  (body: image, cpuCount, memoryMB, …)
  → SandboxLaunchSource::Image
  → for_create_fresh            (orchestrator/service.rs)
  → start_fresh                 (sandbox/firecracker/sandbox.rs)
      → UblkDeviceManager::create_overlaybd_runtime_device(...)
      → Firecracker boots a kernel from the ublk block device
```

The ublk device is backed by **overlaybd**. On the first cold boot of an
image, AgentENV resolves the OCI manifest, converts the OCI layers to overlaybd
format, and caches the converted commit on the local disk
(`$AENV_HOME/image-cache/commits`). Subsequent cold boots of the same image
read that local commit directly — the registry is only the origin for the
first conversion and an on-demand fallback for evicted blocks. The VM does
**not** boot from the registry; it boots from a local overlaybd block device.

`launch_sandbox` calls `wait_for_ready()` (envd ready) **before** returning the
`201`, so the POST round-trip already includes kernel boot + envd ready. There
is no separate ready probe to run — `create_ms` is "time until a command can
execute in the sandbox".

**What this measures:** end-to-end cold-boot latency of a fresh sandbox —
image resolve/convert (first run only), local block-device setup, kernel boot,
and envd handshake. It is the only one of the three paths that actually boots
a kernel.

### 2. Snapshot start — `POST /sandboxes` (template) → `for_create_from_snapshot` → `Resume`

```
POST /sandboxes  (template=…)
  → for_create_from_snapshot    (orchestrator/service.rs)
  → from_snapshot → Resume       (sandbox/firecracker/sandbox.rs)
```

This is a **snapshot load, not a boot**. Firecracker restores a paused VM from
its memory snapshot (`vm_state.bin`) and a rootfs snapshot. No kernel init, no
envd handshake-from-scratch — the VM resumes exactly where it was paused
(envd already up). That is why it is ~100 ms vs the cold boot's hundreds of ms.

**What this measures:** the cost of resuming a paused sandbox — snapshot
read + VM restore. This is the "VM snapshot start" row.

### 3. Snapshot lazy-load — in-guest `latbench` first-touch vs second-touch

When the sandbox was paused, its working set (256 MiB) was **populated inside
the sandbox** so the snapshot stores real page contents. On resume those pages
are restored **on demand** (lazy):

- **first touch** = page-in from the snapshot (slow) — the lazy-load debt
- **second touch** = already resident (fast)

The first-minus-second delta is the snapshot lazy-load cost. The in-guest
probe (`latbench`) measures this at nanosecond source precision; the harness
drives the `populate → pause → resume → measure` cycle (and repeats it to
suppress per-run jitter).

**What this measures:** the memory-latency cost the snapshot shifts from
start time into first use. First touch pays the deferred page-in; second
touch proves the page is now resident. Write modes use `MAP_SHARED`
(in-place write to the restored page, no copy-on-write) so first-write =
lazy restore + write, matching a payload that writes the restored anonymous
memory.

### Why cold boot uses `curl`, not the e2b SDK / `urllib`

The e2b SDK has no cold-boot entry — `AsyncSandbox.create(template=)` only
hits `POST /sandboxes` (the snapshot path). So cold boot must call
`POST /sandboxes-cold` directly. A `urllib`-based POST of that endpoint
consistently returned `HTTP 500 "Gateway Timeout"` at ~6.1 s, while the
byte-identical `curl` POST returned `201` in ~0.6 s. Root cause was not
pinned down and is not worth chasing; `curl` is the proven-working path, so
the cold-boot bench shells out to `curl` via `subprocess`.

### Why warmup is discarded

- **Cold boot (`--warmup 2`):** the first cold boot of an image pays the
  one-time OCI→overlaybd convert + layer fetch and is a consistent outlier.
  Discarding the leading samples leaves the steady-state cold boot (local SSD
  block read + boot + envd).
- **Latency bench (`--warmup 1`):** the first snapshot resume after a fresh
  template load runs cold (template cache, host scheduler state).

Raw samples (including warmup) always land in `report.json`; only the
headline median/p99 drop them.

## Run

```bash
export E2B_API_URL=http://127.0.0.1:8000
export E2B_API_KEY=e2b_000000   # auth not enforced yet; any dummy value works

# real cold boot, x86 vs arm
python aenv_bench/aenv_cold_boot_bench.py \
    --image x86=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-x86_64 \
    --image arm=127.0.0.1:6000/ubuntu-aenv-latency-bench:24.04-linuxarm64

# snapshot start + lazy-load latency
python aenv_bench/aenv_latency_bench.py \
    --template arm=aenv-latency-arm --template x86=aenv-latency-x86
```

Headlines are **median** (mean was pulled by the long tail); a `p99` row
follows each startup metric as a noise ceiling, and the console prints a
`min..max` spread so remaining jitter is visible. See each script's
`--help` for the variance knobs (`--samples`, `--warmup`, `--gap`,
`--lazy-repeats`, `--control`).

## Output

- Cold boot: `results/aenv_cold_boot/<ts>/{report.json,report.tsv,report.md}`
- Latency: `results/aenv_latency/<ts>/` (same trio)

`report.tsv` is paste-ready for Excel — rows are fixed-order so x86 and arm
runs from separate machines paste-align row-by-row.

## Image build

The shared image (Dockerfiles + the in-guest `latbench` probe) is built under
[`dockerfile_build/aenv_latency/`](../dockerfile_build/aenv_latency/) — see its
README for the build + `aenv pull` template-registration steps.
