# oversub-bench / oversub-compare Usage Guide

`oversub-bench` drives the replay kernel across a set of memory/CPU oversubscription ratios and
produces per-trial + per-ratio degradation curves; `oversub-compare` compares **already-produced**
sweep results. The base replay workflow (modes, ratio configuration, reading results) lives in
[bench-core-usage.md](bench-core-usage.md) §8.1–8.4.

## What it is

The driver (`src/bench_core/oversub.py`) sweeps the replay kernel across oversubscription ratios:
`running_concurrency` (N) stays fixed (from the base config), `total_count = k×N` scales per trial.
One `bench-core` invocation per trial (one per shard when process-sharded); the driver reads each
trial's machine-readable `run_summary.json` and aggregates per-trial + per-ratio degradation curves.

**Layering: kernel emits raw facts, driver computes valid.** The driver imports no kernel data-path
internals (lifecycle / admission / stats / observability) — only the CLI, the YAML schema, the
`run_summary.json` schema, and the shared `setup_logging` helper. The kernel does not know it is
"ratio k of a sweep"; it emits what happened, the driver decides whether a trial ran to completion.

## 1. Quick start

```bash
# fake smoke — whole artifact chain in seconds. Pin --modes exec_ephemeral:
# FakeProvider is EphemeralCapable (not lifecycle), so a lifecycle trial would
# come back invalid.
oversub-bench --config config/common/replay-exec-only.yaml --provider fake --modes exec_ephemeral \
  --ratios 1,2,3 --test-duration 5 --output-root results/fake_sweep

oversub-bench --sweep-config config/oversub/lifecycle-1to3.yaml          # a ready-made sweep
oversub-bench --sweep-config config/oversub/template.yaml --ratios 4     # override one knob
oversub-bench --config config/common/replay.yaml --provider aenv --ratios 1,2,3   # no sweep-config
```

Precedence: **CLI flag > sweep-config > built-in default**. `--sweep-config` carries every knob plus
`base_config`; `--config` is required only when no sweep-config sets `base_config`. Run
`oversub-bench --help` for the full flag set (`--running-concurrency`, `--modes`, `--repeats`,
`--test-duration`, `--multiplier`, `--shards`, `--failure-tolerance`, `--cooldown-sec`,
`--cleanup-between-trials`, `--trial-timeout-sec`, `--output-root`, `--reuse`, `--stop-on-failure`,
`--dry-run`, `--no-vm-monitor`, `--bench-core-bin`).

## 2. Writing a sweep-config

`config/oversub/template.yaml` is the annotated template; `lifecycle-1to3.yaml` /
`exec-ephemeral-1to3.yaml` / `lifecycle-1to6.yaml` are ready-made sweeps. Copy one and edit. Unknown
keys are **rejected** (a typo like `repeat:` vs `repeats:` fails loudly).

| key | default | meaning |
|-----|---------|---------|
| `base_config` | — (required) | base replay stress profile; deep-copied per trial |
| `provider` | `aenv` | `{aenv, e2b, docker, fake}` |
| `running_concurrency` | base `replay.running_concurrency` | N running slots (fixed across ratios) |
| `ratios` | `1,2,3` | k values (list or `"1,2,3"`); `total_count = k×N` |
| `modes` | `exec_ephemeral,lifecycle` | replay modes to sweep (one curve each; default = exec_ephemeral pure-COW first, then lifecycle); a WARNING fires when the driver auto-applies the default |
| `multiplier` | — (unset) | per-sandbox trajectory count M (exec_ephemeral + fixed only; each sandbox runs M then self-stops; unset = 1 trajectory per sandbox) |
| `repeats` | `1` | repeats per `(mode, ratio)` (medians across repeats) |
| `test_duration` | base `test.duration` (600) | hard ceiling per trial (sec) |
| `failure_tolerance` | `0.0` | max `failure_rate` for a trial to count valid |
| `cooldown_sec` | `30` | settle time between trials |
| `cleanup_between_trials` | `on` | pre-trial `bench-core --cleanup` teardown of leftovers |
| `trial_timeout_sec` | `21600` (6h) | outer wall-clock defense-in-depth backstop (a hung kernel cannot block the sweep); `0` = off |
| `output_root` | `results/oversub/oversub-N{N}-{ts}/` | sweep output dir |
| `reuse` | `false` | skip a `(mode, ratio, repeat)` already valid (restart safety) |
| `stop_on_failure` | `false` | halt on first invalid trial |
| `no_vm_monitor` | `false` | pass `--no-vm-monitor` through to bench-core |
| `bench_core_bin` | `[bench-core]` | kernel subprocess command (tests point at a stub) |
| `auto_compare` | `true` | post-sweep, auto-run a single-series `oversub-compare` into `<output_root>/comparison/` (per-stat degradation + same-trajectory cross-ratio table); `--no-auto-compare` disables |

