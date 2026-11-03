"""SandboxSched backend state (SDK handle + backend lifecycle).

Mirrors :mod:`env_provider.e2b.schemas`: the kernel works on host-agnostic
:class:`bench_core.schemas.BenchSandbox` (built via ``BenchSandbox.from_instance``
from the lean contract :class:`env_provider.SandboxInstance`); the manager +
adapter keep the OpenSandbox SDK handle (``SandboxSync``) here and translate
out of it. The backend status enum + creation metrics are shared with e2b /
docker via :mod:`env_provider._base` and re-exported under the sandboxsched
names so imports read naturally.
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
    """Per-sandbox SandboxSched backend state.

    Carries the SDK handle (``sandbox_obj``, an ``opensandbox.sync.sandbox.
    SandboxSync``) + backend lifecycle flags + creation timing. Workflow task
    metrics (browser/coding/document), per-step timing, and round-robin tab
    state live on the kernel's :class:`bench_core.schemas.BenchSandbox`, not
    here: the adapter translates a ``SandboxState`` into a handle-free
    :class:`env_provider.SandboxInstance`, then the kernel rebuilds its own
    metrics state via ``BenchSandbox.from_instance``. The kernel never sees a
    sandboxsched ``SandboxState``.
    """

    sandbox_id: int  # Sequence number (1, 2, 3...) -- kernel index, NOT the SDK id
    sandbox_obj: object | None = None  # OpenSandbox SandboxSync handle reference
    batch_id: int = -1  # Batch ID

    workflow_type: str = "browser"  # Selects the ready-check strategy (set by the manager from kernel_config)

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    is_alive: bool = True  # Sandbox alive status
    stopped_by_cleanup: bool = False  # Killed by normal benchmark cleanup
    torn_down: bool = False  # Server-side kill succeeded (cleanup skips; set by _kill_tracked / kill_one)
    warmup_done: bool = False  # Warmup phase completed flag (read by the adapter)
