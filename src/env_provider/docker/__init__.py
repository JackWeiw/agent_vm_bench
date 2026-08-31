"""Docker :class:`EnvironmentProvider` adapter.

Wraps :class:`env_provider.docker.manager.SandboxManager` behind the
kernel's :class:`env_provider.EnvironmentProvider` contract. The manager
owns the Docker SDK handles (``ContainerState.docker_container``); the adapter
translates those into host-agnostic :class:`SandboxInstance` objects and routes
``exec`` calls back through the manager's handle table -- the kernel never sees
an SDK type.

This is the only docker-specific code the kernel ever loads. It is
lazy-imported by ``bench_core.bench._build_provider`` (the
``python -m bench_core --provider docker`` smoke path), so ``bench_core``
itself never depends on the Docker SDK -- the layering rule (kernel must not
import provider packages) holds.

Scope note: this adapter exposes the contract; ``docker_bench.bench``'s own
``run_benchmark`` (the 4-step ``agent-browser`` workflow + QPS report) is
intentionally left intact for now. Routing docker's single-test entry through
the host-agnostic kernel is a deliberate, separately-reviewed follow-up -- the
adapter is the prerequisite, so that migration is a wiring change there, not an
adapter change here.
"""
from __future__ import annotations

import logging
import shlex
import threading
from typing import Any, Mapping

from bench_core.config import KernelConfig
from env_provider import (
    CommandResult,
    CreationMetrics,
    EnvironmentProvider,
    SandboxInstance,
    SandboxStatus,
)

from .config import Config
from .manager import SandboxManager
from .schemas import ContainerState
from .schemas import ContainerStatus as DockerStatus

logger = logging.getLogger(__name__)

# docker ContainerStatus -> kernel SandboxStatus. docker's PORT_READY /
# PORT_FAILED are workflow-neutralised to READY / READY_FAILED, mirroring the
# e2b adapter: the kernel report renders the workflow-specific label ("port"),
# so the status name itself stays host-agnostic. Keyed by the enum's value
# string (not by member identity) so the lookup stays correct when the
# ContainerStatus enum class is re-bound across the provider/state boundary.
_STATUS_MAP: dict[str, SandboxStatus] = {
    DockerStatus.PENDING.value: SandboxStatus.PENDING,
    DockerStatus.CREATING.value: SandboxStatus.CREATING,
    DockerStatus.CREATED.value: SandboxStatus.CREATED,
    DockerStatus.PORT_READY.value: SandboxStatus.READY,
    DockerStatus.ACTIVE.value: SandboxStatus.ACTIVE,
    DockerStatus.FAILED.value: SandboxStatus.FAILED,
    DockerStatus.PORT_FAILED.value: SandboxStatus.READY_FAILED,
    DockerStatus.OFFLINE.value: SandboxStatus.OFFLINE,
    DockerStatus.KILLED.value: SandboxStatus.KILLED,
}


