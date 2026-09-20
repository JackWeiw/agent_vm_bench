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
| Overview | scalar (consolidated, grouped/color-coded) | **single summary**: Run (mode/total_count/running_concurrency/test_duration/wall_sec/steps/success/failed/overcommit_ratio) + Throughput (steps_per_sec/effective_parallelism/exec_wall_utilization/concurrency) + Admission & QPS (running-slot maximum/active/peak_active/granted/avg_queue_wait/waiting + QPS limiter qps/inflight_cap/in_flight/dispatched/avg_wait/max_wait + per-operation dispatch/wait sub-tables) + Retry (retry_count/time_lost_to_retry_sec/retries_per_slice_p95 + per-operation retry_queued) + **Lifecycle overhead %** (lifecycle/trajectory; aggregate/mean/p95 ratios) + **Per-step timing** sub-table (exec/think/pause/resume pooled percentiles). See "Overview fields" below for the Lifecycle overhead % and Per-step timing fields. Column A labels are bold/filled; groups separated by banner rows |
| Per-step timings | pooled percentiles | fleet `latency` (= pure exec time) n/min/max/avg/p50/p95/p99, bucketed by `action_type`; embedded line chart (ms). **Naming note**: this sheet buckets *pure exec latency* by *action type* (shell/bash/…); the Overview **Per-step timing** section pools by *phase* (exec/think/pause/resume) — different lens, do not conflate |
| Lifecycle overhead | pooled percentiles | `resume` / `pause` / `slice_total` / `slot_held` / `interaction` percentiles (**raw seconds**); embedded chart (ms). lifecycle/trajectory only. **Note**: this sheet is absolute durations in seconds (chart in ms); the overhead **ratio (%)** is not here — it lives in the Overview `lifecycle_overhead_*_pct` scalars |
| Trajectory summary | one row per trajectory | n_steps + segment sums (slice_total/exec/resume/pause/interaction_total/slot_contention_wait/resume_rate_pacing_wait/pause_rate_pacing_wait/running_slot_held) + avg_slice (seconds). Sorted by trajectory_id; trajectory mode also appends create/kill percentiles |
| Step detail | one row per step event | 20 columns (see below); includes success and synthesized `slice_failed` rows; sorted by (trajectory, sandbox, step); frozen header + autofilter |
| Concurrency states | one row per second | per-second dominant-state counts (pausing/paused/resuming/exec/active); chart |
| Gantt | chart | per-sandbox phase timeline (resume/exec/pause), embedded PNG; auto-shrinks row height for large fleets |
| Snapshot sizes | one row per pause | logical/disk/inherited/cumulative MiB + generations/files; chart. `SnapshotSizeCapable` (aenv) only |

> With `monitor.merge_report: true`, up to three host sheets (`VM_Stats` /
> `NUMA_Overview` / `DevKit_TopDown`) are appended after `Snapshot sizes` (copied from the
> vm_monitor `resource_report.xlsx`). With the default `false`, the vm_monitor report stays
> a separate file and the workbook carries only the 8 sheets above.

#### Overview fields

The Overview dashboard is grouped in order Run → Throughput → Admission & QPS → Retry → Lifecycle overhead % → Per-step timing (Column A bold/filled + banner-separated). The Lifecycle overhead % and Per-step timing groups appear in lifecycle/trajectory mode only (absent in exec_only or a fully-failed run).

**① Lifecycle overhead %** (scalar kvs — the overhead **ratio**, not seconds):

| field | what it is | what to read / why it matters |
|-------|------------|------------------------------|
| `lifecycle_overhead_aggregate_pct` | (resume+pause summed) / slice_total summed ×100 | overall lifecycle-overhead share; high = resume/pause dominate, exec share is small |
| `lifecycle_overhead_mean_pct` | mean of per-slice ratios | typical per-step overhead rate; vs aggregate tells if a few long tails skew it |
| `lifecycle_overhead_p95_pct` | p95 of per-slice ratios | tail-worst overhead rate; high p95 + low mean = a few extreme slices |

