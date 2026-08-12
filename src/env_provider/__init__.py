"""Host-agnostic environment-provider contract + shared state types.

This package is the shared seam that the benchmark kernel (``bench_core``) and
every provider implementation (e2b, docker, future kata / agentenv) depend on
-- neither the kernel nor any provider owns it. The kernel holds only
:class:`SandboxInstance` and drives :class:`EnvironmentProvider`; a provider
keeps its SDK handles internally and adapts them to this contract.

Design notes
------------
* ``exec()`` is the sole command primitive. File writes (e.g. the coding verify
  script) go through ``exec`` as a heredoc/``cat`` -- no separate upload method,
  so adding a provider is implementing one method.
* Browser automation rides HTTP on top of a backend the provider starts via
  :meth:`prepare_env` / :meth:`prepare`; the kernel never speaks the SDK.
* The provider keeps any SDK handle internally (``id -> handle``); the kernel
  holds only the host-agnostic :class:`SandboxInstance`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class SandboxStatus(Enum):
    """Lifecycle state of a sandbox, set on ``CreationMetrics.status``.

    The names are workflow-neutral: ``READY`` covers both "browser ports open"
    and "command responsive" (coding/document); the report layer renders the
    workflow-specific label. ``READY_FAILED`` is the dual of ``READY`` for the
    readiness check (port probe / command / validate).
    """

    PENDING = "pending"  # waiting for creation
    CREATING = "creating"  # create call in flight
    CREATED = "created"  # create API succeeded, awaiting readiness
    READY = "ready"  # readiness check passed, can run tasks
    ACTIVE = "active"  # running tasks
    FAILED = "failed"  # creation failed
    READY_FAILED = "ready_failed"  # readiness check failed
    OFFLINE = "offline"  # went down at runtime
    KILLED = "killed"  # torn down by cleanup


@dataclass
class CreationMetrics:
    """Per-sandbox creation timing + status (the perf bench's core measurement).

    Unifies the byte-parallel ``CreationMetrics`` in ``e2b_bench/schemas.py`` and
    ``docker_bench/schemas.py`` under workflow-neutral names. ``ready_check_*``
    generalises e2b's "port wait" and coding's "command/validate wait"; every
    provider has a create step and a readiness step, so this shape is host-agnostic.
    """

    submit_time: float = 0.0  # wall-clock when creation was submitted
    ready_time: float = 0.0  # wall-clock when readiness was achieved
    create_elapsed: float = 0.0  # create API call time, excluding readiness check
    ready_check_elapsed: float = 0.0  # time spent waiting for readiness (port probe / command / validate)
    total_elapsed: float = 0.0  # submit -> ready = create_elapsed + ready_check_elapsed
    status: SandboxStatus = SandboxStatus.PENDING
    error: str = ""  # creation error message
    ready_check_error: str = ""  # readiness-check error message


@dataclass
class CommandResult:
    """Unified return shape for :meth:`EnvironmentProvider.exec`.

    Providers adapt their SDK's command-execution result to this shape
    (e2b ``sbx.commands.run`` -> here; docker ``container.exec_run`` -> here).
    """

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class SandboxInstance:
    """Host-agnostic sandbox/instance state.

    Unifies ``SandboxState`` (e2b) and ``ContainerState`` (docker). The provider
    keeps any SDK handle internally (a ``{index: handle}`` table); the kernel
    touches only the fields below, so adding a provider means holding your own
    handle table -- not subclassing this type.
    """

    id: str
    index: int
    numa_node: int | None = None
    ready: bool = False
    is_alive: bool = True
    warmup_done: bool = False
    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)


class EnvironmentProvider(ABC):
    """Contract a sandbox backend implements so the benchmark kernel can drive it.

    Subclasses set ``name`` (a stable identifier such as ``"e2b"`` / ``"docker"``)
    and implement the abstract lifecycle + ``exec`` methods. The optional hooks
    (``prepare_env``, ``prepare``, ``detect_from_ids``, ``save_ids``) default to
    no-ops; a provider overrides only what it needs.
    """

    name: str = "base"

    # ------------------------------------------------------------------ lifecycle
    @abstractmethod
    def create_all(self) -> Mapping[int, SandboxInstance]:
        """Create all sandboxes and return ``{index: instance}``.

        Ready-checks (port probing, command readiness) happen *inside* this
        method before instances are returned, so the kernel only ever sees
        ready instances.
        """

    @abstractmethod
    def detect_existing(self) -> Mapping[int, SandboxInstance]:
        """Detect already-running sandboxes; return ``{index: instance}``."""

    def detect_from_ids(self, ids_file: str | None = None) -> Mapping[int, SandboxInstance] | None:
        """Load sandboxes from a persisted-IDs file.

        When ``ids_file`` is None the provider uses its own configured path
        (e2b holds one; docker has none). Return ``None`` when the provider
        does not support ID persistence or has no path/file -- the caller then
        falls back to :meth:`detect_existing`. Default: unsupported.
        """
        return None

    def save_ids(self, instances: Mapping[int, SandboxInstance], ids_file: str | None = None) -> None:
        """Persist sandbox IDs for a later :meth:`detect_from_ids`.

        When ``ids_file`` is None the provider persists to its own configured
        path; a provider with no path is a no-op. Default no-op.
        """
        return None

    @abstractmethod
    def check_alive(self, inst: SandboxInstance) -> bool:
        """Return whether the sandbox backing ``inst`` is still alive."""

    @abstractmethod
    def cleanup_all(self) -> None:
        """Tear down all sandboxes (unifies ``kill_all`` / ``remove_all``)."""

    # ------------------------------------------------------------------ setup hooks
    def prepare_env(self) -> None:
        """Provider-level setup, called once before create/detect.

        e2b sets ``E2B_*`` SDK environment variables here; docker needs nothing.
        Default: no-op.
        """
        return None

    def prepare(self, inst: SandboxInstance) -> None:
        """Per-instance prep, called before warmup.

        docker starts the agent-browser backend + clears cache here; e2b needs
        nothing (the backend is baked into the template). Default: no-op.
        """
        return None

    # ------------------------------------------------------------------ command exec
    @abstractmethod
    def exec(
        self,
        inst: SandboxInstance,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run ``command`` inside the sandbox backing ``inst``.

        Returns a unified :class:`CommandResult`. Timeout semantics are
        provider-native (e2b supports it directly; docker uses a watchdog);
        the kernel is unaware of the difference.
        """


__all__ = [
    "SandboxStatus",
    "CreationMetrics",
    "CommandResult",
    "SandboxInstance",
    "EnvironmentProvider",
]
