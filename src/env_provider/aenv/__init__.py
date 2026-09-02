"""AENV provider -- E2B-SDK provider pointed at an AENV server, with lifecycle.

AENV has no separate SDK: it is driven via the E2B SDK (``Sandbox``) pointed at
an AENV server (``E2B_API_URL=http://<aenv-host>``). This provider subclasses
:class:`env_provider.e2b.E2BProvider` and reuses the E2B ``SandboxManager``
handle table (create/detect/cleanup/exec/translate all inherited). It adds
``pause``/``resume`` via the E2B SDK's ``beta_pause()`` / ``Sandbox.connect()``,
satisfying :class:`env_provider.LifecycleCapable`.

The ``e2b`` provider stays exec-only (cloud E2B's ``beta_pause`` is uncertain;
AENV is the snapshot backend). Adding lifecycle to cloud e2b later is a
one-liner (add the methods to ``E2BProvider``).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from bench_core.config import KernelConfig
from env_provider import EphemeralCapable, SandboxInstance

from env_provider.aenv._snapshot import scan_snapshot_sizes
from env_provider.e2b import E2BProvider
from env_provider.e2b.config import Config

# Hardcoded fallback when neither AENV_HOME_PATH / AENV_HOME nor an explicit
# ``aenv.snapshot_dir`` override is set -- the AENV default install location.
DEFAULT_SNAPSHOT_DIR = "/var/lib/aenv/persisted-sandboxes/artifacts"
# The persisted-sandboxes tree lives under ``<aenv-home>/persisted-sandboxes/
# artifacts/<sandbox_id>/``; this suffix is appended to the resolved home path.
_SNAPSHOT_SUBDIR = Path("persisted-sandboxes") / "artifacts"


def _resolve_snapshot_base(config: Config) -> str:
    """Resolve the persisted-sandboxes base dir for snapshot-size scanning.

    Priority:
      1. ``aenv.snapshot_dir`` -- explicit YAML override (verbatim, no suffix).
      2. ``$AENV_HOME_PATH`` -- the AENV server's home-path env var.
      3. ``$AENV_HOME`` -- the README's home-path convention.
      4. ``DEFAULT_SNAPSHOT_DIR`` (``/var/lib/aenv/...``).

    The AENV server reads AENV_HOME_PATH to decide where to *write* persisted
    sandboxes; bench-core reads the same var to *scan* them, so the two stay in
    sync without a per-run path knob. The YAML override is the escape hatch for
    non-standard layouts.
    """
    if config.snapshot_dir:
        return config.snapshot_dir
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
