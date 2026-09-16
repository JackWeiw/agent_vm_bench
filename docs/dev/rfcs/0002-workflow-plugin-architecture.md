---
rfc: 0002
title: Workflow plugin architecture — TaskRunner ABC + registry to make adding a workload a one-file change
status: Implemented
author: "@JackWeiw"
shepherd: ""
areas: [e2b, docker, aenv]
created: 2026-09-15
updated: 2026-09-16
---

# Workflow plugin architecture — TaskRunner ABC + registry

> Refactor RFC: records the direction to end shotgun-surgery workflow extensibility. Not for
> immediate merge-as-code; a phased implementation plan follows acceptance. **Living document**:
> amended per implementation phase (see Phasing) — status flips to **Implemented** when all phases
> ship.

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
@dataclass(frozen=True)            # immutable spec metadata — no runtime mutation, no implicit coupling
class WorkflowSpec:
    name: str                              # "browser" | "coding" | "document" | "replay"
    warmup_runner: type[TaskRunner]
    task_runner: type[TaskRunner]
    round_runner: type[TaskRunner]
    metrics_cls: type[TaskMetricsBase]
    step_order: tuple[str, ...]            # immutable; tuple matches the frozen spec
    ready_probe: ReadyProbe | None        # None => exec-based default probe
    config_section: str                    # YAML key, e.g. "agent"
    config_cls: type[WorkflowConfigBase]   # typed per-workflow view (from_raw + validate), see §3
    report_formatters: ReportFormatters   # snapshot / step-timing / overview, see §4

class RegistrationError(ValueError):
    """Duplicate or invalid workflow registration."""

WORKFLOW_REGISTRY: dict[str, WorkflowSpec] = {}

def register_workflow(spec: WorkflowSpec, *, force: bool = False) -> None:
    """Called at the bottom of each task_runner/<wf>.py.

    Fails fast at startup: issubclass-checks every class field and rejects duplicate names, so a
    miswired spec surfaces as a clear error at import time, not a deferred AttributeError mid-bench.
    `force=True` is for tests/dev hot-reload only.
    """
    if not isinstance(spec, WorkflowSpec):
        raise TypeError(f"expected WorkflowSpec, got {type(spec).__name__}")
    for attr, base in (("warmup_runner", TaskRunner), ("task_runner", TaskRunner),
                       ("round_runner", TaskRunner), ("config_cls", WorkflowConfigBase),
                       ("metrics_cls", TaskMetricsBase)):
        if not issubclass(getattr(spec, attr), base):
            raise TypeError(f"{spec.name}.{attr} must subclass {base.__name__}")
    if not isinstance(spec.report_formatters, ReportFormatters):   # @runtime_checkable Protocol, see §4
        raise TypeError(f"{spec.name}.report_formatters must satisfy ReportFormatters")
    if spec.name in WORKFLOW_REGISTRY and not force:
        raise RegistrationError(
            f"workflow '{spec.name}' already registered; use force=True to override")
    WORKFLOW_REGISTRY[spec.name] = spec
```

Every site that today does `if workflow_type == "browser": ... elif ...` becomes
`spec = WORKFLOW_REGISTRY[workflow_type]` then `spec.task_runner(ctx)` / `spec.metrics_cls(...)` /
`spec.step_order` / `spec.ready_probe`.

### 2. `TaskRunner` ABC + `RunContext`

The constructor-signature divergence (replay's extra kwargs) is the blocker for a uniform factory.
Solve it with a context object every runner takes:

```python
@dataclass(frozen=True)            # runners must not rebind ctx.config / ctx.stop_event
class RunContext:
    state: BenchSandbox
    config: KernelConfig
    provider: EnvironmentProvider
    stop_event: threading.Event
    round_id: int | None = None
    # replay-only knobs, ignored by non-replay runners (narrow with `assert ctx.x is not None`):
    series: LifecycleSeriesWriter | None = None
    admission: AdmissionController | None = None
    launch_pacer: LaunchPacer | None = None
    scanner: SnapshotScanner | None = None
    # bloat speed-bump: future workflow-specific params go here first; promote to a typed
    # field when one workflow has >3 of a kind, or to a RunContext subclass when a family
    # needs structurally-typed extras. See Design decisions §3.
    ext: dict[str, object] = field(default_factory=dict)