> Near-zero slices (`< MIN_SLICE_SEC`, e.g. zero-placeholders on exception paths) are excluded so a tiny slice cannot explode the per-sample ratio. This ratio also appears in the text report's `[Lifecycle Overhead]` block and `run_summary.json`'s `lifecycle_overhead.pct_of_slice_total`. The `Lifecycle overhead` sheet's segment table is **raw seconds** (chart in ms) — ratio vs seconds are two lenses, don't conflate.

**② Per-step timing** (sub-table — the four phases of each step, pooled percentiles; columns = `segment/n/min/max/avg/p50/p95/p99`):

| field | what it is | what to read / why it matters |
|-------|------------|------------------------------|
| `exec` | derived = slice_total − resume − pause, pure command-execution wall | the "real work" time; a high exec share = good efficiency |
| `think` | = `natural_delay_secs`, inter-step LLM/think delay | non-productive gap; high = large inter-step gaps or `delay_scale>0` |
| `pause` | pause phase: API + rate-pacing + inflight | pause cost; rises under oversub / snapshot reuse |
| `resume` | resume phase: API + ready_wait + inflight | resume cost; rises under oversub / snapshot reuse |

> Naming disambiguation: the Overview **Per-step timing** section pools by **phase** (exec/think/pause/resume) across the **whole fleet**; the `Per-step timings` **sheet** buckets `latency` (= pure exec) by **action type** (`action_type`) — the former is "by phase", the latter "by action type".

#### Step detail columns (26, seconds)

Sub-segments nest under their parent so the sum invariant is verifiable in-sheet:
`resume_sec == resume_inflight_wait_sec + resume_api_sec + resume_ready_wait_sec` (rate-pacing is PRE-lease, excluded),
`pause_sec == pause_rate_pacing_wait_sec + pause_inflight_wait_sec + pause_api_sec` (rate-pacing is IN-lease, included),
`interaction_total_sec == slice_total_sec + natural_delay_sec + capacity_wait_sec + resume_rate_pacing_wait_sec` (the inter-step think-delay lives in `natural_delay_sec`, counted once — not a separate delay term). `natural_delay_sec` itself is the think-delay actually slept by the runner before the slice (`step.delay_time_sec * replay_delay_scale`) plus the scheduler's residual `ready_at` park (`replay_pause_duration`), so it is nonzero whenever the trajectory has inter-step gaps and `delay_scale > 0`.

