"""Workflow plugin seam — registry + TaskRunner ABC + RunContext + config/report contracts.

This is the workflow-side counterpart to RFC 0001's provider ABC: a single
``WORKFLOW_REGISTRY`` of :class:`WorkflowSpec` replaces every ``workflow_type``
if/elif chain, so a new workload is one new ``task_runner/<wf>.py`` module + one
``register_workflow`` call. The 4 in-tree workflows (browser / coding / document
/ replay) self-register at module import; the kernel imports those modules at
startup so registration fires before the first dispatch.

Phase 0 (this module): the seam EXISTS and is populated. Phase 1 migrates the
12 runners to ``TaskRunner`` + ``do_run`` and collapses the construction
dispatch (``_create_task_runner`` / ``start_warmup`` / ``round_robin``) to
registry lookups; runner fields are validated as ``TaskRunner`` subclasses.
``config_cls`` / ``report_formatters`` remain optional until Phases 2/3 land
their implementations. See ``docs/dev/rfcs/0002``.

Design notes (deviations from RFC §2, all flagged in the RFC living doc):

- ``TaskRunner`` exposes the contract (``__init__(ctx)`` + ``do_run()``) and
  opt-in shared *helpers* (``_gate_ready`` / ``_mark_offline_on_consecutive``)
  rather than a rigid template-method ``run()`` owning every guard. The 12
  runners' guards diverge by kind (warmup is one-shot; task loops; round carries
  step-times + classify/record), so one ``run()`` shape would force-fit. The
  helpers let each kind reuse what applies without bending them into one mold.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # annotations only — keeps the seam SDK-free + cycle-free at runtime
    from bench_core.config import KernelConfig
    from bench_core.observability.admission import Admission  # noqa: F401 (replay knob)
    from bench_core.observability.run_summary import LifecycleSeriesWriter
    from bench_core.schemas import BenchSandbox, Snapshot, TaskMetricsBase
    from env_provider import EnvironmentProvider


class RegistrationError(ValueError):
    """Duplicate or invalid workflow registration."""


class WorkflowConfigError(ValueError):
    """Unified config-validation failure — one type for the upper layer to catch."""


@dataclass(frozen=True)
class RunContext:
    """Everything a runner needs, frozen so it cannot rebind ``config`` / ``stop_event``.

    Non-replay runners leave the replay-only knobs ``None`` and ignore them; the
    replay runner narrows locally (``assert ctx.series is not None`` at entry).
    ``ext`` is the designated buffer for future workflow-specific params — the next
    workflow funnels its extras through this one field instead of bolting on N flat
    ones (promote to a typed field at >3 of a kind, or to a subclass for a family).
    Mutable contents (``stop_event``) stay mutable by design: ``frozen`` blocks field
    *rebinding*, not in-place mutation.
    """

    state: BenchSandbox
    config: KernelConfig
    provider: EnvironmentProvider
    stop_event: threading.Event
    round_id: int | None = None
    # Replay-only knobs (None for browser/coding/document; replay asserts non-None).
    series: LifecycleSeriesWriter | None = None
    admission: Admission | None = None
    launch_pacer: Any | None = None  # LaunchPacer — replay trajectory pacing
    scanner: Any | None = None  # SnapshotScanner — replay snapshot-size scan
    # Designated buffer for future workflow-specific params (see class docstring).
    ext: dict[str, object] = field(default_factory=dict)


class TaskRunner(threading.Thread, ABC):
    """Runner contract + opt-in shared guards. Subclasses implement ``do_run()``.

    The ready gate, the consecutive-errors breaker, and (for round runners)
    classify/record are the shared copy-paste lifted out of the 12 runners. Because
    those guards do NOT have one uniform shape across warmup / task / round kinds,
    they are exposed as helpers a subclass calls where applicable, not a forced
    template ``run()`` — see the module docstring. ``run()`` here just delegates to
    ``do_run()`` so the contract has a single seam.
    """

    def __init__(self, ctx: RunContext) -> None:
        super().__init__(daemon=True)
        self.ctx = ctx
        # Common attributes every runner currently sets by hand — surfaced here so
        # migrated runners inherit them instead of redeclaring.
        self.state = ctx.state
        self.config = ctx.config
        self.provider = ctx.provider
        self.stop_event = ctx.stop_event
        self.consecutive_errors = 0

    def run(self) -> None:
        """Single seam — delegates to the workflow-specific ``do_run()``."""
        self.do_run()

    @abstractmethod
    def do_run(self) -> None:
        """Workflow-specific entry point (replaces the old per-class ``run`` body)."""
        ...

    # --- opt-in shared guards (call where the guard applies) ---

    def _gate_ready(self) -> bool:
        """Return False (with a warning) when the sandbox is not ready; True otherwise.

        Warmup / task / round runners all gate on ``state.ready``. Centralizing the
        log keeps the message consistent; the caller returns early on False.
        """
        if not self.state.ready:
            # Defer the import so this module stays free of the schemas dependency
            # at definition time; ``creation_metrics`` is a SandboxInstance field.
            logger = _get_logger()
            logger.warning(f"[Sandbox{self.state.index}] Cannot start: " f"{self.state.creation_metrics.status.value}")
            return False
        return True

    def _mark_offline_on_consecutive(self) -> bool:
        """Increment the error counter; return True if the sandbox was just marked offline.

        Task / round runners use the "3 consecutive failures -> offline" rule. Returns
        True once the breaker trips (so the caller breaks its loop); False otherwise.
        """
        self.consecutive_errors += 1
        if self.consecutive_errors >= 3:
            self.state.is_alive = False
            _get_logger().warning(f"[Sandbox{self.state.index}] Marked offline (3 consecutive failures)")
            return True
        return False


def _get_logger():
    import logging

    return logging.getLogger("bench_core.workflow_registry")


@dataclass(frozen=True)
class WorkflowSpec:
    """Static metadata for one workflow — the unit the registry holds.

    ``config_cls`` / ``report_formatters`` are ``None`` until Phases 2/3 land their
    implementations; ``register_workflow`` skips their issubclass check when ``None``.
    Runner fields are ``type[TaskRunner]`` (the 12 in-tree runners migrated in Phase 1).
    """

    name: str
    warmup_runner: type[TaskRunner]
    task_runner: type[TaskRunner]
    round_runner: type[TaskRunner]
    metrics_cls: type  # type[TaskMetricsBase] — imported lazily to avoid a cycle
    step_order: tuple[str, ...]
    config_section: str
    ready_probe: object | None = None  # Phase 1: port _ready.check dispatch onto this
    config_cls: type | None = None  # Phase 2: type[WorkflowConfigBase]
    report_formatters: object | None = None  # Phase 3: ReportFormatters impl


class WorkflowConfigBase(ABC):
    """Per-workflow typed config view (Phase 2 target).

    ``KernelConfig.from_raw`` will route the workflow's YAML section through
    ``migrate`` -> ``from_raw`` -> ``validate``, producing a typed view (not an
    opaque dict) so static type-checking survives at the core config layer. The
    stubs below are abstract so a Phase 2 impl cannot forget a method.
    """

    @classmethod
    @abstractmethod
    def migrate(cls, raw: dict) -> dict:
        """Forward-compat transform for legacy YAML fields (renames/defaults/deprecations)."""
        ...

    @classmethod
    @abstractmethod
    def from_raw(cls, raw: dict) -> WorkflowConfigBase:
        """Parse this workflow's YAML section into the typed view (call migrate first)."""
        ...

    @abstractmethod
    def validate(self, kernel_config: KernelConfig | None = None) -> None:
        """Raise WorkflowConfigError on an invalid config (optional cross-section check)."""
        ...


