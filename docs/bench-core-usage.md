# bench-core Usage Guide (src kernel)

[**中文版**](bench-core-usage-zh.md)

> The host-agnostic stress-test kernel. It drives e2b / docker / aenv / fake backends
> through the `EnvironmentProvider` abstraction. It coexists with — and shares no code
> with — the frozen legacy `e2b_bench/` and `docker_bench/`. For architecture, see the
> [design doc](superpowers/specs/2026-08-12-environment-provider-bench-core-design.md).

## Overview

`bench_core` decouples the stress flow from the sandbox implementation: the kernel issues
every command through a single `exec()` primitive, and a sandbox backend (e2b / docker /
aenv / future kata …) only has to run that command inside a sandbox and return the result.
Swapping a backend is therefore just `--provider` — **the same stress profile runs the same
load curve on any backend**.

- `src/bench_core/` — the kernel: `run_benchmark` spine, stats / round-robin / task
  runners, `KernelConfig`. It never statically imports a backend SDK.
- `src/env_provider/` — the contract (`EnvironmentProvider` ABC + `SandboxInstance`) and
  the e2b / docker / aenv / fake provider impls (opt-in submodules; the contract itself
  stays SDK-free).
- `config/common/` — backend-agnostic workflow configs (each carries an `e2b:` and a
  `docker:` block; `--provider` selects which is read).

---

## 1. Install

After an editable install both `bench-core` and `python -m bench_core` work with **no
`PYTHONPATH=src`**:

> Requires **Python 3.10+** (CI runs 3.13; see `pyproject.toml` `requires-python`).

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

`pip install -e .` pulls the core deps declared in `pyproject.toml` (`psutil`, `paramiko`,
`flask`, `PyYAML`, `pandas`, `openpyxl`, …) — `requirements.txt` is not needed for the
kernel. Backend SDKs are opt-in (install only the one you use; `fake` needs none):

```bash
pip install e2b       # --provider e2b
pip install docker    # --provider docker
```

> Verify in one command (no SDK): `bench-core --provider fake --config config/common/browser.yaml --create-only -n 1`
> — if it runs, the kernel + CLI + config parse are all ready.

---

## 2. Config

### 2.1 File structure

Each YAML in `config/common/` is one workflow and carries both backend blocks:

```yaml
workflow_type: browser        # browser | coding | document | replay

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
sandbox:      { total_count: 100, ... }
create_batch: { size: 20, interval: 3 }
task_batch:   { size: 10, interval: 5 }
browser:      { urls: [...], warmup_urls: [...], ... }
test:         { duration: 160, benchmark_mode: "round_robin", ... }
report:       { output_dir: "results/browser", filename_prefix: "browser_bench" }
```

`KernelConfig.from_raw` is the **single reader** of the shared sections
(`workflow_type` + `sandbox` / `create_batch` / `task_batch` / `browser` / `coding` /
`document` / `replay` / `test` / `report` / `monitor`); each backend's own `Config.from_raw`
reads only its block. `--provider` picks the block, so **the same stress profile runs on
any backend**.

### 2.2 Workflow configs

| Config | workflow | Notes |
|--------|----------|-------|
| `browser.yaml` | browser | round-robin tab-switch, 100 sandboxes |
| `coding-ts.yaml` | coding | TypeScript (vuejs/core), `npx tsx` verify, `verify_repeat: 3` |
| `coding-go.yaml` | coding | Go (gohugoio/hugo), `go run` verify, `verify_repeat: 1` |
| `coding-python.yaml` | coding | Python (django/django), `python3` verify, `verify_repeat: 1` |
| `docker.yaml` | browser | docker-only small profile (10 containers, single URL) |
| `replay.yaml` | replay | aenv lifecycle 1:1 baseline (see §8) |
| `replay-exec-only.yaml` | replay | exec_only baseline (e2b/docker/fake) |
| `replay-trajectory.yaml` | replay | trajectory oversubscription profile |

### 2.3 Why the coding configs are so thin

All three coding configs **omit `source_files`** — `KernelConfig.__post_init__` auto-fills
the canonical replacement pairs from `CODING_LANGUAGE_DEFAULT_SOURCE_FILES[language]`
(6 pairs per language, drawn from real SWE-bench instances): ts → vuejs/core, go →
gohugoio/hugo, python → django/django. The pairs live in
`src/bench_core/payload/coding_payload.py`; a config only declares `language` +
`verify_cmd` + `verify_repeat`.

