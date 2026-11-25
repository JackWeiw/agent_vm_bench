"""AENV provider -- E2B-SDK provider pointed at an AENV server, with lifecycle.

AENV has no separate SDK: it is driven via the E2B SDK (``Sandbox``) pointed at
an AENV server (``E2B_API_URL=http://<aenv-host>``). This provider subclasses
:class:`env_provider.e2b.E2BProvider` and reuses the E2B ``SandboxManager``
handle table (create/detect/cleanup/exec/translate all inherited). It adds
``pause``/``resume`` via the E2B SDK's ``beta_pause()`` / ``Sandbox.connect()``,
satisfying :class:`env_provider.LifecycleCapable`.

Optionally pins each sandbox's warm-pooled firecracker to a dedicated CCL vCPU
set (``aenv.ccl_bind``) in ``exec_only`` / ``exec_ephemeral`` replay mode, so
concurrent VMs don't contend for the same physical cores; see
:mod:`env_provider.aenv._ccl`. ``aenv.pin_cpus`` instead pins every
firecracker to one fixed host CPU set (cold-start perf measurement,
fail-closed: a failed pin kills the sandbox).

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
from env_provider import ProviderConfigError, SandboxInstance
from env_provider._base import BackendSandboxStatus
from env_provider.aenv._ccl import (
    CCL_PID_RESOLVE_ATTEMPTS,
    CCL_PID_RESOLVE_TIMEOUT_SEC,
    RESOLVER,
    bind_ccl_affinity,
    bind_process_all_threads,
)
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


# Sanity cap for _parse_pin_cpus: no real host has more CPUs, and a YAML typo
# ("0-99999999999") must fail the pre-check instead of allocating the range.
_MAX_PIN_SPAN = 1 << 16


def _parse_pin_cpus(value: str) -> set[int]:
    """Strictly parse an ``aenv.pin_cpus`` CPU list ("2,3" / "2-3") to ids.

    Unlike _ccl.parse_cpu_list (lenient, tolerant of sysfs text), this raises
    ValueError on any malformed token so a bad YAML aborts BEFORE the first
    sandbox is created.
    """
    out: set[int] = set()
    for part in value.strip().split(","):
        part = part.strip()
        if not part:
            continue
        first, dash, last = part.partition("-")
        try:
            start = int(first)
            end = int(last) if dash else start
        except ValueError:
            raise ValueError(f"invalid pin_cpus {value!r}: bad token {part!r}") from None
        if start < 0 or end < start:
            raise ValueError(f"invalid pin_cpus {value!r}: bad range {part!r}")
        if end - start + 1 > _MAX_PIN_SPAN:
            raise ValueError(f"invalid pin_cpus {value!r}: range {part!r} wider than {_MAX_PIN_SPAN} CPUs")
        out.update(range(start, end + 1))
    if not out:
        raise ValueError(f"invalid pin_cpus {value!r}: no CPUs parsed")
    return out


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
    # scans /proc + MMDS to map sandbox_id -> firecracker pid, then pins EVERY
    # thread of that process (one-shot: main first so later-spawned threads
    # inherit the mask, then a /proc/<pid>/task sweep) to a sysfs-detected
    # silicon L3 domain (AMD CCD / Kunpeng CL; round-robin by sandbox index)
    # so concurrent VMs don't contend for the same physical cores. Opt-in via
    # ``aenv.ccl_bind``;
    # viable in exec_only / exec_ephemeral (lifecycle respawns the
    # firecracker on each resume -> pid unstable); best-effort (Linux + root on
    # the AENV host);
    # never unwinds a successful create. See :mod:`env_provider.aenv._ccl`.
    def create_all(self, *, templates: dict[int, str | None] | None = None) -> dict[int, SandboxInstance]:
        """Create all sandboxes, then pin / CCL-bind each one's firecracker.

        ``pin_cpus`` is parsed strictly BEFORE any sandbox is created: a
        malformed pin aborts the run outright instead of creating sandboxes it
        would then have to kill. Both pin and CCL binding run as post-create
        passes over the manager's states (the base create already joined its
        concurrent workers). Pinning is fail-closed (:meth:`_pin_fc`); CCL
        binding is gated by :meth:`_ccl_active` (exec_only / exec_ephemeral
        only -- lifecycle respawns the firecracker per resume, so the pid is
        unstable) and skipped when pin_cpus is set (the two would overwrite
        each other).
        """
        pin_cpus: set[int] | None = None
        if self._config.pin_cpus:
            try:
                pin_cpus = _parse_pin_cpus(self._config.pin_cpus)
            except ValueError as exc:
                # Abort before the first sandbox exists, and raise (not return
                # {}) so the CLI exits non-zero: an empty mapping would look
                # like "nothing to test" and CI would report success.
                raise ProviderConfigError(f"invalid aenv pin_cpus: {exc}") from None
        states = self.manager.create_all(templates=templates)
        if pin_cpus is not None and self._pin_active():
            for state in states.values():
                self._pin_fc(state, pin_cpus)
        if self._ccl_active():
            for state in states.values():
                self._bind_ccl(state)
        return self._translate(states)

    def _pin_active(self) -> bool:
        """True iff firecracker CPU pinning should run for this run.

        Skipped in lifecycle replay mode: each ``resume`` claims a fresh
        firecracker from the warm pool, so the pin is lost after the first
        resume while the log said PINNED OK. Non-replay workflows (the
        --create-only cold-start measurement) and exec_only / exec_ephemeral
        keep a stable firecracker pid for the whole run.
        """
        if not self._config.pin_cpus:
            return False
        wf = getattr(self._kernel_config, "workflow_config", None)
        mode = getattr(wf, "replay_mode", None)
        if mode == "lifecycle":
            logger.warning(
                "pin_cpus is set but replay mode is %r: each resume respawns the "
                "firecracker from the warm pool and loses the pin; skipping pin_cpus "
                "for this run.",
                mode,
            )
            return False
        return True

    def _pin_fc(self, state, cpus: set[int]) -> None:
        """Pin one sandbox's firecracker to ``aenv.pin_cpus`` (fail-closed).

        Resolves the firecracker pid via MMDS ``instanceID`` (an exact
        sandbox_id match) and binds every thread with read-back verification
        (:mod:`env_provider.aenv._ccl`). On any failure the sandbox is killed
        and the state marked FAILED: ``--create-only`` returns before cleanup
        and ``save_ids`` records only ready instances, so a live-but-unpinned
        sandbox would leak as an orphan and skew the next ``--detect``.
        """
        sbx = getattr(state, "sandbox_obj", None)
        if sbx is None:
            return  # create already failed; nothing to pin
        sandbox_id = str(getattr(sbx, "sandbox_id", "") or "")
        error: str | None = None
        try:
            if not sandbox_id:
                error = "handle carries no sandbox_id"
            else:
                pid = RESOLVER.resolve_firecracker_pid(
                    sandbox_id,
                    attempts=CCL_PID_RESOLVE_ATTEMPTS,
                    timeout_sec=CCL_PID_RESOLVE_TIMEOUT_SEC,
                )
                if pid is None:
                    error = f"could not resolve firecracker pid via MMDS for sandbox {sandbox_id}"
                else:
                    verified, detail = bind_process_all_threads(pid, cpus)
                    if verified:
                        logger.info(
                            "[aenv-pin] PINNED OK sandbox=%s pid=%d cpus=%s %s",
                            sandbox_id,
                            pid,
                            self._config.pin_cpus,
                            detail,
                        )
                    else:
                        error = f"bind not verified for pid {pid} (sandbox {sandbox_id}): {detail}"
        except Exception as exc:  # noqa: BLE001 -- collected into the fail-closed path below
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            self._pin_fail_closed(state, sbx, error)

    def _pin_fail_closed(self, state, sbx, error: str) -> None:
        """Kill the just-created sandbox and mark the state FAILED (no orphan)."""
        logger.error("[aenv-pin] PIN FAILED sandbox=%s: %s", getattr(sbx, "sandbox_id", "?"), error)
        try:
            sbx.kill()
            state.torn_down = True  # cleanup_all must not re-kill a dead sandbox
        except Exception as kill_err:  # noqa: BLE001 -- leave torn_down False so cleanup retries
            logger.warning("[aenv-pin] kill after pin failure also failed; cleanup_all will retry: %s", kill_err)
        state.creation_metrics.status = BackendSandboxStatus.FAILED
        state.creation_metrics.error_msg = f"firecracker cpu pin failed: {error}"
        state.is_alive = False

    def _ccl_active(self) -> bool:
        """True iff CCL binding should run for this run.

        Binding is viable in ``exec_only`` / ``exec_ephemeral``: the firecracker
        pid is stable for the bind window (exec_only: the whole run, no
        pause/resume; exec_ephemeral: per round -- create_one -> bind -> run ->
        kill_one, no resume). In lifecycle each ``resume`` respawns
        the firecracker under a new pid (AENV pause kills the process; resume
        takes a fresh one from the warm pool), so the pid is unstable and
        re-binding per resume would block the stress path (post-resume commands
        depend on the binding). Read the replay mode off the kernel's workflow
        view; a missing/non-replay config skips (backward compat: ``ccl_bind``
        is a no-op outside exec_only / exec_ephemeral).
        """
        if not getattr(self._config, "ccl_bind", False):
            return False
        if self._config.pin_cpus:
            logger.warning(
                "ccl_bind is set but pin_cpus is also set: the CCL bind would "
                "overwrite the pin_cpus cpuset; honoring pin_cpus and skipping "
                "ccl_bind for this run."
            )
            return False
        wf = getattr(self._kernel_config, "workflow_config", None)
        mode = getattr(wf, "replay_mode", None)
        if mode not in ("exec_only", "exec_ephemeral"):
            logger.warning(
                "ccl_bind is set but replay mode is %r; CCL binding applies only "
                "to exec_only / exec_ephemeral (lifecycle respawns the "
                "firecracker on each resume, so the pid is unstable). Skipping CCL "
                "binding for this run.",
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
        path whose post-resume commands depend on the binding. CCL binding runs
        only at create time (exec_only's ``create_all`` pass; exec_ephemeral's
        ``create_one``), never on resume -- see :meth:`_ccl_active`.
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

    # ------------------------------------------------------------------ ephemeral (exec_ephemeral mode)
    def create_one(
        self, index: int, *, template: str | None = None, metadata: dict[str, str] | None = None
    ) -> SandboxInstance:
        """Create a single sandbox on demand (exec_ephemeral mode, EphemeralCapable).

        Mirrors the single-sandbox path of ``_create_batch_concurrent``: build a
        state, run ``_create_single`` (forwarding ``metadata`` + ``template``),
        populate ``_slot_templates`` so ``_to_instance`` stamps the resolved
        template onto ``SandboxInstance.template``, run the base readiness
        probe, and map the result via ``_apply_ready`` -- so timing is
        consistent with ``create_all``. The returned instance's
        ``creation_metrics`` reflects this trajectory; the runner records
        per-trajectory create/kill timings in the lifecycle series
        (``trajectory_create`` / ``trajectory_kill`` events).
        """
        pin_cpus: set[int] | None = None
        if self._config.pin_cpus:
            try:
                pin_cpus = _parse_pin_cpus(self._config.pin_cpus)
            except ValueError as exc:
                raise ProviderConfigError(f"create_one({index}) aborted: {exc}") from None

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
            # Pin first (fail-closed, same semantics as create_all: a failed
            # pin kills the sandbox and fails the create), then the CCL bind.
            # exec_ephemeral reuses create_all's per-create CCL bind: each
            # create_one yields a fresh e2b sandbox_id (pid-resolvable via
            # MMDS) and the firecracker is stable for the round (no
            # pause/resume). Best-effort (never unwinds the create); a no-op
            # when _ccl_active is False (non-exec modes, ccl_bind unset, or
            # pin_cpus set).
            if pin_cpus is not None and self._pin_active():
                self._pin_fc(state, pin_cpus)
                if state.creation_metrics.status == BackendSandboxStatus.FAILED:
                    raise RuntimeError(f"create_one({index}) failed: {state.creation_metrics.error_msg}")
            if self._ccl_active():
                self._bind_ccl(state)
        else:
            state.creation_metrics.status = BackendSandboxStatus.FAILED
            state.creation_metrics.error_msg = result["error"]
            state.is_alive = False
            raise RuntimeError(f"create_one({index}) failed: {result['error']}")
        return self._to_instance(state)

    def kill_one(self, inst: SandboxInstance) -> None:
        """Tear down a single sandbox (exec_ephemeral mode, EphemeralCapable).

        On a successful server-side kill the state is marked ``torn_down`` so
        the final ``cleanup_all`` skips it (re-killing an already-dead sandbox
        only yields misleading "not found" warnings). A failed kill leaves the
        flag False so cleanup retries it -- no leak. The handle is kept either
        way for ``check_alive`` / post-mortem diagnostics.
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
