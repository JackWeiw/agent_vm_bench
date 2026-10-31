# Agent VM Bench - Project Guide for AI Assistants

This file provides project-specific conventions, architecture overview, and working guidelines for AI assistants working on this codebase.

## What is Agent VM Bench

Agent VM Bench is a performance-testing framework for virtualization scenarios. The **`bench_core` kernel + `env_provider` contract** (under `src/`) is the host-agnostic core: one stress profile drives any sandbox backend by swapping `--provider`, and the same `config/common/*.yaml` runs on either e2b or docker. Scenarios:

- **E2B Sandbox** — Firecracker microVMs via the E2B API (`--provider e2b`)
- **Docker Containers** — browser automation in containerized environments (`--provider docker`)
- **OpenStack VM Memory Overcommit** — QEMU/KVM VMs with `smap_tool` memory migration (legacy `vm_bench/` + `auto_vm_test.py`; not yet ported to the kernel)

The framework collects **50+ performance metrics** from hardware counters, kernel metrics, and application-level measurements. The new kernel collects creation/readiness timing and workflow-level task metrics; integration with `vm_monitor` / `smap_tool` is a follow-on phase (batch scheduler).

The frozen-legacy `e2b_bench/`, `docker_bench/`, and OpenStack tools (`vm_bench/`, `auto_vm_test.py`, `batch_test_scheduler.py`) remain for existing users and **share no code with the `src/` kernel**. Edit them only for bug fixes; new work goes in `src/`.

## Build, Lint, Test Commands

> Requires **Python 3.10+** (CI runs 3.13; `pyproject.toml` `requires-python = ">=3.10"`). Ruff `target-version = "py310"` aligns with the runtime floor (lint floor ≤ runtime floor; raising it needs raising `requires-python` first).

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .          # registers bench-core + pulls core deps from pyproject.toml

bench-core --provider fake --config config/common/browser.yaml --create-only -n 1   # smoke (no SDK)
python -m pytest                     # full suite (src/bench_core + src/env_provider + legacy packages)
python -m pytest src/bench_core/tests src/env_provider/tests   # src kernel + providers only
ruff check .                         # lint (target-version py310, line-length 120)
ruff format --check .                # format check
python -m pre_commit run --all-files # pre-commit hooks (run on staged files before committing)
```

`pip install -e .` is sufficient — `pyproject.toml`'s `dependencies` declare the core stack (`psutil`, `paramiko`, `flask`, `PyYAML`, `pandas`, `openpyxl`, `e2b`, `aiohttp`); `requirements.txt` is a PyPI-publish superset and is redundant for the kernel. Backend SDKs are optional: `pip install e2b` and/or `pip install docker` only when using that provider; `--provider fake` needs none.

`pyproject.toml` sets `pythonpath = ["src"]` for pytest, so tests resolve `bench_core` / `env_provider` without an editable install. The `[project.scripts]` entries are `bench-core = "bench_core.bench:main"` and `vm-monitor = "vm_monitor.cli:main"`.

## Architecture

### Layered architecture (src kernel)

```
┌─────────────────────────────────────────────────────────────┐
│  CLI / entry                                                 │
│  bench-core (bench_core.bench.main) --provider {fake,e2b,docker,aenv} │
└─────────────────────────────────────────────────────────────┘
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│  bench_core (kernel)   │   │  env_provider (contract)       │
│  run_benchmark spine    │◀──│  EnvironmentProvider ABC       │
│  stats / round_robin    │   │  SandboxInstance / Metrics    │
│  task_manager / runners │   │  CommandResult / SandboxStatus │
│  KernelConfig.from_raw  │   │  (SDK-free)                    │
└────────────────────────┘   └──────────────────────────────┘
                                          │
                          ┌───────────────┼───────────────┐
                          ▼               ▼                 ▼
                   env_provider.e2b  env_provider.docker  env_provider.fake
                   (opt-in submodule, pulls e2b SDK)  (pulls docker SDK)  (no SDK)
