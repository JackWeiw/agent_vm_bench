"""AENV provider -- E2B-SDK provider pointed at an AENV server, with lifecycle.

AENV has no separate SDK: it is driven via the E2B SDK (``Sandbox``) pointed at
an AENV server (``E2B_API_URL=http://<aenv-host>``). This provider subclasses
:class:`env_provider.e2b.E2BProvider` and reuses the E2B ``SandboxManager``
handle table (create/detect/cleanup/exec/translate all inherited). It adds
``pause``/``resume`` via the E2B SDK's ``beta_pause()`` / ``Sandbox.connect()``,
satisfying :class:`env_provider.LifecycleCapable`.

Optionally pins each sandbox's warm-pooled firecracker to a dedicated CCL vCPU
set (``aenv.ccl_bind``) in ``exec_only`` replay mode, so concurrent VMs don't
contend for the same physical cores; see :mod:`env_provider.aenv._ccl`.

The ``e2b`` provider stays exec-only (cloud E2B's ``beta_pause`` is uncertain;
AENV is the snapshot backend). Adding lifecycle to cloud e2b later is a
one-liner (add the methods to ``E2BProvider``).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from bench_core.config import KernelConfig
from env_provider import SandboxInstance
from env_provider.aenv._ccl import bind_ccl_affinity
from env_provider.aenv._snapshot import scan_snapshot_sizes
from env_provider.e2b import E2BProvider
from env_provider.e2b.config import Config

logger = logging.getLogger(__name__)

# Hardcoded fallback when neither AENV_HOME_PATH / AENV_HOME nor an explicit
# ``aenv.snapshot_dir`` override is set -- the AENV default install location.
DEFAULT_SNAPSHOT_DIR = "/var/lib/aenv/persisted-sandboxes/artifacts"
# The persisted-sandboxes tree lives under ``<aenv-home>/persisted-sandboxes/
# artifacts/<sandbox_id>/``; this suffix is appended to the resolved home path.
_SNAPSHOT_SUBDIR = Path("persisted-sandboxes") / "artifacts"


def _resolve_snapshot_base(config: Config) -> str:
    """Resolve the persisted-sandboxes base dir for snapshot-size scanning.

    Priority:
      1. ``aenv.snapshot_dir`` -- explicit YAML full-path override (no suffix).
      2. ``aenv.aenv_home_path`` -- YAML home path (+ persisted-sandboxes/artifacts).
      3. ``$AENV_HOME_PATH`` -- the AENV server's home-path env var (fallback).
      4. ``$AENV_HOME`` -- the README's home-path convention (fallback).
      5. ``DEFAULT_SNAPSHOT_DIR`` (``/var/lib/aenv/...``).

    The YAML ``aenv_home_path`` is the primary source (no shell env var to
    export -- the path lives in the config alongside everything else). The env
    vars are kept as a lower-priority fallback for when bench-core runs on the
    same host as the AENV server with AENV_HOME_PATH already set.
    """
    if config.snapshot_dir:
        return config.snapshot_dir
    if config.aenv_home_path:
        return str(Path(config.aenv_home_path) / _SNAPSHOT_SUBDIR)
    for var in ("AENV_HOME_PATH", "AENV_HOME"):
        home = os.environ.get(var)
        if home:
            return str(Path(home) / _SNAPSHOT_SUBDIR)
    return DEFAULT_SNAPSHOT_DIR


try:
    from e2b import Sandbox
except ImportError:
    # Mock for unit tests / dev without the E2B SDK. aenv only uses
    # Sandbox.connect (in resume); the create/list/kill seams live in the
    # e2b manager's own mock. This mock keeps the module importable.
    class Sandbox:  # type: ignore[no-redef]
        @staticmethod
        def connect(sandbox_id):  # noqa: ARG001
            class MockSandbox:
                sandbox_id = sandbox_id

            return MockSandbox()


def build_provider(config: KernelConfig, raw_config: dict) -> AenvProvider:
    """Construct an AenvProvider from a raw YAML dict (kernel smoke path).

    Mirrors ``env_provider.e2b.build_provider``: the kernel's KernelConfig is
    already built; the e2b-style Config is rebuilt here from the ``aenv:``
    block (same shape as ``e2b:``, with ``E2B_API_URL`` pointing at the AENV
    server).
    """
    stop_event = threading.Event()
    aenv_config = Config.from_raw(raw_config, block="aenv") if raw_config else Config()
    return AenvProvider(config, aenv_config, stop_event)


class AenvProvider(E2BProvider):
    """E2B-SDK provider pointed at an AENV server, with lifecycle pause/resume.

    Subclasses E2BProvider (reuses create/detect/cleanup/exec/translate +
    SandboxManager handle table). Adds pause/resume via the E2B SDK's
    beta_pause / connect, satisfying LifecycleCapable.
    """

    name = "aenv"
    default_replay_mode = "lifecycle"

    # ---------------------------------------------------------- CCL affinity
    # AENV keeps a warm firecracker pool: each firecracker exists before create()
    # with only a random --api-sock path (no sandbox_id in cmdline). bind_ccl
    # scans /proc + MMDS to map sandbox_id -> firecracker pid, then
    # os.sched_setaffinity-pins it to a dedicated CCL vCPU set so concurrent VMs
    # don't contend for the same physical cores. Opt-in via ``aenv.ccl_bind``;
    # viable only in exec_only (lifecycle/trajectory respawn the firecracker on
    # each resume -> pid unstable); best-effort (Linux + root on the AENV host);
    # never unwinds a successful create. See :mod:`env_provider.aenv._ccl`.
    def create_all(self, *, templates: dict[int, str | None] | None = None) -> dict[int, SandboxInstance]:
        """Create all sandboxes, then pin each to its CCL (exec_only, best-effort).

        Binding runs as a post-create pass over the manager's states (the base
        create already joined its concurrent workers), so the first bind primes
        the pid cache for the rest. Faithful to the reference, which binds
        immediately after a successful create; for best-effort pinning of a
        long-lived firecracker the timing is immaterial. Gated by
        :meth:`_ccl_active` (exec_only only -- lifecycle/trajectory respawn the
        firecracker per resume, so the pid is unstable).
        """
        states = self.manager.create_all(templates=templates)
        if self._ccl_active():
            for state in states.values():
                self._bind_ccl(state)
        return self._translate(states)

    def _ccl_active(self) -> bool:
        """True iff CCL binding should run for this run.

        Binding is viable only in ``exec_only``: the firecracker pid is stable
        for the whole run (no pause/resume). In lifecycle/trajectory each
        ``resume`` respawns the firecracker under a new pid (AENV pause kills
        the process; resume takes a fresh one from the warm pool), so the pid
        is unstable and re-binding per resume would block the stress path
        (post-resume commands depend on the binding). Read the replay mode off
        the kernel's workflow view; a missing/non-replay config skips (backward
        compat: ``ccl_bind`` is a no-op outside exec_only).
        """
        if not getattr(self._config, "ccl_bind", False):
            return False
        wf = getattr(self._kernel_config, "workflow_config", None)
        mode = getattr(wf, "replay_mode", None)
        if mode != "exec_only":
            logger.warning(
                "ccl_bind is set but replay mode is %r; CCL binding applies only "
                "to exec_only (lifecycle/trajectory respawn the firecracker on each "
                "resume, so the pid is unstable). Skipping CCL binding for this run.",
                mode,
            )
            return False
        return True

    def _bind_ccl(self, state) -> None:
        """Pin one sandbox's firecracker to its CCL. Best-effort; never raises.

        ``state.sandbox_id`` is the 1-based kernel sequence; the CCL index is
        0-based (``sandbox_id - 1``), matching the reference's ``index - 1``
        convention. Failed/no-handle states have ``sandbox_obj is None`` ->
        empty sandbox_id -> the bind helper skips with a warning.
        """
        sbx = getattr(state, "sandbox_obj", None)
        sandbox_id = str(getattr(sbx, "sandbox_id", "") or "") if sbx is not None else ""
        sandbox_index = state.sandbox_id - 1
        try:
            bind_ccl_affinity(sandbox_id, sandbox_index, self._config)
        except Exception as aff_err:  # noqa: BLE001 -- never unwind a working sandbox
            logger.warning(
                "CCL bind failed for sandbox index %s: %s: %r",
                state.sandbox_id,
                type(aff_err).__name__,
                aff_err,
            )

    def pause(self, inst: SandboxInstance) -> None:
        """Memory-snapshot the sandbox (E2B SDK beta_pause)."""
        state = self.manager.sandbox_states.get(inst.index)
        if state is None or state.sandbox_obj is None:
            raise RuntimeError(f"No AENV handle for sandbox index {inst.index}")
        state.sandbox_obj.beta_pause()

    def resume(self, inst: SandboxInstance) -> None:
        """Restore the sandbox from its snapshot (Sandbox.connect).

        ``connect`` returns a fresh handle on the resumed sandbox; swap it in
        (mirrors the e2b manager's ``_attach``, which connects to running
        sandboxes in detect mode). ``inst.id`` is the e2b sandbox_id (set at
        create via ``_to_instance``); it persists across pause/resume.

        CCL binding is intentionally NOT re-applied here: a resumed sandbox's
        firecracker has respawned under a new pid (AENV pause kills the process;
        resume takes a fresh one from the warm pool), so the cached pid is
        stale and re-resolving it (a /proc + MMDS scan) would block the stress
        path whose post-resume commands depend on the binding. CCL binding is
        exec_only-only (see :meth:`_ccl_active`), where the pid is stable.
        """
        state = self.manager.sandbox_states.get(inst.index)
        if state is None:
            raise RuntimeError(f"No AENV handle for sandbox index {inst.index}")
        state.sandbox_obj = Sandbox.connect(inst.id)

    def snapshot_sizes(self, inst: SandboxInstance) -> dict | None:
        """Stat the sandbox's persisted-snapshot tree (inode-deduped).

        Base resolves via :func:`_resolve_snapshot_base` (explicit
        ``aenv.snapshot_dir`` override, else ``$AENV_HOME_PATH``/``$AENV_HOME`` +
        ``persisted-sandboxes/artifacts``, else the hardcoded default). The
        sandbox's own dir is ``<base>/<sandbox_id>/``. Returns ``None`` if the
        dir is absent, so the runner skips the ``snapshot_size`` series event
        without crashing.
        """
        base = _resolve_snapshot_base(self._config)
        sandbox_dir = Path(base) / inst.id
        return scan_snapshot_sizes(sandbox_dir)

    # ------------------------------------------------------------------ ephemeral (trajectory mode)
    def create_one(
        self, index: int, *, template: str | None = None, metadata: dict[str, str] | None = None
    ) -> SandboxInstance:
        """Create a single sandbox on demand (trajectory mode, EphemeralCapable).

        Mirrors the single-sandbox path of ``_create_batch_concurrent``: build a
        state, run ``_create_single`` (forwarding ``metadata`` + ``template``),
        populate ``_slot_templates`` so ``_to_instance`` stamps the resolved
        template onto ``SandboxInstance.template``, run the base readiness
        probe, and map the result via ``_apply_ready`` -- so timing is
        consistent with ``create_all``. The returned instance's
        ``creation_metrics`` reflects this trajectory; the runner accumulates
        per-trajectory ``create_sec``/``kill_sec`` into ``ReplayMetrics``.
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
