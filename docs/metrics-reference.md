# Metrics Reference

This document describes every metric produced by the **two active instrumentation paths** in Agent VM Bench:

- **Part I — `bench_core` kernel** (`src/bench_core/`, `bench-core` CLI): the host-agnostic stress kernel. Sandbox **creation timing** + **workflow task metrics** (browser / coding / document).
- **Part II — `vm_monitor`** (`vm_monitor/`, `vm-monitor` CLI): host-level VMM monitoring. Real-time Excel sheets + dark-themed SVG time-curve reports covering host CPU/memory, disk I/O, NUMA, swap, page-cache pressure, and (when enabled) external hardware profilers.

The frozen legacy OpenStack **batch summary** (`batch_summary_*.xlsx`, produced by `batch_test_scheduler.py`) is intentionally **not** covered here — it aggregates cross-run and is retained only for existing OpenStack users. See `usage-guide.md` for that path.

---

## Table of contents

**Part I — `bench_core` kernel**
- [1.1 Creation timing report (`--create-only`)](#11-creation-timing-report---create-only)
- [1.2 Data model fields](#12-data-model-fields)
- [1.3 Workflow task metrics](#13-workflow-task-metrics)
- [1.4 Round-robin comparison](#14-round-robin-comparison)
- [1.5 Final benchmark report structure](#15-final-benchmark-report-structure)

**Part II — `vm_monitor` host-level metrics**
- [2.1 Always-on sheets](#21-always-on-sheets)
- [2.2 Self-collected timeline sheets](#22-self-collected-timeline-sheets)
- [2.3 External hardware profiler sheets](#23-external-hardware-profiler-sheets---enable-capture)
- [2.4 Embedded Excel charts](#24-embedded-excel-charts)
- [2.5 SVG reports](#25-svg-reports)
- [2.6 Per-sample history field reference](#26-per-sample-history-field-reference)
- [2.7 Collection sources & calculation formulas](#27-collection-sources--calculation-formulas)

**Analysis**
- [3. Using metrics for bottleneck identification](#3-using-metrics-for-bottleneck-identification)

---

## Part I — `bench_core` kernel metrics

The kernel produces a single text report per run, written to the config's `report.output_dir` as `{filename_prefix}_{YYYYMMDD_HHMMSS}.txt`. `--create-only` mode emits a shorter creation-timing report and exits. The full workflow run emits the complete report (sections 1.1–1.5).

## 1.1 Creation timing report (`--create-only`)

Emitted by `bench.py::_create_only_report()`. Three percentile sections + a status block.

### [Sandbox Status]

| Label | Meaning |
|-------|---------|
| `Total:` | Number of sandboxes requested |
| `Ready:` | Count reaching `SandboxStatus.READY` |
| `Create Failed:` | Count of `SandboxStatus.FAILED` |
| `Ready Check Failed:` | Count of `SandboxStatus.READY_FAILED` |
| `Create Failed IDs:` | First 10 failed sandbox indices (only when failures exist) |
| `Ready Failed IDs:` | First 10 ready-failed indices (only when ready-failed exist) |

### Percentile sections

Three sections share this format:

```
Min: {min:.1f}s  Max: {max:.1f}s  Avg: {avg:.1f}s
P50: {p50:.1f}s  P95: {p95:.1f}s  P99: {p99:.1f}s
```

| Section | Field | Population |
|---------|-------|------------|
| `[Sandbox.create Performance]` | `creation_metrics.create_elapsed` | All non-failed/non-pending/non-creating instances. Described as *"create API call time, excluding ready check"*. |
| `[Ready Check Wait Performance]` | `creation_metrics.ready_check_elapsed` | `READY` instances only. |
| `[Total Startup Performance]` | `creation_metrics.total_elapsed` | `READY` instances only. *"sandbox.create + ready check"*. |

The full benchmark report (non-create-only) renames the ready-check section per workflow — see [1.5](#15-final-benchmark-report-structure).

## 1.2 Data model fields

### `CreationMetrics` (`env_provider/__init__.py`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `submit_time` | `float` | Wall-clock when creation was submitted |
| `ready_time` | `float` | Wall-clock when readiness was achieved |
| `create_elapsed` | `float` | Create API call time, excluding the readiness check |
| `ready_check_elapsed` | `float` | Time waiting for readiness (port probe / command / validate) |
| `total_elapsed` | `float` | `submit → ready` = `create_elapsed + ready_check_elapsed` |
| `status` | `SandboxStatus` | `PENDING` / `CREATING` / `CREATED` / `READY` / `ACTIVE` / `FAILED` / `READY_FAILED` / `OFFLINE` / `KILLED` |
| `error` | `str` | Creation error message |
| `ready_check_error` | `str` | Readiness-check error message |

### `BenchSandbox` (`schemas.py`) — extends `SandboxInstance`

| Attribute | Type | Notes |
|-----------|------|-------|
| `workflow_type` | `str` | `"browser"` / `"coding"` / `"document"` |
| `browser_metrics` / `coding_metrics` / `document_metrics` | `*Metrics` | The `.task_metrics` property returns the one matching `workflow_type` |
| `stopped_by_cleanup` | `bool` | Distinguishes clean teardown from runtime offline |
| `consecutive_failures` | `int` | |
| `last_task_time` | `float` | |
| `tab_ids` | `list[str]` | |

### `Snapshot` (`schemas.py`) — one sample from `stats_collector`

| Field | Type | Notes |
|-------|------|-------|
| `timestamp` / `elapsed` | `float` | Wall-clock + seconds since benchmark start |
| `total_sandboxes` / `active_sandboxes` / `offline_sandboxes` | `int` | |
| `creation_stats` | `dict` | `{"create": {...}, "ready_check": {...}, "total": {...}}`; each sub-dict has `min`/`max`/`avg`/`p50`/`p95`/`p99`. Internal — not printed verbatim. |
| `browser_total` / `browser_success` / `browser_avg_latency` / `browser_p99_latency` | `int`/`float` | |
| `coding_total` / `coding_success` / `coding_verify_success` / `coding_compile_only` / `coding_avg_latency` / `coding_p99_latency` | `int`/`float` | `coding_verify_success` = pairs whose real-assertion verify script passed; `coding_compile_only` = compiled+ran with no assertion |
| `document_total` / `document_success` / `document_avg_latency` / `document_p99_latency` | `int`/`float` | |
| `round_total` / `round_success` | `int` | Round-robin mode |

## 1.3 Workflow task metrics

Every workflow's metrics object extends `TaskMetricsBase`, providing: `total_tasks`, `success_count`, `failed_count`, `timeout_count`, `latencies`, `last_error`, `avg_latency`, `p99_latency`. The report prints a `[<Workflow> Task Statistics]` block + a `[Step-Level Timing (... Mode)]` table per workflow.

### Browser (`[Browser Task Statistics]`)

| Label | Meaning |
|-------|---------|
| `Total Tasks:` | Sum of `total_tasks` |
| `Success:` | Sum of `success_count` |
| `Failed:` | `{total_failed} (timeout: {total_timeout})` |
| `Success Rate:` | `{rate:.1f}%` |
| `Avg Latency:` | `{avg_ms:.1f}ms` |
| `P99 Latency:` | `{p99_ms:.1f}ms` |

**Step order** (`[Step-Level Timing (Tab-Switch Mode)]`): `open_tab` → `page_load` → `snapshot` → `click` → `screenshot`. Columns: `Step`, `Count`, `Avg(ms)`, `P50(ms)`, `P95(ms)`, `P99(ms)`, `Tail`.

> **Note (fixed mode only):** `BrowserTaskRunner._run_single_task` adds a flat `+10 s` to elapsed time to simulate LLM response latency. The round-robin `TabOperationRunner` does **not** add this offset. Browser latency in fixed mode is therefore ~10 s higher per task than in round-robin mode.

### Coding (`[Coding Task Statistics]`)

Same base labels as browser, plus:

| Label | Meaning |
|-------|---------|
| `Verify Success:` | `{verify_success}/{total_tasks} ({rate:.1f}%) [assert: {verify_success}, compile-only: {compile_only}]` |

**Step order** (`[Step-Level Timing (Coding Mode)]`): `find` → `read` → `edit` → `verify` → `diff`.

> **Note:** For the Go language, a `verify_clean` step (`go clean -cache`) is timed into `step_times` but is **not** part of `CODING_STEP_ORDER`, so it appears in raw per-step data and round-robin per-round logs, but is excluded from the step-level timing table.

### Document (`[Document Task Statistics]`)

Same base labels as browser, plus a `Case Kind:` row (`config.document_case_kind`).

**Step order** is phase-driven, one row per phase:

- **XLSX**: `XLSX-P01-inspect_prepare`, `XLSX-P02-build`, `XLSX-P03-process_publish`, `XLSX-P04-verify_deliver`
- **PDF**: `PDF-P01-inspect_prepare`, `PDF-P02-build`, `PDF-P03-process_publish`, `PDF-P04-verify_deliver`

The step-level table title is `[Step-Level Timing (Document {XLSX|PDF} Mode)]`.

### Tail-ratio column

The `Tail` column in every step-level table is `P99/P50`, classified as:

| Ratio | Severity |
|-------|----------|
| `< 1.2x` | minimal |
| `1.2x – 1.5x` | moderate |
| `> 1.5x` | significant |

> **Note:** `last_error` on each metrics object holds only the *most recent* error per sandbox (overwritten on each failure), so the error classification reflects the last error, not a full history.

## 1.4 Round-robin comparison

`RoundRobinTaskManager` logs a per-round line at round end:

```
[Round {round_id}] Completed: {runner_count} sandboxes, avg: {step_name}={avg_ms:.0f}ms, ...
```

The final report adds a `[Round Comparison]` table (round-robin mode only):

| `Round` | `Tasks` | `Success%` | `Avg(s)` | `P50(s)` | `P95(s)` | `P99(s)` | `Tail` |

Footer: `Summary: {total_tasks} tasks across {active_rounds} rounds`. The `Tail` column uses the same severity bands as step-level tables.

## 1.5 Final benchmark report structure

Assembled by `stats_collector.generate_report()` in this order:

1. **`[Test Configuration]`** — `Backend:`, `Total Sandboxes:`, `Workflow:`/`Document Case:` (document only), `Mode:` (`Detect existing sandboxes` / `Create-only (Phase 0)` / `Full workflow`), `Create Batch:`/`Create Interval:` (or `Full concurrent creation`), `Task Batch:`/`Task Interval:` (or `Full concurrent start`), `Test Duration:`.
2. **`[Sandbox Status]`** — `Created (API):`, `Command Ready:` (coding/document) or `Ports Ready:` (browser), `Create Failed:`, `Ready Check Failed:`/`Port Check Failed:`, `Offline (runtime):`, plus conditional failed/offline ID lists.
3. **`[Sandbox.create Performance]`** — percentile block; *"create API call time, excluding ready check"* (coding/document) or *"…excluding port wait"* (browser).
4. **Ready-check percentile block** — title varies: `[Ready Check Wait Performance]` (coding, *"Waiting for 'uname -a' command response"*) / `[Document Asset Check Performance]` (document, *"Running document-bench-validate inside the sandbox"*) / `[Port Check Wait Performance]` (browser, *"Waiting for 18789 openclaw-gateway + 11436 llama-server ports"*).
5. **`[Total Startup Performance]`** — *"sandbox.create + ready check"* (coding/document) or *"sandbox.create + port wait"* (browser).
6. **Workflow task stats** — `[<Workflow> Task Statistics]` + `[Step-Level Timing (... Mode)]` (see [1.3](#13-workflow-task-metrics)).
7. **`[Failed Sandbox Error Details]`** + **`[Error Type Classification]`** — only when task failures exist. Top-10 sandboxes by failure count + a classification table (`Error Type` | `Count` | `Sandboxes`). Per-workflow error display order:
   - Browser: `Open tab failed`, `Page load failed`, `Snapshot failed`, `Click failed`, `Screenshot failed`, `Chrome start failed`, `D-Bus connection error`, `Gateway connection error`, `Sandbox unreachable`, `Timeout`, `Other`
   - Coding: `Checkout failed`, `Edit failed`, `Verify failed`, `OOM`, `Sandbox unreachable`, `Timeout`, `Other`
   - Document: `Read failed`, `Write failed`, `Verifier failed`, `Timeout`, `Other`
   - Error types not in the workflow's display list fold into `Other` rather than being dropped.
8. **`[Round Comparison]`** — round-robin mode only (see [1.4](#14-round-robin-comparison)).

---

## Part II — `vm_monitor` host-level metrics

`vm-monitor` writes two artifact families into its `--log-dir` (default `logs_<timestamp>/`):

- **`resource_report.xlsx`** — the sheets below (sections 2.1–2.3) + embedded charts (2.4).
- **`*.svg`** — dark-themed time-curve reports (2.5).

Sheets are **conditional**: a sheet is written only when its source history (or, for profiler sheets, the parsed tool log) is non-empty. Column/header strings below are exact.

## 2.1 Always-on sheets

### `Summary` (always written) — columns `Metric` | `Value` | `Unit`

| Metric | Unit | Condition |
|--------|------|-----------|
| `Test Date` | | |
| `Duration` | | |
| `Sampling Interval` | seconds | |
| `NUMA Nodes` | | |
| `Host Avg CPU` | % | only if `host_cpu_history` non-empty |
| `Host Peak CPU` | % | only if `host_cpu_history` non-empty |
| `Host Avg Memory` | MB | only if `host_cpu_history` non-empty |
| `Host Peak Memory` | MB | only if `host_cpu_history` non-empty |
| `Hugepage Total` | MB | |
| `Hugepage Avg Used` | MB | |
| `Hugepage Peak Used` | MB | |
| `Hugepage Peak Usage %` | % | |
| `Swap Total` | MB | only if `swap_history` non-empty |
| `Swap Avg Used` | MB | swap conditional |
| `Swap Peak Used` | MB | swap conditional |
| `Swap Peak Usage %` | % | swap conditional |
| `Swap Cached Avg` | MB | swap conditional |
| `Swap Cached Peak` | MB | swap conditional |
| `Swap Cached Avg Ratio %` | % | swap conditional |
| `Swap Avg In Rate` | MiB/s | swap conditional |
| `Swap Avg Out Rate` | MiB/s | swap conditional |
| `Swap Peak In Rate` | MiB/s | swap conditional |
| `Swap Peak Out Rate` | MiB/s | swap conditional |
| `Swap Total In (MiB)` | MiB | swap conditional |
| `Swap Total Out (MiB)` | MiB | swap conditional |
| `Total VMs` | | |
| `Alive VMs at End` | | |
| `VM Avg CPU` | % | |
| `VM Peak Total CPU` | % | |
| `Total Avg Memory` | MB | |
| `Disk Write Peak` | MB/s | |
| `Dirty Avg` | MB | |
| `Dirty Peak` | MB | |
| `Writeback Peak` | MB | |
| `ublk Devices Peak` | | |
| `Page Scan Peak` | MiB/s | |
| `Page Reclaim Peak` | MiB/s | |
| `File Refault Peak` | MiB/s | |

### `NUMA_Overview` — one row per NUMA node

Consolidates what were previously three separate sheets; there is **no** `NUMA_CPU`, `NUMA_Memory`, or `Hugepage_Per_NUMA` sheet.

| Column | Source |
|--------|--------|
| `NUMA Node` | node id |
| `Avg CPU (%)` | `numa_cpu_history` mean |
| `Peak CPU (%)` | `numa_cpu_peak` |
| `Avg Used (MB)` | `numa_memory_history` used mean |
| `Peak Used (MB)` | `numa_memory_history` used max |
| `Avg Usage (%)` | `numa_memory_history` usage mean |
| `HP Avg Total (MB)` | `hugepage_per_numa` total_mb mean |
| `HP Avg Used (MB)` | `hugepage_per_numa` used_mb mean |
| `HP Avg Usage (%)` | `hugepage_per_numa` usage_pct mean |

### `VM_Stats` — one row per detected VM

`VM Name` | `PID` | `Samples` | `Avg CPU (%)` | `Max CPU (%)` | `Avg Memory (MB)` | `Max Memory (MB)` | `Avg Hugepage (MB)`

### `Raw_VM_Data` — per-sample, per-VM raw rows

`Timestamp` | `VM Name` | `PID` | `CPU (%)` | `Memory (MB)` | `Hugepage (MB)`

## 2.2 Self-collected timeline sheets

One row per sampling interval. These sheets do **not** require external tools — `vm_monitor` reads `/proc` and `/sys` directly.

### `Disk_IO_Timeline`

Per-device columns are emitted in `monitor.target_disks` order (auto-discovered from `/sys/block` by default; override with `--disks`). 7 columns per device:

`Timestamp` · `{dev} Read (MB/s)` · `{dev} Write (MB/s)` · `{dev} Util (%)` · `{dev} Inflight` · `{dev} Queue Depth` · `{dev} Read Await (ms)` · `{dev} Write Await (ms)` · `ublk Devices`

> Sectors→MiB uses `_SECTOR_SIZE_BYTES = 512` (kernel block-layer stat is always 512-byte sectors). Virtual/software layers (`loop`/`ram`/`sr`/`zram`/`md`/`dm`) are excluded from auto-discovery.

### `Host_Mem_Timeline`

`Timestamp` · `Cached (MB)` · `Buffers (MB)` · `Dirty (MB)` · `Writeback (MB)`

> `Cached (MB)` here is `/proc/meminfo` `Cached + SReclaimable` (slab reclaimable is folded into the cached total), **not** the raw `Cached` field.

### `NUMA_Memory_Timeline` — focus nodes only

Only **focus** NUMA nodes are emitted: the CLI `--numa` list + the remote-borrowing node (`--remote-numa`, default `5` — the platform's designated cross-socket borrowing node; pass a negative value to disable). Non-focus nodes are dropped. 7 columns per focus node `{N}`:

`Timestamp` · `NUMA{N} Total (MB)` · `NUMA{N} Used (MB)` · `NUMA{N} Free (MB)` · `NUMA{N} Available (MB)` · `NUMA{N} SwapCache (MB)` · `NUMA{N} AnonPages (MB)` · `NUMA{N} Usage (%)`

> The per-NUMA meminfo collector also gathers `Active`/`Inactive`/`FilePages`, but the sheet exporter does **not** emit them — only the 7 fields above per node.

### `Swap_Timeline`

`Timestamp` · `Swap Used (MB)` · `Swap Free (MB)` · `Swap Cached (MB)` · `Swap Cache Ratio (%)` · `Swap In Rate (MiB/s)` · `Swap Out Rate (MiB/s)` · `NUMA{N} SwapCache (MB)` (one per NUMA id found in `numa_memory_history`, sorted)

> There is **no** `Swap_Stats` sheet — swap aggregates live on the `Summary` sheet. Per-NUMA SwapCache is joined by sample index; if `numa_memory_history` is shorter than `swap_history`, trailing rows are filled with `0`.

### `Host_Pressure_Timeline` — only if `host_pressure_history` non-empty

`Timestamp` · `Page Scan (MiB/s)` · `Page Reclaim (MiB/s)` · `File Refault (MiB/s)` · `Anon Pages (MB)` · `File Cache (MB)` · `SReclaimable (MB)` · `IOWait (%)` · `Procs Running` · `Procs Blocked`

> There is **no** `Pressure`/`Host_Pressure`/`Host_CPU`/`Host_Resources` sheet — host CPU/memory totals are on `Summary`; host memory detail is `Host_Mem_Timeline`; pressure is `Host_Pressure_Timeline`.

### `VM_Total_Memory_Timeline`

`Timestamp` · `VM Total Memory (MB)` · `VM Count` · `NUMA{N} VM Memory (MB)` (one per NUMA id found across all samples' `per_numa`, sorted)

> The history entry also carries `swapcache_mb` / `swapcache_per_numa`, but the sheet does **not** emit them.

## 2.3 External hardware profiler sheets (`--enable-capture`)

These sheets are written only when the corresponding tool log exists in `--log-dir` (tools run in parallel via `LogCapture` when `--enable-capture` is passed). Tool paths come from `.env` (`DEVKIT_PATH`, `KSYS_PATH`, `UB_WATCH_PATH`, `SMAP_BW_PATH`, `GETFRE_PATH`). See [Usage Guide – Log Collection Tools](usage-guide.md#log-collection-tools).

### `DevKit_TopDown` — columns `Metric` | `Value` | `Report Count`

CPU pipeline top-down analysis (13 rows):

| Metric string | Meaning |
|---------------|---------|
| `Cycles Avg` | Average CPU cycles |
| `Instructions Avg` | Average instructions retired |
| `IPC Avg` / `IPC Max` / `IPC Min` | Instructions-per-cycle |
| `Bad Speculation (%)` | Branch-prediction failures |
| `Frontend Bound (%)` | Instruction fetch/decode bottleneck |
| `Retiring (%)` | Useful work completed |
| `Backend Bound (%)` | Execution-backend stalls |
| `L3 Bound (%)` | L3 cache latency |
| `Mem Bound (%)` | Memory-access bottleneck |
| `Latency Bound (%)` | Memory-latency bound |
| `Bandwidth Bound (%)` | Memory-bandwidth bound |

A companion `TopDown_Timeline` sheet carries per-sample columns verbatim from the parsed `devkit_top_down` timeline dict (dynamic).

### `DevKit_Memory` — columns `Metric` | `Value` | `Report Count`

| Metric string | Meaning |
|---------------|---------|
| `L1D Miss (%)` / `L1I Miss (%)` | L1 data/instruction cache miss rate |
| `L2D Miss (%)` / `L2I Miss (%)` | L2 data/instruction cache miss rate |
| `DDR Write (MB/s)` / `DDR Read (MB/s)` | System DDR bandwidth |
| `NUMA{N} L3 Hit Rate (%)` | Per-node L3 read hit rate (dynamic, one per NUMA node found) |

A companion `Memory_Timeline` sheet carries per-sample DDR bandwidth columns (dynamic).

### `NUMA_Bandwidth` — columns `NUMA Node` | `Read (MB/s)` | `Write (MB/s)`

One row per NUMA node, parsed from `devkit_mem` output.

### `KSys` — columns `Metric` | `Value`

| Metric string | Meaning |
|---------------|---------|
| `L2 Miss Latency Max` / `Min` / `Avg` | L2 miss latency (cycles) |
| `L3 Miss Latency Max` / `Min` / `Avg` | L3 miss latency (cycles) |
| `IPC` | IPC from ksys |
| `Retiring (%)` / `Frontend Bound (%)` / `Bad Speculation (%)` / `Backend Bound (%)` | Top-down breakdown |

### `UBWatch_Latency` — columns `Metric` | `Value`

NUMA interconnect latency (8 rows):

| Metric string | Meaning |
|---------------|---------|
| `Latency Path` | Path string, e.g. `N0->N2` |
| `Samples` | Sample count |
| `Avg Read (ns)` / `Avg Write (ns)` | Average read/write latency |
| `Min Read (ns)` / `Min Write (ns)` | Minimum read/write latency |
| `Max Read (ns)` / `Max Write (ns)` | Maximum read/write latency |

### `UBWatch_Bandwidth` — interconnect throughput

`Chip` | `Ports` | `Avg Write (MB/s)` | `Avg Read (MB/s)` | `Avg Sum (MB/s)` | `Max Write (MB/s)` | `Max Read (MB/s)` | `Max Sum (MB/s)`

One row per chip+port group.

### `SMAPBW_Summary` — columns `Metric` | `Value`

SMAP (Secure Memory Access Protection) migration bandwidth:

| Metric string | Meaning |
|---------------|---------|
| `Total Cycles` | Total migration cycles |
| `Total Pages` | Total migrated pages |
| `Avg Bandwidth (GB/s)` | Average migration bandwidth |
| `Min Bandwidth (GB/s)` | Minimum bandwidth |
| `Max Bandwidth (GB/s)` | Maximum bandwidth |

### `SMAPBW_Cycles`

`Cycle` | `Pages` | `Duration (s)` | `Bandwidth (GB/s)` plus dynamic `N{x}->N{y}_pages` direction columns (one per migration direction).

### `Getfre_Summary` — core frequency per NUMA

`NUMA` | `Avg Frequency (MHz)` | `Min Frequency (MHz)` | `Max Frequency (MHz)` | `Sample Count` | `Core Count` — one row per NUMA node.

### `Getfre_NUMA{N}` — per-core detail (one sheet per NUMA, no separator: `Getfre_NUMA0`, `Getfre_NUMA1`, …)

`Core ID` | `Avg Frequency (MHz)` | `Min Frequency (MHz)` | `Max Frequency (MHz)` | `Sample Count`

> Getfre defaults (`total_cores`, `numa_nodes`) are auto-detected from host sysfs topology; physical cores are deduped by `thread_siblings_list`. Override via `getfre_config.yaml` (`GETFRE_CONFIG_PATH`).

## 2.4 Embedded Excel charts

Written by `_add_charts` after all sheets:

| Chart title | Type | Anchored on |
|-------------|------|-------------|
| CPU Top-down Analysis | Pie | `DevKit_TopDown` |
| IPC Over Time | Line | `TopDown_Timeline` |
| Memory Bound Breakdown | Bar | `DevKit_TopDown` |
| DDR Bandwidth Over Time | Line | `Memory_Timeline` |
| Cache Miss Rate Comparison | Bar | `DevKit_Memory` |
| Swap In/Out Rate Over Time | Line | `Swap_Timeline` |
| SwapCache per NUMA Over Time | Line | `Swap_Timeline` |
| NUMA Free/Used Memory (Focus Nodes) | Line | `NUMA_Memory_Timeline` |
| NUMA SwapCache & Usage% (Focus Nodes) | Line (dual Y: MB + %) | `NUMA_Memory_Timeline` |
| VM Total Memory Over Time | Line | `VM_Total_Memory_Timeline` |
| Disk Write MB/s Over Time | Line | `Disk_IO_Timeline` |
| Dirty + Writeback Over Time | Line | `Host_Mem_Timeline` |
| Page-Cache Pressure Over Time | Line | `Host_Pressure_Timeline` |

## 2.5 SVG reports

Written by `svg_exporter.export_svg_reports` (skip with `--no-svg`). Dark theme (`#07101f` canvas, `#101827` panels); palette `#60a5fa #f59e0b #fb7185 #4ade80 #c084fc #22c55e #f97316 #38bdf8`. Each file is written only when its source history is non-empty.

| Filename | Document title | Panels (title → series) | Width |
|----------|-----------------|--------------------------|-------|
| `disk_io.svg` | Disk I/O Time Curves | Disk Read Throughput (MiB/s; per-dev `{d} Read`) · Disk Write Throughput (MiB/s; per-dev `{d} Write`) · Disk Busy Utilization (%; per-dev `{d}`, dashed 100% threshold) · ublk Devices (count) | 1440 |
| `disk_latency.svg` | Disk Latency Time Curves | Disk Queue Depth (per-dev `{d} Queue`) · Disk Avg Latency (await, ms; per-dev `{d} R-await` + `{d} W-await`) | 1200 |
| `host_resources.svg` | Host Resource Time Curves | Host CPU Usage (`CPU`, `IOWait`) · Host Memory Used (GiB; `Used`) · Host Dirty Pages (auto MiB/GiB/TiB; `Dirty`, dashed dirty-limit) · Writeback / Cached / Buffers (auto unit) | 1440 |
| `host_pressure.svg` | Host Pressure Time Curves | Page-Cache Pressure (MiB/s; `Scan`/`Reclaim`/`Refault`) · Anonymous / File Cache (auto MiB/GiB; `Anon`/`File Cache`/`SReclaimable`) · Runnable / Blocked Procs (count; `Running`/`Blocked`) | 1440 |
| `swap.svg` | Swap Activity Time Curves | Swap Used (auto MiB/GiB; `Used`) · Swap In/Out Rate (MiB/s; `In`/`Out`) · Swap Cache (auto MiB/GiB; `Cached`) · Swap Used cumulative view (auto; `Used`) | 1440 |
| `numa.svg` | NUMA Time Curves | NUMA Memory Used (GiB; per-node `Node {n}`) · NUMA CPU Usage (%; per-node `Node {n}`) | 1440 |
| `vm_total.svg` | VM Total Memory Time Curves | VM Total Memory (GiB; `Total`) · VM Count (count; `VMs`) | 1200 |

- `disk_latency.svg` is written only when at least one disk snapshot carries `avg_queue_depth`.
- `host_pressure.svg` is written only when `host_pressure_history` is non-empty.
- NUMA SVG memory is auto-scaled from MB history to GiB; CPU is `%`.

## 2.6 Per-sample history field reference

What each sample dict contains (drives the sheet columns above):

| History list | Entry keys |
|--------------|------------|
| `disk_history` | `ts`, `disks` → per-dev: `r_mb_s`, `w_mb_s`, `util_pct`, `inflight`, `r_mb`, `w_mb`, `avg_queue_depth`, `read_await_ms`, `write_await_ms` |
| `numa_memory_history` | `ts`, `nodes` → per-node: `node`, `total_mb`, `free_mb`, `available_mb`, `swap_cached_mb`, `active_mb`, `inactive_mb`, `anon_pages_mb`, `file_pages_mb`, `used_mb`, `usage_pct` (+ back-compat aliases `total`/`used`/`free`/`usage`) |
| `swap_history` | `ts`, `capacity` {`total_mb`, `free_mb`, `used_mb`, `usage_pct`}, `cache` {`cached_mb`, `cached_ratio_pct`}, `activity` {`pswpin_delta`, `pswpout_delta`, `swap_in_rate`, `swap_out_rate`, `pswpin_cumulative`, `pswpout_cumulative`} |
| `host_cpu_history` | plain list of floats (no dict, no `ts`) |
| `host_mem_history` | `used_mb`, `total_mb`, `usage` |
| `host_mem_detail_history` | `ts`, `cached_mb`, `buffers_mb`, `dirty_mb`, `writeback_mb` |
| `ublk_history` | `ts`, `ublk_devices` |
| `host_pressure_history` | `ts`, `page_scan_mib_s`, `page_reclaim_mib_s`, `file_refault_mib_s`, `anon_pages_mb`, `file_cache_mb`, `sreclaimable_mb`, `iowait_pct`, `procs_running`, `procs_blocked` |
| `vm_total_memory_history` | `ts`, `total_mb`, `vm_count`, `per_numa` (dict), `swapcache_mb`, `swapcache_per_numa` (dict) |
| `hugepage_per_numa_history` | `ts`, `nodes` → {node_id: {`total_mb`, `free_mb`, `used_mb`, `usage_pct`}} |
| `hugepage_used_history` | plain list of floats |
| `numa_cpu_history` | `defaultdict(list)` keyed by node id → list of floats |

## 2.7 Collection sources & calculation formulas

`vm_monitor` derives its self-collected metrics from a small set of Linux
pseudo-files. Every metric below is computed inside `VMMonitorBase.collect_sample()`
(or a helper it calls) — no external process is spawned. All formulas are
verified against `vm_monitor/base.py`; the symbols used:

- `_SECTOR_SIZE_BYTES = 512` — kernel block-layer stat sector size (always 512 B, regardless of device block size).
- `_BYTES_PER_MIB = 2**20` (1,048,576) — note this is **MiB**, not decimal MB.
- `_PAGE_SIZE` — host page size from `os.sysconf("SC_PAGE_SIZE")` (typically 4096); used to convert page counts ↔ MiB.

### Source files

| Source | Fields read | Collected by |
|--------|-------------|--------------|
| `/sys/block/<dev>/stat` | fields 0,2,3,4,6,7,8,9,10 (reads_completed, sectors_read, read_ms, writes_completed, sectors_written, write_ms, inflight, ms_io, weighted_ms) | `collect_disk_stats` → `_compute_disk_io_rates` |
| `/proc/meminfo` | `Cached`, `SReclaimable`, `Buffers`, `Dirty`, `Writeback`, `SwapCached`, `AnonPages`, `Shmem`, `MemTotal` (all kB → /1024 → MB) | `_read_meminfo` |
| `/proc/vmstat` | `pswpin`, `pswpout`, `pgscan_kswapd`, `pgscan_direct`, `pgscan_direct_throttle`, `pgsteal_kswapd`, `pgsteal_direct`, `workingset_refault_file` (raw page counters) | `_read_vmstat` |
| `/proc/stat` | `cpu` line (jiffies, index 4 = iowait), `procs_running`, `procs_blocked` | `collect_host_pressure` |
| `/proc/sys/vm/dirty_bytes`, `dirty_background_bytes`, `dirty_ratio`, `dirty_background_ratio` | dirty throttle thresholds (read once) | `_read_dirty_limits_mb` |
| `/sys/devices/system/node/node{N}/meminfo` | `MemTotal`, `MemFree`, `MemAvailable`, `SwapCached`, `Active`, `Inactive`, `AnonPages`, `FilePages` (kB → /1024 → MB) | `get_numa_nodes_memory` |
| `/sys/devices/system/node/node{N}/cpulist` | logical CPU IDs on the node | `collect_numa_cpu` (via psutil per-CPU % averaged over the node's cores) |
| `/dev/ublkb*` (glob) | device count | `collect_ublk_count` |
| psutil | `cpu_percent`, `virtual_memory`, `swap_memory` | `collect_host_stats`, `collect_swap_stats` |

> `collect_swap_stats` and `collect_host_mem_detail` each call `_read_meminfo()`
> independently — three reads of `/proc/meminfo` per sample. Each is `<1 ms` on a
> local disk; the cost is negligible versus the sampling interval.

### Disk I/O (`_compute_disk_io_rates`)

Per-device rates from two `/sys/block/<dev>/stat` snapshots; `Δ` = current −
previous (first sample has no baseline → zero rates).

| Metric | Formula |
|--------|---------|
| `Read (MB/s)` = `r_mb_s` | `Δsectors_read × 512 / 2²⁰ / interval` |
| `Write (MB/s)` = `w_mb_s` | `Δsectors_written × 512 / 2²⁰ / interval` |
| `Util (%)` = `util_pct` | `min(100, (Δms_io / 10) / interval × 100)` — `ms_io` ticks at 10 ms granularity, so this is the fraction of wall-time the device spent servicing I/O, capped at 100%. |
| `Inflight` | current `inflight` (field 8, instantaneous, not a rate) |
| `Queue Depth` = `avg_queue_depth` | `Δweighted_ms / 1000 / interval` — `weighted_ms` is I/O-time weighted by queue depth, so this is the mean in-flight request count over the interval. |
| `Read Await (ms)` = `read_await_ms` | `Δread_ms / Δreads_completed` (0 when no reads completed) — mean latency per completed read I/O. |
| `Write Await (ms)` = `write_await_ms` | `Δwrite_ms / Δwrites_completed` (0 when no writes completed) |

### Swap (`collect_swap_stats`)

Capacity/cache come from psutil (`swap_memory`) + `/proc/meminfo`; activity from
`/proc/vmstat` page-counter deltas.

| Metric | Formula |
|--------|---------|
| `Swap Used/Free/Total (MB)` | psutil `swap_memory()` (bytes → /2²⁰ → MB) |
| `Swap Cached (MB)` | `/proc/meminfo` `SwapCached` (kB → /1024 → MB) |
| `Swap Cache Ratio (%)` | `SwapCached / SwapUsed × 100` (0 when `SwapUsed = 0`) |
| `Swap In Rate (MiB/s)` | `Δpswpin × (_PAGE_SIZE / 2²⁰) / interval` |
| `Swap Out Rate (MiB/s)` | `Δpswpout × (_PAGE_SIZE / 2²⁰) / interval` |

`pswpin`/`pswpout` are page counts; the page-size factor converts pages → MiB
so the rate matches its "MiB/s" label.

### Host memory detail (`collect_host_mem_detail`)

All from `/proc/meminfo` (kB → /1024 → MB):

| Metric | Formula |
|--------|---------|
| `Cached (MB)` | `Cached + SReclaimable` (page cache + reclaimable slab) |
| `Buffers (MB)` | `Buffers` |
| `Dirty (MB)` | `Dirty` |
| `Writeback (MB)` | `Writeback` |

### Page-cache pressure (`_compute_pressure_rates` + `collect_host_pressure`)

From `/proc/vmstat` page-counter deltas; `mib_per_page = _PAGE_SIZE / 2²⁰`.

| Metric | Formula |
|--------|---------|
| `Page Scan (MiB/s)` | `(Δpgscan_kswapd + Δpgscan_direct + Δpgscan_direct_throttle) × mib_per_page / interval` |
| `Page Reclaim (MiB/s)` | `(Δpgsteal_kswapd + Δpgsteal_direct) × mib_per_page / interval` |
| `File Refault (MiB/s)` | `Δworkingset_refault_file × mib_per_page / interval` |

Each `Δ` is clamped to `max(0, …)` so a counter reset (host reboot mid-run)
cannot produce a negative rate.

From `/proc/meminfo` (same `_read_meminfo()`):

| Metric | Formula |
|--------|---------|
| `Anon Pages (MB)` | `AnonPages` |
| `File Cache (MB)` | `max(0, Cached + Buffers − Shmem)` |
| `SReclaimable (MB)` | `SReclaimable` |

From `/proc/stat`:

| Metric | Formula |
|--------|---------|
| `IOWait (%)` | `max(0, Δiowait) / Δtotal_jiffies × 100` — `iowait` and `total` are the cumulative `cpu` line values (jiffies); deltas taken vs the prior sample, so this is the share of CPU time spent in `iowait` over the interval. |
| `Procs Running` / `Procs Blocked` | literal `procs_running` / `procs_blocked` lines (instantaneous counts) |

### Dirty throttle thresholds (`_read_dirty_limits_mb`)

Read **once** (lazy, on first `collect_host_pressure`); drawn as dashed lines on
the Dirty time curve.

| Metric | Formula |
|--------|---------|
| `dirty_background_limit_mb` / `dirty_limit_mb` | If `/proc/sys/vm/dirty_bytes > 0`: `dirty_background_bytes / 2²⁰`, `dirty_bytes / 2²⁰`. Else (kernel in ratio mode): `dirty_background_ratio / 100 × MemTotal`, `dirty_ratio / 100 × MemTotal` (MemTotal in MB from `/proc/meminfo`). Returns `(0, 0)` when the sysctls are unavailable. |

### NUMA memory (`get_numa_nodes_memory`)

Per node, from `/sys/devices/system/node/node{N}/meminfo` (kB → /1024 → MB):

| Metric | Formula |
|--------|---------|
| `Total (MB)` | `MemTotal` |
| `Free (MB)` | `MemFree` |
| `Available (MB)` | `MemAvailable` |
| `Used (MB)` | `MemTotal − MemFree` |
| `Usage (%)` | `Used / Total × 100` (0 when `Total = 0`) |

`SwapCached` / `Active` / `Inactive` / `AnonPages` / `FilePages` are passed
through verbatim; only the seven sheet columns in [2.2](#numa_memory_timeline--focus-nodes-only)
are exported, the rest stay in history.

### NUMA CPU (`collect_numa_cpu`)

For each focus node, the logical CPU list is read from
`/sys/devices/system/node/node{N}/cpulist`; `psutil.cpu_percent(percpu=True)`
gives the per-CPU %, and the node value is the **arithmetic mean** of the
per-CPU values for the cores on that node (`sum / valid_count`, where
`valid_count` excludes cores whose psutil value is unavailable).

### Auto-discovered host topology

These are not metrics per se, but they drive which devices/nodes/cores are
sampled — noted here so the formulas above are reproducible on any host:

| Discovery | Source | Used for |
|-----------|--------|----------|
| Block devices | `/sys/block/`, excluding `loop`/`ram`/`sr`/`zram`/`md`/`dm` | `--disks all` default target set |
| NUMA nodes | `/sys/devices/system/node/node{N}` | `numa_nodes` default, focus-set construction |
| Physical cores (per node / host-wide) | `node{N}/cpulist` + per-CPU `topology/thread_siblings_list` (keeps the lowest sibling ID, so each physical core appears once) | `numa_to_physical_cores`, getfre `total_cores` default |

---

## 3. Using metrics for bottleneck identification

### CPU bottleneck
- `bench_core`: the kernel doesn't emit CPU TopDown — inspect task `Avg Latency` + `P99 Latency`, the `[Round Comparison]` Tail ratio, and creation-timing P95/P99 to localize lifecycle regressions.
- `vm_monitor`: high `Backend Bound (%)` / `Bad Speculation (%)` on `DevKit_TopDown`; low `IPC` on `KSys`; high `VM Avg CPU` / `VM Peak Total CPU` on `Summary`.

### Memory / cache bottleneck
- `vm_monitor`: high `Mem Bound (%)` / `L3 Bound (%)` (`DevKit_TopDown`); high `L2D/L1D Miss (%)` (`DevKit_Memory`); high `L2/L3 Miss Latency Avg` (`KSys`); rising `Dirty Peak` / `Writeback Peak` (`Summary`, `Host_Mem_Timeline`).

### NUMA / interconnect
- `vm_monitor`: compare per-node `Read (MB/s)` / `Write (MB/s)` (`NUMA_Bandwidth`); high `Avg Read (ns)` / `Avg Write (ns)` (`UBWatch_Latency`); per-node `NUMA{N} Usage (%)` drift on `NUMA_Memory_Timeline` / `numa.svg`.

### Disk I/O
- `vm_monitor`: high `{dev} Util (%)` + rising `{dev} Read/Write Await (ms)` + `{dev} Queue Depth` (`Disk_IO_Timeline` / `disk_io.svg` / `disk_latency.svg`); `Disk Write Peak` on `Summary`.

### Swap / memory pressure
- `vm_monitor`: rising `Swap In/Out Rate` + `Swap Cached` (`Swap_Timeline` / `swap.svg`); high `Page Scan` / `Page Reclaim` / `File Refault` (`Host_Pressure_Timeline` / `host_pressure.svg`); `Swap Peak Usage %` on `Summary`.

### Sandbox lifecycle (kernel)
- `bench_core`: `--create-only` creation-timing P95/P99 of `create_elapsed` vs `ready_check_elapsed` to localize startup cost; workflow `Verify Success:` (coding) and `Success Rate:` + Tail ratio (browser/document) to localize task regressions.

---

## Related documentation

- [Usage Guide – Log Collection Tools](usage-guide.md#log-collection-tools): `.env` paths + flags for devkit / ksys / ub_watch / smap_bw / getfre.
- [bench-core usage (中文)](bench-core-usage-zh.md): `bench-core` install → config → CLI → cleanup → troubleshooting.
- [System design](design.md) / [design-en.md](design-en.md): architecture and data flow.