class TaskRunner(threading.Thread, ABC):
    """Template method: `run()` is concrete, subclasses implement `do_run()`.

    Guards (ready gate, consecutive-errors breaker, perf_counter timing, exception
    classification, metrics recording) live in `run()`, so a subclass cannot bypass
    them by overriding run.
    """
    def __init__(self, ctx: RunContext) -> None: ...
    def run(self) -> None:
        # ready gate -> offline-breaker -> timing -> self.do_run() -> _classify_exception
        # / _record_metrics  (all shared, all here)
        self.do_run()
    @abstractmethod
    def do_run(self) -> None: ...
```

Replay passes the extra knobs; browser/coding/document leave them `None`. The shared runner-level
copy-paste (the `if not self.state.ready` gate, the `consecutive_errors >= 3 -> is_alive = False`
offline rule, `_classify_exception`, `_record_metrics`) lifts into `TaskRunner.run()` as the template
method — subclasses implement only `do_run()`, so the guards cannot be bypassed by an overridden
`run()`. `RunContext` is frozen so a runner cannot rebind `config` / `stop_event`; the `ext` dict is
the designated buffer for future workflow-specific params, slowing `RunContext` bloat (mutable contents
like `stop_event` stay mutable by design — `frozen` blocks field *rebinding*, not in-place mutation).

### 3. Open, type-safe config path

`KernelConfig.from_raw` reads `workflow_type`, looks up the spec, and builds the workflow's **typed**
config view — not an opaque `dict`. An opaque block would lose static type-checking at the core config
layer (the same risk class as the `Snapshot` dict rejected in §4). Each workflow implements a config
view behind an ABC, and `KernelConfig` triggers validation uniformly:

```python
class WorkflowConfigError(ValueError):
    """Unified config-validation failure — one type for the upper layer to catch."""

class WorkflowConfigBase(ABC):
    @classmethod
    @abstractmethod
    def migrate(cls, raw: dict) -> dict:
        """Forward-compat transform for legacy YAML fields (renames, defaults, deprecations).
        Centralizes compat so each workflow's from_raw does not re-implement it."""
        ...
    @classmethod
    @abstractmethod
    def from_raw(cls, raw: dict) -> "WorkflowConfigBase":
        """Parse this workflow's YAML section into the typed view (call migrate first)."""
        ...
    @abstractmethod
    def validate(self, kernel_config: KernelConfig | None = None) -> None:
        """Raise WorkflowConfigError on an invalid config. The optional kernel_config
        enables cross-section checks (port conflicts, resource limits vs sandbox batch)."""
        ...

# in KernelConfig.from_raw:
wf_spec = WORKFLOW_REGISTRY[wf]
section = wf_spec.config_cls.migrate(raw.get(wf_spec.config_section) or {})
workflow_config = wf_spec.config_cls.from_raw(section)   # typed, not dict
workflow_config.validate(kernel_config=self)             # uniform hook incl. cross-section
```

The per-workflow dataclass fields (`coding_*`, `document_*`, `replay_*`, `browser_*`) move into each
workflow's own typed view class (e.g. `CodingConfig(WorkflowConfigBase)`, `ReplayConfig(...)`). The
`__post_init__` replay-specific validation moves onto `ReplayConfig.validate()`. `validate()`'s
hardcoded allowed-set becomes `WORKFLOW_REGISTRY.keys()`. Type safety is preserved: the kernel holds a
`WorkflowConfigBase` reference, each workflow holds its own concrete subclass. **Access convention**:
only the workflow's own runner/formatter downcast — at entry, e.g.
`assert isinstance(ctx.config.workflow_config, ReplayConfig)` — localizing the cast to the code that
owns the concrete type (same pattern as the `RunContext` narrowing). The alternative — a spec-level
typed binding with base-side safe conversion — is noted but rejected as premature until >1 workflow
needs it. Validation failures raise `WorkflowConfigError` (a `ValueError` subclass) so the upper layer
catches one type, and `migrate()` centralizes forward-compat so legacy YAML keeps working across
versions.

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
@runtime_checkable
class ReportFormatters(Protocol):
    def format_snapshot(self, metrics: TaskMetricsBase, snap: Snapshot) -> str:
        """Per-workflow snapshot section (today's format_<wf>_stats_section).

        - `snap` is READ-ONLY: read generic fields only; all workflow-specific data comes
          from `metrics`. Do NOT add fields to `snap` — that is the regressed path §4 removes.
        - The impl may downcast `metrics` to its concrete subclass at entry; the kernel
          guarantees `type(metrics) is spec.metrics_cls`, so the cast is safe — no defensive checks.
        - Return plain text (the report is a .txt); include the section heading.
        """
        ...
    def format_step_timing(self, metrics: TaskMetricsBase) -> str:
        """Per-workflow step-timing table (today's format_<wf>_step_timing_table).
        Plain text, includes the table heading; same downcast contract as format_snapshot."""
        ...
    def format_overview(self, metrics: TaskMetricsBase, rounds: int) -> str:
        """Overview / run-summary section. `rounds` = number of rounds COMPLETED
        (0 during a fixed-duration single-round run). Plain text, includes heading."""
        ...
```