| column | what it is | what to read / why it matters |
|-------|------------|------------------------------|
| `trajectory_id` | instance_id of the trajectory this step belongs to | primary key to filter/group by trajectory; answers "which trajectory" |
| `sandbox_index` | index of the executing sandbox in the fleet (0..N-1), not the backend sandbox_id | locates "which sandbox ran it"; aligns with Gantt/concurrency rows |
| `round_id` | round-robin round number; empty for fixed/trajectory modes | which concurrent group this step belongs to; spot inter-round degradation |
| `step_index` | step index within the trajectory (0-based) | ordinal of this step in its trajectory; aligns to the original trajectory |
| `action_type` | `shell` / `bash` / `str_replace_editor` / `submit` / `finish` / `done` | estimates latency by action class; matches `Per-step timings` buckets |
| `slice_failed` | runner-synthesized failed slice (exception/stop_on_error); when True the duration columns below are 0 | failure flag; filter failed steps to their `exit_code`; excluded from percentiles |
| `resume_sec` | total resume time = inflight + api + ready_wait (rate-pacing excluded, pre-lease) | resume overhead; a major non-exec term under oversub/snapshot reuse |
| `resume_rate_pacing_wait_sec` | QPS-limiter 1/qps rate-pacing time-wait (resume, PRE-lease: excluded from resume_sec / running_slot_held) | shaping pressure; nonzero = the QPS cap is binding |
| `resume_api_sec` | pure resume API call time | the backend's intrinsic resume cost; strips waits to see the bare API |
| `resume_ready_wait_sec` | post-resume readiness probe wait (lifecycle/trajectory; 0 in exec_only) | readiness-probe cost; high = slow warm-up after resume |
| `exec_sec` | pure `provider.exec()` wall time (= Per-step timings `latency`) | the "real work" time; the core efficiency metric |
| `pause_sec` | total pause time = rate-pacing + inflight + api (rate-pacing included, in-lease) | pause cost; rises with snapshot reuse / lifecycle oversub |
| `pause_rate_pacing_wait_sec` | QPS-limiter 1/qps rate-pacing time-wait (pause, IN-lease: included in pause_sec / running_slot_held) | shaping pressure (pause side); nonzero = the QPS cap is binding |
| `pause_api_sec` | pure pause API call time | the backend's intrinsic pause cost; strips waits to see the bare API |
| `slice_total_sec` | resume + exec + pause; 0 for failed slices (excluded from percentiles) | per-step active wall; denominator of the overhead ratio, numerator of `avg_slice` |
| `interaction_total_sec` | full interaction budget = slice_total + natural_delay + capacity_wait + resume_rate_pacing (≥ slice_total; the think-delay is in `natural_delay_sec`, counted once) | broad wall incl. think/wait; the excess over slice_total = non-productive overhead |
| `slot_contention_wait_sec` | derived composite = natural_delay + capacity_wait (FIFO running-slot contention; admission) | admission contention total; high = slot contention or large inter-step gaps |
| `natural_delay_sec` | inter-step think-delay — `step.delay_time_sec * replay_delay_scale` slept by the runner before the slice, plus the scheduler's residual `ready_at` park (= `replay_pause_duration`); the inter-step gap pacing (component of slot_contention_wait_sec) | inter-step think/gap; the dominant non-productive term at 1:1, so interaction_total ≫ slice_total |
| `capacity_wait_sec` | FIFO running-slot token contention -- the genuine queue wait (component of slot_contention_wait_sec) | genuine queueing; nonzero = not enough running slots (oversub signal) |
| `rate_pacing_wait_sec` | per-step rate-pacing total = resume_rate_pacing_wait_sec + pause_rate_pacing_wait_sec (1/qps shaping, a RATE control; not the FIFO queue -- see capacity_wait_sec) | total shaping wait; distinguishes "shaping" from "queueing" vs capacity_wait |
| `inflight_wait_sec` | per-step inflight-fuse block total = resume_inflight_wait_sec + pause_inflight_wait_sec (a CONCURRENCY control) | concurrency-fuse wait; nonzero = the inflight cap is binding |
| `resume_inflight_wait_sec` | inflight-fuse block on resume (component of resume_sec) | resume-side concurrency block; splits the block source |
| `pause_inflight_wait_sec` | inflight-fuse block on pause (component of pause_sec) | pause-side concurrency block; splits the block source |
| `running_slot_held_sec` | total running-slot hold time (acquire → release) | slot occupancy duration; numerator of effective_parallelism, oversub granularity |
| `exit_code` | `provider.exec()` exit code | success/failure/timeout verdict; pairs with `slice_failed`/`timed_out` |
| `timed_out` | whether a timeout exit code was hit | timeout flag; filter timeouts to compute the timeout rate |

#### Trajectory summary columns (21, seconds, sum-based)

One row per trajectory (instance) for **cost attribution** — where this trajectory's total
wall time went (pause vs. resume vs. exec vs. the wait components). Uses **sum, not percentiles**:
per-instance per-step distributions are already in `Step detail` (filter by `trajectory_id`)
and `Lifecycle overhead` (pooled); this sheet answers "total breakdown + wasteful wait".
`n_steps` counts all step events (including `slice_failed` steps — they contribute 0 to
sums but count as attempts, so `avg_slice` reflects per-attempt cost).

