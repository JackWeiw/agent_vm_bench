---
rfc: 0002
title: Workflow plugin architecture — TaskRunner ABC + registry to make adding a workload a one-file change
status: Draft
author: "@JackWeiw"
shepherd: ""
areas: [e2b, docker]
created: 2026-09-15
updated: 2026-09-15
---

# Workflow plugin architecture — TaskRunner ABC + registry

> Refactor RFC: records the direction to end shotgun-surgery workflow extensibility. Not for
> immediate merge-as-code; a phased implementation plan follows acceptance.

## Summary

Today adding a new agent workload (workflow) type is ~25 edits across 7 files with no enforcing
contract — `workflow_type` is re-tested in an `if/elif` chain duplicated verbatim in `bench.py`,
`task_manager.py`, `round_robin.py`, `stats_collector.py`, `schemas.py`, `config.py`, and
`src/env_provider/_ready.py`. This RFC proposes a `WorkflowSpec` registry + a `TaskRunner` ABC + a
uniform `RunContext`, collapsing the dispatch to one dict lookup so a new workflow is one new module
+ one registry entry. It finishes the polymorphism that already exists for metrics
(`BenchSandbox.task_metrics`) but was never carried through to runner construction and config
parsing — the same factoring the provider side (RFC 0001) already has.

## Motivation

The provider side is well-factored: an `EnvironmentProvider` ABC + `BaseSandboxManager` template +
capability Protocols + a lazy-import registration seam (RFC 0001, shipped). Adding a backend is one
submodule + two registration lines, contract untouched. The workflow side never got the same
treatment.

Evidence — the `workflow_type` if/elif chain is duplicated verbatim (each ending in
`raise ValueError("Unsupported workflow_type")`) at:

- `bench.py` — `_print_header`, and ~14 `== "replay"` / `== "document"` branches in `run_benchmark`
- `task_manager.py` — `start_warmup`, `wait_warmup`, `_create_task_runner`, `wait_all`
- `round_robin.py` — `_start_round`, `_wait_for_active_runners`
- `stats_collector.py` — `_take_snapshot`, `_print_snapshot`, `generate_report`, error/status/
  ready-check dispatch (~10 sites)
- `schemas.py` — `get_step_order`, `BenchSandbox.task_metrics`
- `config.py` — `KernelConfig.from_raw` hardcodes each section; `validate()` hardcodes the allowed set
- `src/env_provider/_ready.py` — `ReadyChecker.check`

`replay` was bolted on as the 4th workflow and is the stress test: its addition threaded
`== "replay"` into ~14 sites in `bench.py` and ~10 in `stats_collector.py`. The one clean seam —
`BenchSandbox.task_metrics`, a polymorphic property over `TaskMetricsBase` — already lets the
error/round paths be workflow-agnostic. The snapshot/report/dispatch paths were never converted.

There is **no `TaskRunner` ABC**. Each workflow ships 3 duck-typed `threading.Thread` subclasses
(warmup / fixed / round), and their constructors diverge — `replay` adds
`series=, admission=, launch_pacer=, scanner=` kwargs — so a generic runner factory is currently
impossible. Config is equally closed: `KernelConfig.from_raw` hardcodes each section
(`browser`/`coding`/`document`/`replay`) with named `raw.get(...)` + ~70 hand-mapped fields; adding
an `agent:` section is ~5 edits in `config.py` alone, plus `validate()`, `__post_init__`, and the
`cls(...)` mapping.

**Goals**

- A new workload = one new `task_runner/<wf>.py` module + one `WORKFLOW_REGISTRY` entry. Zero edits
  to `bench.py` / `task_manager.py` / `round_robin.py` / `stats_collector.py` / `_ready.py` /
  `schemas.py` / `config.py`.
- A `TaskRunner` ABC that enforces the runner contract (replaces copy-paste-learned convention).
- An open config path: `from_raw` reads an arbitrary `workflow_type` and routes its section through
  the registry.
- No behavior change for the existing 4 workflows — pure structural refactor, validated by the
  existing ~270 `FakeProvider`-driven tests.

**Non-goals**

- Changing what the 4 existing workflows *do* (metrics, steps, readiness probes unchanged).
- Touching the provider side (RFC 0001 territory).
- New workflows themselves — this only builds the plugin seam.
- Multi-node scaling (tracked separately).

## Detailed design

### 1. `WorkflowSpec` registry

A single registration table replaces every `if/elif` chain:

```python
@dataclass
class WorkflowSpec:
    name: str                              # "browser" | "coding" | "document" | "replay"
    warmup_runner: type[TaskRunner]
    task_runner: type[TaskRunner]
    round_runner: type[TaskRunner]
    metrics_cls: type[TaskMetricsBase]
    step_order: list[str]
    ready_probe: ReadyProbe | None         # None => exec-based default probe
    config_section: str                    # YAML key, e.g. "agent"
    report_formatters: ReportFormatters    # stats-section + step-timing formatters

WORKFLOW_REGISTRY: dict[str, WorkflowSpec] = {}

def register_workflow(spec: WorkflowSpec) -> None:   # called at import of task_runner/<wf>.py
    WORKFLOW_REGISTRY[spec.name] = spec
```

Every site that today does `if workflow_type == "browser": ... elif ...` becomes
`spec = WORKFLOW_REGISTRY[workflow_type]` then `spec.task_runner(ctx)` / `spec.metrics_cls(...)` /
`spec.step_order` / `spec.ready_probe`.

### 2. `TaskRunner` ABC + `RunContext`