### 2.4 Credentials: placeholder auto-fallback

`your_e2b_access_token_here` / `your_e2b_api_key_here` in a YAML are placeholders. The e2b
provider treats them as unset and falls back to `~/.e2b/config.json` (the E2B CLI config,
reading `teamApiKey` / `accessToken`), so **copy the template as-is — don't put real keys
in YAML**. Point `E2B_CONFIG` at another path to override.

### 2.5 Readiness is provider-transparent

Readiness (waiting for a sandbox to be usable after creation) is a **workflow concern, not
a backend knob** — driven by `src/env_provider/_ready.py` (`ReadyChecker`), with e2b/docker
running the same logic:

- browser → port scan for `18789` (openclaw-gateway) + `11436` (llama-server)
- coding → `uname -a` returns non-empty
- document → `document-bench-validate` exits 0 (a completed non-zero exit is an immediate
  image failure, not retried)
- replay → reuses the coding probe

Constants `READY_MAX_WAIT = 300`, `READY_INTERVAL = 5`. **No `port_check` / timing knobs
appear in any `config/common/*.yaml`** — they are kernel constants. (The legacy
`config/docker/docker_bench.yaml` still ships a `port_check:` block, but the kernel's
docker `Config.from_raw` never reads it.)

---

## 3. CLI

```text
bench-core --config <yaml> --provider {fake,e2b,docker,aenv} [mode/params]
```

| Flag | Description |
|------|-------------|
| `--config` | YAML config path |
| `--provider` | `fake` (no SDK) / `e2b` / `docker` / `aenv` |
| `-n, --total-count` | override sandbox count |
| `--workflow-type` | `browser` / `coding` / `document` / `replay` |
| `-bm, --benchmark-mode` | `fixed` / `round_robin` |
| `--round-count` / `--round-size` / `--test-duration` / `--benchmark-percent` | override benchmark params |
| `--create-only` | create + ready-check + persist IDs, then exit (keep running) |
| `--detect` | reuse existing sandboxes (no create); no cleanup at end |
| `--warmup-only` | create/detect + warmup, then exit (keep running) |
| `--cleanup` | list + kill all existing sandboxes, then exit |
| `-o, --output-dir` | override report output dir |
| `--report-format` | `txt` (default) / `xlsx` / `both` (xlsx adds an openpyxl workbook) |
| `--vm-monitor` | `auto` (default, by provider `vmm_type`) / `true` / `false` |
| `--no-vm-monitor` | short-circuit vm_monitor off (overrides `--vm-monitor` and YAML) |

> `bench-core: command not found`? See §7. Equivalent: `python -m bench_core …`.

---

## 4. Workflow: the phase ladder

Validate tier-by-tier — create → reuse+warmup → short benchmark → cleanup — so a failure
locates the phase immediately. `--create-only` and `--detect` both leave sandboxes running,
so finish with `--cleanup`.

### Tier 0 — fake (zero deps, validates the kernel)

No SDK, no daemon, seconds. Validates the full `run_benchmark` spine + report generation:

```bash
bench-core --provider fake --config config/common/browser.yaml   --test-duration 10 -n 3
bench-core --provider fake --config config/common/coding-ts.yaml --test-duration 10 -n 3
```

### Tier 1 — docker (local daemon, real backend)

Prerequisite: Docker daemon reachable; image built and its openclaw-gateway (18789) +
llama-server (11436) listen (browser readiness scans these ports).

```bash
# 1) create 2 containers, ready-check, persist IDs
bench-core --provider docker --config config/common/browser.yaml --create-only -n 2

# 2) detect existing containers + warmup (docker detect keys on prefix oc-bench-*)
bench-core --provider docker --config config/common/browser.yaml --detect --warmup-only

# 3) detect + 30s short benchmark + report (detect mode does not kill at the end)
bench-core --provider docker --config config/common/browser.yaml --detect --test-duration 30

# 4) cleanup
bench-core --provider docker --config config/common/browser.yaml --cleanup
```

> To validate **only the provider wiring** (not the 300s port wait), temporarily point a
> coding config's `docker.image` at the chromium image and run its `--create-only`:
> coding readiness is `uname -a`, which a browser image satisfies instantly. This checks
> create/list/exec_probe/cleanup without depending on the openclaw services.

### Tier 2 — e2b (cloud firecracker, real backend)