| column | what it is | what to read / why it matters |
|-------|------------|------------------------------|
| `trajectory_id` | instance | per-trajectory cost-attribution key; compare overhead structure across trajectories |
| `n_steps` | total steps replayed for this trajectory (including failed) | work volume; the denominator — pair with sums for unit cost |
| `n_failed` | number of failed steps | this trajectory's failure count; high = unstable trajectory |
| `n_timeout` | number of steps that hit a timeout exit code | timeout count; high = this trajectory is stuck on slow commands |
| `success_rate` | success ratio (None when 0 steps attempted) | trajectory health; low = heavy failure on this trajectory |
| `slice_total_sum_s` | total active wall time = resume + exec + pause (sum invariant) | this trajectory's active wall; denominator of overhead ratio, the cost baseline |
| `exec_sum_s` | total pure command-execution time | "real work" total; exec share = this trajectory's efficiency |
| `resume_sum_s` | total resume time | resume overhead; rises under oversub/snapshot reuse |
| `pause_sum_s` | total pause time | pause overhead; rises with snapshot reuse / lifecycle |
| `interaction_total_sum_s` | full interaction budget = slice_total + natural_delay + capacity_wait + resume_rate_pacing (≥ slice_total; the think-delay is in natural_delay, not a separate term) | broad wall incl. think/wait; minus slice_total = non-productive overhead |
| `slot_contention_wait_sum_s` | total admission slot-contention wait (derived = natural_delay + capacity_wait) | admission contention total; high = slot contention or large inter-step gaps |
| `natural_delay_sum_s` | total inter-step think-delay + residual park (component of slot_contention) | think/gap total; the dominant non-productive term at 1:1 |
| `capacity_wait_sum_s` | total FIFO running-slot token contention (component of slot_contention) | genuine queueing total; nonzero = not enough running slots (oversub signal) |
| `rate_pacing_wait_sum_s` | total rate-pacing = resume_rate_pacing + pause_rate_pacing (1/qps shaping) | total shaping wait; distinguishes "shaping" from "queueing" vs capacity |
| `inflight_wait_sum_s` | total inflight-fuse block = resume_inflight + pause_inflight | concurrency-fuse total; nonzero = the inflight cap is binding |
| `resume_rate_pacing_wait_sum_s` | total QPS-limiter 1/qps rate-pacing time-wait for resume (pre-lease) | resume-side shaping pressure; splits the shaping source |
| `pause_rate_pacing_wait_sum_s` | total QPS-limiter 1/qps rate-pacing time-wait for pause (in-lease) | pause-side shaping pressure; splits the shaping source |
| `resume_inflight_wait_sum_s` | total inflight-fuse block on resume (component of resume) | resume-side concurrency block; splits the block source |
| `pause_inflight_wait_sum_s` | total inflight-fuse block on pause (component of pause) | pause-side concurrency block; splits the block source |
| `running_slot_held_sum_s` | total running-slot hold time (slot occupancy / oversubscription granularity) | slot occupancy total; cross-check for effective_parallelism |
| `avg_slice_s` | slice_total_sum / n_steps, typical per-step cost | per-attempt step cost; compare "expensiveness" across trajectories |

At 1:1 (no oversubscription) with the QPS / inflight-fuse knobs unset, `capacity_wait`,
`rate_pacing`, `inflight`, and the resume/pause rate-pacing/inflight splits are legitimately
0 — they only turn nonzero under oversubscription + control-plane limiting. `natural_delay`
(the think-delay) is nonzero whenever the trajectory has inter-step gaps and `delay_scale > 0`;
it is the dominant non-productive term at 1:1, so `interaction_total` ≫ `slice_total` there.

> Finer resume/pause sub-segments (api_sec / ready_wait / inflight_wait / rate_pacing_wait) per step live in
> `Step detail`; per-second concurrency in `Concurrency states`; snapshot memory in
> `Snapshot sizes`. Host-level system resources (CPU/memory/NUMA) are in the separate
> vm_monitor `resource_report.xlsx` (`monitor.merge_report: false`) or merged into this
> workbook's `VM_Stats` / `NUMA_Overview` / `DevKit_TopDown` sheets (`merge_report: true`).

### 8.5 Oversubscription sweep (`oversub-bench`)

The `oversub-bench` driver (`src/bench_core/oversub.py`) sweeps the replay kernel across memory/CPU oversubscription ratios: `running_concurrency` (N) stays fixed (from the base config), `total_count = k×N` scales per trial. One `bench-core` invocation per trial; the driver reads each trial's machine-readable `run_summary.json` and aggregates per-trial + per-ratio degradation curves.