```

The kernel (`bench_core`) and every provider impl depend on the contract (`env_provider`); neither owns it. The contract stays **SDK-free** — `from env_provider import EnvironmentProvider` never imports the e2b or docker SDK. Providers are **opt-in leaf submodules** loaded only by `bench_core.bench._build_provider` when selected, so the kernel never loads a backend it does not use. Adding a backend (kata, agentenv, …) is one new `env_provider` submodule; contract + kernel untouched.

**`bench_core` kernel** (`src/bench_core/`):
- `bench.py` — `run_benchmark(config, provider)` spine: `prepare_env → create/detect → (create-only | warmup-only | cleanup-only | benchmark) → stop → report`. `_build_provider` lazy-imports the selected provider submodule. `_promote` lifts provider `SandboxInstance` → kernel `BenchSandbox` (attaches workflow metrics).
- `config.py` — `KernelConfig`. `KernelConfig.from_raw(raw)` is the **single reader** of the shared stress sections (`sandbox` / `create_batch` / `task_batch` / `browser` / `coding` / `document` / `test` / `report` / `workflow_type`). `__post_init__` auto-fills `coding_source_files` from `CODING_LANGUAGE_DEFAULT_SOURCE_FILES[language]` (ts→vuejs/core, go→gohugoio/hugo, python→django/django), so coding configs omit `source_files`.
- `task_manager/fixed.py` + `task_runner/{browser,coding,document}.py` — the three workflows. Browser rides HTTP (agent-browser on port 18789) on top of a backend the provider starts; coding/document are pure `exec` (verify script written via heredoc through `exec`).
- `task_manager/round_robin.py` — `RoundRobinTaskManager` (group rotation, per-step timing).
- `stats_collector.py` — snapshots + report generation (`generate_report` / `save_report`).
- `coding_payload.py` — canonical coding replacement pairs + verify scripts.
- `monitor.py` — `MonitorController` + `MonitorConfig`: host-level `vm-monitor` orchestration around the stress phase (stress-file sync subprocess; auto-enable by provider `vmm_type`; outputs to `report.output_dir/vm_monitor/`; optional host-sheet merge into replay obs xlsx).
- `schemas.py` (`BenchSandbox`), `utils.py` (`calc_percentiles`, `setup_logging`).

**`env_provider` contract** (`src/env_provider/__init__.py`): `EnvironmentProvider` ABC (`create_all`, `detect_existing`, `detect_from_ids`, `save_ids`, `check_alive`, `cleanup_all`, `cleanup_existing`, `prepare_env`, `prepare`, `exec`) + `SandboxInstance` / `CreationMetrics` / `CommandResult` / `SandboxStatus`. `exec()` is the **sole command primitive** — file writes go through `exec` as a heredoc, no separate upload method, so adding a provider is implementing `exec`. The provider keeps any SDK handle internally (`id → handle`); the kernel holds only the host-agnostic `SandboxInstance`. Optional capability Protocols (replay workflow) gate lifecycle features: `LifecycleCapable` (`pause`/`resume`), `EphemeralCapable` (`create_one`/`kill_one`), `SnapshotSizeCapable` (`snapshot_sizes`) — `aenv` satisfies all three; `e2b`/`docker`/`fake` are exec-only.

**Shared backend infra** (`src/env_provider/_base.py`, `_ready.py`) — the e2b/docker managers' duplicated create/detect/cleanup skeletons were lifted here:
- `_ready.py` — `ReadyChecker`: workflow-driven poll-until-ready; backend supplies `_exec_probe`. browser=port scan (`ss|netstat grep :{port}`, ports `18789` openclaw-gateway + `11436` llama-server), coding=`uname -a`, document=`document-bench-validate` (non-zero exit = immediate image failure, not retried). Constants `READY_MAX_WAIT=300`, `READY_INTERVAL=5`.
- `_base.py` — `BackendSandboxStatus` / `BackendCreationMetrics` (byte-identical across backends) + `BaseSandboxManager(ABC)` lifecycle template. Subclass supplies SDK seams (`_create_single`, `_list_existing`, `_external_id`, `_attach`, `_kill_one`, `_exec_probe`) + class attrs (`_handle_attr`, `_noun`, `_id_attr`, `_set_killed_on_cleanup`). `_ready_config` is a **concrete base method** returning shared constants — readiness is a workflow concern, so there are no per-backend readiness knobs and no `port_check` block in any YAML.

**Provider impls**: `env_provider/e2b/` (`config.py` `setup_e2b_env` + placeholder-credential fallback to `~/.e2b/config.json`, `manager.py`, `schemas.py`), `env_provider/docker/` (`config.py` reads only image/prefix/resources, `manager.py`, `schemas.py`), `env_provider/aenv/` (`__init__.py` `AenvProvider` subclasses `E2BProvider` and reuses the E2B `SandboxManager` handle table + SDK — AENV has no separate SDK, it is the E2B SDK pointed at an AENV server via `E2B_API_URL`; adds `pause`/`resume` (E2B `beta_pause`/`Sandbox.connect`), `snapshot_sizes` (scans the persisted-sandboxes tree), `create_one`/`kill_one` (trajectory mode); `default_replay_mode = "lifecycle"`; reads the `aenv:` config block, same shape as `e2b:` + optional `aenv.snapshot_dir` / `aenv.aenv_home_path` / `aenv.ccl_bind`), `env_provider/fake.py` (in-memory, drives `run_benchmark` in unit tests with no SDK). `ccl_bind` (`aenv/_ccl.py`) is an opt-in, best-effort host-side CPU-affinity pin: at the create path (`create_all` for exec_only, `create_one` for exec_ephemeral) it resolves sandbox_id → firecracker pid via an MMDS `instanceID` scan (`/proc` + per-process unix socket) and `sched_setaffinity`-pins it to a dedicated core set, round-robin by sandbox index. exec_only/exec_ephemeral only — lifecycle/trajectory skip it (pid unstable across resume); requires root on the AENV host; every failure degrades to warning + skip, never unwinding a successful create.

**Config layering**: one `config/common/*.yaml` per workflow carries the per-backend blocks it drives; `--provider` selects which `Config.from_raw` reads. Browser/coding/document configs carry **both** `e2b:` and `docker:` blocks; `config/common/replay.yaml` (the aenv lifecycle/snapshot backend) carries `aenv:` + `docker:` blocks (aenv subclasses the e2b `Config` and reads its own block). The kernel reads only the shared sections. Credential placeholders (`your_e2b_access_token_here` / `your_e2b_api_key_here`) are treated as unset → fall back to `~/.e2b/config.json`.

### Legacy tiers (frozen, share no code with the kernel)

The pre-`src/` three-tier stack is retained as-is:

```
Batch Scheduling Layer:  batch_test_scheduler.py (OpenStack), e2b_bench/batch_scheduler.py (E2B)
Core Test Layer:          auto_vm_test.py, e2b_bench/bench.py, docker_bench/bench.py
Tool Execution Layer:     vm_bench/, sandbox_manager.py, container_manager.py, external tools
```

These have their own managers / stats / round-robin (`e2b_bench/run_benchmark` is standalone, ~479 lines, zero `bench_core`/`env_provider` imports). There are now **two parallel benchmark implementations**; editing one does not affect the other.

### Key Packages

| Package | Purpose | Key Files |
|---------|---------|-----------|
| `src/bench_core/` | host-agnostic kernel (recommended) | `bench.py`, `config.py`, `task_manager/{fixed,round_robin}.py`, `stats_collector.py`, `task_runner/{browser,coding,document}.py`, `coding_payload.py` |
| `src/env_provider/` | provider contract + e2b/docker/aenv/fake impls | `__init__.py` (ABC + capability Protocols), `_base.py`, `_ready.py`, `e2b/`, `docker/`, `aenv/`, `fake.py` |
| `vm_monitor/` | VMM monitoring (QEMU/Firecracker) | `base.py`, `qemu.py`, `firecracker.py`, `parsers.py`, `exporters.py` |
| `e2b_bench/` | E2B sandbox testing (frozen legacy) | `bench.py`, `round_robin.py`, `task_runner.py`, `sandbox_manager.py`, `batch_scheduler.py`, `stats_collector.py` |
| `docker_bench/` | Docker container testing (frozen legacy) | `bench.py`, `container_manager.py` |
| `vm_bench/` | OpenStack VM browser/QA + creation (legacy) | `bench.py`, `vm_manager.py`, `task_runner.py`, `stats_collector.py` |

## Code Conventions

### Python style

- PEP 8; type hints on public APIs; max line length **120**.
- f-strings for formatting; `pathlib.Path` over `os.path`.
- `from __future__ import annotations` at the top of kernel/provider modules (PEP 604 `int | None` in annotations).
- Ruff config in `pyproject.toml`: **`target-version = "py38"`** — do NOT bump to py310 casually; it activates ~740 UP findings repo-wide. Leave it for a dedicated modernization PR.
- English-only comments and commit messages (no Chinese in code/comments; GitHub issues/PRs/RFCs in English even when the conversation is Chinese).

### Configuration

- All configs in YAML under `config/`; new backend-agnostic workflows go in `config/common/` (one file per workflow, both `e2b:` + `docker:` blocks).
- Template configs use `{{PLACEHOLDER}}` or `your_*_here` for dynamic values; the e2b provider treats credential placeholders as unset and falls back to `~/.e2b/config.json`.
- Environment variables loaded from `.env` files. Never hardcode paths — use config or `.env`.
- Never declare per-backend readiness (port/timing) knobs in YAML — readiness is provider-transparent (kernel constants).

### Host-level monitor (`monitor:` section)

A top-level `monitor:` block (peer of `report:`) controls host-level `vm_monitor` collection during the stress phase. Default `enabled: auto` turns it on only for providers with a VMM (`vmm_type`): e2b/aenv → `firecracker`; docker/fake → skipped. `vm-monitor` runs as a local subprocess (stress-file sync) bracketing active stress; outputs (CSV + `resource_report.xlsx` + SVG) land in `<report.output_dir>/vm_monitor/`. By default (`merge_report: false`) the replay result is **two separate files**: vm_monitor's `resource_report.xlsx` (system resources) stands alone, and the replay `<prefix>_obs.xlsx` carries trajectory/replay metrics only (incl. a per-trajectory per-step detail sheet). Set `merge_report: true` to copy host sheets (`VM_Stats`/`NUMA_Overview`/`DevKit_TopDown`) into the obs workbook instead. `--no-vm-monitor` short-circuits it off. Never compromises the bench: missing binary/tools or an unwritable lock dir degrade to a warning + skip.

**Log-capture rotation** (`monitor.log_rotation:` sub-block, default off): devkit (top-down + memory in parallel) → ksys → `perf stat` → AMDuProfPcm rotate one slot at a time (default 15 s per slot, `interval_sec`; optional `intervals:` dict — `{devkit, ksys, perf, uprof}` — sets a per-tool slot duration, falling back to `interval_sec` for omitted tools) for the whole stress window instead of all capture tools running concurrently; each turn writes a timestamped log under `<output_dir>/log_capture/{devkit/{topdown,memory},ksys,perf,uprof}/` (raw artifacts, not parsed into sheets). `perf_events` (list or comma string) feeds `perf stat -e`; omitted → vm_monitor's built-in set. The `uprof` turn runs `AMDuProfPcm -m memory,pipeline_util -a -A system,package -d <interval>` with the binary from `UPROF_PATH` (.env); an unset path skips the slot. `tools:` (list or comma string) rotates only the named tools in canonical slot order — default all four; unknown names are dropped, an empty result rotates every tool. Requires `capture` (auto/true); ub_watch/smap_bw/getfre keep their all-parallel behavior; a ksys turn waits only for its collect phase (the parse phase runs in the background and overlaps later slots — inherent to ksys, same as the parallel mode).

**Always-on raw snapshots** (no switch): `vm_monitor` writes raw /proc + per-VM + per-disk CSV snapshots to a `raw_data/` subdirectory of the log dir (kept separate from `resource_report.xlsx` and the xlsx-overflow CSVs in the parent dir) on a background daemon thread (queue.SimpleQueue; enqueue is O(1) so the sampling loop never blocks on file I/O; joined + flushed before export). Files are named by scope so the dimension is obvious:
- `host_mem_raw.csv` (wide, /proc/meminfo all fields) · `host_vmstat_raw.csv` (wide, /proc/vmstat all fields) · `host_cpu_raw.csv` (wide, /proc/stat aggregate cpu jiffies + ctxt + softirq + procs_running/blocked; `intr` skipped)
- `host_percpu_raw.csv` (long, one row per cpu per sample — join with `numa_cpu_map.csv` to rebuild per-NUMA CPU%)
- `vm_cpu_raw.csv` (long, per-VM `/proc/<pid>/stat` utime+stime jiffies per sample) · `ublk_cpu_raw.csv` (long, per-ublk-daemon utime+stime jiffies at 1s sub-sample cadence) · `hostproc_cpu_raw.csv` (long, per named host process — utime+stime jiffies + `pss_mb` at 1s sub-sample cadence; written only when `--host-proc` specs are set; derive cores via delta **grouped by (name, pid)** so a pid restart resets the baseline, then merge onto one curve per `name`)
- `disk_io_raw.csv` (long, per-device `/sys/block/<dev>/stat` 17 standard counters at 1s sub-sample cadence)
- `numa_cpu_map.csv` (static `node,cpu` mapping, written once — boot-constant)

All raw counters are cumulative kernel values; an agent derives rates via delta over the real interval between rows' `timestamp` (the CSV header is written from the first non-empty row, so columns are self-describing via `pd.read_csv`). Derivation formulas live in [docs/dev/metrics-reference.md](docs/dev/metrics-reference.md): disk I/O (sectors→MB/s, `weighted_ms`→await/queue-depth, `ms_io`→util) under "Disk I/O"; iowait% + page-scan/reclaim/refault rates under "Page-cache pressure"; swap in/out under "Swap"; and per-VM / per-ublk-daemon / per-host-proc jiffies→CPU% (`÷ _CLK_TCK`) under "Per-VM & ublk-daemon CPU". Note `/proc/stat` `guest`/`guest_nice` are double-counted (already in user/nice) — total is `sum(first 8 fields)`, not a naive sum of all 10. Files land in `<report.output_dir>/vm_monitor/raw_data/`; the xlsx exporters never touch these filenames. The one deliberate consumer: `vm_monitor/raw_report.py` **derives `host_resources.xlsx`** from raw_data (spotbox-aligned `Host resources` / `Per-VM` / `Disk IO` / `HostProc` sheets, each table + charts; post-hoc rerunnable, independent of the live run and of external tools). The fleet-level `vm_cores` there is a **single aggregate jiffies delta** across all VMs (bounded by construction) — never a sum of per-VM normalized rates over the NaN-sparse pivot, which overshoots the physical core count.

### Host-process monitor (`--host-proc`)

A generalization of the always-on ublk-daemon tracker: monitor **any user-specified host process by name**, primarily the agentENV **"server"** daemon (`comm=server` + cmdline contains `release/server`, per `spotbox_monitor.py`). CLI `--host-proc comm[:cmdline_substr]` (repeatable); bench-core `monitor.host_procs` YAML (list or comma string — comma-string mode forbids a comma inside a spec). Each spec tracks **one pid** (resolved lazily by `/proc/<pid>/comm` + optional `/proc/<pid>/cmdline` substring, re-resolved on pid miss — same restart handling as ublk). Output: raw `hostproc_cpu_raw.csv` (always-on raw contract) + a derived `HostProc` sheet in `host_resources.xlsx` (CPU cores per process + PSS GB). `--no-hostproc` (or `monitor.skip: [hostproc]`) disables the whole collector. Edge-case logging: a never-seen spec fires a one-time WARNING after `_HOSTPROC_MISS_THRESHOLD` consecutive misses (surfaces a typo'd spec instead of an empty CSV); a spec matching >1 pid fires a one-time WARNING (tracks the first).

### Replay workflow (trajectory / lifecycle replay)

`config/common/replay.yaml` drives the replay workflow (`workflow_type: replay`): deterministic replay of recorded SWE-bench trajectories against sandbox backends, primarily `--provider aenv` (lifecycle pause/resume) or `--provider e2b` (exec-only). `replay.yaml` ships as the aenv **lifecycle 1:1 (no-oversubscription) baseline**; see [docs/guide/bench-core-usage-zh.md](docs/guide/bench-core-usage-zh.md) §8 for the mode guide. Modes (per `replay.mode`, defaults to the provider's `default_replay_mode`):

- `exec_only` — long-lived sandboxes, continuous exec of trajectory steps, no pause/resume. Baseline for pure exec-replay cost; used when the backend has no lifecycle capability (e2b/docker/fake).
- `lifecycle` — long-lived sandboxes: `create_all` → pause once → per-step `provider.resume()`/`pause()` (aenv `LifecycleCapable`). Oversubscription = **snapshot memory reuse** — pause frees RAM, so `k×N` sandboxes fit in `running_concurrency = N` slots. Emits `initial_pause` + per-slice `pause`/`resume` segments and `snapshot_size` events (aenv `SnapshotSizeCapable`).
- `trajectory` — ephemeral `create_one`/`kill_one` per trajectory (`EphemeralCapable`); per-step resume/pause still runs. Oversubscription = **queue limiting** (M slots gate concurrent trajectories; the rest defer `create`, not pause-to-free-memory). `launch_interval_sec` (per-sandbox create pacing) applies to the ephemeral modes (trajectory + exec_ephemeral) — lifecycle pre-creates via `create_all` batches (`create_batch`, integer-second intervals), so it has no sub-second per-VM launch pacing.
- `exec_ephemeral` — ephemeral `create_one`/`kill_one` per round (`EphemeralCapable`) with **no** pause/resume and **no** admission (k×N concurrent): the customer's real "spin up once → run → kill; to reuse, spin up again" workload (B's `run_no_lifecycle_oversubscription_benchmark` model). Between rounds the sandbox is killed and recreated fresh; `launch_interval_sec` paces the per-round `create_one` burst. **This is the oversub sweep default** (`_TRUE_DEFAULTS["modes"] = "lifecycle,exec_ephemeral"`); the driver emits a WARNING when that default is auto-applied (no `--modes`). `tasks_per_sec` includes per-round create+kill cost — not comparable to `exec_only`. With `replay.multiplier` (M; driver flag `--multiplier`, which flips the trial to `fixed` + stop-when-done via `KernelConfig.stop_when_done`) each of the k×N slots runs exactly M trajectories — each on a fresh sandbox, no round barrier — then self-stops (shared-deadline `wait_for_completion`, not a sequential join); the M-loop is gated on persistent create failure, not `is_alive` (which `kill_one` flips per trajectory), and the driver's `compute_valid` is M-aware (expected completions = M×k×N).

The four modes differ by **sandbox lifecycle** (long-lived exec-only / long-lived pause-resume / ephemeral pause-resume / ephemeral bare-exec), not by "whether a rate limiter is attached"; `launch_interval_sec` exists in the ephemeral modes (trajectory + exec_ephemeral) because frequent per-round `create_one` calls need pacing. Oversubscription ratio: with a fixed host (e.g. 1.5 TiB / 4 GiB per VM → 384 baseline), `running_concurrency` stays at the baseline (N slots) and `total_count` scales to `k×384` for ratio `1:k` (1:2 → 768/384, 1:3 → 1152/384). Scale `round_size` with `total_count` (single group = all concurrent); each ratio is one run. The driver also pins `replay.trajectory_limit = N` per trial so every ratio replays the same first-N trajectory pool (per template; the kernel clamps to N//T for a T-template manifest) — the issue set stays fixed across ratios and only concurrency varies.

Process sharding (`oversub.shards` / `--shards`, default `auto`): a single CPython process saturates one GIL — the thread-per-sandbox kernel pins at ~1 core and caps step dispatch (~50–55 steps/s at N=384) regardless of host vCPU count, so high-ratio trials go **client-bound** (inter-step gaps overshoot recorded think time; server-side load stops growing). Sharding lifts this by splitting one trial's `k×N` slots into P kernel subprocesses (`auto` = P=ratio, ~N slots each — every process carries the load already proven at 1:1). Mechanism lives in `oversub_shards.py` (driver composes it, never the reverse); the shard boundary sits **below** the kernel artifact contract, so merging yields a schema-faithful `run_summary.json` + `trajectories/index.json` and every downstream consumer (`compute_valid`, `trial_row`, `_trajectory_rows`, ratio summaries, `compare_sweeps`, `--reuse`) is shard-unaware. Only **admission-free** modes shard (`exec_ephemeral`/`exec_only`); `lifecycle`/`trajectory` enforce a GLOBAL `running_concurrency` and would over-admit to P×N if split, so they always run single-process (explicit P>1 downgrades with a WARNING). shard 0 owns the host `vm_monitor` (rest get `--no-vm-monitor`); docker `container_prefix` is suffixed `-s{i}` per shard; `sandbox_index` is re-keyed by cumulative slot offset (run keys stay unique); pacing knobs (`launch_interval_sec`, `create_batch`/`task_batch` intervals) scale ×P to preserve the global create/dispatch rate; for P≤ratio shard sizes are **group-aligned** (integer multiples of N → the merged per-trajectory run counts exactly match a single-process trial; P>ratio falls back to a slot-level split with a WARNING — totals preserved, per-trajectory mix redistributes). Merge fidelity: the merged wall is the trial **envelope** (max completed − min started, the same semantics as the kernel's own `wall_sec == completed_epoch − started_epoch`) and rates recompute over it; merged index entries re-aggregate ALL shards' runs with the kernel's rules (success_rate = per-run mean, elapsed = upper-median p50 success-only with all-runs fallback + `all_failed`, error types first-seen across runs, deep `file` stays per-shard); a crashed shard is never backfilled (surfaces in merged `error`, fails validity). P=1 keeps the legacy single-process path byte-identical.

Outputs: a text report (`<output_dir>/<prefix>_<ts>.txt`), a JSONL lifecycle series (`<prefix>_lifecycle_series.jsonl`), and (with `report.format: xlsx|both`) an 8-sheet observability workbook (`<prefix>_obs.xlsx`: Overview [consolidates Run / Throughput / Admission & QPS / Retry impact as grouped, color-coded sections], Per-step timings, Lifecycle overhead, Trajectory summary, Step detail, Concurrency states, Gantt, Snapshot sizes). The vm_monitor system-resource report (`resource_report.xlsx`) is a separate file by default (`monitor.merge_report: false`). All series events are `time.time()`-stamped for direct join with `vm_monitor` host samples (see `monitor:` above). Fixed-mode passes carry a distinct `round_id` (the run key is `(trajectory_id, sandbox_index, round_id)` — repeated passes of one trajectory stay separate run rows instead of merging), and each run row decomposes as `elapsed_sec = create_sec + kill_sec + stress_sec` (in `trajectories/index.json` + the oversub `trajectory-detail.csv`).

Multi-template routing: trajectories may declare per-file concrete templates via a side `template_manifest` (referenced by `replay.template_manifest`). The manifest is the authoritative run set -- only trajectories it maps to a concrete template string enter the pool; entries absent from it (or mapped to a null/non-string value) are skipped rather than falling back to the provider default (all skips aggregate into one INFO summary, not per-file WARNINGs). The fleet is round-robin-allocated over the pool's (template, trajectory) pairs, runners route by template affinity (orphan templates skip with a count), and the ephemeral modes (trajectory, exec_ephemeral) pass `template=` per `create_one`. Omit the manifest entirely for the legacy single-template path (every trajectory uses the provider block's `template`).

### Logging

- Python `logging` module, not `print`. Levels: DEBUG (verbose), INFO (progress), WARNING (issues), ERROR (failures).
- Streaming file logs use `buffering=1` (line buffering); include timestamps.

### Git / commit / PR rules (mandatory)

- **Branch first.** Never commit directly to `main`; cut a feature branch (`feat/...`).
- **Relevant files only.** `git add` only the files relevant to the change — never `git add -A` with unrelated churn. **Never commit `docs/superpowers/*`** (plans/specs are local working docs).
- **Pre-commit before commit.** Run `python -m pre_commit run --files <files>` on the staged set; fix failures before committing.
- **No Claude attribution.** Do not add `Co-Authored-By: Claude` or similar trailers.
- **Commit messages** via heredoc `git commit -F - <<'EOF'`; Conventional Commit prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `ci:`, `chore:`).
- **Squash-merge.** Multi-commit PRs are squash-merged into `main` — one commit per PR (PR title + body become the commit message) — so `main` stays linear and each PR is a single revertable unit. (`CONTRIBUTING.md` already declares this the default.)
- **IP & secret scrubbing.** Private `192.168.x.x` addresses are OK to commit; scrub public IPs. Never commit real E2B access tokens / API keys — leave placeholders.
- **Frozen legacy.** `e2b_bench/` and `docker_bench/` are frozen standalone legacy — do not refactor them into the kernel.

## Entry Points

| Script | Purpose | CLI |
|--------|---------|-----|
| `bench-core` (`bench_core.bench:main`) | host-agnostic kernel (recommended) | `--config`, `--provider {fake,e2b,docker,aenv}`, `--create-only`, `--detect`, `--warmup-only`, `--cleanup`, `-n`, `-bm`, `--test-duration`, `--vm-monitor {auto,true,false}`, `--no-vm-monitor` |
| `vm-monitor` (`vm_monitor.cli:main`) | VMM monitoring | `-t`, `-i`, `--vmm`, `--enable-capture` |
| `vm_bench/__main__.py` | OpenStack VM browser/QA + create | `--create-only`, `--detect`, `-n`, `--start-ip`, `--browser-url` |
| `auto_vm_test.py` | Single OpenStack test | `--config` |
| `batch_test_scheduler.py` | Batch OpenStack tests | `--config`, `--dry-run`, `--offline` |
| `e2b_bench/__main__.py` | E2B testing (frozen legacy) | `--config`, `--batch`, `--detect`, `-bm round_robin` |
| `docker_bench/__main__.py` | Docker testing (frozen legacy) | `--config`, `--create-only`, `--detect` |

### bench-core phase ladder

`--create-only` and `--detect` both leave sandboxes running; finish with `--cleanup`. Tier validation: Tier 0 `--provider fake` (no SDK) → Tier 1 docker (local daemon) → Tier 2 e2b (cloud). See [docs/guide/bench-core-usage-zh.md](docs/guide/bench-core-usage-zh.md).

## External Tool Dependencies

### Required tools (config via `.env`)

| Tool | Purpose | Config Key |
|------|---------|------------|
| `smap_tool` | Memory migration | Hardcoded in legacy design — should move to config |
| `devkit_top_down` | CPU top-down analysis | `DEVKIT_PATH` |
| `devkit_mem` | Cache/memory metrics | `DEVKIT_PATH` |
| `ksys` | Kernel metrics | `KSYS_PATH`, `KSYS_CONFIG_PATH` |
| `ub_watch` | NUMA interconnect | `UB_WATCH_PATH` |
| `smap_bw` | SMAP bandwidth | `SMAP_BW_PATH` |
| `getfre` | Core frequency | `GETFRE_PATH`, `GETFRE_CONFIG_PATH` |
| `AMDuProfPcm` | AMD uProf system counters (rotation slot) | `UPROF_PATH` |

### Tool output parsing

Each tool produces logs parsed by `vm_monitor/parsers.py`:

- `devkit_top_down.log` → DevKit_TopDown sheet (13 metrics)
- `devkit_mem.log` → DevKit_Memory, NUMA_Bandwidth sheets
- `ksys.log` → KSys sheet (11 metrics)
- `ub_watch.log` → UBWatch_Latency sheet (7 metrics)
- `smap_bw.log` → SMAPBW_Summary sheet (5 metrics)
- `getfre_NUMA*.log` → Getfre_Summary sheet

## Testing Workflow Patterns

### bench-core kernel (`src/bench_core/bench.py`)

```
1. provider.prepare_env()                                  # e2b sets SDK env vars
2. [cleanup-only] provider.cleanup_existing() -> exit     # --cleanup: list+kill, skips ready probe
3. create_all() | detect_from_ids() | detect_existing()     # --create-only/--detect
   [create-only] -> creation timing report -> save_ids() -> exit (keep running)
4. warmup waves (orchestrator)                              # [warmup-only] -> save_ids() -> exit (keep running)
5. stats_collector.start()
6. monitor.start() (auto vm-monitor, host-level; begin_stress at dispatch entry, end_stress at exit)
7. dispatch: round_robin -> RoundRobinTaskManager.run() | fixed -> TaskManager.start_all(); sleep(duration)
8. stop; stats_collector.stop(); cleanup_all() (skip in detect mode)
9. stats_collector.generate_report() + save_report()
```

### OpenStack VM test (`auto_vm_test.py`)

```
1. Delete old VMs → Confirm deletion
2. Create new VMs (n) → Wait for SSH ready
3. Start smap_tool (memory migration)
4. Warmup phase (browser warmup on all VMs)
5. Start vm_monitor (background)
6. Benchmark phase (active_percent VMs)
7. Collect results → Generate Excel
8. Cleanup (stop smap_tool, delete VMs)
```

### E2B batch test (`e2b_bench/batch_scheduler.py`)

```
1. Group tasks by (total_count, ratio) from matrix config
2. For each group: create sandboxes (shared) → start smap_tool → warmup (shared)
   - For each benchmark_percent: start vm_monitor → run benchmark (fixed/round-robin)
     → stop vm_monitor → save task results
   - Cleanup group
3. Extract metrics from vm_monitor + browser reports → Aggregate styled Excel summary
```

## Metrics Reference

See [docs/dev/metrics-reference.md](docs/dev/metrics-reference.md) for complete metric descriptions — `bench_core` kernel timing/task metrics, every `vm_monitor` sheet, and the `/proc`+`/sys` collection sources & calculation formulas.

### Key metrics to watch

| Metric | High Value Indicates | Sheet |
|--------|---------------------|-------|
| `td_backend_bound_percent` | CPU/memory stalls | DevKit_TopDown |
| `td_mem_bound_percent` | Memory bottleneck | DevKit_TopDown |
| `mem_l3d_miss_percent` | L3 cache issues | DevKit_Memory |
| `ksys_l3_latency_avg` | L3 latency high | KSys |
| `ub_avg_read_ns` | NUMA latency high | UBWatch_Latency |
| `browser_p99_latency_ms` | Browser performance issue | bench_report |

## Common Modifications

### Adding a new provider (kata / agentenv / gvisor)

1. Create `src/env_provider/<name>.py` implementing `EnvironmentProvider` (lifecycle + `exec`); keep an internal `id → handle` table.
2. If it has an SDK manager with create/detect/cleanup, subclass `BaseSandboxManager` (`_base.py`) and supply the SDK seams + class attrs; put config/schemas under `src/env_provider/<name>/`. For a lifecycle-capable backend that reuses an existing SDK, subclass that provider directly (see `env_provider/aenv/` — `AenvProvider` subclasses `E2BProvider` and only adds `pause`/`resume`/`snapshot_sizes`/`create_one`/`kill_one`, no new manager).
3. Register the provider in **two** places in `bench_core.bench` (both must be kept in sync):
   - the `_PROVIDERS` roster — drives the `--provider` argparse `choices` and the lifecycle/trajectory capability hint (`_capability_hint`);
   - the `_build_provider` dispatch (lazy import — the kernel never imports a backend statically).
4. Add `config/common/*.yaml` blocks (`<name>:`) as needed — the kernel reads only shared sections.
5. Add unit tests under `src/env_provider/tests/` (drive via `FakeProvider`-style stubs; no live SDK needed).

### Adding a new workflow

1. Add a `task_runner/<workflow>.py` + dispatch in `bench.py`.
2. If it needs a readiness probe, extend `ReadyChecker` (`_ready.py`) — keep it provider-transparent (no per-backend knobs).
3. Add `config/common/<workflow>-*.yaml` (both backend blocks).

### Adding a new metric source (legacy `vm_monitor`)

1. Add collection tool path to `.env`
2. Add parser in `vm_monitor/parsers.py`
3. Add exporter in `vm_monitor/exporters.py`
4. Add metric extraction in `batch_test_scheduler.py` or `e2b_bench/metrics_extractor.py`
5. Update `docs/dev/metrics-reference.md`

### Adding a new VMM type

1. Create class in `vm_monitor/` extending `VMMonitorBase`
2. Implement: `get_process_names()`, `extract_vm_id()`, `get_vms_realtime()`
3. Register in `vm_monitor/__init__.py`; add CLI flag in `vm_monitor/cli.py`
4. Update `docs/guide/usage-guide.md`

## File Locations

### Results directory structure

```
results/
├── batch_summary_*.xlsx         # OpenStack batch summary
├── vm{n}_ratio{r}_active{p}_*/  # OpenStack single test
│   ├── config.yaml, test_log.txt, vm_bench_lite/, vm_monitor/, summary/
└── e2b/batch/                   # E2B batch results
    ├── tc{n}_ratio{r}_*/        # Group dir (smap_tool/, task dirs, vm_monitor/)
    └── e2b_batch_summary_*.xlsx
