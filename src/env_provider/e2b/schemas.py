"""E2B backend state (SDK handle + backend lifecycle).

The kernel works on host-agnostic :class:`bench_core.schemas.BenchSandbox` (built
fresh via ``BenchSandbox.from_instance`` from the lean contract
:class:`env_provider.SandboxInstance`); the e2b manager + adapter keep the SDK
handle here and translate out of it. Workflow task metrics (browser/coding/
document), step-order constants, and the batch-scheduler types live in
:mod:`bench_core.schemas` (the kernel) -- not duplicated here.

The backend status enum + creation metrics are shared with docker via
:mod:`env_provider._base` (they were byte-identical); this module re-exports
them under the e2b names so the manager/adapter imports unchanged. Only
:class:`SandboxState` (the e2b handle + lifecycle flags) is defined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from env_provider._base import (
    BackendCreationMetrics as CreationMetrics,
)
from env_provider._base import (
    BackendSandboxStatus as SandboxStatus,
)

__all__ = ["CreationMetrics", "SandboxState", "SandboxStatus"]


@dataclass
class SandboxState:
    """Per-sandbox E2B backend state.

    Carries the SDK handle (``sandbox_obj``) + backend lifecycle flags + creation
    timing. Workflow task metrics (browser/coding/document), per-step timing, and
    round-robin tab state live on the kernel's :class:`bench_core.schemas.BenchSandbox`,
    not here: the adapter translates a ``SandboxState`` into a handle-free
    :class:`env_provider.SandboxInstance`, then the kernel rebuilds its own
    metrics state via ``BenchSandbox.from_instance``. The kernel never sees an
    e2b ``SandboxState``.
    """

    sandbox_id: int  # Sequence number (1, 2, 3...)
    sandbox_obj: object | None = None  # E2B Sandbox object reference (handle)
    batch_id: int = -1  # Batch ID

    workflow_type: str = "browser"  # Selects the ready-check strategy (set by the manager from kernel_config)

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    is_alive: bool = True  # Sandbox alive status
    stopped_by_cleanup: bool = False  # Killed by normal benchmark cleanup
    warmup_done: bool = False  # Warmup phase completed flag (read by the adapter)