Splitting the contract into three named methods (rather than a single fuzzy formatter) pins the
abstraction and prevents leakage: a workflow cannot silently drop one of the three report surfaces.
The contract is explicit on three counts: (1) `snap` is read-only and carries only generic fields —
the regressed "stuff business fields into `Snapshot`" path is forbidden at the contract level; (2) the
impl owns the `metrics` downcast, and the kernel guarantees the type matches `spec.metrics_cls`, so no
per-impl defensive checks; (3) return format is pinned — plain text matching today's `.txt` report
(the xlsx/obs workbook path is separate and not these formatters' concern), and each method emits its
own heading so sections compose consistently across workflows.

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
- Risk of `RunContext` bloat (replay-only knobs leaking onto every runner) — mitigated by
  `None`-defaults, the `frozen` rebinding block, and the `ext` buffer that funnels future params
  through one designated field instead of N flat ones.
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
3. **`RunContext`: flat + frozen + `ext` buffer, not a subclass.** A
   `ReplayRunContext(RunContext)` would force the replay runner to downcast and break the uniform
   factory `spec.task_runner(ctx)`. Flat keeps the factory uniform; non-replay runners simply ignore
   the `None` fields; replay narrows locally (`assert ctx.series is not None` at entry) — type-safe
   where it matters, no structural smell. Two refinements: `frozen=True` blocks field rebinding (a
   runner cannot rebind `config` / `stop_event`); an `ext: dict` buffer is the designated home for
   future workflow-specific params, so the next workflow does not bolt another flat field on — promote
   to a typed field when one workflow has >3 of a kind, or to a `RunContext` subclass when a family
   needs structurally-typed extras. (Mutable contents like `stop_event` stay mutable by design —
   `frozen` blocks rebinding, not in-place mutation.)
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

## Implementation notes (Phase 0)

Phase 0 shipped `src/bench_core/workflow_registry.py` (`RunContext` + `TaskRunner` ABC +
`WorkflowSpec` + `WorkflowConfigBase` + `ReportFormatters` Protocol + `register_workflow` /
`WORKFLOW_REGISTRY`) and the 4 in-tree workflows self-register at module import. No dispatch
change, no runner migration, no behavior change (the full `FakeProvider` suite stays green).
Two new tests guard the phase: `test_workflow_registry.py` (4 specs registered + duplicate /
issubclass rejection) and `test_config_compat.py` (golden resolved-field values for every
shipped `config/common/*.yaml`, the regression gate for Phase 2).

Three deviations from the RFC text above, flagged here for review (each is a deliberate
resolution of a tension the RFC glossed over, not a silent deviation):

1. **Runner-issubclass gate deferred (P0 → P1).** The 12 existing runners subclass
   `threading.Thread` and take positional `(state, config, stop_event, provider, …)`, not
   `RunContext`. Migrating their constructors ⟹ changing every call site = the dispatch
   collapse (Phase 1). So Phase 0 `register_workflow` validates runner fields as
   `issubclass(cls, threading.Thread)` and tightens to `TaskRunner` in Phase 1 once the
   runners migrate. `metrics_cls` / `config_cls` issubclass checks are enforced now.

2. **`TaskRunner` exposes opt-in shared helpers, not a rigid template-method `run()`.**
   §2 says all shared guards lift into `run()` and subclasses implement only `do_run()`.
   In practice the guards diverge by runner kind — warmup is one-shot (ready gate only),
   task loops (ready gate + `consecutive_errors >= 3` breaker), round carries
   `step_times` + `_classify_exception` / `_record_metrics`. One `run()` shape would
   force-fit three shapes. `TaskRunner` therefore defines `__init__(ctx)` + `@abstractmethod
   do_run()` + opt-in helpers (`_gate_ready`, `_mark_offline_on_consecutive`) that each kind
   calls where applicable. `run()` delegates to `do_run()` so the contract has one seam.

3. **`config_cls` / `report_formatters` are optional in P0.** Their implementations land in
   Phase 2 (typed config views) / Phase 3 (report formatters). `WorkflowSpec` makes both
   `… | None = None`; `register_workflow` skips their issubclass check when `None`. The
   fields tighten to required when the implementations land.

## Implementation notes (Phase 1)

Phase 1 migrated the 12 runners onto the `TaskRunner` contract and collapsed the three
construction-dispatch sites to registry lookups. The kernel now builds every runner through
`WORKFLOW_REGISTRY[wf].{warmup,task,round}_runner(RunContext(...))`; the per-workflow
if/elif chains that selected a runner class are gone from `_create_task_runner` and
`round_robin._start_round`, and the warmup construction line is de-dispatched in
`start_warmup`.

What shipped:

- All 12 runners (`browser` / `coding` / `document` / `replay` × warmup / task / round)
  now subclass `TaskRunner`, take `__init__(ctx: RunContext)`, and implement `do_run()`
  (the old per-class `run` body, unchanged). `TaskRunner.__init__` sets the common attrs
  (`state` / `config` / `provider` / `stop_event` / `consecutive_errors`); each runner
  pulls its extras from `ctx` (`round_id`, replay `series` / `admission` / `launch_pacer` /
  `scanner`, the document `executor`).
- `register_workflow`'s runner issubclass gate tightened from `threading.Thread` to
  `TaskRunner` (Phase 0 deviation 1 resolved); `WorkflowSpec` runner fields are
  `type[TaskRunner]`.
- `_create_task_runner` (fixed mode) and `_start_round` (round-robin) are single registry
  lookups — the 4-way if/elif is gone. `start_warmup` keeps its elif (deviation 2 below).
- `ensure_workflow_registered(name)` imports the active workflow's `task_runner/<wf>.py`
  module so its `register_workflow` fires before the first dispatch. Called in
  `TaskManager` / `RoundRobinTaskManager` `__init__`. Preserves the package's lazy-load
  principle (`task_runner/__init__.py` stays a pure namespace): a browser-only run imports
  only `browser`, not coding / document / replay.
- `ReplayBaseRunner` (replay's internal slice-machinery base, shared by its 3 runners)
  gets a run-hostile `do_run` stub. Subclassing `TaskRunner` (abstract `do_run`) would
  have made the base uninstantiable, breaking ~24 unit tests that legitimately construct
  it (via `__init__` and `__new__`) to drive its slice / lifecycle / admission helpers in
  isolation. The raising stub restores that instantiability — the base stays a testable
  helper, not a runnable runner — while honestly signaling "not meant to run directly":
  the 3 concrete replay runners override `do_run` with their loops; the bare base's
  `do_run` raises `NotImplementedError`. (A pure-ABC base + per-test doubles was rejected:
  it would paper 24 test sites with stand-ins for a base whose non-`do_run` methods are
  the intended unit-test surface.)

Deviations from the RFC text, flagged for review:

1. **The opt-in helpers (`_gate_ready` / `_mark_offline_on_consecutive`, Phase 0 deviation 2)
   are NOT adopted in Phase 1.** Reading all 12 guards after migration, they do not share a
   uniform shape: warmup runners set `warmup_done` (and document warmup sets a metric, no
   warning); round runners composite-guard on `ready or is_alive`; replay warns "not ready"
   and sets `warmup_done`. A single helper would either force-fit or homogenise the
   per-workflow log prefixes ("Cannot start warmup" / "Cannot start tasks" / "Cannot start
   replay") — a behavior change. The helpers remain as opt-in contract surface; adopting
   them (and unifying the log prefix) is a separate cleanup PR. Phase 1 is a
   behavior-identical migration: same guards, same log text, only the constructor + dispatch
   seam changed.

2. **`start_warmup`'s elif on `workflow_type` is retained, not collapsed.** That elif
   carries per-workflow logging banners + skip guards (browser no-urls early return, coding
   skip-verify) — presentation / orchestration, not object construction. Only the
   construction line was de-dispatched (`spec.warmup_runner(ctx)`); the elif skeleton stays.
   The RFC §2 anticipated this (start_warmup is interleaved with workflow-specific
   orchestration). A future cleanup can data-drive the banners once Phase 2's typed config
   views own the per-workflow fields.

3. **Three metadata-dispatch sites are deferred out of Phase 1**, each for a concrete
   reason. `_ready.check` (the `ready_probe` field): the probes are `ReadyChecker` instance
   methods using `self._exec` / `self._max_wait`, so `spec.ready_probe` cannot hold a bound
   method — porting needs a probe abstraction (a `ReadyProbe` Protocol or free-function
   refactor of `_ready.py`), its own review. `_print_header`: its branches are
   config-field-specific (coding `project` / `language`, replay `traj` / `mode`), which
   reads cleanly off Phase 2's typed config views, not off the registry. `get_step_order`:
   collapsing it onto the registry would invert the layering (the lower `schemas` layer
   importing the registry seam), and document's `case_kind` (xlsx / pdf) variant means it
   is not a pure lookup. None of these build workflow-specific objects, so they are not on
   the dispatch-collapse critical path.

What is NOT done (deferred): adopting the opt-in guards; `ready_probe`; Phase 2 typed
`WorkflowConfigBase` views (route `KernelConfig.from_raw` through
`config_cls.migrate` / `from_raw` / `validate`, move ~70 config fields off
`KernelConfig`, gated by `test_config_compat`); Phase 3 `ReportFormatters` (move
per-workflow totals off `Snapshot`). Status stays **Active** (not Implemented) until
Phase 3 lands.

## Implementation notes (Phase 2)

Phase 2 opens the type-safe config path from RFC §3. `KernelConfig.from_raw` now looks up the
active workflow's `WorkflowSpec.config_cls`, builds the typed view via
`migrate → from_raw → validate`, and attaches it as `KernelConfig.workflow_config`. Four typed
views ship — `BrowserConfig` / `CodingConfig` / `DocumentConfig` / `ReplayConfig` — each owned
by its `task_runner/<wf>.py` module (RFC: views are "owned by the workflow module").

**P2 is a 2-PR sub-stack** (`P2-1` additive seam, then `P2-2` field move) so the move's blast
radius (165 production + ~30 test access sites) is gated by a proven equivalence check, not done
in one risky commit.

What shipped in **P2-1** (this PR):

- The four `<Workflow>Config(WorkflowConfigBase)` views, each a mutable `@dataclass` (matches
  `KernelConfig`, which is mutable; `RunContext` is frozen, the views are not). Fields:
  `BrowserConfig` 7, `CodingConfig` 9, `DocumentConfig` 7, `ReplayConfig` 16 (40 total — the
  per-workflow fields that today live flat on `KernelConfig`).
- `WorkflowSpec.config_cls` wired on all four specs; `register_workflow`'s issubclass gate
  (`WorkflowConfigBase`) now runs for real (Phase 0 left it `None`).
- `KernelConfig.workflow_config: WorkflowConfigBase | None` field (annotation-only import of
  `WorkflowConfigBase` under `TYPE_CHECKING` — no runtime cycle).
- `KernelConfig.from_raw` builds the view **alongside** the flat fields (dual population,
  behavior-identical): the flat per-workflow fields are still populated exactly as today, and
  `__post_init__` / `validate()` are unchanged. The view is a typed mirror, not the source of
  truth yet — `P2-2` flips it.
- `WorkflowConfigBase` ABC contract standardized (see "Plugin contract" below): `migrate`
  returns a raw dict (forward-compat only); `from_raw` owns typed parsing + post-parse
  defaulting/normalization; `validate(self, kernel_config: KernelConfig)` is a **required**
  param (cross-section checks read it, self-contained checks ignore it) — no per-impl
  signature drift.
- The gate (`test_config_compat.py`) extended: a `dataclasses.fields()`-driven equivalence test
  asserts every view field == the flat field for every shipped YAML (auto-extends when a field
  is added), plus negative tests — illegal `workflow_type` raises `WorkflowConfigError`; an
  invalid `replay_control_plane_qps` raises the same `ValueError` family pre- and post-view
  (consistency); `ReplayConfig.validate` cross-section `replay_running_concurrency <=
  total_count` raises; CLI `--workflow-type` override selects the right view.
- `bench.py` `--workflow-type` override is routed through `raw` **before** `from_raw`
  (`load_config(path, workflow_type_override=...)`), not post-hoc on the built config — setting
  it post-hoc would leave the typed view bound to the YAML's workflow (D7).

Deviations from the RFC text, each with reason + follow-up:

1. **`warmup_only` stays shared on `KernelConfig`** (D3). Reason: it is a bench-mode toggle
   (`--warmup-only`, consumed by `bench.py` orchestration), not a browser-runner field;
   `warmup_urls` / `warmup_loops` / `warmup_delay` (warmup *content*) did move to
   `BrowserConfig`. Follow-up: none — `warmup_only` is orthogonal to the workflow axis.

2. **`document` has no shipped YAML** (D4). Reason: `config/common/document*.yaml` is empty, so
   the golden gate pins no `DocumentConfig` values. `DocumentConfig` is covered by an inline-dict
   unit test instead. Follow-up: ship a `document.yaml` later → hook it into the golden gate's
   `EXPECTED` and the equivalence test.

3. **`ensure_workflow_registered(wf)` is called inside `from_raw`'s method body, never at
   `config.py` module top-level** (D6). Reason: the runner modules import `config` at runtime;
   hoisting the call to module top-level would re-enter `config` mid-load → `ImportError`. A
   `# ponytail:` comment marks this non-obvious ceiling. Follow-up: none — structural.

4. **Dual validation in P2-1** (transient). Both the flat `__post_init__` / `validate()` blocks
   and `view.validate(config)` run, checking the same values. Reason: P2-1 is additive and
   behavior-identical; removing the flat blocks in the same PR would mix the seam with the field
   move. Follow-up: P2-2 removes the flat `__post_init__` per-workflow blocks and the
   `validate()` hardcoded `workflow_type` set (reads `WORKFLOW_REGISTRY.keys()` or drops —
   `from_raw`'s registry lookup is the authoritative gate), leaving `view.validate` as the sole
   check.

What is NOT done (deferred to P2-2): remove the 40 flat per-workflow fields from `KernelConfig`;
migrate the 165 production + ~30 test access sites to the view (runner-internal → cross-workflow
branches → unguarded cross sites, risk-ascending for easy locate/rollback); add `isinstance`
guards at the 6 unguarded cross-cutting sites (D5 — real guards, not `__getattr__` delegation,
which would defeat the static-typing goal); the zombie-field test (`assert not
hasattr(cfg, "browser_urls")` for the moved fields). P3 `ReportFormatters` (move per-workflow
totals off `Snapshot`) follows. Status stays **Active** (not Implemented) until P3 lands.

## Implementation notes (Phase 3)

P3 lands the report-layer collapse — the final phase. `stats_collector` (1641 → ~580
lines) no longer branches on `workflow_type` anywhere in the report path:
`generate_report` / `_print_snapshot` / `_take_snapshot` / `format_error_section` /
`format_sandbox_status_section` / `format_round_comparison_table` all read
`spec.report_formatters` (the `ReportFormatters` strategy on each `WorkflowSpec`).
`_take_snapshot` projects cumulative task totals + a recent-latency window from the
polymorphic `BenchSandbox.task_metrics`; per-workflow narrows (replay trajectory
progress, browser ports, coding verify) are rendered live by
`format_snapshot_line` at print time, not snapshotted. `ErrorClassifier` /
`TableFormatter` / `*_ERROR_DISPLAY` / `replay_pool_size` / `replay_traj_target` /
`MIN_SLICE_SEC` lifted into `report_helpers` (neutral leaf) so the per-workflow
formatters (in `task_runner/<wf>.py`, co-located with runner + config + spec) import
shared helpers without reaching into the host — the `stats_collector → task_runner.*`
import edge (and the `DocumentConfig` / `ReplayConfig` narrows) is gone, dissolving
the cycle the helpers-extraction broke. The per-workflow formatters port the existing
`format_*_stats_section` / `format_*_step_timing_table` / (replay)
`format_throughput_section` / `format_trajectory_summary_section` /
`_format_lifecycle_overhead_by_round` bodies byte-for-byte (`self.X → ctx.X`).

Behavior gate: a fixed-fixture `generate_report()` across all 4 workflows (including
the full replay lifecycle / admission / throughput / trajectory sections) is
byte-identical to the P2 base (golden diff = 0); the ~864-test `FakeProvider` suite
stays green; ruff + pre-commit clean.

Deviations from the RFC text, each with reason + follow-up:

1. **`ReportFormatter` keeps 1-line delegating facades** (D8). The host retains
   generic `format_stats_section` / `format_step_timing` / `format_throughput_section`
   / `format_trajectory_summary_section` methods that delegate to
   `self._fmt.<same>(self._ctx)`, rather than forcing every caller/tests to reach into
   `WORKFLOW_REGISTRY` directly. Reason: minimizes test churn (existing tests construct
   `ReportFormatter(config, states)` and call methods on it); the facade names are
   *generic* (not per-workflow), so the dispatch-collapse goal — no `workflow_type`
   if/elif in the report path — is still met. Follow-up: none — the facades are the
   host's stable surface; a future caller may use the registry directly.

2. **`ensure_workflow_registered` fires at `StatsCollector` / `ReportFormatter`
   construction, not only in `config.from_raw`** (D9). Reason: the registry is
   populated by `task_runner` module import side-effects, and Mock-config tests bypass
   `from_raw`, so the strategy lookup would `KeyError` without the lazy ensure. The
   call is idempotent (no-op for real runs where `from_raw` already registered).
   Follow-up: none — structural (D6's reasoning applies).

3. **`ReportFormatters` is an ABC with default no-op methods, not a
   `runtime_checkable Protocol`** (D10). The original RFC text specified a 3-method
   Protocol; the implementation uses an ABC with 3 `@abstractmethod`s + 4 default
   no-op methods (`format_throughput_section` / `format_trajectory_summary_section` /
   `format_config_extras` / `format_round_extras` return `[]`) + 5 class-attr
   constants. Reason: the strategy has shared defaults (non-replay surfaces return
   `[]`), which an ABC expresses naturally (a Protocol cannot supply bodies); the ABC
   also enforces the contract at registration via `isinstance` in `register_workflow`,
   stronger than a Protocol's structural check. Follow-up: none.

4. **`ReportContext` is a frozen dataclass, decoupling formatters from the host class**
   (D11). Carries `(config, sandbox_states, admission_snapshot, wall_sec)`. Reason:
   the formatters live in `task_runner` modules; they must not hold a back-reference
   to the `ReportFormatter` host instance (that would re-create the cycle the
   helpers-extraction broke) — the dependency stays one-directional
   (`task_runner → observability`, never reverse). Follow-up: none.

All four phases (P0–P3) now shipped. Status flips **Active → Implemented**.

## Plugin contract — a workflow's config view

A new workflow's config view is one class in `task_runner/<wf>.py`, registered via
`config_cls=` on its `WorkflowSpec`. The `WorkflowConfigBase` ABC pins three responsibilities:

- **`migrate(cls, raw: dict) -> dict`** — forward-compat only. Owns legacy shape repair (field
  renames, deprecated-key defaults, format upgrades) and returns a raw dict (not a typed
  object), so `from_raw` stays the single typed-parsing step. Identity (`return raw`) when there
  is nothing to migrate yet. A future YAML rename touches `migrate` alone — never the base class
  or a runner.
- **`from_raw(cls, raw: dict) -> <Workflow>Config`** — typed parsing + post-parse
  defaulting/normalization (e.g. filling a list from a language default, force-disabling a knob
  in a given mode). Does **not** mutate `kernel_config`; cross-section checks belong to
  `validate`.
- **`validate(self, kernel_config: KernelConfig) -> None`** — raise `WorkflowConfigError` on an
  invalid config. `kernel_config` is always passed (the view is validated after the dataclass is
  built); cross-section checks read it, self-contained checks ignore it. Required param — no
  per-impl signature drift.

Naming: `<Workflow>Config` (e.g. `BrowserConfig`). Registration: set
`config_cls=<Workflow>Config` in the module's `register_workflow(WorkflowSpec(...))` call. The
view is mutable (`@dataclass`, not frozen) to match `KernelConfig`. Runners narrow at entry:
`assert isinstance(ctx.config.workflow_config, <Workflow>Config)` (the `RunContext`-narrowing
pattern) — restoring static type-checking at the core config layer, the RFC's whole point.
