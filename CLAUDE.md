# Agent VM Bench - Project Guide for AI Assistants

This file provides project-specific conventions, architecture overview, and working guidelines for AI assistants working on this codebase.

## What is Agent VM Bench

Agent VM Bench is a performance-testing framework for virtualization scenarios. The **`bench_core` kernel + `env_provider` contract** (under `src/`) is the host-agnostic core: one stress profile drives any sandbox backend by swapping `--provider`, and the same `config/common/*.yaml` runs on either e2b or docker. Scenarios:

- **E2B Sandbox** — Firecracker microVMs via the E2B API (`--provider e2b`)
- **CubeSandbox** — Cloud Hypervisor (KVM) microVMs via the `cubesandbox` SDK, native pause/resume WITH memory snapshot (`--provider cubesandbox`; lifecycle-capable like aenv)
- **Docker Containers** — browser automation in containerized environments (`--provider docker`)
- **OpenStack VM Memory Overcommit** — QEMU/KVM VMs with `smap_tool` memory migration (legacy `vm_bench/` + `auto_vm_test.py`; not yet ported to the kernel)

The framework collects **50+ performance metrics** from hardware counters, kernel metrics, and application-level measurements. The new kernel collects creation/readiness timing and workflow-level task metrics; integration with `vm_monitor` / `smap_tool` is a follow-on phase (batch scheduler).

The frozen-legacy `e2b_bench/`, `docker_bench/`, and OpenStack tools (`vm_bench/`, `auto_vm_test.py`, `batch_test_scheduler.py`) remain for existing users and **share no code with the `src/` kernel**. Edit them only for bug fixes; new work goes in `src/`.

## Build, Lint, Test Commands

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .          # registers bench-core + pulls core deps from pyproject.toml

bench-core --provider fake --config config/common/browser.yaml --create-only -n 1   # smoke (no SDK)
python -m pytest                     # full suite (src/bench_core + src/env_provider + legacy packages)
python -m pytest src/bench_core/tests src/env_provider/tests   # src kernel + providers only
ruff check .                         # lint (target-version py38, line-length 120)
ruff format --check .                # format check
python -m pre_commit run --all-files # pre-commit hooks (run on staged files before committing)
```

`pip install -e .` is sufficient — `pyproject.toml`'s `dependencies` declare the core stack (`psutil`, `paramiko`, `flask`, `PyYAML`, `pandas`, `openpyxl`, `e2b`, `aiohttp`); `requirements.txt` is a PyPI-publish superset and is redundant for the kernel. Backend SDKs are optional extras (`[project.optional-dependencies]`): `pip install e2b` (e2b + aenv, which subclasses the e2b SDK), `pip install docker`, and/or `pip install cubesandbox` only when using that provider; `--provider fake` needs none.

`pyproject.toml` sets `pythonpath = ["src"]` for pytest, so tests resolve `bench_core` / `env_provider` without an editable install. The `[project.scripts]` entries are `bench-core = "bench_core.bench:main"` and `vm-monitor = "vm_monitor.cli:main"`.

## Architecture

### Layered architecture (src kernel)

```
┌─────────────────────────────────────────────────────────────┐
│  CLI / entry                                                 │
│  bench-core (bench_core.bench.main) --provider {fake,e2b,docker,aenv,cubesandbox} │
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
                          ┌──────────────────┼──────────────────┐
                          ▼                  ▼                   ▼
            env_provider.e2b/aenv   env_provider.docker   env_provider.fake
            env_provider.cubesandbox
            (opt-in leaf submodules — e2b/aenv pull the e2b SDK, docker pulls
             the docker SDK, cubesandbox pulls the cubesandbox SDK, fake = no SDK)
