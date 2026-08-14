"""Docker backend state (SDK handle + backend lifecycle).

The kernel works on host-agnostic :class:`bench_core.schemas.BenchSandbox` (built
fresh via ``BenchSandbox.from_instance`` from the lean contract
:class:`env_provider.SandboxInstance`); the docker manager + adapter keep the
container handle here and translate out of it. Workflow task metrics live in
:mod:`bench_core.schemas` (the kernel) -- not duplicated here.

The backend status enum + creation metrics are shared with e2b via
:mod:`env_provider._base` (they were byte-identical); this module re-exports
them under the docker names so the manager/adapter imports unchanged. Only
:class:`ContainerState` (the docker handle + lifecycle flags) is defined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from env_provider._base import (
    BackendCreationMetrics as CreationMetrics,
    BackendSandboxStatus as ContainerStatus,
)

__all__ = ["ContainerState", "ContainerStatus", "CreationMetrics"]


@dataclass
class ContainerState:
    """Per-container docker backend state.

    Carries the container handle (``docker_container``) + backend lifecycle flags
    + creation timing. Workflow task metrics and per-step timing live on the
    kernel's :class:`bench_core.schemas.BenchSandbox`, not here: the adapter
    translates a ``ContainerState`` into a handle-free
    :class:`env_provider.SandboxInstance`, then the kernel rebuilds its own
    metrics state via ``BenchSandbox.from_instance``. The kernel never sees a
    docker ``ContainerState``.
    """

    container_id: int  # Sequence number (1, 2, 3...)
    container_name: str = ""  # Docker container name
    docker_container: object | None = None  # Docker container object reference (handle)
    batch_id: int = -1  # Batch ID

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    is_alive: bool = True  # Container alive status
    stopped_by_cleanup: bool = False  # Removed by normal benchmark cleanup (set by the base)
    browser_started: bool = False  # OpenClaw browser backend started flag (read by the adapter as warmup_done)