**Layering: kernel emits raw facts, driver computes valid.** The driver imports no kernel data-path internals — only the CLI, the YAML schema, the `run_summary.json` schema, and the shared `setup_logging` helper. The kernel does not know it is "ratio k of a sweep"; it emits what happened, the driver decides whether a trial ran to completion.

#### Invocation

```
oversub-bench --sweep-config config/oversub/lifecycle-1to3.yaml
oversub-bench --sweep-config config/oversub/lifecycle-1to3.yaml --ratios 4   # override one knob
oversub-bench --config config/common/replay.yaml --provider aenv --ratios 1,2,3   # no sweep-config
```

Precedence: **CLI flag > sweep-config > built-in default**. `--sweep-config` carries every knob plus `base_config`; `--config` is required only when no sweep-config sets `base_config`. Run `oversub-bench --help` for the full flag set (`--running-concurrency`, `--modes`, `--repeats`, `--test-duration`, `--failure-tolerance`, `--cooldown-sec`, `--cleanup-between-trials`, `--trial-timeout-sec`, `--output-root`, `--reuse`, `--stop-on-failure`, `--dry-run`, `--no-vm-monitor`, `--bench-core-bin`).

#### sweep-config keys

`config/oversub/template.yaml` is the annotated template; `lifecycle-1to3.yaml` is a ready aenv lifecycle 1:1/1:2/1:3 sweep. Copy either and edit. Unknown keys are **rejected** (a typo like `repeat:` vs `repeats:` fails loudly).

| key | default | meaning |
|-----|---------|---------|
| `base_config` | — (required) | base replay stress profile; deep-copied per trial |
| `provider` | `aenv` | `{aenv, e2b, docker, fake}` |
| `running_concurrency` | base `replay.running_concurrency` | N running slots (fixed across ratios) |
| `ratios` | `1,2,3` | k values (list or `"1,2,3"`); `total_count = k×N` |
| `modes` | `lifecycle,exec_only` | replay modes to sweep (one curve each) |
| `repeats` | `1` | repeats per `(mode, ratio)` for medians |
| `test_duration` | base `test.duration` (600) | hard ceiling per trial (sec) |
| `failure_tolerance` | `0.0` | max `failure_rate` for a trial to count valid |
| `cooldown_sec` | `30` | settle time between trials |
| `cleanup_between_trials` | `on` | pre-trial `bench-core --cleanup` teardown of leftovers |
| `trial_timeout_sec` | `0` | outer wall-clock per trial; `0` = off |
| `output_root` | `results/oversub/oversub-N{N}-{ts}/` | sweep output dir |
| `reuse` | `false` | skip completed-valid trials (restart safety) |
| `stop_on_failure` | `false` | halt on first invalid trial |
| `no_vm_monitor` | `false` | pass `--no-vm-monitor` through to bench-core |
| `bench_core_bin` | `[bench-core]` | kernel subprocess command (tests point at a stub) |

#### Per-trial config overrides

The driver deep-copies `base_config` and rewrites only the oversub fields; everything else passes through:

| field | set to | why |
|-------|--------|-----|
| `sandbox.total_count` | `k×N` | the oversubscription target |
| `replay.running_concurrency` | `N` (fixed) | the baseline slot count |
| `replay.mode` | the sweep mode | lifecycle / exec_only / trajectory |
| `test.round_size` | `k×N` | all k×N sandboxes run in one group — without this, k≥2 silently runs sequential groups of N, corrupting the oversubscription dynamics |
| `test.duration` | `test_duration` | the time ceiling |
| `report.output_dir` / `report.filename_prefix` | per-trial dir/prefix | isolate each trial's artifacts |