Prerequisite: `~/.e2b/config.json` has credentials; e2b dev server (`E2B_API_URL`)
reachable; templates built (`openclaw-browser-v1` / `openclaw-coding-{ts,go,python}-v1`).

```bash
# 1) create + persist IDs to sandboxs_ts.txt
bench-core --provider e2b --config config/common/coding-ts.yaml --create-only -n 2

# 2) detect from the ID file + warmup
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --warmup-only

# 3) detect + 30s short benchmark + report
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --test-duration 30

# 4) cleanup
bench-core --provider e2b --config config/common/coding-ts.yaml --cleanup
```

browser / coding-go / coding-python are the same with a different `--config`; each declares
its own `sandbox_ids_file` (`sandboxs.txt` / `sandboxs_ts.txt` / `sandboxs_go.txt` /
`sandboxs_python.txt`).

### Phase cheat sheet

| Command | Verifies | Sandbox fate |
|---------|----------|--------------|
| `--create-only` | create + ready + ID persistence | kept |
| `--detect --warmup-only` | detect + attach + warmup | kept |
| `--detect --test-duration N` | full spine + report | kept (detect doesn't cleanup) |
| `--cleanup` | list + teardown | removed |

---

## 5. Report

Every run is stamped into its own subdirectory so outputs never overwrite a previous run:
`<output_dir>/<filename_prefix>_<run_stamp>/` (run_stamp = `%Y%m%d-%H%M%S`). Defaults:
`output_dir = "results/kernel"`, `filename_prefix = "bench"` (both set in the `report:`
YAML section; `-o` overrides `output_dir`). Inside that subdir:

| File | When |
|------|------|
| `<prefix>.log` | always (JSON-lines for lifecycle/trajectory replay modes) |
| `<prefix>_<timestamp>.txt` | always — the text report |
| `<prefix>_obs.xlsx` | replay workflow + `--report-format xlsx|both` |
| `<prefix>_lifecycle_series.jsonl` | replay lifecycle **and** trajectory modes |
| `replay_result.json` (per trajectory) + `trajectories/index.json` | when a series file exists |
| `vm_monitor/` | auto-enabled for providers with a `vmm_type` (e2b/aenv → firecracker) |

`--create-only` emits a **creation-timing report**: a `[Sandbox Status]` block
(`Total` / `Ready` / `Create Failed` / `Ready Check Failed`) plus percentile sections for
`Sandbox.create`, `Ready Check Wait`, and `Total Startup` (Min/Max/Avg + P50/P95/P99). A
full run emits a performance report (task stats + snapshots); the `Create Failed` /
`Ready Check Failed` counters pinpoint whether the failure is at create, ready, or task
phase.

---

## 6. Python API

```python
from bench_core.bench import run_benchmark, load_config
from bench_core.config import KernelConfig
from env_provider.fake import FakeProvider

# 1) load from YAML (KernelConfig reads shared sections; raw is passed through to the backend)
config, raw = load_config("config/common/browser.yaml")

# 2) build a provider (e2b/docker/aenv build_provider also take (config, raw))
provider = FakeProvider(count=config.total_count)

# 3) run
result = run_benchmark(config, provider)
print(result["report"])            # report text
print(result["filepath"])          # report file path (None on create-only/warmup-only)
print(result["admission_snapshot"])  # replay admission snapshot (None outside lifecycle/trajectory)
```

Signatures:
- `load_config(path) -> tuple[KernelConfig, dict]` — opens the YAML, calls
  `KernelConfig.from_raw(raw)`, returns `(config, raw_dict)`.
- `run_benchmark(config, provider) -> dict` — returns `{"report", "filepath",
  "admission_snapshot"}` on the full path; early-exit paths (`--create-only`,
  `--warmup-only`, `--cleanup`) return only `{"report", "filepath"}`; returns `{}` if no
  sandbox reaches ready.

---

## 7. Troubleshooting

**`bench-core: command not found`**
The script installs under the active interpreter's `Scripts/` (e.g. conda's
`C:\Users\<user>\miniconda3\Scripts\bench-core.exe`), which is only on `PATH` when that
env is active. Activate it, or use `python -m bench_core …` (no `PATH` needed). The command
is `bench-core` (hyphen), not `bench_core`.

**Ready-check timeout (Ready Check Failed)**
- browser: openclaw-gateway (18789) + llama-server (11436) didn't come up inside the
  sandbox → check the image/template.
