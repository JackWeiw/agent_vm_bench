"""Host-agnostic kernel configuration (core fields only).

``KernelConfig`` holds the configuration the benchmark kernel needs to drive any
provider. Provider-specific config (e2b env vars, docker image, NUMA binding,
smap_tool) lives in the provider's own config; vm_monitor is orchestrated
host-side via the ``monitor:`` section (see MonitorController). The kernel
reads only the host-agnostic subset.

Per-workflow config (browser/coding/document/replay knobs) lives on typed views
(``BrowserConfig``/``CodingConfig``/``DocumentConfig``/``ReplayConfig``)
attached as ``workflow_config`` and built by ``from_raw`` via the active
``WorkflowSpec``'s ``config_cls`` (``migrate -> from_raw -> validate``); runners
narrow at entry. The kernel carries only the shared stress fields
(sandbox/batch/benchmark/test/report) plus the ``warmup_only`` bench-mode
toggle. Per-language coding profiles, replacement pairs, and verify templates
live in :mod:`bench_core.payload.coding_payload`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bench_core.observability.monitor import MonitorConfig
from bench_core.workflow_registry import WORKFLOW_REGISTRY, WorkflowConfigError, ensure_workflow_registered

if TYPE_CHECKING:
    # Annotation-only (the field is a string under `from __future__ import
    # annotations`); never imported at runtime, so no config <-> registry cycle.
    from bench_core.workflow_registry import WorkflowConfigBase

# In-sandbox scene layout per document case kind. These paths live inside the
# sandbox image (the document seed is baked in by the provider's prepare hook);
# they are host-agnostic, so they belong to the kernel, not to e2b.
DOCUMENT_SCENE_LAYOUTS: dict[str, dict[str, str]] = {
    "pdf": {
        "seed_dir": "/opt/document-bench/pdf",
        "workspace_dir": "/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01",
    },
    "xlsx": {
        "seed_dir": "/opt/document-bench/xlsx",
        "workspace_dir": "/root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01",
    },
}


def document_scene_layout(case_kind: str) -> dict[str, str]:
    """Return the in-sandbox ``{seed_dir, workspace_dir}`` layout for a case kind."""
    try:
        return DOCUMENT_SCENE_LAYOUTS[case_kind]
    except KeyError:
        raise ValueError("document_case_kind must be 'pdf' or 'xlsx'") from None


@dataclass
class KernelConfig:
    """Core benchmark configuration shared across all providers."""

    # --- sandbox control ---
    total_count: int = 100
    detect_existing: bool = False
    create_only: bool = False
    cleanup_only: bool = False  # --cleanup: list + kill existing sandboxes, then exit

    # --- batch control (None = full concurrent) ---
    create_batch_size: int | None = None
    create_batch_interval: int | None = None
    task_batch_size: int | None = None
    task_batch_interval: int | None = None

    # --- benchmark ---
    benchmark_percent: float = 1.0
    benchmark_mode: str = "fixed"  # "fixed" | "round_robin"
    round_count: int | None = None
    round_size: int = 5
    round_interval: int = 5

    # --- workflow axis (orthogonal to the environment axis) ---
    workflow_type: str = "browser"  # "browser" | "coding" | "document" | "replay"

    # Phase 2: the workflow's typed config view. Built by ``from_raw`` via the
    # active spec's ``config_cls`` (``migrate -> from_raw -> validate``); ``None``
    # only when constructed directly (not via ``from_raw``). Runners narrow at
    # entry: ``assert isinstance(ctx.config.workflow_config, CodingConfig)``.
    workflow_config: WorkflowConfigBase | None = None

    # --- warmup (shared --warmup-only toggle; warmup URLs/loops/delay are on BrowserConfig) ---
    warmup_only: bool = False

    # --- test run ---
    test_duration: int = 600
    stats_interval: int = 10

    # --- report ---
    output_dir: str = "results/kernel"
    filename_prefix: str = "bench"
    report_format: str = "txt"

    # --- monitor (host-side vm_monitor orchestration) ---
    monitor: MonitorConfig = field(default_factory=MonitorConfig)

    # --- derived counts ---
    @property
    def benchmark_count(self) -> int:
        """Sandbox count for the benchmark phase (floored at 1)."""
        return max(1, int(self.total_count * self.benchmark_percent))

    @property
    def create_batch_count(self) -> int:
        """Number of creation batches (1 when concurrent / unset)."""
        if not self.create_batch_size:
            return 1
        return (self.total_count + self.create_batch_size - 1) // self.create_batch_size

    @property
    def task_batch_count(self) -> int:
        if not self.task_batch_size:
            return 1
        return (self.total_count + self.task_batch_size - 1) // self.task_batch_size

    def validate(self) -> None:
        """Raise :class:`WorkflowConfigError` for invalid settings; call after construction.

        The ``workflow_type`` gate is registry-driven: a newly-registered
        workflow passes without editing a hardcoded set here. ``from_raw``'s
        ``ensure_workflow_registered`` lookup is the authoritative gate (it
        fires before this is ever called on a ``from_raw`` config), so an
        unknown workflow is rejected here too; the per-view
        ``WorkflowConfigBase.validate`` owns cross-section checks.
        """
        if self.workflow_type not in WORKFLOW_REGISTRY:
            raise WorkflowConfigError(f"Unsupported workflow_type: {self.workflow_type!r}")
        if self.round_size <= 0:
            raise WorkflowConfigError(f"round_size must be > 0, got {self.round_size}")
        if self.benchmark_mode not in {"fixed", "round_robin"}:
            raise WorkflowConfigError(f"benchmark_mode must be fixed or round_robin, got {self.benchmark_mode}")

    @classmethod
    def from_raw(cls, raw: dict) -> KernelConfig:
        """Build a ``KernelConfig`` from a raw YAML dict in the unified schema.

        The single reader of the shared stress sections (``sandbox`` /
        ``create_batch`` / ``task_batch`` / ``browser`` / ``test`` / ``report``
        / ``workflow_type``). Lifts the nested->flat mapping the e2b and docker
        provider Configs each used to carry a copy of, so the kernel reads the
        YAML's stress params instead of falling back to defaults.

        Backend blocks (``e2b:`` / ``docker:``) are ignored here -- the provider
        reads them from the same raw dict. Missing sections fall back to the
        dataclass defaults, so a backend-only YAML still loads.
        """
        sandbox = raw.get("sandbox") or {}
        create_batch = raw.get("create_batch") or {}
        task_batch = raw.get("task_batch") or {}
        browser = raw.get("browser") or {}
        test = raw.get("test") or {}
        report = raw.get("report") or {}
        monitor = raw.get("monitor") or {}

        # workflow_type: top-level wins, then the legacy workflow.type form.
        wf = raw.get("workflow_type")
        if wf is None:
            wf = (raw.get("workflow") or {}).get("type", "browser")

        # Phase 2: build the workflow's typed config view. ``ensure_workflow_registered``
        # imports the active workflow's ``task_runner`` module (idempotent via the
        # importlib cache) so its ``register_workflow`` fires and ``config_cls`` is set
        # before the lookup. Per-workflow knobs live only on the view; the kernel
        # keeps just the shared fields (plus warmup_only from the browser section).
        # ponytail: keep the ensure call inside from_raw -- runner modules import config
        # at runtime, so config is fully loaded before from_raw runs; hoisting this to
        # module top-level would re-enter config mid-load (ImportError).
        try:
            ensure_workflow_registered(wf)
        except ModuleNotFoundError:
            raise WorkflowConfigError(f"Unsupported workflow_type: {wf!r}") from None
        spec = WORKFLOW_REGISTRY[wf]
        view = None
        if spec.config_cls is not None:
            section = spec.config_cls.migrate(raw.get(spec.config_section) or {})
            view = spec.config_cls.from_raw(section)

        config = cls(
            # --- sandbox control ---
            total_count=sandbox.get("total_count", 100),
            detect_existing=sandbox.get("detect_existing", False),
            create_only=sandbox.get("create_only", False),
            # --- batch control ---
            create_batch_size=create_batch.get("size"),
            create_batch_interval=create_batch.get("interval"),
            task_batch_size=task_batch.get("size"),
            task_batch_interval=task_batch.get("interval"),
            # --- benchmark ---
            benchmark_percent=test.get("benchmark_percent", 1.0),
            benchmark_mode=test.get("benchmark_mode", "fixed"),
            round_count=test.get("round_count"),
            round_size=test.get("round_size", 5),
            round_interval=test.get("round_interval", 5),
            # --- workflow ---
            workflow_type=wf,
            warmup_only=browser.get("warmup_only", False),
            # --- test run ---
            test_duration=test.get("duration", 600),
            stats_interval=test.get("stats_interval", 10),
            # --- report ---
            output_dir=report.get("output_dir", "results/kernel"),
            filename_prefix=report.get("filename_prefix", "bench"),
            report_format=report.get("format", "txt"),
            # --- monitor ---
            monitor=MonitorConfig.from_raw(monitor),
            workflow_config=view,
        )
        if view is not None:
            # Cross-section checks (e.g. replay_running_concurrency <= total_count)
            # run after the dataclass is built, so kernel_config is available.
            view.validate(config)
        return config
