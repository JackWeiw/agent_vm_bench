"""SandboxSched :class:`EnvironmentProvider` adapter.

Wraps :class:`env_provider.sandboxsched.manager.SandboxManager` behind the
kernel's :class:`env_provider.EnvironmentProvider` contract. The manager owns
the OpenSandbox SDK handles (``SandboxState.sandbox_obj`` -- an async
``opensandbox.Sandbox``); the adapter translates those into host-agnostic
:class:`SandboxInstance` objects and routes ``exec`` calls back through the
manager's handle table -- the kernel never sees an SDK type.

This is the only sandboxsched-specific code the kernel ever loads. It is
lazy-imported by ``bench_core.bench._build_provider``, so ``bench_core`` itself
never depends on the opensandbox SDK -- the layering rule (kernel must not
import provider packages) holds.

Lifecycle / ephemeral / snapshot support: the OpenSandbox E2B runtime (Firecracker
microVMs, the default) supports pause/resume/snapshot, so this provider
implements :class:`env_provider.LifecycleCapable` (``pause``/``resume`` via
the handle's ``pause`` / the SDK's ``Sandbox.resume``), :class:`env_provider.
EphemeralCapable`` (``create_one``/``kill_one`` for trajectory mode), and
:class:`env_provider.SnapshotSizeCapable`` (``snapshot_sizes`` via the SDK's
``create_snapshot``). The containerd runtime has no
pause/snapshot -- a config knob (``runtime``) selects the E2B runtime; lifecycle
calls on a containerd sandbox raise a clear error from the SDK.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

from bench_core.config import KernelConfig
from env_provider import (
    CommandResult,
    CreationMetrics,
    EnvironmentProvider,
    SandboxInstance,
    SandboxStatus,
)
from env_provider._base import BackendSandboxStatus

from .config import Config
from .manager import SandboxManager
from .schemas import SandboxState

logger = logging.getLogger(__name__)

# BackendSandboxStatus.value -> kernel SandboxStatus. The manager sets a
# BackendSandboxStatus on each state's creation_metrics (PENDING/CREATING/
# CREATED/PORT_READY/ACTIVE/FAILED/PORT_FAILED/OFFLINE/KILLED -- the shared
# backend-granular enum from env_provider._base, re-exported via schemas).
# Keyed by the enum's value string so the lookup stays correct when the enum
# class is re-bound across the provider/state module boundary (mirrors e2b's
# _STATUS_MAP). Unknown values map to FAILED defensively.
_STATUS_MAP: dict[str, SandboxStatus] = {
    BackendSandboxStatus.PENDING.value: SandboxStatus.PENDING,
    BackendSandboxStatus.CREATING.value: SandboxStatus.CREATING,
    BackendSandboxStatus.CREATED.value: SandboxStatus.CREATED,
    BackendSandboxStatus.PORT_READY.value: SandboxStatus.READY,
    BackendSandboxStatus.ACTIVE.value: SandboxStatus.ACTIVE,
    BackendSandboxStatus.FAILED.value: SandboxStatus.FAILED,
    BackendSandboxStatus.PORT_FAILED.value: SandboxStatus.READY_FAILED,
    BackendSandboxStatus.OFFLINE.value: SandboxStatus.OFFLINE,
    BackendSandboxStatus.KILLED.value: SandboxStatus.KILLED,
}


class SandboxSchedProvider(EnvironmentProvider):
    """EnvironmentProvider backed by a SandboxSched :class:`SandboxManager`.

    The adapter holds the kernel's :class:`KernelConfig` (shared stress params)
    plus the sandboxsched :class:`Config` (connection, sandbox_group, template,
    IDs-file path) plus the stop event. The :class:`SandboxManager` is
    constructed lazily on first use -- so the kernel can run host-side preflight
    and fail before any SDK client is built. The kernel never sees the manager
    or SDK types directly.
    """

    name = "sandboxsched"
    # OpenSandbox's E2B runtime is Firecracker microVMs (same VMM as e2b/aenv).
    vmm_type = "firecracker"
    # E2B runtime supports pause/resume/snapshot -> lifecycle is the natural
    # default (mirrors aenv). containerd has no pause -> use exec_only there.
    default_replay_mode = "lifecycle"

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
    def create_all(self, *, templates: dict[int, str | None] | None = None) -> Mapping[int, SandboxInstance]:
        return self._translate(self.manager.create_all(templates=templates))

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

    def cleanup_existing(self) -> int:
        return self.manager.cleanup_existing()

    # ------------------------------------------------------------------ setup hooks
    def prepare_env(self) -> None:
        self._config.setup_env()

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
            raise RuntimeError(f"No sandboxsched handle for sandbox index {inst.index}")
        # E2B-mode sandboxes serve the data plane through envd (Connect RPC),
        # not the SDK's execd REST contract -- commands.run 404s there by
        # design. Route through the manager's envd client (server proxy or
        # direct CNI access per use_server_proxy).
        exit_code, stdout, stderr = self.manager.envd_exec(
            state, command, timeout=timeout, cwd=cwd, env=env,
        )
        return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    # ------------------------------------------------------------------ id persistence
    def save_ids(self, instances: Mapping[int, SandboxInstance], ids_file: str | None = None) -> None:
        path = ids_file or self._config.sandbox_ids_file
        if not path:
            return
        entries = [(inst.id, inst.template) for inst in instances.values() if inst.ready and inst.id]
        if not entries:
            logger.warning(f"No ready sandbox IDs to save to {path}")
            return
        # Overwrite: each kernel run owns the file (mirrors e2b). Persist
        # ``<id>\t<template>`` when a concrete template was bound; bare ``<id>``
        # otherwise. detect_from_file recovers the template so affinity routing
        # survives the create->detect boundary.
        with open(path, "w") as handle:
            for sid, template in entries:
                if template:
                    handle.write(f"{sid}\t{template}\n")
                else:
                    handle.write(f"{sid}\n")
        logger.info(f"Saved {len(entries)} sandbox IDs to: {path}")

    # ------------------------------------------------------------------ lifecycle (LifecycleCapable)
    def pause(self, inst: SandboxInstance) -> None:
        """Pause the sandbox (async SDK ``Sandbox.pause`` on the bridge loop).

        The sandbox transitions to PAUSED; ``resume`` restores it. The handle
        stays valid across pause (pause is a server-side state change, not a
        transport close) -- but resume re-resolves the execd endpoint under a
        fresh handle, so ``resume`` swaps the handle in.
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None or state.sandbox_obj is None:
            raise RuntimeError(f"No sandboxsched handle for sandbox index {inst.index}")
        self.manager.pause_handle(state)

    def resume(self, inst: SandboxInstance) -> None:
        """Resume a paused sandbox (``Sandbox.resume`` by id, on the bridge).

        ``resume`` performs the server-side resume and returns a fresh async
        ``Sandbox`` (the execd endpoint may change across pause/resume); swap
        it in (mirrors aenv's resume swapping ``Sandbox.connect``). ``inst.id``
        is the OpenSandbox sandbox id (set at create via ``_to_instance``); it
        persists across pause/resume.
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None:
            raise RuntimeError(f"No sandboxsched handle for sandbox index {inst.index}")
        state.sandbox_obj = self.manager.resume_handle(inst.id)

    # ------------------------------------------------------------------ snapshot (SnapshotSizeCapable)
    def snapshot_sizes(self, inst: SandboxInstance) -> dict | None:
        """Stat a snapshot's size for a paused sandbox.

        Triggers ``Sandbox.create_snapshot`` (creates a persistent snapshot
        from the paused sandbox) on the bridge loop and reports its identity.
        Returns None
        if the snapshot dir is absent or the operation fails, so the runner
        skips the ``snapshot_size`` series event without crashing.

        Note: unlike aenv (which stats a local overlaybd dir), sandboxsched
        snapshots are server-side objects with no client-visible byte size in
        the SDK model (``SnapshotInfo`` has no size field). This returns a
        minimal dict (snapshot_id + state) so the series event fires for
        latency accounting; a real size requires a server-side query (deferred).
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None or state.sandbox_obj is None:
            return None
        try:
            snap = self.manager.snapshot_handle(state)
            return {
                "snapshot_id": getattr(snap, "id", ""),
                "snapshot_state": getattr(getattr(snap, "status", None), "state", ""),
            }
        except Exception as e:
            logger.warning(f"snapshot_sizes failed for sandbox {inst.id}: {e}")
            return None

    # ------------------------------------------------------------------ ephemeral (EphemeralCapable)
    def create_one(
        self, index: int, *, template: str | None = None, metadata: dict[str, str] | None = None
    ) -> SandboxInstance:
        """Create a single sandbox on demand (trajectory mode, EphemeralCapable).

        Mirrors the single-sandbox path of ``_create_batch_concurrent``: build a
        state, run ``_create_single`` (forwarding ``metadata`` + ``template``),
        populate ``_slot_templates`` so ``_to_instance`` stamps the resolved
        template onto ``SandboxInstance.template``, run the base readiness
        probe, and map the result via ``_apply_ready`` -- so timing is
        consistent with ``create_all``.
        """
        mgr = self.manager
        state = mgr._new_state(index)
        mgr._states[index] = state
        result = mgr._create_single(state, metadata=metadata, template=template)
        label = f"{mgr._noun}{index}"
        if result["success"]:
            mgr._slot_templates[index] = result.get("template")
            ready = mgr._ready_checker().check(
                mgr._handle_of(state),
                self._kernel_config.workflow_type,
                label,
            )
            mgr._apply_ready(state, ready, create_elapsed=result["create_elapsed"])
        else:
            state.creation_metrics.status = BackendSandboxStatus.FAILED
            state.creation_metrics.error_msg = result["error"]
            state.is_alive = False
            raise RuntimeError(f"create_one({index}) failed: {result['error']}")
        return self._to_instance(state)

    def kill_one(self, inst: SandboxInstance) -> None:
        """Tear down a single sandbox (trajectory mode, EphemeralCapable).

        On a successful server-side kill the state is marked ``torn_down`` so
        the final ``cleanup_all`` skips it (re-killing an already-dead sandbox
        only yields misleading warnings). A failed kill leaves the flag False so
        cleanup retries it -- no leak. The handle is kept either way for
        ``check_alive`` / post-mortem diagnostics.
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None:
            return  # never created (runner finally-path safety)
        try:
            if self.manager._handle_of(state) is not None:
                self.manager._kill_one(state)
                state.torn_down = True
        finally:
            inst.is_alive = False
            state.is_alive = False

    # ------------------------------------------------------------------ translation
    def _translate(self, states: Mapping[int, SandboxState]) -> dict[int, SandboxInstance]:
        """Translate ``{index: SandboxState}`` -> ``{index: SandboxInstance}``."""
        return {index: self._to_instance(state) for index, state in states.items()}

    def _to_instance(self, state: SandboxState) -> SandboxInstance:
        cm = state.creation_metrics
        sbx_id = ""
        if state.sandbox_obj is not None:
            # Sandbox.id is the OpenSandbox sandbox id (set at create by
            # the SDK). Falls back to "" for failed/no-handle states.
            sbx_id = str(getattr(state.sandbox_obj, "id", "") or "")
        status = _STATUS_MAP.get(cm.status.value, SandboxStatus.FAILED)
        return SandboxInstance(
            id=sbx_id,
            index=state.sandbox_id,
            numa_node=None,  # sandboxsched has no client-side NUMA binding
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
            template=self.manager._slot_templates.get(state.sandbox_id),
        )


def from_config(kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> SandboxSchedProvider:
    """Build a :class:`SandboxSchedProvider` from a KernelConfig + Config.

    The SandboxManager is constructed lazily on first use, so this is cheap and
    does not talk to the opensandbox SDK yet.
    """
    return SandboxSchedProvider(kernel_config, config, stop_event)


def build_provider(config: KernelConfig, raw_config: dict) -> SandboxSchedProvider:
    """Construct a :class:`SandboxSchedProvider` from a raw YAML dict.

    ``config`` is the already-built :class:`KernelConfig` (shared stress params
    from :meth:`KernelConfig.from_raw`); the sandboxsched backend Config is
    rebuilt here from the ``sandboxsched:`` block of the same raw dict. Both are
    passed to the provider: the kernel drives ``config``, the manager reads
    backend knobs from the sandboxsched Config.
    """
    stop_event = threading.Event()
    sched_config = Config.from_raw(raw_config) if raw_config else Config()
    return from_config(config, sched_config, stop_event)
