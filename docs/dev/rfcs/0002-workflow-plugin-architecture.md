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
- An open, type-safe config path: `from_raw` reads an arbitrary `workflow_type` and routes its
  section through a typed per-workflow config view (no opaque `dict` at the core layer).
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
    config_cls: type[WorkflowConfigBase]   # typed per-workflow view (from_raw + validate), see §3
    report_formatters: ReportFormatters    # snapshot / step-timing / overview, see §4

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

### 3. Open, type-safe config path

`KernelConfig.from_raw` reads `workflow_type`, looks up the spec, and builds the workflow's **typed**
config view — not an opaque `dict`. An opaque block would lose static type-checking at the core config
layer (the same risk class as the `Snapshot` dict rejected in §4). Each workflow implements a config
view behind an ABC, and `KernelConfig` triggers validation uniformly:

```python
class WorkflowConfigBase(ABC):
    @classmethod
    @abstractmethod
    def from_raw(cls, raw: dict) -> "WorkflowConfigBase":
        """Parse this workflow's YAML section into the typed view."""
        ...
    @abstractmethod
    def validate(self) -> None:
        """Raise ValueError on an invalid config."""
        ...

# in KernelConfig.from_raw:
wf_spec = WORKFLOW_REGISTRY[wf]
section = raw.get(wf_spec.config_section) or {}
workflow_config = wf_spec.config_cls.from_raw(section)   # typed, not dict
workflow_config.validate()                               # uniform validation hook
```

The per-workflow dataclass fields (`coding_*`, `document_*`, `replay_*`, `browser_*`) move into each
workflow's own typed view class (e.g. `CodingConfig(WorkflowConfigBase)`, `ReplayConfig(...)`). The
`__post_init__` replay-specific validation moves onto `ReplayConfig.validate()`. `validate()`'s
hardcoded allowed-set becomes `WORKFLOW_REGISTRY.keys()`. Type safety is preserved: the kernel holds a
`WorkflowConfigBase` reference, each workflow holds its own concrete subclass.

### 4. Snapshot + report via the metrics polymorphism

`Snapshot` becomes **workflow-agnostic**: the per-workflow totals (`browser_total`, `coding_total`,
... `replay_*`) move off `Snapshot` and project from `BenchSandbox.task_metrics` instead. `Snapshot`
keeps only the generic, typed fields (creation / ready / timing / error counts). The per-workflow
breakdown lives on the typed metrics subclasses (`BrowserMetrics` / `CodingMetrics` /
`ReplayMetrics`, already `TaskMetricsBase` subclasses) — that is where type safety belongs, and those
subclasses are owned by the workflow module, not the kernel. This removes the `schemas.py` Snapshot
touch point entirely: adding a workflow no longer adds `Snapshot` fields.

`stats_collector._take_snapshot` / `_print_snapshot` / `generate_report` stop branching on
`workflow_type` and call `spec.report_formatters`. The formatter contract is explicit — three standard
methods, all taking `TaskMetricsBase`:

```python
class ReportFormatters(Protocol):
    def format_snapshot(self, metrics: TaskMetricsBase, snap: Snapshot) -> str:
        """Per-workflow snapshot section (today's format_<wf>_stats_section)."""
        ...
    def format_step_timing(self, metrics: TaskMetricsBase) -> str:
        """Per-workflow step-timing table (today's format_<wf>_step_timing_table)."""
        ...
    def format_overview(self, metrics: TaskMetricsBase, rounds: int) -> str:
        """Overview / run-summary section."""
        ...
```

Splitting the contract into three named methods (rather than a single fuzzy formatter) pins the
abstraction and prevents leakage: a workflow cannot silently drop one of the three report surfaces,
and each method's signature guarantees the formatter reads only the metrics object — no hidden coupling
to `workflow_type` strings or private snapshot internals.

### Phasing (each step independently mergeable, mirrors RFC 0001)

- **Phase 0** — Introduce `TaskRunner` ABC + `RunContext` + `WorkflowSpec` + `WorkflowConfigBase` +
  registry; route the existing 4 workflows through it. No elif removal yet, no behavior change. The
  ~270 `FakeProvider` tests stay green; add a **config-compatibility test** (see Testing) before any
  config field moves.
