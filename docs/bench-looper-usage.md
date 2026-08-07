# Bench Looper - Usage Guide (EN)

In-image benchmark looper for the **browser**, **coding-go**, and **coding-ts**
scenarios on openEuler ARM. It moves the host-side E2B-API single-sandbox
drivers (`e2b_bench/task_runner.py` for the browser workflow,
`e2b_bench/coding_task_runner.py` for the coding workflows) into the image, so
a container runs one scenario end-to-end via a single entry point.

This mirrors the document-bench in-image looper (`document-bench-pdf` /
`document-bench-xlsx`): the same `--loops` argument, the same
`<scenario>/<run-id>/{iterations.jsonl,summary.json}` results layout, and the
same `sleep infinity` default CMD. The host keeps the multi-container
orchestration (concurrency, `vm_monitor`, `smap_tool`, report aggregation) - this
looper is the single-container execution unit.

> Scope: openEuler **ARM** only (the slicing target). The x86 Dockerfiles stay
> dir-scoped and minimal (no looper).

## Prerequisites

Load the openEuler base tar (it is not on a pull-able registry):

```bash
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/aarch64/openEuler-docker.aarch64.tar.xz
xz -d openEuler-docker.aarch64.tar.xz
docker load -i openEuler-docker.aarch64.tar   # -> openeuler-24.03-lts-sp3:latest
```

## Build the images

All three ARM builds use `dockerfile_build/` as the build context (passed as
the last positional arg) so the shared `_bench_looper/bench_looper` package is
reachable for COPY. Run from the repo root:

```bash
# Browser (agent-browser + Playwright Chromium + looper)
docker build -f dockerfile_build/browser_openeuler/Dockerfile \
  -t openeuler-agent-browser:24.03-lts-sp3-linuxarm64 dockerfile_build/

# Coding (Go) - gohugoio/hugo + Go toolchain + looper
docker build -f dockerfile_build/coding/go/Dockerfile.openeuler \
  -t openeuler-coding-go-bench:24.03-lts-sp3-linuxarm64 dockerfile_build/

# Coding (TypeScript) - vuejs/core + node/pnpm/tsx + looper
docker build -f dockerfile_build/coding/ts/Dockerfile.openeuler \
  -t openeuler-coding-bench:24.03-lts-sp3-linuxarm64 dockerfile_build/
```

For a proxied build, add `--build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...`
(see each scenario README).

## Run a scenario end-to-end

Each image installs one entry point at `/usr/local/bin` and defaults to
`sleep infinity`. Two run forms are supported.

### One-shot (run and exit)

Each image installs one entry point at `/usr/local/bin` and defaults to
`sleep infinity`. Run end-to-end and exit with `docker run --rm`; mount a host
dir to `/results` and set `BENCH_RESULTS_DIR=/results` so JSON lands on the
host. Results go to `/results/<scenario>/<run-id>/{iterations.jsonl,summary.json}`.

Scenario -> image + entry point:

| Scenario   | Image                                              | Entry point        | Results dir          |
|------------|-----------------------------------------------------|--------------------|---------------------|
| browser     | openeuler-agent-browser:24.03-lts-sp3-linuxarm64      | `browser-bench`     | `/results/browser/`    |
| coding-go   | openeuler-coding-go-bench:24.03-lts-sp3-linuxarm64    | `coding-bench-go`   | `/results/coding-go/`  |
| coding-ts   | openeuler-coding-bench:24.03-lts-sp3-linuxarm64        | `coding-bench-ts`   | `/results/coding-ts/`  |

```bash
mkdir -p results

# Browser - needs host/bridge networking to reach the http.server (see below)
docker run --rm --network host --cpus=2 --memory=4g \
  -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-agent-browser:24.03-lts-sp3-linuxarm64 \
  browser-bench --loops 100

# Coding (Go) - offline (go run of stdlib-only verify scripts, no module fetch)
docker run --rm --cpus=2 --memory=4g \
  -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-coding-go-bench:24.03-lts-sp3-linuxarm64 \
  coding-bench-go --loops 100

# Coding (TypeScript) - offline (npx tsx resolves the pre-installed tsx)
docker run --rm --cpus=2 --memory=4g \
  -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-coding-bench:24.03-lts-sp3-linuxarm64 \
  coding-bench-ts --loops 100
```

