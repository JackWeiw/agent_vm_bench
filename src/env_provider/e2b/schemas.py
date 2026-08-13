"""E2B backend state (SDK handle + backend lifecycle).

The kernel works on host-agnostic :class:`bench_core.schemas.BenchSandbox` (built
fresh via ``BenchSandbox.from_instance`` from the lean contract
:class:`env_provider.SandboxInstance`); the e2b manager + adapter keep the SDK
handle here and translate out of it. Workflow task metrics (browser/coding/
document), step-order constants, and the batch-scheduler types live in
:mod:`bench_core.schemas` (the kernel) -- not duplicated here.

This module carries only:
- :class:`SandboxStatus` -- e2b's backend status enum (PORT_READY / PORT_FAILED;
  the adapter's ``_STATUS_MAP`` translates to the contract's workflow-neutral
  ``env_provider.SandboxStatus``).
- :class:`CreationMetrics` -- e2b creation/ready timing (``port_ready_time``
  etc.; the adapter maps to the contract's ``CreationMetrics``).
- :class:`SandboxState` -- per-sandbox backend state: the SDK handle
  (``sandbox_obj``), backend lifecycle flags, and creation metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SandboxStatus(Enum):
    """Sandbox status enumeration"""

    PENDING = "pending"  # Waiting for creation
    CREATING = "creating"  # Creating in progress
    CREATED = "created"  # sandbox.create succeeded, waiting for ports
    PORT_READY = "port_ready"  # Ports ready, can execute tasks
    ACTIVE = "active"  # Active, executing tasks
    FAILED = "failed"  # Creation failed
    PORT_FAILED = "port_failed"  # Port check failed
    OFFLINE = "offline"  # Runtime offline
    KILLED = "killed"  # Killed


@dataclass
class CreationMetrics:
    """Sandbox creation performance metrics"""

    submit_time: float = 0.0  # Creation submit time
    create_ready_time: float = 0.0  # sandbox.create success time (excluding port wait)
    port_ready_time: float = 0.0  # Ports ready time
    create_elapsed: float = 0.0  # sandbox.create elapsed time (seconds)
    port_wait_elapsed: float = 0.0  # Port wait elapsed time (seconds)
    total_elapsed: float = 0.0  # Total elapsed = create_elapsed + port_wait_elapsed
    status: SandboxStatus = SandboxStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""  # Port check error message


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

    workflow_type: str = "browser"  # Selects the ready-check strategy (_check_ready)

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)

    is_alive: bool = True  # Sandbox alive status
    stopped_by_cleanup: bool = False  # Killed by normal benchmark cleanup
    warmup_done: bool = False  # Warmup phase completed flag (read by the adapter)