@runtime_checkable
class ReportFormatters(Protocol):
    """Per-workflow plain-text report surfaces (Phase 3 target).

    Three named methods pin the contract so a workflow cannot silently drop one
    surface. ``snap`` is READ-ONLY (generic fields only); all workflow-specific data
    comes from ``metrics``. Each method emits its own heading so sections compose
    consistently across workflows. The impl may downcast ``metrics`` to its concrete
    subclass at entry — the kernel guarantees ``type(metrics) is spec.metrics_cls``.
    """

    def format_snapshot(self, metrics: TaskMetricsBase, snap: Snapshot) -> str:
        ...

    def format_step_timing(self, metrics: TaskMetricsBase) -> str:
        ...

    def format_overview(self, metrics: TaskMetricsBase, rounds: int) -> str:
        ...


WORKFLOW_REGISTRY: dict[str, WorkflowSpec] = {}


def register_workflow(spec: WorkflowSpec, *, force: bool = False) -> None:
    """Register a workflow spec; called at the bottom of each ``task_runner/<wf>.py``.

    Fails fast at startup: type-checks the spec, issubclass-checks every class field,
    and rejects duplicate names, so a miswired spec surfaces as a clear error at import
    time — not a deferred ``AttributeError`` mid-bench. ``force=True`` is for tests/dev
    hot-reload only.
    """
    if not isinstance(spec, WorkflowSpec):
        raise TypeError(f"expected WorkflowSpec, got {type(spec).__name__}")

    # TaskMetricsBase imported lazily so this module does not pull schemas at import
    # time (keeps the registry importable before schemas is fully loaded if needed).
    from bench_core.schemas import TaskMetricsBase

    if not isinstance(spec.metrics_cls, type) or not issubclass(spec.metrics_cls, TaskMetricsBase):
        raise TypeError(f"{spec.name}.metrics_cls must subclass TaskMetricsBase")
    for attr in ("warmup_runner", "task_runner", "round_runner"):
        cls = getattr(spec, attr)
        if not isinstance(cls, type) or not issubclass(cls, TaskRunner):
            raise TypeError(f"{spec.name}.{attr} must subclass TaskRunner")
    if spec.config_cls is not None and (
        not isinstance(spec.config_cls, type) or not issubclass(spec.config_cls, WorkflowConfigBase)
    ):
        raise TypeError(f"{spec.name}.config_cls must subclass WorkflowConfigBase or be None")
    if not isinstance(spec.step_order, tuple):
        raise TypeError(f"{spec.name}.step_order must be a tuple")
    if not spec.name:
        raise TypeError("workflow spec name must be a non-empty string")

    if spec.name in WORKFLOW_REGISTRY and not force:
        raise RegistrationError(f"workflow '{spec.name}' already registered; use force=True to override")
    WORKFLOW_REGISTRY[spec.name] = spec


def ensure_workflow_registered(name: str) -> None:
    """Import the named workflow's ``task_runner`` module so its register_workflow fires.

    Preserves the package's lazy-load principle (``task_runner/__init__.py`` is a
    pure namespace): a browser-only run imports only ``bench_core.task_runner.browser``,
    not coding/document/replay. The dispatch managers call this at construction so the
    registry holds the active workflow's spec before the first runner is built.
    """
    import importlib

    importlib.import_module(f"bench_core.task_runner.{name}")