Resource limits (`--cpus` / `--memory` above) cap each container; adjust per
host and slice. For overcommit/swap-out measurement, start N containers whose
`--memory` sum exceeds host RAM and let them swap. Useful flags:

| Flag                  | Purpose                                                |
|-----------------------|--------------------------------------------------------|
| `--cpus=N`             | CPU quota (N cores, fractional ok, e.g. 1.5).          |
| `--memory=4g`         | Memory hard limit (the overcommit lever).              |
| `--memory-swap=4g`    | Set = `--memory` to forbid extra swap, or larger to allow it. |
| `--cpuset-cpus=0-3`    | Pin to specific CPUs (CPU isolation for slicing).       |
| `--cpuset-mems=2`      | Pin memory to a NUMA node (matches the old numa_bind).  |
| `--memory-reservation=3g` | Soft memory limit (reclaim hint).                  |

Tip: pass `--run-id <name>` to name the results subdir explicitly (otherwise a
random id is used), and `--duration S` to stop after S wall-clock seconds
instead of a fixed loop count.

### Long-running container + `docker exec` (slicing)

The default CMD is `sleep infinity`, so start a long-running container and drive
it via `docker exec`. This is the form the slicing harness attaches to:

```bash
docker run -d --name g1 --cpus=2 --memory=4g \
  -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-coding-go-bench:24.03-lts-sp3-linuxarm64

docker exec g1 coding-bench-go --loops 100
# repeat with different --run-id / loop counts as needed
docker stop g1 && docker rm g1
```

For an N-container slicing run, start N such containers (names g1..gN) with
`--memory` summing past host RAM, then `docker exec` each. Pin to a NUMA node
with `--cpuset-mems=<node>` to reproduce the old `numa_bind` behavior.

### Browser networking

The browser scenario fetches pages from an external http.server
(default URLs point at `http://127.0.0.1:8080/...`, reachable with `--network
host` on a Linux host where the http.server runs on the same host). Run it with
host or
bridge networking - **not** `--network none` (unlike the document bench, the
page content is not bundled in the image). Override URLs with `--urls` /
`--warmup-urls`.

## CLI reference

Common flags:

| Flag             | Default | Description                                                  |
|------------------|---------|--------------------------------------------------------------|
| `--loops N`        | 20000   | Round count.                                                 |
| `--duration S`     | 0       | Wall-clock stop in seconds (0 = no limit).                   |
| `--warmup` / `--no-warmup` | on  | One-time warmup before the loop (excluded from results).     |
| `--quiet`         | off     | Suppress per-round INFO logs (failures + final summary still logged). Use for slicing when you want minimal looper footprint. |
| `--results-dir PATH` | `$BENCH_RESULTS_DIR` or `/opt/bench-looper/results` | Host-visible results dir (mount + env). |
| `--run-id ID`      | random  | Run subdir name (defaults to a random 12-char id; or `BENCH_RUN_ID`). |

Scenario-specific:

| Scenario   | Extra flags                                                     |
|------------|-----------------------------------------------------------------|
| browser     | `--urls URL...`, `--warmup-urls URL...` (default: baked `browser_urls.json`) |
| coding-go   | `--skip-verify`, `--verify-timeout S` (go ignores `--verify-repeat`; N=1 cold-compile) |
| coding-ts   | `--skip-verify`, `--verify-timeout S`, `--verify-repeat N` (default 3: N chained `npx tsx` per verify) |

Environment variables:

| Var                 | Default                       | Purpose                          |
|---------------------|-------------------------------|----------------------------------|
| `BENCH_RESULTS_DIR`   | `/opt/bench-looper/results`     | Results root (mount a host dir here). |
| `BENCH_RUN_ID`        | (random)                       | Fixed run subdir name.           |

Exit code: `0` if every iteration succeeded, `1` if any failed. A single failed
iteration does **not** abort the remaining loops.

## Results format

Each run writes to `<results_dir>/<scenario>/<run_id>/`:

```
results/coding-go/<run-id>/
├── iterations.jsonl   # one JSON object per round
└── summary.json        # aggregate counts + per-step percentiles
```

### `iterations.jsonl` (one line per round)