```

The kernel (`bench_core`) and every provider impl depend on the contract (`env_provider`); neither owns it. The contract stays **SDK-free** — `from env_provider import EnvironmentProvider` never imports the e2b, docker, or cubesandbox SDK. Providers are **opt-in leaf submodules** loaded only by `bench_core.bench._build_provider` when selected, so the kernel never loads a backend it does not use. Adding a backend (kata, gvisor, …) is one new `env_provider` submodule; contract + kernel untouched.

**`bench_core` kernel** (`src/bench_core/`):
- `bench.py` — `run_benchmark(config, provider)` spine: `prepare_env → create/detect → (create-only | warmup-only | cleanup-only | benchmark) → stop → report`. `_build_provider` lazy-imports the selected provider submodule. `_promote` lifts provider `SandboxInstance` → kernel `BenchSandbox` (attaches workflow metrics).
- `config.py` — `KernelConfig`. `KernelConfig.from_raw(raw)` is the **single reader** of the shared stress sections (`sandbox` / `create_batch` / `task_batch` / `browser` / `coding` / `document` / `test` / `report` / `workflow_type`). `__post_init__` auto-fills `coding_source_files` from `CODING_LANGUAGE_DEFAULT_SOURCE_FILES[language]` (ts→vuejs/core, go→gohugoio/hugo, python→django/django), so coding configs omit `source_files`.
- `task_manager.py` + `task_runner/{browser,coding,document}.py` — the three workflows. Browser rides HTTP (agent-browser on port 18789) on top of a backend the provider starts; coding/document are pure `exec` (verify script written via heredoc through `exec`).
- `round_robin.py` — `RoundRobinTaskManager` (group rotation, per-step timing).
- `stats_collector.py` — snapshots + report generation (`generate_report` / `save_report`).
- `coding_payload.py` — canonical coding replacement pairs + verify scripts.
- `monitor.py` — `MonitorController` + `MonitorConfig`: host-level `vm-monitor` orchestration around the stress phase (stress-file sync subprocess; auto-enable by provider `vmm_type`; outputs to `report.output_dir/vm_monitor/`; optional host-sheet merge into replay obs xlsx).
- `schemas.py` (`BenchSandbox`), `utils.py` (`calc_percentiles`, `setup_logging`).

**`env_provider` contract** (`src/env_provider/__init__.py`): `EnvironmentProvider` ABC (`create_all`, `detect_existing`, `detect_from_ids`, `save_ids`, `check_alive`, `cleanup_all`, `cleanup_existing`, `prepare_env`, `prepare`, `exec`) + `SandboxInstance` / `CreationMetrics` / `CommandResult` / `SandboxStatus`. `exec()` is the **sole command primitive** — file writes go through `exec` as a heredoc, no separate upload method, so adding a provider is implementing `exec`. The provider keeps any SDK handle internally (`id → handle`); the kernel holds only the host-agnostic `SandboxInstance`.

**Shared backend infra** (`src/env_provider/_base.py`, `_ready.py`) — the e2b/docker managers' duplicated create/detect/cleanup skeletons were lifted here:
- `_ready.py` — `ReadyChecker`: workflow-driven poll-until-ready; backend supplies `_exec_probe`. browser=port scan (`ss|netstat grep :{port}`, ports `18789` openclaw-gateway + `11436` llama-server), coding=`uname -a`, document=`document-bench-validate` (non-zero exit = immediate image failure, not retried). Constants `READY_MAX_WAIT=300`, `READY_INTERVAL=5`.
- `_base.py` — `BackendSandboxStatus` / `BackendCreationMetrics` (byte-identical across backends) + `BaseSandboxManager(ABC)` lifecycle template. Subclass supplies SDK seams (`_create_single`, `_list_existing`, `_external_id`, `_attach`, `_kill_one`, `_exec_probe`) + class attrs (`_handle_attr`, `_noun`, `_id_attr`, `_set_killed_on_cleanup`). `_ready_config` is a **concrete base method** returning shared constants — readiness is a workflow concern, so there are no per-backend readiness knobs and no `port_check` block in any YAML.

**Provider impls**: `env_provider/e2b/` (`config.py` `setup_e2b_env` + placeholder-credential fallback to `~/.e2b/config.json`, `manager.py`, `schemas.py`; exec-only), `env_provider/aenv/` (subclasses `E2BProvider`, adds `pause`/`resume` (E2B `beta_pause`/`connect`) + `snapshot_sizes` (host stat scan) + `create_one`/`kill_one` — `LifecycleCapable` + `EphemeralCapable` + `SnapshotSizeCapable`), `env_provider/cubesandbox/` (Cloud Hypervisor microVM via the `cubesandbox` SDK; native `pause(wait=True)`/`connect`-resume, `SnapshotInfo` has no size fields so `snapshot_sizes` returns `None` for now; same lifecycle/ephemeral shape as aenv), `env_provider/docker/` (`config.py` reads only image/prefix/resources, `manager.py`, `schemas.py`), `env_provider/fake.py` (in-memory, drives `run_benchmark` in unit tests with no SDK).

**Config layering**: one `config/common/*.yaml` per workflow carries **both** `e2b:` and `docker:` blocks; `--provider` selects which `Config.from_raw` reads. The kernel reads only the shared sections. Credential placeholders (`your_e2b_access_token_here` / `your_e2b_api_key_here`) are treated as unset → fall back to `~/.e2b/config.json`.

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
| `src/bench_core/` | host-agnostic kernel (recommended) | `bench.py`, `config.py`, `task_manager.py`, `round_robin.py`, `stats_collector.py`, `task_runner/{browser,coding,document}.py`, `coding_payload.py` |
| `src/env_provider/` | provider contract + e2b/aenv/docker/cubesandbox/fake impls | `__init__.py` (ABC + Protocols), `_base.py`, `_ready.py`, `e2b/`, `aenv/`, `docker/`, `cubesandbox/`, `fake.py` |
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

A top-level `monitor:` block (peer of `report:`) controls host-level `vm_monitor` collection during the stress phase. Default `enabled: auto` turns it on only for providers with a VMM (`vmm_type`): e2b/aenv → `firecracker`; docker/fake/cubesandbox → skipped (cube's VMM process name — `cube-hypervisor` vs `cloud-hypervisor` — is unresolved; vm_monitor integration is a follow-on). `vm-monitor` runs as a local subprocess (stress-file sync) bracketing active stress; outputs (CSV + `analysis_report.xlsx` + SVG) land in `<report.output_dir>/vm_monitor/`. By default (`merge_report: false`) the replay result is **two separate files**: vm_monitor's `analysis_report.xlsx` (system resources) stands alone, and the replay `<prefix>_obs.xlsx` carries trajectory/replay metrics only (incl. a per-trajectory per-step detail sheet). Set `merge_report: true` to copy host sheets (`VM_Stats`/`NUMA_Overview`/`DevKit_TopDown`) into the obs workbook instead. `--no-vm-monitor` short-circuits it off. Never compromises the bench: missing binary/tools or an unwritable lock dir degrade to a warning + skip.

### Replay workflow (trajectory / lifecycle replay)

`config/common/replay.yaml` drives the replay workflow (`workflow_type: replay`): deterministic replay of recorded SWE-bench trajectories against sandbox backends, primarily `--provider aenv` or `--provider cubesandbox` (both lifecycle pause/resume) or `--provider e2b` (exec-only). `replay.yaml` ships as the aenv **lifecycle 1:1 (no-oversubscription) baseline**; see [docs/bench-core-usage-zh.md](docs/bench-core-usage-zh.md) §8 for the mode guide. Modes (per `replay.mode`, defaults to the provider's `default_replay_mode`):

- `exec_only` — long-lived sandboxes, continuous exec of trajectory steps, no pause/resume. Baseline for pure exec-replay cost; used when the backend has no lifecycle capability (e2b/docker/fake).
- `lifecycle` — long-lived sandboxes: `create_all` → pause once → per-step `provider.resume()`/`pause()` (aenv + cubesandbox, `LifecycleCapable`). Oversubscription = **snapshot memory reuse** — pause frees RAM, so `k×N` sandboxes fit in `running_concurrency = N` slots. Emits `initial_pause` + per-slice `pause`/`resume` segments and `snapshot_size` events (aenv `SnapshotSizeCapable` returns host-statted sizes; cubesandbox returns `None` for now — its `SnapshotInfo` has no size fields, so the `snapshot_size` event is skipped, not crashed).
- `trajectory` — ephemeral `create_one`/`kill_one` per trajectory (`EphemeralCapable`; aenv + cubesandbox); per-step resume/pause still runs. Oversubscription = **queue limiting** (M slots gate concurrent trajectories; the rest defer `create`, not pause-to-free-memory). `launch_interval_sec` (per-sandbox create pacing) is **trajectory-only** — lifecycle pre-creates via `create_all` batches (`create_batch`, integer-second intervals), so it has no sub-second per-VM launch pacing.

The three modes differ by **sandbox lifecycle** (long-lived exec-only / long-lived pause-resume / ephemeral create-kill), not by "whether a rate limiter is attached"; `launch_interval_sec` exists in trajectory only because frequent per-trajectory `create_one` calls need pacing. Oversubscription ratio: with a fixed host (e.g. 1.5 TiB / 4 GiB per VM → 384 baseline), `running_concurrency` stays at the baseline (N slots) and `total_count` scales to `k×384` for ratio `1:k` (1:2 → 768/384, 1:3 → 1152/384). Scale `round_size` with `total_count` (single group = all concurrent); each ratio is one run.

Outputs: a text report (`<output_dir>/<prefix>_<ts>.txt`), a JSONL lifecycle series (`<prefix>_lifecycle_series.jsonl`), and (with `report.format: xlsx|both`) an 8-sheet observability workbook (`<prefix>_obs.xlsx`: Overview [consolidates Run / Throughput / Admission & QPS / Retry impact as grouped, color-coded sections], Per-step timings, Lifecycle overhead, Trajectory summary, Step detail, Concurrency states, Gantt, Snapshot sizes). The vm_monitor system-resource report (`analysis_report.xlsx`) is a separate file by default (`monitor.merge_report: false`). All series events are `time.time()`-stamped for direct join with `vm_monitor` host samples (see `monitor:` above).

Multi-template routing: trajectories may declare per-file concrete templates via a side `template_manifest` (referenced by `replay.template_manifest`). The fleet is round-robin-allocated over the pool's (template, trajectory) pairs, runners route by template affinity (orphan templates skip with a count), and trajectory mode passes `template=` per `create_one`.

### Logging

- Python `logging` module, not `print`. Levels: DEBUG (verbose), INFO (progress), WARNING (issues), ERROR (failures).
- Streaming file logs use `buffering=1` (line buffering); include timestamps.

### Git / commit / PR rules (mandatory)

- **Branch first.** Never commit directly to `main`; cut a feature branch (`feat/...`).
- **Relevant files only.** `git add` only the files relevant to the change — never `git add -A` with unrelated churn. **Never commit `docs/superpowers/*`** (plans/specs are local working docs).
- **Pre-commit before commit.** Run `python -m pre_commit run --files <files>` on the staged set; fix failures before committing.
- **No Claude attribution.** Do not add `Co-Authored-By: Claude` or similar trailers.
- **Commit messages** via heredoc `git commit -F - <<'EOF'`; Conventional Commit prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `ci:`, `chore:`).
- **IP & secret scrubbing.** Private `192.168.x.x` addresses are OK to commit; scrub public IPs. Never commit real E2B access tokens / API keys — leave placeholders.
- **Frozen legacy.** `e2b_bench/` and `docker_bench/` are frozen standalone legacy — do not refactor them into the kernel.

## Entry Points

| Script | Purpose | CLI |
|--------|---------|-----|
| `bench-core` (`bench_core.bench:main`) | host-agnostic kernel (recommended) | `--config`, `--provider {fake,e2b,docker,aenv,cubesandbox}`, `--create-only`, `--detect`, `--warmup-only`, `--cleanup`, `-n`, `-bm`, `--test-duration`, `--vm-monitor {auto,true,false}`, `--no-vm-monitor` |
| `vm-monitor` (`vm_monitor.cli:main`) | VMM monitoring | `-t`, `-i`, `--vmm`, `--enable-capture` |
| `vm_bench/__main__.py` | OpenStack VM browser/QA + create | `--create-only`, `--detect`, `-n`, `--start-ip`, `--browser-url` |
| `auto_vm_test.py` | Single OpenStack test | `--config` |
| `batch_test_scheduler.py` | Batch OpenStack tests | `--config`, `--dry-run`, `--offline` |
| `e2b_bench/__main__.py` | E2B testing (frozen legacy) | `--config`, `--batch`, `--detect`, `-bm round_robin` |
| `docker_bench/__main__.py` | Docker testing (frozen legacy) | `--config`, `--create-only`, `--detect` |

### bench-core phase ladder

`--create-only` and `--detect` both leave sandboxes running; finish with `--cleanup`. Tier validation: Tier 0 `--provider fake` (no SDK) → Tier 1 docker (local daemon) → Tier 2 e2b/aenv/cubesandbox (cloud / lifecycle-capable microVM). See [docs/bench-core-usage-zh.md](docs/bench-core-usage-zh.md).

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

See [docs/metrics-reference.md](docs/metrics-reference.md) for complete metric descriptions — `bench_core` kernel timing/task metrics, every `vm_monitor` sheet, and the `/proc`+`/sys` collection sources & calculation formulas.

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

### Adding a new provider (kata / gvisor / …)

The `cubesandbox` provider (`src/env_provider/cubesandbox/`) is the most recent worked example of this recipe — a full lifecycle backend (`LifecycleCapable` + `EphemeralCapable` + `SnapshotSizeCapable`) over the native `cubesandbox` SDK; mirror it for a new lifecycle-capable backend.

1. Create `src/env_provider/<name>/` implementing `EnvironmentProvider` (lifecycle + `exec`); keep an internal `id → handle` table. For replay lifecycle/trajectory modes, also implement the `LifecycleCapable` / `EphemeralCapable` / `SnapshotSizeCapable` Protocols (structural — no base class needed; `isinstance` checks are runtime).
2. If it has an SDK manager with create/detect/cleanup, subclass `BaseSandboxManager` (`_base.py`) and supply the SDK seams + class attrs; put config/schemas under `src/env_provider/<name>/`.
3. Register the provider name in `bench_core.bench._build_provider` (lazy import).
4. Add `config/common/*.yaml` blocks (`<name>:`) as needed — the kernel reads only shared sections.
5. Add the SDK to `[project.optional-dependencies]` in `pyproject.toml` (the module imports it under try/except; install only when using that provider).
6. Add unit tests under `src/env_provider/tests/` (drive via `FakeProvider`-style stubs; no live SDK needed).

### Adding a new workflow

1. Add a `task_runner/<workflow>.py` + dispatch in `bench.py`.
2. If it needs a readiness probe, extend `ReadyChecker` (`_ready.py`) — keep it provider-transparent (no per-backend knobs).
3. Add `config/common/<workflow>-*.yaml` (both backend blocks).

### Adding a new metric source (legacy `vm_monitor`)

1. Add collection tool path to `.env`
2. Add parser in `vm_monitor/parsers.py`
3. Add exporter in `vm_monitor/exporters.py`
4. Add metric extraction in `batch_test_scheduler.py` or `e2b_bench/metrics_extractor.py`
5. Update `docs/metrics-reference.md`

### Adding a new VMM type

1. Create class in `vm_monitor/` extending `VMMonitorBase`
2. Implement: `get_process_names()`, `extract_vm_id()`, `get_vms_realtime()`
3. Register in `vm_monitor/__init__.py`; add CLI flag in `vm_monitor/cli.py`
4. Update `docs/usage-guide.md`

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

- [docs/bench-core-usage-zh.md](docs/bench-core-usage-zh.md) — src kernel usage (install→config→CLI→cleanup→troubleshooting)
- [docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md](docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md) — EnvironmentProvider + bench_core design (local working doc)
- [docs/design.md](docs/design.md) / [docs/design-en.md](docs/design-en.md) — system architecture (CN/EN)
- [docs/metrics-reference.md](docs/metrics-reference.md) — bench_core kernel + vm_monitor sheet metrics, /proc & /sys collection sources, calculation formulas
- [docs/usage-guide.md](docs/usage-guide.md) — detailed legacy tool usage
- [docs/e2b-bench-usage.md](docs/e2b-bench-usage.md) / [docs/docker-bench-usage.md](docs/docker-bench-usage.md) — frozen legacy backend guides
