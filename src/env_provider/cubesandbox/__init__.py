"""CubeSandbox :class:`EnvironmentProvider` adapter.

Wraps :class:`env_provider.cubesandbox.manager.CubesandboxManager` behind the
kernel's :class:`env_provider.EnvironmentProvider` contract. The manager owns
the CubeSandbox SDK handles (``CubeSandboxState.cube_sandbox``); the adapter
translates those into host-agnostic :class:`SandboxInstance` objects and routes
``exec`` calls back through the manager's handle table -- the kernel never sees
an SDK type.

This is the only cube-specific code the kernel ever loads. It is lazy-imported
by ``bench_core.bench._build_provider``, so ``bench_core`` itself never depends
on the cubesandbox SDK -- the layering rule (kernel must not import provider
packages) holds.

Full surface (Phase 2): create / detect / check_alive / cleanup / exec /
save_ids (exec-only) PLUS ``pause`` / ``resume`` (native CubeSandbox
pause/resume -- :class:`LifecycleCapable`), ``snapshot_sizes`` (returns
``None`` for now -- the cube SDK's ``SnapshotInfo`` exposes no size fields, so
the ``snapshot_size`` series event is skipped until size introspection lands),
and ``create_one`` / ``kill_one`` (trajectory-mode ephemeral lifecycle --
:class:`EphemeralCapable`). ``default_replay_mode`` is ``lifecycle``;
``vmm_type`` is ``None`` (vm_monitor integration is deferred; CubeSandbox's VMM
process name -- ``cube-hypervisor`` vs ``cloud-hypervisor`` -- is unresolved
and host-level monitoring is a follow-on phase).
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

from .config import Config
from .manager import CubesandboxManager
from .schemas import CubeSandboxState
from .schemas import SandboxStatus as CubeSandboxStatus

try:
    from cubesandbox import Sandbox
except ImportError:
    # Mock for unit tests / dev without the cubesandbox SDK. resume() uses
    # Sandbox.connect(id); the manager has its own mock for create/list/kill.
    # This mock keeps the provider module importable.
    class Sandbox:  # type: ignore[no-redef]
        @staticmethod
        def connect(sandbox_id, *args, **kwargs):  # noqa: ARG004
            class _Mock:
                sandbox_id = sandbox_id

            return _Mock()


logger = logging.getLogger(__name__)

# CubeSandboxStatus (shared BackendSandboxStatus from _base) -> kernel
# SandboxStatus. PORT_READY / PORT_FAILED are workflow-neutralised to READY /
# READY_FAILED: the kernel report renders the workflow-specific label ("port"
# for browser, "command" for coding), so the status name itself stays
# host-agnostic. Keyed by the enum's value string (not by member identity) so
# the lookup stays correct when the enum class is re-bound across the
# provider/state module boundary.
_STATUS_MAP: dict[str, SandboxStatus] = {
    CubeSandboxStatus.PENDING.value: SandboxStatus.PENDING,
    CubeSandboxStatus.CREATING.value: SandboxStatus.CREATING,
    CubeSandboxStatus.CREATED.value: SandboxStatus.CREATED,
    CubeSandboxStatus.PORT_READY.value: SandboxStatus.READY,
    CubeSandboxStatus.ACTIVE.value: SandboxStatus.ACTIVE,
    CubeSandboxStatus.FAILED.value: SandboxStatus.FAILED,
    CubeSandboxStatus.PORT_FAILED.value: SandboxStatus.READY_FAILED,
    CubeSandboxStatus.OFFLINE.value: SandboxStatus.OFFLINE,
    CubeSandboxStatus.KILLED.value: SandboxStatus.KILLED,
}


class CubesandboxProvider(EnvironmentProvider):
    """EnvironmentProvider backed by a :class:`CubesandboxManager`.

    The adapter holds the kernel's :class:`KernelConfig` (shared stress params)
    plus the cube :class:`Config` (for env-var setup, the IDs-file path, idle
    TTL) plus the stop event. The :class:`CubesandboxManager` is constructed
    lazily on first use -- so the kernel can run host-side preflight and fail
    before any SDK client is built. The kernel never sees the manager or SDK
    types directly.
    """

    name = "cubesandbox"
    # CubeSandbox has native pause/resume WITH a memory snapshot (Cloud
    # Hypervisor fork), so lifecycle is its natural replay mode -- the same
    # memory-reuse oversubscription shape as aenv (pause frees RAM, so k*N
    # sandboxes fit in N running slots).
    default_replay_mode = "lifecycle"
    # vm_monitor integration deferred -- CubeSandbox's VMM process name is
    # unresolved (cube-hypervisor vs cloud-hypervisor) and host-level monitoring
    # is a follow-on phase.
    vmm_type = None

    def __init__(self, kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> None:
        self._kernel_config = kernel_config
        self._config = config
        self._stop_event = stop_event
        self._manager: CubesandboxManager | None = None

    @property
    def manager(self) -> CubesandboxManager:
        """The wrapped CubesandboxManager, constructed on first access.

        Lazy so the kernel's preflight / prepare_env / header-print can run (and
        fail) before any SDK client is built. Tests inject a mock by setting
        ``_manager`` directly.
        """
        if self._manager is None:
            self._manager = CubesandboxManager(self._kernel_config, self._config, self._stop_event)
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
        # Delegate to the manager's list->connect->kill path, which skips the
        # readiness probe (a dead sandbox must not stall teardown on uname).
        return self.manager.cleanup_existing()

    # ------------------------------------------------------------------ setup hooks
    def prepare_env(self) -> None:
        self._config.setup_cube_env()

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
        if state is None or state.cube_sandbox is None:
            raise RuntimeError(f"No CubeSandbox handle for sandbox index {inst.index}")
        # Only forward kwargs the cube SDK accepts; user/cwd/envs are passed
        # through when the kernel sets them (it currently sets only timeout).
        kwargs: dict[str, Any] = {"user": "root"}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if cwd is not None:
            kwargs["cwd"] = cwd
        if env is not None:
            kwargs["envs"] = env
        try:
            result = state.cube_sandbox.commands.run(command, **kwargs)
        except Exception as exc:
            # CubeSandbox's commands.run returns a CommandResult on success
            # (even on nonzero exit); it only raises on transport/protocol
            # errors (httpx timeout, HTTP error, Connect stream parse error),
            # and those carry no partial output. A timeout maps to exit 124
            # (the timeout(1) convention) so the runner records a timed_out
            # step and continues; any other transport error propagates (a
            # genuine control-plane failure should surface, not be swallowed).
            name = type(exc).__name__.lower()
            if "timeout" in name or "timed" in name:
                return CommandResult(exit_code=124, stdout="", stderr=str(exc)[:200])
            raise
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

    # ------------------------------------------------------------------ lifecycle (replay)
    def pause(self, inst: SandboxInstance) -> None:
        """Memory-snapshot the sandbox (native CubeSandbox ``pause``).

        CubeSandbox's ``pause(wait=True)`` blocks until the snapshot is stable
        -- unlike E2B's beta_pause, it is a first-class native op (the Cloud
        Hypervisor fork writes a memory snapshot the resume restores from), so
        ``wait=True`` keeps the pause synchronous with the lifecycle runner's
        per-step accounting.
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None or state.cube_sandbox is None:
            raise RuntimeError(f"No CubeSandbox handle for sandbox index {inst.index}")
        state.cube_sandbox.pause(wait=True)

    def resume(self, inst: SandboxInstance) -> None:
        """Restore the sandbox from its snapshot (``Sandbox.connect``).

        ``connect`` attaches to an existing sandbox AND auto-resumes it if
        paused (the cube SDK's standalone ``resume()`` is deprecated in favor
        of ``connect``). It returns a fresh handle on the resumed sandbox; swap
        it in (mirrors the cube manager's ``_attach``, which connects to
        running sandboxes in detect mode). ``inst.id`` is the cube sandbox_id
        (set at create via ``_to_instance``); it persists across pause/resume.
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None:
            raise RuntimeError(f"No CubeSandbox handle for sandbox index {inst.index}")
        state.cube_sandbox = Sandbox.connect(inst.id)

    def snapshot_sizes(self, inst: SandboxInstance) -> dict | None:
        """Stat the sandbox's snapshot disk usage -- ``None`` for now (deferred).

        The cube SDK's ``SnapshotInfo`` exposes only ``snapshot_id`` + ``names``
        (no size fields), so per-pause snapshot-size collection cannot be
        satisfied from the control plane yet. Returning ``None`` keeps the
        provider :class:`SnapshotSizeCapable` (the runner probes it right after
        ``pause``) while signalling "no data" -- the ``snapshot_size`` series
        event is skipped and the Snapshot-sizes sheet stays header-only, the
        same shape as a provider that never implements the method. A follow-on
        phase can stat the cube snapshot dir on the host (like aenv's
        ``scan_snapshot_sizes``) once that path is verified.
        """
        return None

    # ------------------------------------------------------------------ ephemeral (trajectory mode)
    def create_one(
        self, index: int, *, template: str | None = None, metadata: dict[str, str] | None = None
    ) -> SandboxInstance:
        """Create a single sandbox on demand (trajectory mode, EphemeralCapable).

        Mirrors the single-sandbox path of ``create_all``: build a state, run
        ``_create_single`` (forwarding ``metadata`` + ``template``), stamp
        ``_slot_templates`` so ``_to_instance`` stamps the resolved template
        onto ``SandboxInstance.template``, run the base readiness probe, and
        map the result via ``_apply_ready`` -- so timing is consistent with
        ``create_all``. The returned instance's ``creation_metrics`` reflects
        this trajectory; the runner accumulates per-trajectory
        ``create_sec``/``kill_sec`` into ``ReplayMetrics``.
        """
        from env_provider._base import BackendSandboxStatus

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
        """Tear down a single sandbox (trajectory mode, EphemeralCapable)."""
        state = self.manager.sandbox_states.get(inst.index)
        if state is None:
            return  # never created (runner finally-path safety)
        try:
            if self.manager._handle_of(state) is not None:
                self.manager._kill_one(state)
        finally:
            inst.is_alive = False
            state.is_alive = False

    # ------------------------------------------------------------------ translation
    def _translate(self, states: Mapping[int, CubeSandboxState]) -> dict[int, SandboxInstance]:
        """Translate ``{index: CubeSandboxState}`` -> ``{index: SandboxInstance}``."""
        return {index: self._to_instance(state) for index, state in states.items()}

    def _to_instance(self, state: CubeSandboxState) -> SandboxInstance:
        cm = state.creation_metrics
        sbx_id = ""
        if state.cube_sandbox is not None:
            sbx_id = str(getattr(state.cube_sandbox, "sandbox_id", "") or "")
        status = _STATUS_MAP.get(cm.status.value, SandboxStatus.FAILED)
        # v1 has no NUMA binding (CubeSandbox NUMA is host-level
        # distribution_scope, not guest FC_BIND); informational field stays None.
        return SandboxInstance(
            id=sbx_id,
            index=state.sandbox_id,
            numa_node=None,
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


def from_config(kernel_config: KernelConfig, config: Config, stop_event: threading.Event) -> CubesandboxProvider:
    """Build a :class:`CubesandboxProvider` from a KernelConfig + cube Config.

    The CubesandboxManager is constructed lazily on first use, so this is cheap
    and does not talk to the cube SDK yet.
    """
    return CubesandboxProvider(kernel_config, config, stop_event)


def build_provider(config: KernelConfig, raw_config: dict) -> CubesandboxProvider:
    """Construct a :class:`CubesandboxProvider` from a raw YAML dict (kernel smoke path).

    ``config`` is the already-built :class:`KernelConfig` (shared stress params
    from :meth:`KernelConfig.from_raw`); the cube backend Config is rebuilt here
    from the ``cubesandbox:`` block of the same raw dict. Both are passed to the
    provider: the kernel drives ``config``, the manager reads backend knobs
    from the cube Config.
    """
    stop_event = threading.Event()
    cube_config = Config.from_raw(raw_config) if raw_config else Config()
    return from_config(config, cube_config, stop_event)