- coding/document: the sandbox didn't start properly → check the image/template + sandbox
  logs. For document, a completed non-zero `document-bench-validate` means the image itself
  is broken (not retried).

**e2b credential failure**
Confirm `~/.e2b/config.json` exists with `teamApiKey` / `accessToken`, or set `E2B_CONFIG`
to another path. Keep the YAML placeholders (auto-fallback works) — don't write real keys
into YAML.

**docker coding image missing**
The `ubuntu-openclaw-coding-{ts,go,python}:24.04-arm64` images in coding configs are
placeholders — build them first (with the language toolchain + project repo) before running
`--provider docker` coding. The browser image is ready-made.

---

## 8. Replay workflow

`workflow_type: replay` replays recorded SWE-bench agent trajectories (ordered shell +
`str_replace_editor` actions, each with a per-step `delay_time`) verbatim through
`provider.exec()`. The same profile runs on aenv (lifecycle pause/resume) or e2b/docker
(exec_only). `config/common/replay.yaml` is the aenv lifecycle **1:1 (no-oversubscription)
baseline**.

### 8.1 The three modes

The modes differ by **sandbox lifecycle**, not by "whether a rate limiter is attached":

| mode | sandbox lifecycle | per step | concurrency / oversubscription | when to use |
|------|-------------------|----------|-------------------------------|-------------|
| `exec_only` | pre-created, **long-lived**; no create/kill across the run | exec only | none | pure exec-replay cost baseline; backends without lifecycle/ephemeral capability (e2b/docker/fake) |
| `lifecycle` | pre-created, **long-lived**; not killed across the run | acquire slot → resume → exec → pause → release | pause snapshots free RAM, so `k×N` sandboxes fit in `N` slots (**memory oversubscription**) | pause/resume snapshot overhead + memory overcommit; needs `LifecycleCapable` (aenv) |
| `trajectory` | **ephemeral**; create → … → kill per trajectory | acquire slot (held for the whole trajectory) → resume → exec → pause → release | `M` slots gate concurrent trajectories; the rest defer create (**queue limiting, not memory reuse**) | frequent create/kill overhead + launch pacing; needs `EphemeralCapable` (aenv) |

**exec_only vs trajectory** — the difference is *not* "a rate limiter":

- exec_only sandboxes are **long-lived**: the whole run reuses one pre-created fleet, only
  exec, **no create/kill, no pause/resume**.
- trajectory sandboxes are **ephemeral**: each trajectory does `create_one` → run →
  `kill_one`.
- `launch_interval_sec` is trajectory-only because **frequent per-trajectory creates need
  pacing**; exec_only pre-creates once and reuses, so it has no launch pacing. "trajectory
  adds a limiter" is just the surface symptom — the root difference is sandbox lifecycle
  (long-lived reuse vs. ephemeral create/kill).

**lifecycle vs trajectory** — the oversubscription mechanisms differ:

- lifecycle oversubscription = **snapshot memory reuse**. Sandboxes are long-lived; pause
  frees physical RAM, so `total_count = k×N` sandboxes fit in `running_concurrency = N`
  slots. Running slots are acquired/released at **step granularity** (one command =
  acquire/release).
- trajectory oversubscription = **queue limiting**. Running slots are held at **whole-
  trajectory granularity** (acquire before create, release after kill); `M` slots → at most
  `M` trajectories run concurrently, the rest queue — there is **no "pause to free
  memory"**, and a sandbox is killed as soon as its trajectory finishes.

> `launch_interval_sec` (float seconds, per-sandbox create pacing) is **trajectory-only**.
> `create_batch.size` / `create_batch.interval` (integer seconds) pace the initial fleet
> `create_all` for the long-lived modes (lifecycle **and** exec_only); trajectory skips
> `create_all` entirely. Sub-second per-sandbox pacing is therefore only available in
> trajectory mode.

### 8.2 lifecycle memory oversubscription: ratio configuration

With a fixed host, the baseline VM count = host memory / per-VM memory:

- e.g. 1.5 TiB host, 4 GiB per VM → baseline = 1536 / 4 = **384** VMs.
- `running_concurrency` stays at the baseline (N running slots); `total_count` scales to
  `k × baseline` for oversubscription ratio `1:k`.

| ratio | `total_count` | `running_concurrency` | meaning |
|-------|---------------|----------------------|---------|
| 1:1 (baseline) | 384 | 384 | no oversubscription, 384 sandboxes all running |
| 1:2 | 768 | 384 | 2× overcommit, 768 sandboxes multiplexed over 384 slots |
| 1:3 | 1152 | 384 | 3× overcommit |

