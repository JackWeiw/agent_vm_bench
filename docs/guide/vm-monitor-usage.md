# vm_monitor Usage Guide

`vm_monitor` is the host-level resource monitoring tool for Agent VM Bench. It samples VM/VMM processes plus host `/proc` & `/sys` counters, and optionally drives external performance-collection tools (devkit, ksys, ub_watch, smap_bw, getfre) in parallel. Output: CSV, dark-themed SVG time-curves, and an aggregated `resource_report.xlsx`.

- Entry point: `vm-monitor` (`vm_monitor.cli:main`), registered by `pip install -e .`
- Direct module form: `python3 -m vm_monitor` (or `python3 vm_monitor.py` in legacy layout)
- Requires **Linux** (reads `/proc`, `/sys/devices/system/node`, `/sys/block`); **run as root** to read foreign processes' memory maps

## Prerequisites

```bash
# root is strongly recommended — numastat / numa_maps of other processes need it
sudo -i

# install (registers the vm-monitor console script + pulls core deps)
python -m pip install -e .
```

`vm_monitor` itself needs `psutil`, `pandas`, `openpyxl`, `PyYAML`, `python-dotenv` (declared in `pyproject.toml`). The external collection tools (devkit, ksys, …) are **not** pip packages — they are vendor/kernel tools you install separately and point at via `.env`.

## Modes

Three run modes (mutually exclusive on the timing/sync axis):

### Mode 1 — Stress-sync monitoring (wait for a benchmark, then sample)

Used by `bench-core` auto vm-monitor and batch schedulers. vm_monitor idles until a marker appears, then samples for `-t` seconds.

```bash
# file lock (bench-core's default sync mechanism)
sudo vm-monitor --stress-file /tmp/vm_benchmark_running.lock --vmm firecracker -t 300 -i 2

# or detect a stress process by name
sudo vm-monitor --stress-process auto_vm_test --vmm qemu -t 300 -i 2
```

### Mode 2 — Timer monitoring (sample now for N seconds)

```bash
sudo vm-monitor -t 60 -i 2 --vmm qemu
```

### Mode 3 — Timer + parallel log collection (`--enable-capture`)

Runs devkit/ksys/ub_watch/smap_bw/getfre as parallel subprocesses alongside the monitor:

```bash
sudo vm-monitor -t 300 -i 2 --vmm firecracker --enable-capture --auto-skip \
    --numa 2,3 --log-dir /data/test_run_1
```

`--auto-skip` makes missing tools silently disabled (no prompts) — for automated runs. For interactive use, drop `--auto-skip` and vm_monitor will prompt for each missing `.env` path.

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `--vmm` | `qemu` | VMM type: `qemu` or `firecracker` |
| `-t, --time` | `60` | Sampling duration (seconds) |
| `-i, --interval` | `2` | Sampling interval (seconds) |
| `--stress-process` | — | Wait for this process name, then sample |
| `--stress-file` | — | Wait for this lock file, then sample |
| `-o, --output` | — | Output filename prefix (default: `{qemu\|firecracker}_monitor`) |
| `--log-dir` | `logs_<ts>/` | Output directory |
| `--numa` | `all` | NUMA nodes to focus: `all` or comma-separated `0,1` |
| `--remote-numa` | `5` | Designated cross-socket "remote borrowing" node for free-mem watching; negative disables |
| `--disks` | `all` | Block devices for I/O: `all` (auto-discover physical) or `sda,nvme0n1` |
| `--enable-capture` | off | Run devkit/ksys/ub_watch/smap_bw/getfre in parallel |
| `--auto-skip` | off | Silently disable missing capture tools (non-interactive) |
| `--ksys-parse-timeout` | `600` | ksys parse-phase timeout (seconds) |
| `--no-svg` | off | Skip SVG time-curve reports |
| `--no-charts` | off | Skip the xlsx chart phase (sheets still written; faster on huge runs) |
| `--no-{collector}` | off | Skip a `/proc` collector or one devkit sub-tool (see below) |

