"""E2B :class:`EnvironmentProvider` adapter.

Wraps :class:`e2b_bench.sandbox_manager.SandboxManager` behind the kernel's
:class:`bench_core.provider.EnvironmentProvider` contract. The manager owns the
E2B SDK handles (``SandboxState.sandbox_obj``); the adapter translates those
into host-agnostic :class:`SandboxInstance` objects and routes ``exec`` calls
back through the manager's handle table -- the kernel never sees an SDK type.

This is the only e2b-specific code the kernel ever loads. It is lazy-imported
by ``bench_core.bench._build_provider`` (and by ``e2b_bench.bench.main``), so
``bench_core`` itself never depends on the e2b SDK -- the layering rule
(kernel must not import provider packages) holds.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import fields
from typing import Any, Mapping

from bench_core.config import KernelConfig
from bench_core.provider import (
    CommandResult,
    CreationMetrics,
    EnvironmentProvider,
    SandboxInstance,
    SandboxStatus,
)

from .config import Config, numa_node_for_index
from .sandbox_manager import SandboxManager
from .schemas import SandboxState
from .schemas import SandboxStatus as E2BSandboxStatus

logger = logging.getLogger(__name__)

# e2b SandboxStatus -> kernel SandboxStatus. e2b's PORT_READY / PORT_FAILED are
# workflow-neutralised to READY / READY_FAILED: the kernel report renders the
# workflow-specific label ("port" for browser, "command" for coding), so the
# status name itself stays host-agnostic.
_STATUS_MAP: dict[E2BSandboxStatus, SandboxStatus] = {
    E2BSandboxStatus.PENDING: SandboxStatus.PENDING,
    E2BSandboxStatus.CREATING: SandboxStatus.CREATING,
    E2BSandboxStatus.CREATED: SandboxStatus.CREATED,
    E2BSandboxStatus.PORT_READY: SandboxStatus.READY,
    E2BSandboxStatus.ACTIVE: SandboxStatus.ACTIVE,
    E2BSandboxStatus.FAILED: SandboxStatus.FAILED,
    E2BSandboxStatus.PORT_FAILED: SandboxStatus.READY_FAILED,
    E2BSandboxStatus.OFFLINE: SandboxStatus.OFFLINE,
    E2BSandboxStatus.KILLED: SandboxStatus.KILLED,
}


class E2BProvider(EnvironmentProvider):
    """EnvironmentProvider backed by an E2B :class:`SandboxManager`.

    The adapter holds the manager (which owns the SDK handles) and the e2b
    :class:`Config` (for env-var setup and the IDs-file path). The kernel drives
    it through the abstract contract; it never touches the manager or SDK
    types directly.
    """

    name = "e2b"

    def __init__(self, config: Config, manager: SandboxManager) -> None:
        self._config = config
        self._manager = manager

    # ------------------------------------------------------------------ lifecycle
    def create_all(self) -> Mapping[int, SandboxInstance]:
        return self._translate(self._manager.create_all())

    def detect_existing(self) -> Mapping[int, SandboxInstance]:
        return self._translate(self._manager.detect_existing())

    def detect_from_ids(self, ids_file: str | None = None) -> Mapping[int, SandboxInstance] | None:
        path = ids_file or self._config.sandbox_ids_file
        if not path:
            return None
        return self._translate(self._manager.detect_from_file(path))

    def check_alive(self, inst: SandboxInstance) -> bool:
        state = self._manager.sandbox_states.get(inst.index)
        if state is None:
            return False
        return self._manager.check_alive(state)

    def cleanup_all(self) -> None:
        self._manager.kill_all()

    # ------------------------------------------------------------------ setup hooks
    def prepare_env(self) -> None:
        self._config.setup_e2b_env()

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
        state = self._manager.sandbox_states.get(inst.index)
        if state is None or state.sandbox_obj is None:
            raise RuntimeError(f"No E2B handle for sandbox index {inst.index}")
        # Only forward kwargs the e2b SDK accepts; user/cwd/env are passed
        # through when the kernel sets them (it currently sets only timeout).
        kwargs: dict[str, Any] = {"user": "root"}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if cwd is not None:
            kwargs["cwd"] = cwd
        if env is not None:
            kwargs["env"] = env
        result = state.sandbox_obj.commands.run(command, **kwargs)
        return CommandResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    # ------------------------------------------------------------------ id persistence
    def save_ids(self, instances: Mapping[int, SandboxInstance], ids_file: str | None = None) -> None:
        path = ids_file or self._config.sandbox_ids_file
        if not path:
            return
        ids = [inst.id for inst in instances.values() if inst.ready and inst.id]
        if not ids:
            logger.warning(f"No ready sandbox IDs to save to {path}")
            return
        # Overwrite: each kernel run owns the file (wave-append is a
        # batch-scheduler concern, deferred to a follow-on phase).
        with open(path, "w") as handle:
            for sid in ids:
                handle.write(f"{sid}\n")
        logger.info(f"Saved {len(ids)} sandbox IDs to: {path}")

    # ------------------------------------------------------------------ translation
    def _translate(self, states: Mapping[int, SandboxState]) -> dict[int, SandboxInstance]:
        """Translate ``{index: SandboxState}`` -> ``{index: SandboxInstance}``."""
        return {index: self._to_instance(state) for index, state in states.items()}

    def _to_instance(self, state: SandboxState) -> SandboxInstance:
        cm = state.creation_metrics
        sbx_id = ""
        if state.sandbox_obj is not None:
            sbx_id = str(getattr(state.sandbox_obj, "sandbox_id", "") or "")
        status = _STATUS_MAP.get(cm.status, SandboxStatus.FAILED)
        # Mirror the manager's own NUMA convention (0-based index into the
        # numa_bind list); informational only -- the real binding happened at
        # creation time inside the manager.
        numa_node = numa_node_for_index(state.sandbox_id - 1, self._config.numa_bind)
        return SandboxInstance(
            id=sbx_id,
            index=state.sandbox_id,
            numa_node=numa_node,
            ready=(status == SandboxStatus.READY),
            is_alive=state.is_alive,
            warmup_done=state.warmup_done,
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


def from_config(config: Config, stop_event: threading.Event) -> E2BProvider:
    """Build an :class:`E2BProvider` from an already-constructed e2b Config.

    Real entry points (``e2b_bench.bench.main``) build the e2b Config with full
    CLI merge, then call this; the provider owns the manager it constructs.
    """
    manager = SandboxManager(config, stop_event)
    return E2BProvider(config, manager)


def build_provider(config: KernelConfig, raw_config: dict) -> E2BProvider:
    """Construct an :class:`E2BProvider` from a raw YAML dict (kernel smoke path).

    The host-agnostic ``python -m bench_core --provider e2b`` entry has no e2b
    Config object, so it reconstructs one from the raw YAML dict here. The real
    e2b entry builds its Config with CLI merge and calls :func:`from_config`.
    """
    del config  # The kernel's KernelConfig is rebuilt from the e2b Config downstream.
    stop_event = threading.Event()
    e2b_config = Config._from_dict(raw_config) if raw_config else Config()
    return from_config(e2b_config, stop_event)


def kernel_config_from_e2b(config: Config) -> KernelConfig:
    """Translate an e2b :class:`Config` into a host-agnostic :class:`KernelConfig`.

    Copies every field that exists on ``KernelConfig`` by name; e2b-specific
    fields (``template``, ``e2b_*`` env, ``smap_tool`` / ``vm_monitor`` blocks)
    stay on the e2b Config and are not carried over -- the kernel reads only
    the host-agnostic subset.
    """
    valid = {f.name for f in fields(KernelConfig)}
    kwargs = {k: getattr(config, k) for k in valid if hasattr(config, k)}
    return KernelConfig(**kwargs)