`config/common/replay.yaml` is the 1:1 baseline. To test another ratio, change two values
in YAML (or override `total_count` with `-n`, but `running_concurrency` and `round_size`
live in YAML and must move with it):

```yaml
sandbox:
  total_count: 768        # k × baseline
test:
  round_size: 768         # = total_count -> one group = all -> all concurrent
  # running_concurrency: 384   unchanged (N slots)
```

```bash
bench-core --provider aenv --config config/common/replay.yaml -n 768
```

> To sweep several ratios and chart a degradation curve, loop a script that sets
> `total_count` + `round_size` and runs each ratio as one run.

### 8.3 Trajectory format and template_manifest

- The loader (`src/bench_core/payload/replay_payload.py`) expects each trajectory JSON to be
  `{instance_id, environment, trajectory: [{action, delay_time}, …]}` (`environment`
  defaults to `"main"`, `instance_id` falls back to the filename stem), truncated at the
  first terminal action (`submit` / `finish` / `done` — discarded with its `delay_time`).
  Accepted suffixes: `.replay.json` / `.json` / `.traj`. Convert other formats (e.g. raw
  sweagent) to `.replay.json` first.
- `template_manifest` is a side JSON mapping `{trajectory-relative-path: template}` (paths
  relative to `replay_trajectory_dir`, backslashes normalized). With multiple templates,
  non-trajectory modes route by template affinity (orphan templates are skipped with a
  count); trajectory mode passes `template=` into each `create_one`. A missing entry
  resolves to `None` (warning, provider default).

### 8.4 Observability workbook (`*_obs.xlsx`)

With `--report-format xlsx|both` (replay workflow), besides the text report and JSONL
lifecycle series, bench-core emits `<run-dir>/<prefix>_obs.xlsx` — an 8-sheet observability
workbook (openpyxl; `Overview` consolidates the former Admission & QPS / Throughput &
overcommit / Retry scalar sheets into one grouped, color-coded dashboard). All duration
columns are **seconds (s)**, matching the reference `step-detail.csv`; the embedded line
charts in `Per-step timings` / `Lifecycle overhead` use milliseconds (ms) for readability
(header-marked). Sheets that depend on the lifecycle series output headers only (no error)
when the series is absent (e.g. a minimal install).

| Sheet | row granularity | content |
|-------|-----------------|---------|
| Overview | scalar (consolidated, grouped/color-coded) | **single summary**: Run (mode/total_count/running_concurrency/test_duration/wall_sec/steps/success/failed/overcommit_ratio) + Throughput (steps_per_sec/effective_parallelism/exec_wall_utilization/concurrency) + Admission & QPS (running-slot maximum/active/peak_active/granted/avg_queue_wait/waiting + QPS limiter qps/inflight_cap/in_flight/dispatched/avg_wait/max_wait + per-operation dispatch/wait sub-tables) + Retry (retry_count/time_lost_to_retry_sec/retries_per_slice_p95 + per-operation retry_queued). Column A labels are bold/filled; groups separated by banner rows |
| Per-step timings | pooled percentiles | fleet `latency` (= pure exec time) n/min/max/avg/p50/p95/p99, bucketed by `action_type`; embedded line chart (ms) |
| Lifecycle overhead | pooled percentiles | `resume` / `pause` / `slice_total` / `slot_held` / `interaction` percentiles; embedded chart (ms). lifecycle/trajectory only |
| Trajectory summary | one row per trajectory | n_steps + segment sums (slice_total/exec/resume/pause/interaction_total/slot_wait/resume_queue_wait/pause_queue_wait/running_slot_held) + avg_slice (seconds). Sorted by trajectory_id; trajectory mode also appends create/kill percentiles |
| Step detail | one row per step event | 20 columns (see below); includes success and synthesized `slice_failed` rows; sorted by (trajectory, sandbox, step); frozen header + autofilter |
| Concurrency states | one row per second | per-second dominant-state counts (pausing/paused/resuming/exec/active); chart |
| Gantt | chart | per-sandbox phase timeline (resume/exec/pause), embedded PNG; auto-shrinks row height for large fleets |
| Snapshot sizes | one row per pause | logical/disk/inherited/cumulative MiB + generations/files; chart. `SnapshotSizeCapable` (aenv) only |