`test.round_count` (the work-amount knob: how many passes of the fleet) **passes through**. A trial ends at whichever comes first: `round_count` rounds OR `test.duration` seconds (the kernel's own whichever-first semantics). Set `round_count: 1` (base default) for a bounded one-pass trial, `0` for a sustained-until-duration window.

#### `run_summary.json` (kernel → driver contract)

Each trial writes `{prefix}_run_summary.json` — the only contract between kernel and driver. RAW FACTS ONLY (the kernel never recomputes a metric the driver needs):

| field | what it is | what to read / why it matters |
|-------|------------|------------------------------|
| `schema_version` | `1` | contract version; the driver keys schema compat off this |
| `replay_mode`, `provider` | which mode / backend | separates the lifecycle/exec_only/trajectory curves and the backend |
| `started_at`, `completed_at` (+ `_epoch`) | ISO local + epoch seconds | start/end times; epoch joins with vm_monitor host samples |
| `test_duration`, `wall_sec` | configured ceiling vs actual wall | whether the ceiling was hit; wall ≪ duration = early stop/stall |
| `total_count`, `running_concurrency`, `overcommit_ratio` | the trial's k×N / N / k | oversub params; ratio=k is the sweep's x-axis |
| `throughput` | `total, succeeded, failed, total_steps, steps_per_sec, tasks_per_sec` (`total` = sandboxes that ran; `succeeded` = trajectories that ran all steps) | output/failure stats; steps_per_sec=throughput; `total` is the driver's valid gate |
| `admission` | `maximum, peak_active, granted, avg_queue_wait_sec, control_qps, control_dispatched` — `null` for exec_only (no admission controller) | admission control-plane state; `peak_active≤N` is the valid gate; null for exec_only |
| `lifecycle_overhead` | `pause_sec_sum, resume_sec_sum, pct_of_slice_total` — lifecycle/trajectory only; `pct_of_slice_total` is the overhead ratio (%, same source as Overview's `lifecycle_overhead_aggregate_pct`) | lifecycle overhead in absolute + ratio; the driver writes this to `trial-summary.csv`'s `lifecycle_overhead_pct` |
| `paths` | `report, obs_xlsx, lifecycle_series, trajectory_index, vm_monitor_dir` | artifact paths; the driver aggregates / pulls detail from these |
| `error` | error string if the trial errored | failure reason; non-empty = root-cause lead for a non-zero `return_code` |

#### Validity (`compute_valid`)

A trial (one pass of the k×N fleet, `round_count=1`) is **valid** when it ran (close to) the full running fleet without over-admitting:

- `return_code == 0` (the kernel subprocess succeeded);
- `throughput.total >= 0.9 * N` (it ran most of the N running slots — a crash/early-exit fails here; the `test.duration` ceiling caps a stalled run, which then also fails this gate). Mode-agnostic: lifecycle `total` ≈ k×N and exec_only `total` ≈ N, both ≥ 0.9×N on a healthy run;
- `admission.peak_active <= N` (never ran more than N concurrent — dropped for exec_only);
- `failure_rate <= failure_tolerance` (configurable wrap-noise allowance).

`valid` is the gross-failure / over-admission gate, **not** a "every trajectory succeeded" bar — per-trajectory completion is inspectable in `trajectory-detail.csv` (`total` vs `target_count` in `trial-summary.csv`).

#### Outputs

Written after every trial (so a killed driver leaves a complete partial set) into `--output-root` (default `results/oversub/oversub-N{N}-{ts}/`):

| file | granularity | key columns |
|------|-------------|-------------|
| `trial-summary.csv` | one row per `(mode, ratio, repeat)` trial | `total, succeeded, failed, failure_rate, peak_active, wall_sec, tasks_per_sec, steps_per_sec, lifecycle_overhead_pct, return_code, valid, target_count, test_duration` |
| `ratio-summary.csv` | one row per `(mode, ratio)` (medians across repeats) | `attempted, successful, median_wall_sec, median_tasks_per_sec, time_degradation_vs_1_1_pct, throughput_gain_vs_1_1_pct` |
| `trajectory-detail.csv` | one row per trajectory per trial (from `trajectories/index.json`) | `trajectory_id, sandbox_index, n_steps, n_failed, success_rate, elapsed_sec` + the 18 `*_sec` breakdown columns (exec/resume/pause/requested_delay/create/kill/slice_total/interaction_total/slot_contention_wait/natural_delay/capacity_wait/rate_pacing_wait/inflight_wait/resume_rate_pacing_wait/pause_rate_pacing_wait/resume_inflight_wait/pause_inflight_wait/running_slot_held) |
| `benchmark-report.json` | full machine-readable report | `configuration, trials, ratio_summary, trajectory_details` |

Degradation is computed **within a mode** vs that mode's `k=1` baseline, so lifecycle and exec_only each get their own curve (memory-overcommit overhead vs CPU-oversubscription degradation). When a mode has no `k=1` trial, the degradation columns default to `0.0`.

#### Trial order, reuse, cooldown

- **Order:** `for mode, for ratio, for repeat` — the natural degradation curve (k ascends within a mode).
- **`reuse`:** skip a `(mode, ratio, repeat)` whose prior `run_summary.json` is already valid — restart after a Ctrl-C resumes from the last good trial.
- **`cooldown_sec`:** settle time between trials (skipped for the first trial and in `--dry-run`).
- **`cleanup_between_trials: on`:** runs `bench-core --cleanup` (teardown of leftovers) before each trial so survivors from a prior ratio don't corrupt the next.
- **`--dry-run`:** prints each `trial.yaml` + the bench-core command and writes empty outputs — no subprocess runs.

#### Interrupted vs timed-out trials

A trial halted mid-run by Ctrl-C / a driver-initiated SIGTERM is *not* dropped — the kernel's SIGTERM-cooperative `finally` flushes a **partial** `run_summary.json` + `trajectories/index.json` (artifacts are written atomically via temp-file + `os.replace`, so a second SIGTERM mid-flush leaves already-written files intact), and the driver captures it as a row with **`return_code == 130`** and `valid == False`. Its `total` / `wall_sec` reflect only what ran before the interrupt — which is itself the degradation signal (a ratio that stalls at 200/1152 trajectories in the stall window is the oversub breakdown point). An interrupted trial **ends the sweep** (the driver halts with exit 130); a *timed-out* trial (non-zero `return_code` ≠ 130) does **not** — the sweep continues to the next ratio unless `--stop-on-failure` is set.

> `_interrupted` is an internal row sentinel (not a CSV column — `DictWriter` drops it) that `main()` reads to distinguish "halt the sweep" (user interrupt) from "continue" (trial timeout). Downstream analysis scripts should key off the committed `return_code == 130` column, not the in-memory sentinel.

### 8.6 Cross-architecture / cross-config comparison (`oversub-compare`)

`oversub-compare` (`src/bench_core/compare_sweeps.py`) compares and visualizes **already-produced** `oversub-bench` sweep results across architectures / configs. It **runs no sandboxes and produces no benchmark data of its own** — it only consumes the driver's written CSV contract (`trajectory-detail.csv` + `trial-summary.csv`), joining N sweep output dirs on `(mode, ratio, trajectory_id)` to emit delta CSVs + an xlsx workbook. Sibling to `oversub-bench` (imports no kernel data-path internals).

**Typical use:** ARM baseline vs several x86 freq/L3 configs, ratios 1:1–1:6, comparing per-trajectory end-to-end latency by ratio, the exec/resume/pause/wait component latencies, and overall degradation. Host-level freq/L3 caps are set outside the benchmark (BIOS / cpufreq / l3cat / boot); the tool **never sniffs them** — you declare what each dir represents in the manifest.

#### One mode per comparison

A single `oversub-compare` run compares **one mode's set of series** (e.g. `arm-life1 / arm-life2 / x86-life`, or `x86-exec1 / x86-exec2 / arm-exec`). To compare exec_only and lifecycle, **run it twice** — one workbook per mode, side by side. The tool does not assume you mix both modes into one manifest (the `mode` column is constant per dir, so a single-mode manifest stays clean).

#### Manifest

`config/oversub/compare-manifest.yaml` is the commented template:

```yaml
baseline_series: arm-ampere
output_dir: results/oversub/comparison-2026-09-19/   # null = comparison-<ts>/
series:
  - {label: arm-ampere, arch: arm, dir: results/oversub/arm/,       caps: {l3_mb: 256}}
  - {label: x86-base,   arch: x86, dir: results/oversub/x86-base/,   caps: {freq_ghz: 3.0, l3_mb: 64}}
  - {label: x86-freq2,  arch: x86, dir: results/oversub/x86-freq2/,  caps: {freq_ghz: 2.0, l3_mb: 64}}
```

`baseline_series` is the reference all deltas are computed against (ARM by convention; any series works). `caps` is **informational** — surfaced in the Overview + series labels, never parsed. Relative `dir` paths resolve against the manifest file's parent (portable). Unknown keys are rejected (a typo like `serie:` fails loudly).

#### Running the comparison

```bash
oversub-compare --manifest config/oversub/compare-manifest.yaml
oversub-compare --arm results/oversub/arm/ --x86 results/oversub/x86/   # 2-dir shorthand (arm is baseline)
oversub-compare --manifest ... --allow-mismatched-n   # proceed even if N differs (refused by default)
```

#### Inputs (per series dir, the driver's existing contract)

- `trajectory-detail.csv` — one row per `(mode, ratio, repeat, trajectory_id)` + `elapsed_sec` and 18 `*_sec` breakdown columns.
- `trial-summary.csv` — per-trial `wall_sec / tasks_per_sec / lifecycle_overhead_pct / avg_queue_wait_sec`, etc.
- `benchmark-report.json` — `configuration` (for the N-parity check).

A missing file errors with the path.

#### Outputs (`<output_dir>/`)

| File | Contents |
|------|----------|
| `comparison-tidy.csv` | long: `series, arch, <caps…>, mode, ratio, repeat, trajectory_id, metric, value` (concat+melt, most flexible) |
| `comparison-ratio-summary.csv` | one row per `(mode, ratio, series, metric)`: median + delta vs baseline + pct + within-series degradation vs that series' own k=1; a series without k=1 yields blank degradation (no crash) |
| `comparison-trajectory-delta.csv` | join on `(mode, ratio, trajectory_id)`: baseline `elapsed_sec` + each series' value + delta; an absent side stays blank, orphan count surfaces in Overview |
| `comparison.xlsx` | 4 sheets (below) |

#### `comparison.xlsx` four sheets

1. **Overview** — manifest (series/arch/caps/baseline), N-parity check result, per-series valid/invalid trial counts, median-e2e headline table.
2. **Per-ratio** — up to 3 LineCharts, each with one y-axis (no dual-axis): ① absolute median e2e (who's faster); ② within-series degradation % vs k=1 (whose oversub behavior degrades worse); ③ `lifecycle_overhead_pct` (the pause/resume cost share for a lifecycle comparison). For an exec_only comparison ③ self-skips — the driver writes an empty cell for exec_only (no lifecycle_overhead), pandas reads NaN, the subset empties, and the block is omitted (no flat-zero line).
3. **Component heatmaps** — one grid per component (rows=series, cols=ratio, ColorScale single-hue light→dark): `exec / resume / pause / slot_contention_wait`. `create / kill` are **excluded** — in lifecycle/exec_only they are 0 per step (only trajectory mode has per-trajectory create_one/kill_one), so the heatmap would carry only flat zeros; both columns remain in the tidy CSV for drill-down.
4. **Per-trajectory delta** — e2e delta joined per trajectory; freeze panes at D2.

#### N-parity guard

`running_concurrency` must match across series — different N means ratio 1:k maps to different absolute sandbox counts, so the cross-series comparison is apples-to-oranges. A mismatch is refused by default; `--allow-mismatched-n` proceeds anyway. Invalid trials **stay in the median** (the median is robust to one flaky repeat); Overview surfaces the per-series valid/invalid count so you can decide whether to drop + re-run.

#### Runbook

1. On ARM: `oversub-bench --sweep-config config/oversub/lifecycle-1to6.yaml --output-root results/oversub/arm/`
2. On each x86 config (reconfigure host freq/L3 between runs): `oversub-bench ... --output-root results/oversub/x86-<config>/`
3. Edit `config/oversub/compare-manifest.yaml` to point at each dir + declare caps.
4. `oversub-compare --manifest config/oversub/compare-manifest.yaml`
5. Open `results/oversub/comparison-<ts>/comparison.xlsx`.

To compare exec_only vs lifecycle: repeat 1–5 with a separate manifest per mode (one mode's worth of series each) and read the two workbooks side by side.