def _to_text(stream: bytes | str | None) -> str:
    """Normalise a docker SDK exec stream (bytes/str/None) to text."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="ignore")
    return stream


class DockerProvider(EnvironmentProvider):
    """EnvironmentProvider backed by a Docker :class:`SandboxManager`.

    The adapter holds the docker :class:`Config` plus the stop event. The
    :class:`SandboxManager` (and its Docker SDK client) is constructed lazily
    on first use -- so the kernel can run host-side preflight and the header
    print before any SDK client is built. The kernel never sees the manager or
    SDK types directly.
    """

    name = "docker"

    def __init__(self, kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> None:
        self._kernel_config = kernel_config
        self._config = config
        self._stop_event = stop_event
        self._manager: SandboxManager | None = None

    @property
    def manager(self) -> SandboxManager:
        """The wrapped SandboxManager, constructed on first access.

        Lazy so the kernel's prepare_env / header-print can run before any SDK
        client is built. Tests inject a mock by setting ``_manager`` directly.
        """
        if self._manager is None:
            self._manager = SandboxManager(self._kernel_config, self._config, self._stop_event)
        return self._manager

    # ------------------------------------------------------------------ lifecycle
    def create_all(self, *, templates: dict[int, str | None] | None = None) -> Mapping[int, SandboxInstance]:
        return self._translate(self.manager.create_all(templates=templates))

    def detect_existing(self) -> Mapping[int, SandboxInstance]:
        return self._translate(self.manager.detect_existing())

    def check_alive(self, inst: SandboxInstance) -> bool:
        state = self.manager.container_states.get(inst.index)
        if state is None:
            return False
        return self.manager.check_alive(state)

    def cleanup_all(self) -> None:
        # If the manager was never built (e.g. preflight failed before create),
        # there is nothing to tear down.
        if self._manager is None:
            return
        self._manager.cleanup_all()

    def cleanup_existing(self) -> int:
        # Delegate to the manager's list->attach->remove path, which skips the
        # readiness probe (a service-down container must not stall teardown on
        # the 300s port wait).
        return self.manager.cleanup_existing()

    # ------------------------------------------------------------------ setup hooks
    def prepare_env(self) -> None:
        # Docker needs no host-side environment setup (no SDK env vars); the
        # daemon socket is resolved by the SDK from the standard env.
        return None

    def prepare(self, inst: SandboxInstance) -> None:
        """Start the browser backend + clear cache before warmup.

        Only meaningful once docker routes through the kernel (a follow-up);
        the kernel does not call this yet. Implemented for contract compliance
        so the future migration is a wiring change, not an adapter change.
        """
        state = self.manager.container_states.get(inst.index)
        if state is None:
            return
        self.manager.clear_browser_cache(state)
        self.manager.start_browser_backend(state)

    # ------------------------------------------------------------------ command exec
    def exec(
        self,
        inst: SandboxInstance,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        state = self.manager.container_states.get(inst.index)
        if state is None or state.docker_container is None:
            raise RuntimeError(f"No Docker handle for sandbox index {inst.index}")
        cmd = self._wrap_command(command, cwd=cwd, env=env)
        if timeout is None:
            return self._exec(state, cmd)
        # docker's exec_run is a blocking call with no native timeout; run it in
        # a worker thread and raise on timeout so the kernel's runners can
        # classify the failure (they catch Exception and match "timed out").
        return self._exec_with_timeout(state, cmd, timeout)

    # ------------------------------------------------------------------ translation
    def _translate(self, states: Mapping[int, ContainerState]) -> dict[int, SandboxInstance]:
        """Translate ``{index: ContainerState}`` -> ``{index: SandboxInstance}``."""
        return {index: self._to_instance(state) for index, state in states.items()}

    def _to_instance(self, state: ContainerState) -> SandboxInstance:
        cm = state.creation_metrics
        status = _STATUS_MAP.get(cm.status.value, SandboxStatus.FAILED)
        # docker's stable identifier is the container name; the numeric
        # container_id is the kernel index. NUMA binding is a host-level
        # concern docker does not apply.
        return SandboxInstance(
            id=state.container_name,
            index=state.container_id,
            numa_node=None,
            ready=(status == SandboxStatus.READY),
            is_alive=state.is_alive,
            warmup_done=state.browser_started,
            creation_metrics=CreationMetrics(
                submit_time=cm.submit_time,
                ready_time=cm.port_ready_time,
                create_elapsed=cm.create_elapsed,
                ready_check_elapsed=cm.port_wait_elapsed,
                total_elapsed=cm.total_elapsed,
                status=status,
                error=cm.error_msg,
                ready_check_error=cm.port_check_error,
            ),
        )

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _wrap_command(command: str, *, cwd: str | None, env: dict[str, str] | None) -> str:
        """Wrap a command for cwd/env (docker exec_run has no native kwargs).

        ``env`` prefixes the command itself (so the vars apply to it, not to a
        preceding ``cd``); ``cwd`` runs ``cd`` first so both apply to the
        command. No wrapping when neither is set.
        """
        if cwd is None and not env:
            return command
        env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
        full = f"{env_prefix} {command}".strip()
        if cwd is not None:
            return f"cd {shlex.quote(cwd)} && {full}"
        return full

    def _exec(self, state: ContainerState, command: str) -> CommandResult:
        # demux=True splits stdout/stdr so the kernel's runners read the
        # snapshot body from stdout and errors from stderr (docker's own runner
        # uses combined output; the kernel expects them separated).
        result = state.docker_container.exec_run(command, user="root", demux=True)
        output = result.output
        if output is None:
            stdout, stderr = "", ""
        else:
            stdout, stderr = output  # type: ignore[assignment]
        return CommandResult(
            exit_code=result.exit_code,
            stdout=_to_text(stdout),
            stderr=_to_text(stderr),
        )

    def _exec_with_timeout(self, state: ContainerState, command: str, timeout: int) -> CommandResult:
        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["result"] = self._exec(state, command)
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
                box["error"] = exc

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            # The container-side process may keep running (docker exec_run is
            # not interruptible from the host); the benchmark's
            # consecutive-failure handling marks such a sandbox offline anyway.
            raise TimeoutError(f"docker exec timed out after {timeout}s: {command[:80]}")
        if "error" in box:
            raise box["error"]
        return box["result"]


def from_config(kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> DockerProvider:
    """Build a :class:`DockerProvider` from a KernelConfig + docker Config.

    The SandboxManager is constructed lazily on first use, so this is cheap
    and does not talk to the Docker daemon yet.
    """
    return DockerProvider(kernel_config, config, stop_event)


def build_provider(config: KernelConfig, raw_config: dict) -> DockerProvider:
    """Construct a :class:`DockerProvider` from a raw YAML dict (kernel smoke path).

    ``config`` is the already-built :class:`KernelConfig` (shared stress params
    from :meth:`KernelConfig.from_raw`); the docker backend Config is rebuilt
    here from the ``docker:`` block of the same raw dict. Both are passed to the
    provider: the kernel drives ``config``, the manager reads backend knobs from
    the docker Config.
    """
    stop_event = threading.Event()
    docker_config = Config.from_raw(raw_config) if raw_config else Config()
    return from_config(config, docker_config, stop_event)
