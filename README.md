# Agent VM Bench — Host-agnostic sandbox performance benchmarking

[中文说明](README_zh.md)

A performance-testing framework for virtualization scenarios. The **`bench_core`
kernel + `env_provider` abstraction** is the host-agnostic core: one stress
profile drives any sandbox backend (e2b / docker / future kata / agentenv) by
swapping `--provider`, and the same `config/common/*.yaml` runs on either
backend.

- **`src/bench_core/`** — the kernel: `run_benchmark` spine, stats / round-robin
  / task runners, `KernelConfig`. Host-agnostic — never imports a backend SDK.
- **`src/env_provider/`** — the `EnvironmentProvider` contract (ABC +
  `SandboxInstance`) + e2b / docker / fake provider impls (opt-in submodules;
  the contract stays SDK-free).
- **`config/common/`** — backend-agnostic workflow configs (each carries both an
  `e2b:` and `docker:` block; `--provider` selects which is read).

The frozen-legacy `e2b_bench/`, `docker_bench/`, and OpenStack (`vm_bench/`,
`auto_vm_test.py`, `batch_test_scheduler.py`) tools remain for existing users;
they share no code with the new kernel.

## Documentation

| Document | Description |
|----------|-------------|
| [bench-core Usage Guide](docs/bench-core-usage.md) | **src kernel benchmarking (recommended): install→config→CLI→cleanup** |
| [bench-core 使用指南 (中文)](docs/bench-core-usage-zh.md) | 中文版 src 内核压测指南 |
| [Design](docs/design.md) | System architecture and flow design |
| [Design (EN)](docs/design-en.md) | English version of design doc |
| [Metrics Reference](docs/metrics-reference.md) | bench_core + vm_monitor metrics, collection sources & formulas |
| [Usage Guide](docs/usage-guide.md) | Detailed tool usage and configuration |
| [vm_bench Usage](docs/vm_bench-usage-guide.md) | Modular vm_bench package (OpenStack) |
| [E2B Bench Usage](docs/e2b-bench-usage.md) | E2B Sandbox batch performance testing |
| [Docker Bench Usage](docs/docker-bench-usage.md) | Docker container browser automation testing |

## Contributing & Community

- [Contributing guide](CONTRIBUTING.md) — dev setup, tests, and how to open a PR
- [Code of Conduct](CODE_OF_CONDUCT.md) — standards for participation
- [Getting help](SUPPORT.md) — where to ask, report, or propose changes
- [RFC process](docs/rfcs/README.md) — for non-trivial design changes
- [Issue templates](.github/ISSUE_TEMPLATE/) — bug, feature, and performance-anomaly forms

---

## Quick Start (bench-core)

### 1. Install

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

The editable install registers `bench-core` and `python -m bench_core` — **no
`PYTHONPATH=src` needed** — and pulls the core deps (`psutil`, `paramiko`,
`flask`, `PyYAML`, `pandas`, `openpyxl`, ...) declared in `pyproject.toml`.
Backend SDKs are opt-in (install only the one you use; `fake` needs none):

```bash
pip install e2b       # --provider e2b
pip install docker     # --provider docker
```

Verify the kernel + CLI + config parse in one command (no SDK):

```bash
bench-core --provider fake --config config/common/browser.yaml --create-only -n 1
```

### 2. Config

`config/common/*.yaml` — one file per workflow, each carrying **both** `e2b:`
and `docker:` blocks. `--provider` decides which block is read, so **the same
stress profile runs on either backend**:

```yaml
workflow_type: browser        # browser | coding | document

e2b:                          # --provider e2b reads this block
  template: "openclaw-browser-v1"
  numa_bind: 2
  sandbox_ids_file: "sandboxs.txt"
  env: { ... }                # placeholder creds auto-fallback to ~/.e2b/config.json

docker:                       # --provider docker reads this block
  image: "ubuntu-openclaw-chromium:24.04-arm64"
  container_prefix: "oc-bench"
  cpu_limit: 2.0
  memory_limit: "2g"

# === shared stress sections (both backends read -> KernelConfig) ===
sandbox:      { total_count: 100 }
create_batch: { size: 20, interval: 3 }
test:         { duration: 160, benchmark_mode: "round_robin" }
```

| Config | Workflow | Notes |
|--------|----------|-------|
| `browser.yaml` | browser | round-robin tab-switch, 100 sandboxes |
| `coding-ts.yaml` | coding | TypeScript (vuejs/core), `npx tsx` verify |
| `coding-go.yaml` | coding | Go (gohugoio/hugo), `go run` verify |
| `coding-python.yaml` | coding | Python (django/django), `python3` verify |
| `docker.yaml` | browser | docker-only small profile (10 containers) |

Coding configs are minimal: `source_files` omitted → `KernelConfig` auto-fills
the canonical replacement pairs per `language`. Readiness is
**provider-transparent** (browser = port scan, coding = `uname -a`, document =
`document-bench-validate`) — no port/timing knobs in any YAML.

### 3. CLI

```text
bench-core --config <yaml> --provider {fake,e2b,docker} [mode/params]
```