```json
{"iteration": 0, "scenario": "coding-go", "round": 0, "total_ms": 1234.0,
 "success": true, "failed_step": null, "error_type": null, "error_message": null,
 "timed_out": false, "verify_success": true, "compile_only": false,
 "steps": {"find": 12.3, "read": 4.1, "edit": 8.2, "verify_clean": 90.0, "verify": 1100.0, "diff": 5.0}}
```

| Field           | Meaning                                                       |
|-----------------|---------------------------------------------------------------|
| `total_ms`        | Wall-clock time for the whole round.                          |
| `success`         | Round succeeded (all fatal steps ok).                         |
| `failed_step`     | `find`/`read`/`edit`/`verify`/`diff`/`open_tab`/.../`exception`, or null. |
| `error_type`       | `timeout` / `exit_code` / `exception`, or null.              |
| `error_message`    | Truncated stderr/stdout or exception text.                    |
| `timed_out`        | True if a step hit its subprocess timeout.                   |
| `verify_success`   | Verify step passed (coding).                                 |
| `compile_only`     | Verify passed via a compile-only check (no assertion).       |
| `steps`           | Per-step elapsed ms. Coding: `find,read,edit,verify[,verify_clean],diff`. Browser: `open_tab,page_load,snapshot,click,screenshot`. |

### `summary.json`

```json
{
  "scenario": "coding-go",
  "run_id": "a1b2c3",
  "loops_requested": 100,
  "loops_completed": 100,
  "success_count": 99,
  "failure_count": 1,
  "success_rate": 0.99,
  "total_duration_s": 123.4,
  "steps": {
    "verify": {"count": 100, "avg_ms": 1100.0, "p50_ms": 1080.0, "p95_ms": 1450.0, "p99_ms": 1620.0}
  },
  "failures": [{"failed_step": "verify", "error_type": "exit_code", "count": 1}]
}
```

The host-side aggregator (when added) reads these uniformly across all three
scenarios - the schema is the same as the document-bench contract.

## Scenario details

### Browser (`browser-bench`)

Per round (round-robin over the benchmark URLs): open a NEW tab → wait for
networkidle → DOM snapshot → click first element ref → screenshot. Tabs are
never closed, so memory grows per round (the intended memory-pressure model).
Warmup opens one tab per warmup URL (snapshot→click→screenshot on each).

Step order: `open_tab, page_load, snapshot, click, screenshot` (click and
screenshot are non-fatal).

### Coding-Go (`coding-bench-go`)

Per round: `git checkout` reset → `head -20` read → literal find→replace edit →
`go clean -cache` + write ad-hoc test + `go run` verify → `git diff` patch.

Step order: `find, read, edit, verify_clean, verify, diff`. The per-pair
`verify_script` (GitHub Alert case-insensitivity regex) is baked from
`coding_go_bench.yaml`; pairs without one use the shared Go default (compiles +
runs a no-op `package main`). The hugo module graph is intentionally NOT
pre-downloaded (verify scripts import only the Go stdlib).

### Coding-TS (`coding-bench-ts`)

Same skeleton; verify = N independent `npx tsx /tmp/bench_verify_{i}.mjs`
processes chained with `&&` (fail-fast). Each body is stamped from the
`DEFAULT_VERIFY_TEMPLATES` pool (6 compiler-core baseParse cases), offset by
`round_id % pool_len` so consecutive rounds differ. The 8 agent globals + the
compiler-core dynamic import are verbatim the captured openclaw trajectory.

Step order: `find, read, edit, verify, diff` (no `verify_clean`; ts/esbuild has
no persistent compile cache).

## Slicing notes

- The looper is the **single-container execution unit**. Multi-container
  concurrency, NUMA binding, `vm_monitor`/`smap_tool`, and P50/P95/P99 report
  aggregation remain host-side concerns (see `e2b_bench/batch_scheduler.py` /
  `report_aggregator.py` for the E2B equivalent). To slice, start N long-running
  containers and drive each with `docker exec <name> <entry-point> --loops ...`.
- `--loops 20000` (default) keeps a single container producing load for a long
  slicing window; use `--duration S` to bound it by wall-clock instead.
- Each `docker exec` invocation creates a fresh `<run-id>` results subdir; pass
  `--run-id` to name it explicitly.
