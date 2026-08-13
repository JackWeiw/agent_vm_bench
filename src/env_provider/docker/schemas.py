"""Docker backend state (SDK handle + backend lifecycle).

The kernel works on host-agnostic :class:`bench_core.schemas.BenchSandbox` (built
fresh via ``BenchSandbox.from_instance`` from the lean contract
:class:`env_provider.SandboxInstance`); the docker manager + adapter keep the
container handle here and translate out of it. Workflow task metrics live in
:mod:`bench_core.schemas` (the kernel) -- not duplicated here.

This module carries only:
- :class:`ContainerStatus` -- docker's backend status enum (PORT_READY /
  PORT_FAILED; the adapter's ``_STATUS_MAP`` translates to the contract's
  workflow-neutral ``env_provider.SandboxStatus``).
- :class:`CreationMetrics` -- docker creation/ready timing (``port_ready_time``
  etc.; the adapter maps to the contract's ``CreationMetrics``).
- :class:`ContainerState` -- per-container backend state: the container handle
  (``docker_container``), backend lifecycle flags, and creation metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ContainerStatus(Enum):
    """Container status enumeration"""

    PENDING = "pending"  # Waiting for creation
    CREATING = "creating"  # Creating in progress
    CREATED = "created"  # Container created, waiting for ports
    PORT_READY = "port_ready"  # Ports ready, can execute tasks
    ACTIVE = "active"  # Active, executing tasks
    FAILED = "failed"  # Creation failed
    PORT_FAILED = "port_failed"  # Port check failed
    OFFLINE = "offline"  # Runtime offline
    KILLED = "killed"  # Killed/removed


@dataclass
class CreationMetrics:
    """Container creation performance metrics"""

    submit_time: float = 0.0  # Creation submit time
    create_ready_time: float = 0.0  # Container created time (excluding port wait)
    port_ready_time: float = 0.0  # Ports ready time
    create_elapsed: float = 0.0  # Container creation elapsed time (seconds)
    port_wait_elapsed: float = 0.0  # Port wait elapsed time (seconds)
    total_elapsed: float = 0.0  # Total elapsed = create_elapsed + port_wait_elapsed
    status: ContainerStatus = ContainerStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""  # Port check error message


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
    docker_container: object | None = None  # Docker container object reference
    batch_id: int = -1  # Batch ID

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    is_alive: bool = True  # Container alive status
    browser_started: bool = False  # OpenClaw browser backend started flag (read by the adapter as warmup_done)