| Flag | Description |
|------|-------------|
| `--provider` | `fake` (no SDK) / `e2b` / `docker` |
| `-n, --total-count` | override sandbox count |
| `--create-only` | create + ready-check + persist IDs, then exit (keep running) |
| `--detect` | reuse existing sandboxes (no create); no cleanup at end |
| `--warmup-only` | create/detect + warmup, then exit (keep running) |
| `--cleanup` | list + kill all existing sandboxes, then exit |
| `-bm, --benchmark-mode` | `fixed` / `round_robin` |
| `--test-duration` / `--benchmark-percent` / `--round-count` / `--round-size` | benchmark params |
| `-o, --output-dir` | report output dir |

> `bench-core: command not found`? It installs under the active interpreter's
> `Scripts/` (e.g. conda's) — activate that env, or use `python -m bench_core`.

### 4. Phase ladder

Validate tier-by-tier; `--create-only` and `--detect` both leave sandboxes
running, so finish with `--cleanup`.

```bash
# Tier 0 — fake (zero deps, validates the full kernel + report)
bench-core --provider fake --config config/common/browser.yaml    --test-duration 10 -n 3
bench-core --provider fake --config config/common/coding-ts.yaml  --test-duration 10 -n 3

# Tier 1 — docker (local daemon)
bench-core --provider docker --config config/common/browser.yaml --create-only -n 2
bench-core --provider docker --config config/common/browser.yaml --detect --warmup-only
bench-core --provider docker --config config/common/browser.yaml --detect --test-duration 30
bench-core --provider docker --config config/common/browser.yaml --cleanup

# Tier 2 — e2b (cloud firecracker)
bench-core --provider e2b --config config/common/coding-ts.yaml --create-only -n 2
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --warmup-only
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --test-duration 30
bench-core --provider e2b --config config/common/coding-ts.yaml --cleanup
```

| Command | Verifies | Sandbox fate |
|---------|----------|--------------|
| `--create-only` | create + ready + ID persistence | kept |
| `--detect --warmup-only` | detect + attach + warmup | kept |
| `--detect --test-duration N` | full spine + report | kept (detect doesn't cleanup) |
| `--cleanup` | list + teardown | removed |

See the [bench-core usage guide](docs/bench-core-usage.md) for the full
install→config→troubleshooting walkthrough, and the
[design doc](docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md)
for architecture.

---

## Legacy backends

The pre-`src/` tools are frozen and share no code with the kernel. Use them
only if you depend on their specific behaviour (batch scheduler, smap_tool /
vm_monitor integration):

| Backend | Entry | Guide |
|---------|-------|-------|
| E2B batch (legacy) | `python -m e2b_bench --config config/e2b/bench.yaml` | [E2B Bench Usage](docs/e2b-bench-usage.md) |
| Docker (legacy) | `python -m docker_bench --config config/docker/docker_bench.yaml` | [Docker Bench Usage](docs/docker-bench-usage.md) |
| OpenStack VM | `python -m vm_bench --config config/openstack/vm_bench.yaml` | [vm_bench Usage](docs/vm_bench-usage-guide.md) |
| OpenStack batch | `python3 batch_test_scheduler.py --config config/openstack/batch_config.yaml` | — |

### PDF / XLSX document benchmarks

The two document cases share the `openclaw-document-v1` E2B template and the
image under `dockerfile_build/document/`. Tokens / API key / `http://localhost:3000`
in `config/e2b/pdf_bench.yaml` and `config/e2b/xlsx_bench.yaml` are placeholders;
override on the CLI without echoing secrets:

```bash
read -rsp "E2B access token: " DOCUMENT_E2B_ACCESS_TOKEN; echo
read -rsp "E2B API key: " DOCUMENT_E2B_API_KEY; echo
read -rp "E2B API URL (e.g. http://SERVER_IP:3000): " DOCUMENT_E2B_API_URL

DOCUMENT_E2B_ARGS=(
  --e2b-access-token "${DOCUMENT_E2B_ACCESS_TOKEN}"
  --e2b-api-key "${DOCUMENT_E2B_API_KEY}"
  --e2b-api-url "${DOCUMENT_E2B_API_URL}"
  --e2b-http-ssl false --e2b-domain e2b.app
)

python -m e2b_bench -c config/e2b/pdf_bench.yaml  "${DOCUMENT_E2B_ARGS[@]}"
python -m e2b_bench -c config/e2b/xlsx_bench.yaml "${DOCUMENT_E2B_ARGS[@]}"
unset DOCUMENT_E2B_ACCESS_TOKEN DOCUMENT_E2B_API_KEY DOCUMENT_E2B_API_URL DOCUMENT_E2B_ARGS
```

---

## vm_monitor package

Unified monitoring framework for multiple VMM types (used by the legacy batch
scheduler; not yet integrated into the bench-core kernel). Installed by
`pip install -e .` as the **`vm-monitor`** console script. Sampling reads
`/proc` and `/sys`, so a real run needs a **Linux host** (Windows/macOS can
import the package and run `--help`, but collectors return nothing).

| VMM Type | Process Names | CLI Flag |
|----------|---------------|----------|
| QEMU | `qemu-kvm`, `qemu-system` | `--vmm qemu` (default) |
| Firecracker | `firecracker` | `--vmm firecracker` |

### CLI

`vm-monitor` is the entry point (`vm-monitor = "vm_monitor.cli:main"` in
`pyproject.toml`). There is **no `python -m vm_monitor`** entry — use the
console script.

```bash
# Timer mode — monitor 60s at 2s cadence (default vmm=qemu)
vm-monitor -t 60 -i 2 --vmm qemu

# Stress-sync — wait for a lock file, then monitor until it disappears
sudo "$(which vm-monitor)" --stress-file /tmp/bench_running.lock --vmm qemu

# With parallel log collection (devkit/ksys/ub_watch/smap_bw)
sudo "$(which vm-monitor)" -t 60 -i 2 --enable-capture --vmm qemu

# Firecracker microVMs
sudo "$(which vm-monitor)" --vmm firecracker -t 60 -i 2
```

> `sudo vm-monitor ...` may fail to find the script because `sudo` resets
> `PATH`. Use `sudo "$(which vm-monitor)"` (or `sudo -E`) so the venv's
> console script resolves. Reading `/proc`/`/sys`/`/dev/ublkb*` requires root.

Key flags:

| Flag | Purpose |
|------|---------|
| `--vmm {qemu,firecracker}` | VMM to monitor (default `qemu`) |
| `-t, --time` | Duration in seconds (default 60) |
| `-i, --interval` | Sampling interval in seconds (default 3) |
| `--numa 0,1` | NUMA nodes to report (default `1`) |
| `--remote-numa` | Designated "remote borrowing" NUMA node added to the focus set (default `5` — the platform's cross-socket borrowing node; negative disables) |
| `--disks` | Block devices for I/O deltas, comma-separated (e.g. `sda,nvme0n1`). `--disks all` (default) auto-discovers every physical block device on the host; virtual/software layers (loop, ram, sr, zram, md, dm) are excluded |
| `--stress-file` / `--stress-process` | Wait for a stress marker, then monitor |
| `--enable-capture` | Run devkit/ksys/ub_watch/smap_bw in parallel |
| `--no-svg` | Skip the dark-themed SVG time-curve reports |
| `--log-dir` | Output directory (default `logs_<timestamp>/`) |

### Outputs

- `<prefix>.csv`, `summary.csv` — per-sample raw + summary stats.
- `analysis_report.xlsx` — multi-sheet report (Summary, NUMA, VM stats,
  DevKit_TopDown/Memory, KSys, UBWatch, SMAPBW, Getfre, Swap/NUMA/VM-total
  timelines, **Disk_IO_Timeline**, **Host_Mem_Timeline**,
  **Host_Pressure_Timeline**) with charts.
- **SVG time curves** — dependency-free dark-themed `<polyline>` charts with
  grid/legend/threshold lines, grouped one concern per file so each renders
  as a clean PPT slide: `disk_io.svg` (read/write/util + ublk),
  `disk_latency.svg` (queue depth + await), `host_resources.svg`
  (CPU/iowait, mem, dirty+threshold, WB/cached/buffers), `host_pressure.svg`
  (page-cache pressure, anon/file cache, runnable/blocked), plus `swap.svg`,
  `numa.svg`, `vm_total.svg`. Skipped with `--no-svg` or when the source
  history is empty.

### Collected host metrics

Collected natively in `VMMonitorBase.collect_sample()` (no external process):

- **Disk I/O** — per-device read/write MB/s, busy %, inflight, **avg queue
  depth**, and **read/write await latency** from `/sys/block/<dev>/stat` deltas
  (512-byte sectors; busy capped at 100%; queue depth = weighted-I/O-ms).
- **Host memory detail** — Cached+SReclaimable / Buffers / Dirty / Writeback
  from `/proc/meminfo`; the dirty-writeback **throttle threshold** is read
  once from `/proc/sys/vm/dirty_bytes` (or `dirty_ratio`) and drawn as a
  dashed line on the Dirty time curve.
- **Page-cache pressure** — page-scan / page-reclaim / file-refault rates
  (MiB/s) from `/proc/vmstat` deltas (`pgscan_*` / `pgsteal_*` /
  `workingset_refault_file`); anonymous vs file cache (`Cached+Buffers-Shmem`)
  and `SReclaimable` from `/proc/meminfo`; `iowait%` (delta-based) plus
  runnable/blocked procs from `/proc/stat`.
- **ublk** — device count via `glob /dev/ublkb*`.
- Plus existing: per-VM CPU/memory/hugepage, host CPU/mem, per-NUMA
  CPU/memory/hugepage, swap, VM-total-memory aggregation.

### Python API

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor

QEMUMonitor().start_monitoring(duration_seconds=60, interval_seconds=3)
FirecrackerMonitor().start_monitoring(duration_seconds=60, interval_seconds=3)

# Render dark-themed SVG time-curve reports from any monitor instance
from vm_monitor.svg_exporter import export_svg_reports
export_svg_reports(QEMUMonitor(), "logs")
```