## VMM types

| `--vmm` | Matched process names | Use case |
|---------|------------------------|----------|
| `qemu` | `qemu-kvm`, `qemu-system` | QEMU/KVM VMs (OpenStack) |
| `firecracker` | `firecracker` | Firecracker microVMs (E2B / AENV sandboxes) |

Each subclass extends `VMMonitorBase` and supplies `get_process_names()` / `extract_vm_id()` / `get_vms_realtime()`. To add a new VMM, subclass `VMMonitorBase` and register in `vm_monitor/__init__.py` + `vm_monitor/cli.py`.

## `.env` configuration

Loaded by `vm_monitor/config.py::load_env_config`. Place a `.env` in the cwd you launch `vm-monitor` from:

```env
# DevKit CLI (shared by devkit_top_down + devkit_mem)
DEVKIT_PATH=/path/to/devkit

# ksys — BOTH required, ksys skipped if either missing
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/ksys_config.yaml

# ub_watch
UB_WATCH_PATH=/path/to/ub_watch

# smap_bw (run via sudo for dmesg access)
SMAP_BW_PATH=/path/to/smap_bw.py

# getfre + its YAML config
GETFRE_PATH=/path/to/getfre
GETFRE_CONFIG_PATH=/path/to/getfre_config.yaml

# DevKit top-down CPU core range (optional; auto-computed from --numa sysfs cpulist)
DEVKIT_CPU_RANGE=96-191
```

### Key → tool mapping

| `.env` key | Drives | Required for | Notes |
|------------|--------|--------------|-------|
| `DEVKIT_PATH` | `devkit_top_down`, `devkit_mem` | DevKit sheets | shared binary; split with `--no-devkit-*` |
| `DEVKIT_CPU_RANGE` | `devkit_top_down` | top-down CPU scope | optional; auto from `--numa` |
| `KSYS_PATH` + `KSYS_CONFIG_PATH` | `ksys` | KSys sheet | **both required** |
| `UB_WATCH_PATH` | `ub_watch` | UBWatch sheets | optional |
| `SMAP_BW_PATH` | `smap_bw` | SMAPBW sheets | optional; sudo |
| `GETFRE_PATH` + `GETFRE_CONFIG_PATH` | `getfre` | Getfre sheets | optional; config auto-fills from host topology |

Missing-path behavior:
- **`--auto-skip` / bench-core auto vm-monitor**: the tool is silently disabled, a `[WARN]` is printed, and the run continues. The missing tool's sheet is simply absent from `resource_report.xlsx` — never a hard failure.
- **Interactive (no `--auto-skip`)**: vm_monitor prompts for each missing path, `skip` disables, valid paths are written back to `.env` via `python-dotenv`.

### getfre config (`getfre_config.yaml`)

A template ships at [config/tools/getfre_config.yaml](../../config/tools/getfre_config.yaml):

```yaml
getfre_path: /path/to/getfre    # executable (also overridable by GETFRE_PATH in .env)
total_cores: 192                # physical cores (omit to auto-detect from sysfs topology)
interval: 2                     # sampling interval (seconds)
core_interval: 1                # 1 = every physical core, 2 = every other
numa_nodes: [0, 1]              # omit to auto-discover
```

`ksys_config.yaml` and the devkit/ub_watch binaries are vendor tools — their config format is owned by those tools, not this repo. Point `KSYS_CONFIG_PATH` at whatever your ksys distribution ships.

## Log collection tools — exact invocations

`LogCapture` ([vm_monitor/log_capture.py](../../vm_monitor/log_capture.py)) launches each tool as a subprocess (stdout+stderr → its log file). Duration `-d` and interval `-i` mirror vm_monitor's `-t` / `-i`.