- **Phase 1** — Collapse the dispatch elif chains in `task_manager` / `round_robin` /
  `bench._print_header` / `_ready.check` / `schemas.get_step_order` to registry lookups; drive
  `--workflow-type` CLI choices from `WORKFLOW_REGISTRY.keys()`.
- **Phase 2** — Move per-workflow config fields onto typed `WorkflowConfigBase` views; route
  `KernelConfig.from_raw` through `config_cls.from_raw().validate()`.
- **Phase 3** — `stats_collector` snapshot/report via `spec.report_formatters` (3-method contract) +
  the metrics polymorphism; move per-workflow totals off `Snapshot`.

### Testing

- **Config compatibility**: a golden-config test loads every shipped `config/common/*.yaml`, builds
  `KernelConfig` before and after each phase, and asserts the resolved per-workflow config view
  reproduces the legacy field values bit-for-bit — same YAML in, identical resolved config out. Guards
  backward compatibility of every shipped config file across the refactor.
- **Runner behavior**: the existing `FakeProvider`-driven suite (~270 tests) is the regression gate at
  every phase; no phase merges with a red test.

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

## Design decisions (resolving the open questions)

1. **`TaskRunner`: ABC, not Protocol.** `TaskRunner(threading.Thread, ABC)` owns the shared concrete
   code (`__init__`, the `consecutive_errors >= 3` offline rule, `_classify_exception`,
   `_record_metrics`) — that is the whole point of lifting the copy-paste. A `Protocol` is structural
   only: it types the shape but provides no shared implementation, so it would not kill the
   duplication. `threading.Thread` already requires inheritance (override `run`), so ABC is the
   Python-idiomatic fit on both counts. `@abstractmethod def run` enforces the one true seam.
2. **Registration: explicit `WORKFLOW_REGISTRY` dict, not entry-points.** `register_workflow()` is
   called at the bottom of each `task_runner/<wf>.py`; the kernel imports those modules at startup.
   Explicit registration is greppable, needs no packaging metadata, and matches the existing
   `_build_provider` lazy-import style. Entry-points (`importlib.metadata`) are open-closed but hide
   registration and add packaging complexity only justified when *third-party* packages ship workflows
   — not the case (all workflows are in-tree). Revisit if external workflow plugins ever materialize.
3. **`RunContext`: flat dataclass with `None`-defaulted replay knobs, not a subclass.** A
   `ReplayRunContext(RunContext)` would force the replay runner to downcast and break the uniform
   factory `spec.task_runner(ctx)`. Flat keeps the factory uniform; non-replay runners simply ignore
   the `None` fields. Replay narrows locally (`assert ctx.series is not None` at entry) — type-safe
   where it matters, no structural smell. Ceiling: if replay-only context grows past ~6 knobs,
   revisit a typed subclass carried via the spec.
4. **`Snapshot`: workflow-agnostic + typed; per-workflow data stays on typed metrics subclasses.**
   Reject both extremes — neither per-workflow `Snapshot` fields (re-introduces the `schemas.py` touch
   point) nor a metrics-driven `dict` (sacrifices type safety, the same risk class as the opaque config
   block). Instead: `Snapshot` holds generic typed fields only; per-workflow breakdown lives on the
   typed `TaskMetricsBase` subclasses (`BrowserMetrics` etc.), projected by `spec.report_formatters`.
   Type safety lives on the metrics subclasses, owned by each workflow module.
5. **CLI: registry-driven.** `--workflow-type choices=list(WORKFLOW_REGISTRY)` (Phase 1) so adding a
   workflow auto-updates the CLI — no separate touch point.

## Future possibilities

- `agent` and custom workload types land as one-module PRs.
- A workflow can declare capability requirements (e.g. "needs `LifecycleCapable`") the way providers
  declare capability Protocols — symmetric to the provider side.
- The registry + spec pattern becomes the single seam both providers and workflows plug into, making
  the kernel a true two-axis plugin host.
