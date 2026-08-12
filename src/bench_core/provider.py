"""Host-agnostic environment-provider contract + shared state types.

The benchmark kernel (``bench_core``) depends only on :class:`EnvironmentProvider`
and :class:`SandboxInstance`; provider implementations (e2b, docker, kata, ...)
supply the concrete sandbox backend. The kernel never imports provider-specific
types.

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
from typing import Mapping


@dataclass
class CreationMetrics:
    """Per-sandbox creation timing.

    Unifies the byte-parallel ``CreationMetrics`` definitions that today live
    independently in ``e2b_bench/schemas.py`` and ``docker_bench/schemas.py``.
    """

    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0
    success: bool = False
    error: str = ""


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
    keeps any SDK handle internally; the kernel touches only the fields below.
    Provider state classes may subclass this and add provider-specific fields --
    the kernel reads the base view, the provider reads the subclass.
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

    def detect_from_ids(self, ids_file: str | None) -> Mapping[int, SandboxInstance] | None:
        """Load sandboxes from a persisted-IDs file.

        Return ``None`` when the provider does not support ID persistence
        (e2b does, docker does not). Default: unsupported.
        """
        return None

    def save_ids(self, instances: Mapping[int, SandboxInstance], ids_file: str) -> None:
        """Persist sandbox IDs for a later :meth:`detect_from_ids`. Default no-op."""
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