| Tool | Command vm_monitor runs | Output log | xlsx sheet(s) |
|------|-------------------------|------------|----------------|
| **devkit_top_down** | `$DEVKIT_PATH tuner top-down -d <t> -i 3 -c <cpu_range>` | `devkit_top_down.log` | DevKit_TopDown (13 metrics) |
| **devkit_mem** | `$DEVKIT_PATH tuner memory -d <t> -i 3` | `devkit_mem.log` | DevKit_Memory, NUMA_Bandwidth |
| **ksys** | `$KSYS_PATH collect -d <t> -i 3 -c $KSYS_CONFIG_PATH` | `ksys.log` | KSys (11 metrics) |
| **ub_watch** | `$UB_WATCH_PATH -t <t> -i 3` | `ub_watch.log` | UBWatch_Latency, UBWatch_Bandwidth |
| **smap_bw** | `sudo python3 $SMAP_BW_PATH --clear --duration <t> --timeout <t+10>` | `smap_bw.log` | SMAPBW_Summary, SMAPBW_Cycles |
| **getfre** | threaded per-NUMA: `$GETFRE_PATH <total_cores> <core_id>` per core | `getfre_NUMA{N}.log` | Getfre_Summary, Getfre_NUMA{N}, Getfre_Timeline_NUMA{N} |

`<cpu_range>` = `DEVKIT_CPU_RANGE`, or auto-computed from `--numa` via `/sys/devices/system/node/node{N}/cpulist`. If neither resolves, devkit_top_down is skipped (never launched with a wrong range).

### ksys two-phase lifecycle

ksys is the only tool with a long tail after data collection:

1. **Collect phase** — runs for `-d <duration>` seconds.
2. **Parse phase** — parses accumulated data; can take minutes on large samples.

vm_monitor tracks parse progress by scanning `ksys.log` markers:

| Marker in `ksys.log` | Status |
|----------------------|--------|
| `Starting to collect data` | collecting |
| `Starting to parse data` | parsing |
| `Starting to process and print data` / `CPU Metrics` / `Data saved successfully` | completed |

If parse exceeds `--ksys-parse-timeout` (default 600s), ksys is force-terminated and a warning is printed with suggestions: increase `--ksys-parse-timeout`, check `ksys.log` size, reduce the sampling interval. Progress is logged every 30s with 50/75/90% threshold warnings. All other tools use a simple `duration + 60s` timeout.

## Selective collectors (`--no-X`)

All collectors default **ON**. Disabling one leaves its history empty, so its sheet/SVG is omitted — graceful degradation, no downstream guard needed.

`/proc` collectors (target `base`):

| Flag | Skips | Sheet affected |
|------|-------|----------------|
| `--no-hugepage` | hugepage stats | Hugepage rows in Summary |
| `--no-numa-cpu` | per-NUMA CPU% | NUMA CPU lines |
| `--no-host-stats` | host CPU/mem | Host rows in Summary |
| `--no-swap` | swap usage + swap-in/out | Swap_Timeline |
| `--no-host-mem-detail` | cached/dirty/writeback | Host_Mem_Timeline |
| `--no-pressure` | page-cache pressure + iowait | Host_Pressure_Timeline |
| `--no-numa-memory` | per-NUMA meminfo | NUMA_Memory_Timeline |
| `--no-vm-total` | aggregated VM memory | VM_Total_Memory_Timeline |
| `--no-disk` | per-device disk I/O (1s sub-sample) | Disk_IO_Timeline |
| `--no-ublk` | ublk device count | Disk_IO_Timeline ublk column |

devkit split (target `devkit` — the two sub-tools share `DEVKIT_PATH`):

| Flag | Skips |
|------|-------|
| `--no-devkit-mem` | devkit tuner memory |
| `--no-devkit-topdown` | devkit tuner top-down |

`bench-core` forwards `monitor.skip` YAML tokens as `--no-{stem}`.

## Outputs

```
logs_<timestamp>/
├── qemu_monitor.csv         # raw per-sample VM records (or firecracker_monitor.csv)
├── summary.csv              # peak/avg summary
├── resource_report.xlsx     # aggregated Excel (all sheets below)
├── disk_io.svg              # dark-themed SVG time-curves
├── host_resources.svg
├── swap.svg
├── numa.svg
├── vm_total.svg
├── devkit_top_down.log      # raw tool logs (with --enable-capture)
├── devkit_mem.log
├── ksys.log
├── ub_watch.log
├── smap_bw.log
└── getfre_NUMA{N}.log
```

