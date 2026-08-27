"""Host-agnostic kernel configuration (core fields only).

``KernelConfig`` holds the configuration the benchmark kernel needs to drive any
provider. Provider-specific config (e2b env vars, docker image, NUMA binding,
smap_tool, vm_monitor) lives in the provider's own config and never appears here
-- the kernel reads only the host-agnostic subset.

The coding/document fields are host-agnostic: any provider whose sandbox can
run the project/toolchain can execute them, so they belong to the kernel, not
to e2b. The per-language profile machinery, replacement pairs, and verify
templates live in :mod:`bench_core.coding_payload`; the kernel carries the
scalar fields plus the resolved replacement-pair list
(``coding_source_files``), which :meth:`__post_init__` defaults from
``coding_language`` when not supplied explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from bench_core.coding_payload import CODING_LANGUAGE_DEFAULT_SOURCE_FILES, DEFAULT_CODING_SOURCE_FILES

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

    # --- browser ---
    browser_urls: list[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200
    browser_interval_min: float = 0.5
    browser_interval_max: float = 3.0

    # --- warmup ---
    warmup_urls: list[str] = field(default_factory=list)
    warmup_loops: int = 2
    warmup_delay: int = 10
    warmup_only: bool = False

    # --- coding (host-agnostic; provider supplies the sandbox that runs it) ---
    coding_project_dir: str = "/opt/coding-bench"
    coding_language: str = "ts"
    # Replacement pairs to cycle through (see bench_core.coding_payload). When
    # left None, __post_init__ resolves the language's default pair list, so
    # ``KernelConfig(coding_language="go")`` gets the hugo pairs automatically;
    # an explicit list is kept verbatim.
    coding_source_files: list[dict] | None = None
    coding_verify_cmd: str = "npx tsx /tmp/bench_verify.mjs"
    coding_verify_timeout: int = 120
    coding_skip_verify: bool = False
    coding_verify_repeat: int = 3
    coding_interval_min: float = 2.0
    coding_interval_max: float = 10.0

    # --- document ---
    document_case_kind: str = "xlsx"
    # Optional override for the trace-recipe JSON path. When None, the runner
    # auto-resolves it relative to the repo root (the dir holding pyproject.toml),
    # so the kernel works from any repo checkout without provider help.
    document_recipe_path: str | None = None
    document_operation_timeout: int = 900
    document_recalc_timeout: int = 600
    document_task_timeout: int = 1800
    document_interval_min: float = 3.0
    document_interval_max: float = 10.0

    # --- replay (host-agnostic; replays recorded agent trajectories via exec) ---
    replay_trajectory_dir: str = "trajectories"
    replay_trajectory_glob: str = "*.replay.json"
    replay_workdir: str = "/testbed"
    replay_env: dict[str, str] = field(default_factory=dict)
    replay_action_timeout: int = 300
    replay_delay_scale: float = 1.0  # 1.0=realtime think gaps; 0=no delay; 0.1=10x compressed
    replay_stop_on_error: bool = False
    replay_mode: str | None = None  # None = unset; resolved to provider default before validate. exec_only | lifecycle

    # --- test run ---
    test_duration: int = 600
    stats_interval: int = 10

    # --- report ---
    output_dir: str = "results/kernel"
    filename_prefix: str = "bench"

    def __post_init__(self) -> None:
        # Resolve replacement pairs from the language when not supplied. A copy
        # is made so a config never aliases the shared module-level list (callers
        # mutate coding_source_files when locating fallback files).
        if self.coding_source_files is None:
            default = CODING_LANGUAGE_DEFAULT_SOURCE_FILES.get(self.coding_language, DEFAULT_CODING_SOURCE_FILES)
            self.coding_source_files = [dict(p) for p in default]

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

    @property
    def document_seed_dir(self) -> str:
        """In-sandbox seed dir for the active document case kind."""
        return document_scene_layout(self.document_case_kind)["seed_dir"]

    @property
    def document_workspace_dir(self) -> str:
        """In-sandbox workspace dir for the active document case kind."""
        return document_scene_layout(self.document_case_kind)["workspace_dir"]

    def validate(self) -> None:
        """Raise ``ValueError`` for invalid settings; call after construction."""
        if self.workflow_type not in {"browser", "coding", "document", "replay"}:
            raise ValueError(f"Unsupported workflow_type: {self.workflow_type}")
        if self.round_size <= 0:
            raise ValueError(f"round_size must be > 0, got {self.round_size}")
        if self.benchmark_mode not in {"fixed", "round_robin"}:
            raise ValueError(f"benchmark_mode must be fixed or round_robin, got {self.benchmark_mode}")
        if self.workflow_type == "replay" and self.replay_mode not in (None, "exec_only", "lifecycle"):
            raise ValueError(f"replay_mode must be None, 'exec_only', or 'lifecycle', got {self.replay_mode!r}")

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
        coding = raw.get("coding") or {}
        document = raw.get("document") or {}
        replay = raw.get("replay") or {}

        # workflow_type: top-level wins, then the legacy workflow.type form.
        wf = raw.get("workflow_type")
        if wf is None:
            wf = (raw.get("workflow") or {}).get("type", "browser")

        return cls(
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
            # --- browser ---
            browser_urls=browser.get("urls", ["http://192.168.110.10:8080/Weibo.html"]),
            browser_timeout=browser.get("task_timeout", 200),
            browser_interval_min=browser.get("interval_min", 0.5),
            browser_interval_max=browser.get("interval_max", 3.0),
            # --- warmup (lives under browser) ---
            warmup_urls=browser.get("warmup_urls", []),
            warmup_loops=browser.get("warmup_loops", 2),
            warmup_delay=browser.get("warmup_delay", 10),
            warmup_only=browser.get("warmup_only", False),
            # --- coding ---
            coding_project_dir=coding.get("project_dir", "/opt/coding-bench"),
            coding_language=coding.get("language", "ts"),
            coding_source_files=coding.get("source_files"),
            coding_verify_cmd=coding.get("verify_cmd", "npx tsx /tmp/bench_verify.mjs"),
            coding_verify_timeout=coding.get("verify_timeout", 120),
            coding_skip_verify=coding.get("skip_verify", False),
            coding_verify_repeat=coding.get("verify_repeat", 3),
            coding_interval_min=coding.get("interval_min", 2.0),
            coding_interval_max=coding.get("interval_max", 10.0),
            # --- document ---
            document_case_kind=document.get("case_kind", "xlsx"),
            document_recipe_path=document.get("recipe_path"),
            document_operation_timeout=document.get("operation_timeout", 900),
            document_recalc_timeout=document.get("recalc_timeout", 600),
            document_task_timeout=document.get("task_timeout", 1800),
            document_interval_min=document.get("interval_min", 3.0),
            document_interval_max=document.get("interval_max", 10.0),
            # --- replay ---
            replay_trajectory_dir=replay.get("trajectory_dir", "trajectories"),
            replay_trajectory_glob=replay.get("trajectory_glob", "*.replay.json"),
            replay_workdir=replay.get("workdir", "/testbed"),
            replay_env=replay.get("env", {}),
            replay_action_timeout=replay.get("action_timeout", 300),
            replay_delay_scale=replay.get("delay_scale", 1.0),
            replay_stop_on_error=replay.get("stop_on_error", False),
            replay_mode=replay.get("mode"),
            # --- test run ---
            test_duration=test.get("duration", 600),
            stats_interval=test.get("stats_interval", 10),
            # --- report ---
            output_dir=report.get("output_dir", "results/kernel"),
            filename_prefix=report.get("filename_prefix", "bench"),
        )