The constructor-signature divergence (replay's extra kwargs) is the blocker for a uniform factory.
Solve it with a context object every runner takes:

```python
@dataclass
class RunContext:
    state: BenchSandbox
    config: KernelConfig
    provider: EnvironmentProvider
    stop_event: threading.Event
    round_id: int | None = None
    # replay-only knobs, ignored by non-replay runners:
    series: LifecycleSeriesWriter | None = None
    admission: AdmissionController | None = None
    launch_pacer: LaunchPacer | None = None
    scanner: SnapshotScanner | None = None

class TaskRunner(threading.Thread, ABC):
    def __init__(self, ctx: RunContext) -> None: ...
    @abstractmethod
    def run(self) -> None: ...
```

Replay passes the extra knobs; browser/coding/document leave them `None`. The shared runner-level
copy-paste (the `if not self.state.ready` gate, the `consecutive_errors >= 3 -> is_alive = False`
offline rule, `_classify_exception`, `_record_metrics`) lifts into `TaskRunner`.

### 3. Open config path

`KernelConfig.from_raw` reads `workflow_type`, looks up
`WORKFLOW_REGISTRY[workflow_type].config_section`, and stores the section opaquely:

```python
wf_spec = WORKFLOW_REGISTRY[wf]
workflow_config = raw.get(wf_spec.config_section) or {}
```

The per-workflow dataclass fields (`coding_*`, `document_*`, `replay_*`, `browser_*`) move into each
workflow's own typed config view (`wf_spec.config_view(workflow_config)`). The `__post_init__`
replay-specific validation moves onto that view. `validate()`'s hardcoded set becomes
`WORKFLOW_REGISTRY.keys()`.

### 4. Snapshot + report via the metrics polymorphism

`Snapshot`'s per-workflow fields (`browser_total`, `coding_total`, ... `replay_*`) project from
`BenchSandbox.task_metrics` — the polymorphism already exists. `stats_collector._take_snapshot` /
`_print_snapshot` / `generate_report` stop branching on `workflow_type` and instead call
`spec.report_formatters` and read `task_metrics`. The per-workflow `format_*_stats_section` /
`format_*_step_timing_table` methods register on the spec.

### Phasing (each step independently mergeable, mirrors RFC 0001)

- **Phase 0** — Introduce `TaskRunner` ABC + `RunContext` + `WorkflowSpec` + registry; route the
  existing 4 workflows through it. No elif removal yet, no behavior change. Tests stay green.
- **Phase 1** — Collapse the dispatch elif chains in `task_manager` / `round_robin` /
  `bench._print_header` / `_ready.check` / `schemas.get_step_order` to registry lookups.
- **Phase 2** — Config section registry; move per-workflow config fields to per-workflow views.
- **Phase 3** — `stats_collector` snapshot/report via `spec.report_formatters` + metrics polymorphism.

## Drawbacks

- Largest structural refactor since the kernel landed; touches the dispatch hot path in `bench.py` /
  `task_manager` / `stats_collector`.
- Risk of over-abstracting `RunContext` (replay-only knobs leaking onto every runner) — mitigated by
  keeping them `None`-defaulted and optional.
- The metrics/Snapshot polymorphism is only *partial* today; completing it (Phase 3) may surface
  per-workflow metric-shape divergence that the elif chains currently hide.

## Alternatives considered

- **A. Full registry + ABC (recommended, phased)** — collapses ~25 sites to ~2 per workflow; matches
  the provider side's architecture. Most work, phased.
- **B. `TaskRunner` ABC only, keep elif dispatch** — kills runner copy-paste but leaves the 25-site
  elif sprawl and the closed config. Half-measure.
- **C. Leave as-is, publish the touch-point checklist in CLAUDE.md** — zero code, but every future
  workflow (more agent load scenarios are explicitly wanted) pays the full shotgun-surgery tax.
  Not viable long-term.

Recommend **A**, phased. The precedent (RFC 0001's provider refactor) shipped the same shape
incrementally.

## Prior art / references

- RFC 0001 — `EnvironmentProvider` ABC + `BaseSandboxManager` template: the in-repo precedent this
  RFC mirrors for workflows.
- `BenchSandbox.task_metrics` / `TaskMetricsBase` (`schemas.py`) — the existing metrics polymorphism
  this RFC completes for runners / config / reporting.
- `round_robin.py` docstring — admits it "drops most per-workflow dispatch" via `task_metrics`; the
  dispatch was applied to metrics but never to runner construction.

## Unresolved questions

- **Protocol vs ABC** for `TaskRunner` — ABC enforces the contract and gives shared
  `__init__`/offline-rule; Protocol is lighter. Lean ABC (shared runner copy-paste is the thing to
  kill).
- **Registration mechanism** — explicit `WORKFLOW_REGISTRY` dict populated by `task_runner/<wf>.py`
  import, vs Python entry-points (`bench_core.workflows` group). Explicit dict is simpler and
  visible; entry-points are open-closed but hide registration. Lean dict until >6 workflows.
- **`RunContext` vs per-workflow context subclass** — replay's extra knobs: flat `None`-defaulted
  fields on `RunContext`, or a `ReplayRunContext(RunContext)` subclass the replay runner downcasts?
  Flat is simpler; subclass is cleaner-typed. Open.
- **Snapshot shape** — keep per-workflow `Snapshot` fields (typed, status quo) or move to a
  metrics-driven `dict`? Typed is safer; elif removal still works via `spec.report_formatters`.
  Lean typed.
- **CLI** — does the registry drive `--workflow-type` choices (today hardcoded at `bench.py:694`)?
  Likely yes in Phase 1.

## Future possibilities

- `agent` and custom workload types land as one-module PRs.
- A workflow can declare capability requirements (e.g. "needs `LifecycleCapable`") the way providers
  declare capability Protocols — symmetric to the provider side.
- The registry + spec pattern becomes the single seam both providers and workflows plug into, making
  the kernel a true two-axis plugin host.
