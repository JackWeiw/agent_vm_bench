"""Host-agnostic kernel configuration (core fields only).

``KernelConfig`` holds the configuration the benchmark kernel needs to drive any
provider. Provider-specific config (e2b env vars, docker image, NUMA binding,
smap_tool, vm_monitor) lives in the provider's own config and never appears here
-- the kernel reads only the host-agnostic subset.

The coding/document fields are host-agnostic: any provider whose sandbox can
run the project/toolchain can execute them, so they belong to the kernel, not
to e2b. The per-language profile machinery (``CODING_LANGUAGE_PROFILES``) stays
in ``e2b_bench.config``; the kernel only carries the scalar fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class KernelConfig:
    """Core benchmark configuration shared across all providers."""

    # --- sandbox control ---
    total_count: int = 100
    detect_existing: bool = False
    create_only: bool = False

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
    workflow_type: str = "browser"  # "browser" | "coding" | "document"

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
    coding_verify_cmd: str = "npx tsx /tmp/bench_verify.mjs"
    coding_verify_timeout: int = 120
    coding_skip_verify: bool = False
    coding_verify_repeat: int = 3
    coding_interval_min: float = 2.0
    coding_interval_max: float = 10.0

    # --- document ---
    document_case_kind: str = "xlsx"
    document_operation_timeout: int = 900
    document_recalc_timeout: int = 600
    document_task_timeout: int = 1800
    document_interval_min: float = 3.0
    document_interval_max: float = 10.0

    # --- test run ---
    test_duration: int = 600
    stats_interval: int = 10

    # --- report ---
    output_dir: str = "results/kernel"
    filename_prefix: str = "bench"

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
        """Raise ``ValueError`` for invalid settings; call after construction."""
        if self.workflow_type not in {"browser", "coding", "document"}:
            raise ValueError(f"Unsupported workflow_type: {self.workflow_type}")
        if self.round_size <= 0:
            raise ValueError(f"round_size must be > 0, got {self.round_size}")
        if self.benchmark_mode not in {"fixed", "round_robin"}:
            raise ValueError(f"benchmark_mode must be fixed or round_robin, got {self.benchmark_mode}")