```

bench-core reports go to each config's `report.output_dir` + `filename_prefix` (e.g. `results/browser/browser_bench_*.txt`). Create-only emits a creation-timing report (P50/P95/P99 of create + ready-check + total startup).

### Config files

```
config/
├── common/                     # backend-agnostic workflow configs (kernel; both e2b: + docker: blocks)
│   ├── browser.yaml, coding-ts.yaml, coding-go.yaml, coding-python.yaml, docker.yaml
├── openstack/                  # legacy OpenStack (batch_matrix, test_config_template, vm_bench.yaml)
├── e2b/                        # legacy E2B (bench, batch_matrix, batch_template, coding_*, pdf/xlsx_bench)
├── docker/                     # legacy docker (docker_bench.yaml)
└── tools/                      # getfre_config.yaml
```

## Known Issues / Limitations

1. **Deprecated scripts (marked in v0.2.0)**:
   - `vm_bench_lite.py` → deprecated; retained as an internal `ImportError` fallback in `auto_vm_test.py`. Use the `vm_bench/` package.
   - `create_server.py` → deprecated; retained as an internal `ImportError` fallback in `auto_vm_test.py`. Use `vm_bench --create-only`.
   - `qemu_monitor.py` and `qemu_monitor/` → removed in v0.4.0; use `vm_monitor`.
   - `session-replay/` → removed in v0.4.0; use `llm_replay`.
2. **smap_tool path hardcoded** in legacy flows — should move to `.env` or config.
3. **Legacy components lack unit tests** — the frozen `e2b_bench` / `docker_bench` / OpenStack core has little coverage. (The new `src/` kernel + providers have ~270 tests under `src/bench_core/tests/` + `src/env_provider/tests/`, driven by `FakeProvider`.)
4. **Two parallel benchmark implementations** — `src/bench_core` (kernel) and `e2b_bench/` (frozen legacy) are decoupled; the Phase-4 thin-delegate wiring was reverted. The kernel is the forward path.
5. **Working notes in root** — `findings.md`, `progress.md`, `task_plan.md` should move to `docs/`.

## Related Documentation

- [docs/guide/bench-core-usage-zh.md](docs/guide/bench-core-usage-zh.md) — src kernel usage (install→config→CLI→cleanup→troubleshooting)
- [docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md](docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md) — EnvironmentProvider + bench_core design (local working doc)
- [docs/architecture/design.md](docs/architecture/design.md) / [docs/architecture/design-en.md](docs/architecture/design-en.md) — system architecture (CN/EN)
- [docs/dev/metrics-reference.md](docs/dev/metrics-reference.md) — bench_core kernel + vm_monitor sheet metrics, /proc & /sys collection sources, calculation formulas
- [docs/guide/usage-guide.md](docs/guide/usage-guide.md) — detailed legacy tool usage
- [docs/guide/e2b-bench-usage.md](docs/guide/e2b-bench-usage.md) / [docs/guide/docker-bench-usage.md](docs/guide/docker-bench-usage.md) — frozen legacy backend guides