> With `monitor.merge_report: true`, up to three host sheets (`VM_Stats` /
> `NUMA_Overview` / `DevKit_TopDown`) are appended after `Snapshot sizes` (copied from the
> vm_monitor `analysis_report.xlsx`). With the default `false`, the vm_monitor report stays
> a separate file and the workbook carries only the 8 sheets above.

#### Step detail columns (20, seconds)

Sub-segments nest under their parent so the sum invariant is verifiable in-sheet:
`resume_sec == resume_queue_wait_sec + resume_api_sec + resume_ready_wait_sec`,
`pause_sec == pause_queue_wait_sec + pause_api_sec`.

| column | meaning |
|-------|---------|
| `trajectory_id` | instance_id of the trajectory this step belongs to |
| `sandbox_index` | index of the executing sandbox in the fleet (0..N-1), not the backend sandbox_id |
| `round_id` | round-robin round number; empty for fixed/trajectory modes |
| `step_index` | step index within the trajectory (0-based) |
| `action_type` | `shell` / `bash` / `str_replace_editor` / `submit` / `finish` / `done` |
| `slice_failed` | runner-synthesized failed slice (exception/stop_on_error); when True the duration columns below are 0 |
| `resume_sec` | total resume time = queue + api + ready_wait |
| `resume_queue_wait_sec` | QPS-limiter queue wait (resume) |
| `resume_api_sec` | pure resume API call time |
| `resume_ready_wait_sec` | post-resume readiness probe wait (lifecycle/trajectory; 0 in exec_only) |
| `exec_sec` | pure `provider.exec()` wall time (= Per-step timings `latency`) |
| `pause_sec` | total pause time = queue + api |
| `pause_queue_wait_sec` | QPS-limiter queue wait (pause) |
| `pause_api_sec` | pure pause API call time |
| `slice_total_sec` | resume + exec + pause; 0 for failed slices (excluded from percentiles) |
| `interaction_total_sec` | full interaction budget = resume + exec + pause + delay + natural_delay + capacity_wait (≥ slice_total) |
| `slot_contention_wait_sec` | contention wait to acquire a running slot (admission) |
| `running_slot_held_sec` | total running-slot hold time (acquire → release) |
| `exit_code` | `provider.exec()` exit code |
| `timed_out` | whether a timeout exit code was hit |

#### Trajectory summary columns (15, seconds, sum-based)

One row per trajectory (instance) for **cost attribution** — where this trajectory's total
wall time went (pause vs. resume vs. exec vs. queue wait). Uses **sum, not percentiles**:
per-instance per-step distributions are already in `Step detail` (filter by `trajectory_id`)
and `Lifecycle overhead` (pooled); this sheet answers "total breakdown + wasteful wait".
`n_steps` counts all step events (including `slice_failed` steps — they contribute 0 to
sums but count as attempts, so `avg_slice` reflects per-attempt cost).

| column | meaning |
|-------|---------|
| `trajectory_id` | instance |
| `n_steps` | total steps replayed for this trajectory (including failed) |
| `n_failed` | number of failed steps |
| `n_timeout` | number of steps that hit a timeout exit code |
| `success_rate` | success ratio (None when 0 steps attempted) |
| `slice_total_sum_s` | total active wall time = resume + exec + pause (sum invariant) |
| `exec_sum_s` | total pure command-execution time |
| `resume_sum_s` | total resume time |
| `pause_sum_s` | total pause time |
| `interaction_total_sum_s` | full interaction budget incl. delay + capacity_wait (≥ slice_total; for oversubscription analysis) |
| `slot_wait_sum_s` | total admission slot-contention wait |
| `resume_queue_wait_sum_s` | total QPS-limiter queue wait for resume |
| `pause_queue_wait_sum_s` | total QPS-limiter queue wait for pause |
| `running_slot_held_sum_s` | total running-slot hold time (slot occupancy / oversubscription granularity) |
| `avg_slice_s` | slice_total_sum / n_steps, typical per-step cost |

> Finer resume/pause sub-segments (api_sec / ready_wait / queue_wait) per step live in
> `Step detail`; per-second concurrency in `Concurrency states`; snapshot memory in
> `Snapshot sizes`. Host-level system resources (CPU/memory/NUMA) are in the separate
> vm_monitor `analysis_report.xlsx` (`monitor.merge_report: false`) or merged into this
> workbook's `VM_Stats` / `NUMA_Overview` / `DevKit_TopDown` sheets (`merge_report: true`).
