"""Host-agnostic environment-provider contract + shared state types.

The contract (this ``__init__``) is the shared seam the benchmark kernel
(``bench_core``) and every provider implementation depend on. It stays pure --
no SDK imports -- so ``from env_provider import EnvironmentProvider`` never
pulls in the e2b / docker SDKs.

Provider implementations live as opt-in submodules alongside the contract:
``env_provider.e2b``, ``env_provider.docker``, ``env_provider.fake``. Each is a
leaf imported only by ``bench_core.bench._build_provider`` (and the e2b
delegate) when that provider is selected, so the kernel never loads a backend
it does not use. Adding a provider (kata, agentenv, ...) is one new submodule;
the contract and the kernel are untouched.

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
from typing import Mapping, Protocol, runtime_checkable


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


@runtime_checkable
class LifecycleCapable(Protocol):
    """Provider that can pause (memory-snapshot) and resume (restore) a sandbox.

    Replay's lifecycle mode calls these around each step's exec to measure
    snapshot-restore overhead. Providers that don't implement it stay
    exec-only; ``replay_mode: lifecycle`` on a non-capable provider fails fast.
    """

    def pause(self, inst: SandboxInstance) -> None:
        ...

    def resume(self, inst: SandboxInstance) -> None:
        ...


@runtime_checkable
class EphemeralCapable(Protocol):
    """Provider that can create/destroy a single sandbox on demand (trajectory mode).

    Replay's trajectory mode creates one sandbox per trajectory and kills it at
    the trajectory's end, placing create/kill under running-slot + QPS admission.
    Providers that don't implement it stay on exec_only/lifecycle (persistent
    pool); ``replay_mode: trajectory`` on a non-capable provider fails fast in
    ``run_benchmark``. ``metadata`` carries runner/trajectory labels for operator
    visibility only -- it is NOT a create-idempotency key (see the Phase 1 spec,
    G3 deferred).
    """

    def create_one(self, index: int, *, metadata: dict[str, str] | None = None) -> SandboxInstance:
        ...

    def kill_one(self, inst: SandboxInstance) -> None:
        ...


@runtime_checkable
class SnapshotSizeCapable(Protocol):
    """Provider that can stat overlaybd snapshot disk usage per paused sandbox.

    Replay's lifecycle mode probes this right after :meth:`pause`; providers
    that don't implement it skip snapshot-size collection (the snapshot sheet
    stays header-only). Returns a dict of size fields or ``None`` when the
    snapshot dir is absent/unreadable.
    """

    def snapshot_sizes(self, inst: SandboxInstance) -> dict | None:
        ...


class EnvironmentProvider(ABC):
    """Contract a sandbox backend implements so the benchmark kernel can drive it.

    Subclasses set ``name`` (a stable identifier such as ``"e2b"`` / ``"docker"``)
    and implement the abstract lifecycle + ``exec`` methods. The optional hooks
    (``prepare_env``, ``prepare``, ``detect_from_ids``, ``save_ids``) default to
    no-ops; a provider overrides only what it needs.
    """

    name: str = "base"
    default_replay_mode: str = "exec_only"  # replay workflow default; aenv overrides to "lifecycle"

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

    def cleanup_existing(self) -> int:
        """Tear down all *currently running* sandboxes (standalone ``--cleanup``).

        Lists live sandboxes fresh and kills them -- unlike
        :meth:`cleanup_all`, which kills only sandboxes tracked in this
        provider's current run (a prior ``--create-only`` / ``--detect`` run
        left none tracked here). Default: :meth:`detect_existing` then
        :meth:`cleanup_all` (the detect attaches handles the kill needs). A
        provider whose backend can list-and-kill without the readiness probe
        overrides this to skip it (a dead sandbox or a service-down browser
        container must not stall a teardown on the port/command wait). Returns
        the number torn down.
        """
        instances = self.detect_existing()
        self.cleanup_all()
        return len(instances)

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
    "LifecycleCapable",
    "EphemeralCapable",
    "SnapshotSizeCapable",
]
