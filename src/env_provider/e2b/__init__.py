"""E2B :class:`EnvironmentProvider` adapter.

Wraps :class:`env_provider.e2b.manager.SandboxManager` behind the kernel's
:class:`env_provider.EnvironmentProvider` contract. The manager owns the
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
from typing import Any, Mapping

from bench_core.config import KernelConfig
from env_provider import (
    CommandResult,
    CreationMetrics,
    EnvironmentProvider,
    SandboxInstance,
    SandboxStatus,
)

from .config import Config, numa_node_for_index
from .manager import SandboxManager
from .schemas import SandboxState
from .schemas import SandboxStatus as E2BSandboxStatus

logger = logging.getLogger(__name__)

# e2b SandboxStatus -> kernel SandboxStatus. e2b's PORT_READY / PORT_FAILED are
# workflow-neutralised to READY / READY_FAILED: the kernel report renders the
# workflow-specific label ("port" for browser, "command" for coding), so the
# status name itself stays host-agnostic. Keyed by the enum's value string
# (not by member identity) so the lookup stays correct when the SandboxStatus
# enum class is re-bound across the provider/state module boundary.
_STATUS_MAP: dict[str, SandboxStatus] = {
    E2BSandboxStatus.PENDING.value: SandboxStatus.PENDING,
    E2BSandboxStatus.CREATING.value: SandboxStatus.CREATING,
    E2BSandboxStatus.CREATED.value: SandboxStatus.CREATED,
    E2BSandboxStatus.PORT_READY.value: SandboxStatus.READY,
    E2BSandboxStatus.ACTIVE.value: SandboxStatus.ACTIVE,
    E2BSandboxStatus.FAILED.value: SandboxStatus.FAILED,
    E2BSandboxStatus.PORT_FAILED.value: SandboxStatus.READY_FAILED,
    E2BSandboxStatus.OFFLINE.value: SandboxStatus.OFFLINE,
    E2BSandboxStatus.KILLED.value: SandboxStatus.KILLED,
}


class E2BProvider(EnvironmentProvider):
    """EnvironmentProvider backed by an E2B :class:`SandboxManager`.

    The adapter holds the kernel's :class:`KernelConfig` (shared stress params)
    plus the e2b :class:`Config` (for env-var setup, the IDs-file path, and
    NUMA) plus the stop event. The :class:`SandboxManager` is constructed
    lazily on first use -- so the kernel can run host-side preflight (e.g.
    document scene-recipe validation) and fail before any SDK client is built.
    The kernel never sees the manager or SDK types directly.
    """

    name = "e2b"

    def __init__(self, kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> None:
        self._kernel_config = kernel_config
        self._config = config
        self._stop_event = stop_event
        self._manager: SandboxManager | None = None

    @property
    def manager(self) -> SandboxManager:
        """The wrapped SandboxManager, constructed on first access.

        Lazy so the kernel's preflight / prepare_env / header-print can run (and
        fail) before any SDK client is built. Tests inject a mock by setting
        ``_manager`` directly.
        """
        if self._manager is None:
            self._manager = SandboxManager(self._kernel_config, self._config, self._stop_event)
        return self._manager

    # ------------------------------------------------------------------ lifecycle
    def create_all(self) -> Mapping[int, SandboxInstance]:
        return self._translate(self.manager.create_all())

    def detect_existing(self) -> Mapping[int, SandboxInstance]:
        return self._translate(self.manager.detect_existing())

    def detect_from_ids(self, ids_file: str | None = None) -> Mapping[int, SandboxInstance] | None:
        path = ids_file or self._config.sandbox_ids_file
        if not path:
            return None
        return self._translate(self.manager.detect_from_file(path))

    def check_alive(self, inst: SandboxInstance) -> bool:
        state = self.manager.sandbox_states.get(inst.index)
        if state is None:
            return False
        return self.manager.check_alive(state)

    def cleanup_all(self) -> None:
        # If the manager was never built (e.g. preflight failed before create),
        # there is nothing to tear down.
        if self._manager is None:
            return
        self._manager.cleanup_all()

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
        state = self.manager.sandbox_states.get(inst.index)
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
        status = _STATUS_MAP.get(cm.status.value, SandboxStatus.FAILED)
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


def from_config(kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> E2BProvider:
    """Build an :class:`E2BProvider` from a KernelConfig + e2b Config.

    The SandboxManager is constructed lazily on first use, so this is cheap and
    does not talk to the e2b SDK yet.
    """
    return E2BProvider(kernel_config, config, stop_event)


def build_provider(config: KernelConfig, raw_config: dict) -> E2BProvider:
    """Construct an :class:`E2BProvider` from a raw YAML dict (kernel smoke path).

    ``config`` is the already-built :class:`KernelConfig` (shared stress params
    from :meth:`KernelConfig.from_raw`); the e2b backend Config is rebuilt here
    from the ``e2b:`` block of the same raw dict. Both are passed to the
    provider: the kernel drives ``config``, the manager reads backend knobs
    from the e2b Config.
    """
    stop_event = threading.Event()
    e2b_config = Config.from_raw(raw_config) if raw_config else Config()
    return from_config(config, e2b_config, stop_event)