`resource_report.xlsx` sheets: `Summary`, `NUMA_Overview`, `VM_Stats`, `DevKit_TopDown`, `DevKit_Memory`, `NUMA_Bandwidth`, `KSys`, `UBWatch_Latency`, `UBWatch_Bandwidth`, `SMAPBW_Summary`, `SMAPBW_Cycles`, `Getfre_Summary`, `Getfre_NUMA{N}`, `Getfre_Timeline_NUMA{N}`, `Raw_VM_Data`, `Swap_Timeline`, `NUMA_Memory_Timeline`, `VM_Total_Memory_Timeline`, `Disk_IO_Timeline`, `Host_Mem_Timeline`, `Host_Pressure_Timeline`, `Host_CPU_Timeline`.

> The xlsx is written **last** deliberately — `bench-core`'s `MonitorController` polls for `resource_report.xlsx` as the "all artifacts written" signal and reaps the subprocess the moment it appears. SVG runs before xlsx so it isn't dropped by that reap.

## Python API

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

# QEMU
mon = QEMUMonitor()
mon.target_numa_nodes = [0, 1]
mon.start_monitoring(duration_seconds=60, interval_seconds=3)
mon.analyze_and_export("qemu.csv", "summary.csv")

# Firecracker
fc = FirecrackerMonitor()
fc.start_monitoring(duration_seconds=60, interval_seconds=3)

# Custom VMM
class MyMonitor(VMMonitorBase):
    def get_process_names(self): return ("my-vmm",)
    def extract_vm_id(self, pid, cmdline): return f"myvm-{pid}"
    def get_monitor_title(self): return "My VMM Monitoring"
    def get_no_vm_message(self): return "No running instances"
    def get_csv_filename_prefix(self): return "my_vmm"
    def get_vms_realtime(self): ...
```

## bench-core integration (auto vm-monitor)

`bench-core` wraps vm_monitor via `bench_core.monitor.MonitorController`. It is **auto-enabled** when the selected provider declares a `vmm_type` (e2b/aenv → firecracker; docker/fake → skipped). The monitor runs as a local subprocess, bracketing the active stress phase via a synced stress-file, and outputs to `<report.output_dir>/vm_monitor/`.

Relevant `monitor:` YAML block (peer of `report:`):

```yaml
monitor:
  enabled: auto          # auto | true | false (auto = on iff provider has vmm_type)
  merge_report: false    # false = vm_monitor resource_report.xlsx stands alone
                          # true  = copy host sheets into the replay obs workbook
  skip: []               # forward as --no-{stem}: ["disk", "devkit-topdown", ...]
  skip_charts: false     # forward as --no-charts
```

`--no-vm-monitor` on the CLI short-circuits it off. Missing binary/tools or an unwritable lock dir degrade to a warning + skip — never compromises the bench.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `[WARN] Recommended to run as root` | Rerun under `sudo`; numastat/numa_maps of other processes need root |
| `ksys parse timeout` | Increase `--ksys-parse-timeout`; reduce `-i`; check `ksys.log` size |
| devkit_top_down skipped | `DEVKIT_CPU_RANGE` unset and sysfs cpulist unreadable — set `DEVKIT_CPU_RANGE` in `.env` |
| A sheet is missing from xlsx | Its collector was disabled (`--no-X`) or its tool path was unset (`--auto-skip` disabled it) |
| No VMs detected | Wrong `--vmm`, or VM processes not yet started (stress-sync mode still waiting) |
| Disk I/O all zero | `--disks` filtered out physical devices, or no I/O during the window |

For full metric definitions (every sheet's columns + `/proc`/`/sys` sources & formulas), see [Metrics Reference](metrics-reference.md).
