"""CubeSandbox backend state (SDK handle + backend lifecycle).

The kernel works on host-agnostic :class:`bench_core.schemas.BenchSandbox` (built
fresh via ``BenchSandbox.from_instance`` from the lean contract
:class:`env_provider.SandboxInstance`); the cube manager + adapter keep the SDK
handle here and translate out of it. Workflow task metrics (browser/coding/
document), step-order constants, and the batch-scheduler types live in
:mod:`bench_core.schemas` (the kernel) -- not duplicated here.

The backend status enum + creation metrics are shared with e2b/docker via
:mod:`env_provider._base` (byte-identical); this module re-exports them under
the cube names so the manager/adapter imports unchanged. Only
:class:`CubeSandboxState` (the cube handle + lifecycle flags) is defined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from env_provider._base import (
    BackendCreationMetrics as CreationMetrics,
)
from env_provider._base import (
    BackendSandboxStatus as SandboxStatus,
)

__all__ = ["CreationMetrics", "CubeSandboxState", "SandboxStatus"]


@dataclass
class CubeSandboxState:
    """Per-sandbox CubeSandbox backend state.

    Carries the SDK handle (``cube_sandbox``) + backend lifecycle flags +
    creation timing. Workflow task metrics (browser/coding/document), per-step
    timing, and round-robin tab state live on the kernel's
    :class:`bench_core.schemas.BenchSandbox`, not here: the adapter translates a
    ``CubeSandboxState`` into a handle-free :class:`env_provider.SandboxInstance`,
    then the kernel rebuilds its own metrics state via
    ``BenchSandbox.from_instance``. The kernel never sees a
    ``CubeSandboxState``.

    ``sandbox_id`` is the 1-based sequence number (the bench-core index), NOT
    the CubeSandbox sandbox ID; the real cube ID lives on the handle
    (``cube_sandbox.sandbox_id``), mirroring e2b's state/sandbox_obj split.
    """

    sandbox_id: int  # Sequence number (1, 2, 3...) -- the bench-core index
    cube_sandbox: object | None = None  # CubeSandbox Sandbox object (handle)
    batch_id: int = -1  # Batch ID

    workflow_type: str = "browser"  # Ready-check strategy (set from kernel_config)

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    is_alive: bool = True  # Sandbox alive status
    stopped_by_cleanup: bool = False  # Killed by normal benchmark cleanup
    warmup_done: bool = False  # Warmup phase completed flag (read by the adapter)