### Per-trial config overrides

The driver deep-copies `base_config` and rewrites only the oversub fields; everything else passes
through:

| field | set to | why |
|-------|--------|-----|
| `sandbox.total_count` | `k×N` | the oversubscription target |
| `replay.running_concurrency` | `N` (fixed) | the baseline slot count |
| `replay.mode` | the sweep mode | lifecycle / exec_only / exec_ephemeral |
| `test.round_size` | `k×N` | all k×N sandboxes run in one group — without this, k≥2 silently runs sequential groups of N, corrupting the oversubscription dynamics |
| `test.duration` | `test_duration` | the time ceiling |
| `report.output_dir` / `report.filename_prefix` | per-trial dir/prefix | isolate each trial's artifacts |

`test.round_count` (the work-amount knob: how many passes of the fleet) **passes through**. A trial
ends at whichever comes first: `round_count` rounds OR `test.duration` seconds (`1` = bounded
one-pass; `0` = sustained-until-duration). The ratio math itself (baseline VM count,
`total_count = k×384`): [bench-core-usage.md §8.2](bench-core-usage.md#82-lifecycle-memory-oversubscription-ratio-configuration).

## 3. Process sharding (`shards`)

At high ratios the single-process client tops out before the host: the thread-per-sandbox kernel's
dispatch is capped by one GIL (~50–55 steps/s at N=384, so past ratio 7 more load only grows client
queueing, not host load — r9/r10 data is client-bound). `shards` splits a trial's k×N slots into P
`bench-core` subprocesses (`auto` = P=k, ~N slots each — pinning every process at the already-proven
1:1 load, raising the GIL ceiling to ~P×). The mechanism lives in `src/bench_core/oversub_shards.py`;
the driver only composes it:

- **Only admission-free modes shard** (`exec_ephemeral` / `exec_only`). `lifecycle`'s
  `running_concurrency` is a global admission cap — per-shard admission would inflate the peak to P×N
  and silently change the oversub profile, so lifecycle always runs single-process (an explicit P>1
  is a WARNING downgrade, not an error — one sweep config can span modes).
- **P≤ratio splits group-aligned**: every shard's slot count is an integer multiple of N, so the
  merged per-trajectory run counts exactly match a single-process trial; P>ratio degrades to a
  slot-level split with a WARNING — totals preserved, per-trajectory distribution redistributes.
- The host collector is **driver-owned** (never hung off a shard): each shard publishes its own
  stress marker (`shard-XX/stress.lock`, `monitor.external_collector: true` = "publish the marker,
  spawn nothing"), and the driver runs **one** `vm-monitor` over the **union** of those markers
  (first shard's stress start → last shard's stress end), with its output fixed at
  **`trial_dir/vm_monitor/`** (peer of `merged/` and `shard-*/`) — host data does not change with
  the shard count, and a crashed or slow shard can no longer take the host report down. Shard 0 keeps
  the **control-plane fleet gauge** (`monitor.api_state: true` → `raw_data/api_state_raw.csv`): shards
  are kernel processes holding provider handles, while the driver only shells out and has no handle to
  poll the API, so shard 0 samples the gauge in its own collection window; the other shards force it
  off to avoid P duplicate control-plane polls.
- Each shard is a complete independent kernel run: `trial_dir/shard-{i:02d}/` holds `shard.yaml`
  (its total_count/round_size share, its own `report.output_dir`, docker `container_prefix` suffixed
  `-s{i}` to avoid container-name collisions, pacing knobs `launch_interval_sec` /
  `create_batch.interval` / `task_batch.interval` ×P to preserve the global create/dispatch rate) +
  `driver.log` + kernel artifacts; `trial.yaml` remains the unscaled logical provenance.
- The driver merges P copies back to contract shape: `trial_dir/merged/{prefix}_run_summary.json` +
  `merged/trajectories/index.json` + `merged/{prefix}_lifecycle_series.jsonl` (counts summed, wall =
  the **envelope** (max completed − min started, same semantics as the kernel's own `wall_sec ==
  completed_epoch − started_epoch`), rates recomputed over the envelope wall, `sandbox_index`
  re-keyed by shard slot offset so run keys stay unique; merged index entries re-aggregate ALL
  shards' runs with the kernel's rules — success_rate = per-run mean, elapsed_sec = upper-median p50
  success-only with all-runs fallback + `all_failed`, error types first-seen across runs, deep `file`
  stays per-shard; the lifecycle series are concatenated into a **fleet-wide** timeline after re-keying
  `sandbox_index` by slot offset, so an inflight-concurrency analyzer sees all k×N slots, not one
  shard's N — reading a single shard's series caps the observed peak at N and misreads sharded
  concurrency as a regression; `shard_count` / `shard_start_skew_sec` / `shard_return_codes`
  provenance ride on extra fields) — valid, CSV, compare, and `--reuse` are all shard-unaware. A
  crashed shard is **never backfilled with zeros to fake a successful measurement**: the missing
  shard + rc lands in merged `error`, the trial fails validity.
- `shards: 1` (or `auto` at ratio 1) still runs through the sharded machinery: single-shard
  `shard-00/` + `merged/` + driver-owned `trial_dir/vm_monitor/`, byte-identical layout to P≥2 — an
  agent comparing ratios reads one fixed structure, and the host collector is driver-owned at every
  ratio. Only `lifecycle` (non-shardable) keeps the legacy single-process path.
- **One fleet-wide obs workbook**: each shard writes its own `_obs.xlsx` (covering only its N slots —
  not readable as a trial result), and the driver emits `trial_dir/merged/{prefix}_obs.xlsx` with all
  8 sheets. It is never a concatenation of the P workbooks — the three model-driven sheets
  (Overview / Per-step timings / Lifecycle overhead) need `ReplayMetrics` accumulators, which die with
  a shard process, so the driver rebuilds them by replaying the merged series' events through the
  kernel's own `add` / `record_retry_event` / `append_retries_per_slice` via
  `lifecycle_reconstruct.replay_states_from_events` (no second set of aggregation rules); the other
  five sheets read that same merged series directly. The Overview is trial-level: `total_count` =
  k×N, `running_concurrency` = N, `overcommit_ratio` = k, `wall_sec` = the envelope — consistent with
  the merged `run_summary.json` rates; the Admission block is empty by construction (shardable modes
  have no admission). Not generated with `report.format: txt` (kernel's own gate); a render failure is
  a WARNING downgrade, never a takedown of an already-completed trial (per-shard workbooks and merged
  JSON remain). The merged summary's `paths.obs_xlsx` points at it.

## 4. `run_summary.json` fields (kernel → driver contract)

Each trial writes `{prefix}_run_summary.json` — the only contract between kernel and driver, raw
facts only (the kernel never recomputes a metric the driver needs; field table:
[metrics-reference.md §1.6 obs Overview + run_summary annotations](../dev/metrics-reference.md)):

| field | what it is / why it matters |
|-------|-----------------------------|
| `schema_version` | contract version (`2`); informational — the driver reads the throughput fields directly and never validates this (rescoring a schema-1 file behaves exactly as before: same gates, same fields) |
| `replay_mode`, `provider` | which mode / backend; separates the curves and the backend |
| `started_at`, `completed_at` (+ `_epoch`) | start/end times; epoch joins with vm_monitor host samples |
| `test_duration`, `wall_sec` | configured ceiling vs actual wall; wall ≪ duration = early stop/stall |
| `total_count`, `running_concurrency`, `overcommit_ratio` | the trial's k×N / N / k; ratio=k is the sweep's x-axis |
| `throughput` | `total` (trajectory attempts — schema 2 counts per trajectory, not per sandbox slot, so a slot reused across ephemeral create/kill or fixed-multiplier passes counts once per pass) / `succeeded` (trajectories that ran all steps) / `failed` (= total − succeeded, never negative) / `total_steps` / `steps_per_sec` / `tasks_per_sec`; `total` is the driver's valid gate (non-M trials; M trials gate on `succeeded`) |
| `admission` | `maximum, peak_active, granted, avg_queue_wait_sec, control_qps, control_dispatched`; `peak_active≤N` is the valid gate; `null` for exec_only / exec_ephemeral |
| `lifecycle_overhead` | `pause_sec_sum, resume_sec_sum, pct_of_slice_total` (overhead ratio %, same source as Overview's `lifecycle_overhead_aggregate_pct`); lifecycle mode only |
| `paths` | `report, obs_xlsx, lifecycle_series, trajectory_index, vm_monitor_dir` artifact paths |
| `error` | error string if the trial errored; non-empty = root-cause lead for a non-zero `return_code` |

## 5. Validity (`compute_valid`)

A trial (one pass of the k×N fleet, `round_count=1`) is **valid** when it ran (close to) the full
running fleet without over-admitting:

- `return_code == 0`;
- `throughput.total >= 0.9 * N` — it ran most of the N running slots (a crash/early-exit fails here;
  the `test.duration` ceiling caps a stalled run, which then also fails this gate). Mode-agnostic: a
  single-round trial attempts each trajectory once, so lifecycle/exec_ephemeral `total` ≈ k×N and
  exec_only `total` ≈ N, both ≥ 0.9×N on a healthy run; with `multiplier` (M) the floor instead reads
  `succeeded` (completions) ≥ 0.9×M×k×N;
- `admission.peak_active <= N` (never ran more than N concurrent — skipped for exec_only /
  exec_ephemeral);
- `failure_rate <= failure_tolerance`.

`valid` is the gross-failure / over-admission gate, **not** a "every trajectory succeeded" bar —
per-trajectory completion is inspectable in `trajectory-detail.csv` (`total` vs `target_count` in
`trial-summary.csv`).

## 6. Outputs

Written after every trial (so a killed driver leaves a complete partial set) into `--output-root`:

| file | granularity | key columns |
|------|-------------|-------------|
| `trial-summary.csv` | one row per `(mode, ratio, repeat)` trial | `total, succeeded, failed, failure_rate, peak_active, wall_sec, tasks_per_sec, mean_stress_sec, stress_tasks_per_sec, steps_per_sec, lifecycle_overhead_pct, return_code, valid, target_count, test_duration` |
| `ratio-summary.csv` | one row per `(mode, ratio)` (medians across repeats) | `attempted, successful, median_wall_sec, median_tasks_per_sec, median_stress_sec, median_stress_tasks_per_sec, avg_cmd_exec_sec, median_cmd_exec_sec, cmd_exec_degradation_vs_1_1_pct, time_degradation_vs_1_1_pct, throughput_gain_vs_1_1_pct, stress_throughput_gain_vs_1_1_pct` |
| `trajectory-detail.csv` | one row per trajectory per trial (from `trajectories/index.json`) | `trajectory_id, sandbox_index, n_steps, n_failed, success_rate, elapsed_sec` + the 18 `*_sec` breakdown columns. The trajectory's executed command sequence (`action_series`) is **not** in the CSV (a multi-line cell would bloat every Excel row) — find it per trajectory in `replay_result.json` / `index.json`, or machine-readably in `benchmark-report.json` `trajectory_details`. |
| `benchmark-report.json` | full machine-readable report | `configuration, trials, ratio_summary, trajectory_details` (each `trajectory_details` row carries `action_series`) |

Each trial also has its own subdir `<output_root>/<mode>/ratio-XX/repeat-XX/` with the bench-core
native artifacts: `trial.yaml` (that trial's overridden config), `driver.log` (subprocess log),
`*_run_summary.json`, `*_obs.xlsx` (the 8 sheets, see [bench-core-usage.md §8.4](bench-core-usage.md#84-observability-workbook-_obsxlsx)),
`*_lifecycle_series.jsonl`, `trajectories/index.json`, `vm_monitor/` (unless `--no-vm-monitor`),
text report. To drill to a single step / single sandbox, open the obs workbook in this directory.

Degradation is computed **within a mode** vs that mode's `k=1` baseline, so each swept mode gets its
own curve; a mode without a `k=1` trial gets `0.0` degradation columns.

### How to read the results (reading order)

Run one sweep, then read in this order, from the overall curve down to single-step bottlenecks:

1. **`ratio-summary.csv` — the entry point, the whole degradation curve.** One row per
   `(mode, ratio)`, medians across repeats. Two columns matter most:
   `time_degradation_vs_1_1_pct` (wall-clock degradation vs k=1, positive = slower) and
   `stress_throughput_gain_vs_1_1_pct` (throughput gain excluding create/kill, the scoring caliber).
   Oversubscription's core question is "does the throughput gain from overcommit outweigh the
   per-trajectory latency degradation" — read the two curves together. `throughput_gain_vs_1_1_pct` /
   `median_tasks_per_sec` are the end-to-end comparison including create/kill;
   `median_stress_sec` / `median_stress_tasks_per_sec` look at stress windows only. Per-command exec
   time: `avg_cmd_exec_sec` (`sum(exec)/sum(steps)`, excluding runs with failed steps;
   `median_cmd_exec_sec` is the median of per-run means) and its `cmd_exec_degradation_vs_1_1_pct`
   (per-command degradation vs k=1). `median_wall_sec` is the absolute value;
   `all_repeats_successful` means every repeat at that ratio was valid.
2. **`trial-summary.csv` — success/failure and the valid gate.** First filter on the `valid` column
   to drop invalid trials, then compare `total` vs `target_count` (did it run the full fleet),
   `return_code` (non-zero → check `error`; `130` = interrupted, next section). `peak_active` should
   be ≤ `running_concurrency` (`N`); above = over-admission. `lifecycle_overhead_pct` shows whether
   the pause/resume share climbs with k.
3. **`trajectory-detail.csv` — which trajectory lags at which ratio.** Pivot on `trajectory_id` +
   `ratio`, find the largest `elapsed_sec`, then read the 18 `*_sec` breakdown columns to locate the
   bottleneck: slow exec, slow resume/pause (lifecycle overhead), `capacity_wait_sec` (queueing =
   not enough slots), or `natural_delay_sec` (inter-step think time). A non-empty
   `create_error_type`/`kill_error_type` means that trajectory failed at start/stop.
4. **Single-trial subdir — single-step/sandbox level.** Enter `<mode>/ratio-XX/repeat-XX/` and open
   `*_obs.xlsx`: Step detail filtered by `sandbox_index` for one sandbox's per-step latency, Gantt
   for the timeline, Snapshot sizes for snapshot bloat, Lifecycle overhead for pause/resume
   percentiles.
5. **`benchmark-report.json` — feed downstream scripts.** Full JSON of the three CSVs; the
   `configuration` section records the sweep parameters (reproducibility).

> One line: `ratio-summary` shows "did it degrade overall / did throughput rise", `trial-summary`
> shows "which trials were valid", `trajectory-detail` shows "which trajectory is slow and why", and
> the obs workbook shows "which sandbox stalled at which step".

## 7. Trial order, reuse, cooldown, interruption

- **Order**: `for mode, for ratio, for repeat` — k ascends within a mode, the natural degradation
  curve.
- **`reuse`**: skip a `(mode, ratio, repeat)` whose prior `run_summary.json` is already valid — a
  Ctrl-C restart resumes from the last good trial.
- **`cooldown_sec`**: settle time between trials (skipped for the first trial and in `--dry-run`).
- **`cleanup_between_trials: on`**: runs `bench-core --cleanup` (teardown of leftovers) before each
  trial so survivors from a prior ratio don't corrupt the next.
- **`--dry-run`**: prints each `trial.yaml` + the bench-core command and writes empty outputs — no
  subprocess runs.

**Interrupted vs timed-out trials** — a trial halted mid-run by Ctrl-C / a driver-initiated SIGTERM is
*not* dropped: the kernel's SIGTERM-cooperative `finally` flushes a **partial** `run_summary.json` +
`trajectories/index.json` (artifacts are written atomically via temp-file + `os.replace`, so a second
SIGTERM mid-flush leaves already-written files intact), and the driver captures it as a row with
**`return_code == 130`** and `valid == False`. Its `total` / `wall_sec` reflect only what ran before
the interrupt — which is itself the degradation signal (a ratio that stalls at 200/1152 trajectories
in the stall window is the oversub breakdown point). An interrupted trial **ends the sweep** (the
driver halts with exit 130); a *timed-out* trial (non-zero `return_code` ≠ 130) does **not** — the
sweep continues to the next ratio unless `--stop-on-failure` is set.

> `_interrupted` is an internal row sentinel (not a CSV column — `DictWriter` drops it) that `main()`
> reads to distinguish "halt the sweep" (user interrupt) from "continue" (trial timeout). Downstream
> analysis scripts should key off the committed `return_code == 130` column, not the in-memory
> sentinel.

## 8. `oversub-compare` — cross-architecture / cross-config comparison

Compares and visualizes **already-produced** `oversub-bench` sweep results across architectures /
configs. It **runs no sandboxes and produces no benchmark data of its own** — it only consumes the
driver's written CSV contract (`trajectory-detail.csv` + `trial-summary.csv`), joining N sweep output
dirs on `(mode, ratio, trajectory_id)` to emit delta CSVs + an xlsx workbook. Sibling to
`oversub-bench` (imports no kernel data-path internals).

**Typical use:** ARM baseline vs several x86 freq/L3 configs, ratios 1:1–1:6, comparing
per-trajectory end-to-end latency by ratio, the exec/resume/pause/wait component latencies, and
overall degradation. Host-level freq/L3 caps are set outside the benchmark (BIOS / cpufreq / l3cat);
the tool **never sniffs them** — you declare what each dir represents in the manifest.

**One mode per comparison** — a single `oversub-compare` run compares **one mode's set of series**
(e.g. `arm-life1 / arm-life2 / x86-life`, or `x86-exec1 / x86-exec2 / arm-exec`). To compare two
modes, **run it twice** and read the two workbooks side by side. The tool does not assume you mix
both modes into one manifest (the `mode` column is constant per dir, so a single-mode manifest stays
clean).

### Manifest

`config/oversub/compare-manifest.yaml` is the commented template:

```yaml
baseline_series: arm-ampere
output_dir: results/oversub/comparison-2026-09-19/   # null = comparison-<ts>/
series:
  - {label: arm-ampere, arch: arm, dir: results/oversub/arm/,       caps: {l3_mb: 256}}
  - {label: x86-base,   arch: x86, dir: results/oversub/x86-base/,   caps: {freq_ghz: 3.0, l3_mb: 64}}
  - {label: x86-freq2,  arch: x86, dir: results/oversub/x86-freq2/,  caps: {freq_ghz: 2.0, l3_mb: 64}}
```

`baseline_series` is the reference all deltas are computed against (any series works). `caps` is
**informational** — surfaced in the Overview + series labels, never parsed. Relative `dir` paths
resolve against the manifest file's parent (portable). Unknown keys are rejected (a typo like
`serie:` fails loudly).

### Running the comparison

```bash
oversub-compare --manifest config/oversub/compare-manifest.yaml
oversub-compare --arm results/oversub/arm/ --x86 results/oversub/x86/   # 2-dir shorthand (arm is baseline)
oversub-compare --manifest ... --allow-mismatched-n   # proceed even if N differs (refused by default)
```

### Inputs (per series dir, the driver's existing contract)

- `trajectory-detail.csv` — one row per `(mode, ratio, repeat, trajectory_id)` + `elapsed_sec` and
  18 `*_sec` breakdown columns.
- `trial-summary.csv` — per-trial `wall_sec / tasks_per_sec / mean_stress_sec /
  stress_tasks_per_sec / lifecycle_overhead_pct / avg_queue_wait_sec`, etc.
- `benchmark-report.json` — `configuration` (for the N-parity check).

A missing file errors with the path.

### Outputs (`<output_dir>/`)

| file | granularity | contents |
|------|-------------|----------|
| `comparison-tidy.csv` | long | `series, arch, <caps…>, mode, ratio, repeat, trajectory_id, metric, value` (concat+melt, most flexible, pandas-groupable; metrics = the 20 CSV columns + derived `cmd_exec_sec` = `exec_sec/n_steps`, NaN for runs with failed steps) |
| `comparison-ratio-summary.csv` | one row per `(mode, ratio, series, metric)` | p50/p90/p99/avg stat pack (`median_sec/p90_sec/p99_sec/avg_sec`) + delta vs baseline + pct + one within-series degradation column per stat vs that series' own k=1 (`degradation_vs_ratio1_pct`=p50, `deg_p90/p99/avg_vs_ratio1_pct`); trial-level metrics carry the median only; a series without k=1 yields blank degradation (no crash) |
| `comparison-trajectory-delta.csv` | join on `(mode, ratio, trajectory_id)` | baseline `elapsed_sec` + each series' value + delta; an absent side stays blank, orphan count surfaces in Overview |
| `comparison-cross-ratio-trajectory.csv` | one row per `(series, mode, trajectory_id, ratio)` | that trajectory's median `elapsed_sec` + degradation vs the SAME trajectory's ratio-1 median (the driver pins the pool across ratios, so ids join); a tid missing at k=1 stays blank |
| `comparison.xlsx` | 5 sheets | below |

> How to read: open **Overview** in `comparison.xlsx` for N-parity + valid counts, then **Per-ratio**
> for the curves (who's faster / who degrades worse / lifecycle overhead), **Component heatmaps** for
> which component (exec/resume/pause/queueing) is hottest at which ratio, **Per-trajectory delta** to
> find which trajectory pulls ahead, and **Cross-ratio per-traj** for one trajectory's degradation
> across ratios. For scripted analysis use the tidy CSV.

### `comparison.xlsx` five sheets

1. **Overview** — manifest (series/arch/caps/baseline), N-parity check result, per-series
   valid/invalid trial counts, median-e2e headline table.
2. **Per-ratio** — LineCharts, each with one y-axis (no dual-axis): ① absolute median e2e (who's
   faster); ② within-series degradation % vs k=1 (whose oversub behavior degrades worse); ③
   `lifecycle_overhead_pct` (the pause/resume cost share); ④ per series, a task-time (`elapsed_sec`)
   degradation block + chart with one line per stat (p50/p90/p99/avg vs k=1). For an exec_only
   comparison ③ self-skips — the driver writes an empty cell for exec_only, pandas reads NaN, the
   subset empties, and the block is omitted (no flat-zero line).
3. **Component heatmaps** — one grid per component (rows=series, cols=ratio, ColorScale single-hue
   light→dark): `exec / resume / pause / slot_contention_wait`. `create / kill` are **excluded** —
   in lifecycle/exec_only they are 0 per step (only the ephemeral exec_ephemeral mode has
   per-trajectory create_one/kill_one), so the heatmap would carry only flat zeros; both columns
   remain in the tidy CSV for drill-down.
4. **Per-trajectory delta** — e2e delta joined per trajectory; freeze panes at D2.
5. **Cross-ratio per-traj** — the same-trajectory cross-ratio table (`comparison-cross-ratio-trajectory.csv`
   rendered): per trajectory, its median `elapsed_sec` at each ratio + degradation vs its own
   ratio-1 median; freeze panes at D2.

### N-parity guard

`running_concurrency` must match across series — different N means ratio 1:k maps to different
absolute sandbox counts, so the cross-series comparison is apples-to-oranges. A mismatch is refused
by default; `--allow-mismatched-n` proceeds anyway. Invalid trials **stay in the median** (robust to
one flaky repeat); Overview surfaces the per-series valid/invalid count so you decide whether to drop
+ re-run.

### Runbook

1. On host A: `oversub-bench --sweep-config config/oversub/lifecycle-1to6.yaml --output-root results/oversub/arm/`
2. On each x86 config (reconfigure host freq/L3 between runs): `oversub-bench ... --output-root results/oversub/x86-<config>/`
3. Edit `config/oversub/compare-manifest.yaml` to point at each dir + declare caps.
4. `oversub-compare --manifest config/oversub/compare-manifest.yaml`
5. Open `results/oversub/comparison-<ts>/comparison.xlsx`.

To compare two modes: repeat 1–5 with a separate manifest per mode (one mode's worth of series each)
and read the two workbooks side by side.

## 9. Troubleshooting

- **Trial invalid but the log doesn't crash** — following the reading order: (1) `trial-summary.csv`
  `total` vs `target_count` (did it run the full fleet), (2) `return_code` (`130` = interrupted),
  (3) `admission.peak_active` ≤ N (over-admission).
- **High-ratio client-bound (GIL cap)** — single-process dispatch tops out at ~50–55 steps/s; more
  load only grows client queueing. Set `shards` (auto = P=ratio) to split the load across kernel
  subprocesses; note lifecycle (global admission) cannot shard.
