"""CubeSandbox sandbox manager (SDK seams over :class:`BaseSandboxManager`).

The workflow-agnostic create/detect/cleanup skeleton is inherited from
:class:`env_provider._base.BaseSandboxManager`; this module supplies only the
CubeSandbox SDK seams (Sandbox.create / list / connect / kill + the exec probe
+ the get_info liveness check) and the two cube-specific methods the base can't
own: ``detect_from_file`` (ID-file matching) and ``check_alive`` (control-plane
liveness). Readiness probing is delegated to :class:`env_provider._ready.ReadyChecker`
via the base.

CubeSandbox SDK surface (verified against ``cubesandbox`` 0.7.0):

* ``Sandbox.create(template, *, timeout, env_vars, envs, metadata, ...)`` -- the
  ``envs`` alias is E2B-compatible; ``timeout`` is the *idle TTL* (not a create
  wait -- create is a synchronous HTTP POST). Returns a ``Sandbox`` whose
  ``.sandbox_id`` property is the real cube ID.
* ``Sandbox.list() -> list[dict]`` -- a **flat list** of info dicts (NOT a
  paginator); each has ``sandboxID`` / ``templateID`` / ``state``.
* ``Sandbox.connect(sandbox_id)`` -- attaches to an existing sandbox AND
  auto-resumes it if paused (404 -> ``SandboxNotFoundError``).
* instance ``commands.run(cmd, user, timeout, cwd, envs)`` -> ``CommandResult``
  dataclass ``(stdout, stderr, exit_code)``; ``kill()``; ``pause(wait=True)``;
  ``get_info()``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Event
from typing import Any

try:
    from cubesandbox import Sandbox
except ImportError:
    # Mock for development/testing without the cubesandbox SDK. Tests inject a
    # controllable fake via monkeypatch (see test_cubesandbox_manager.py); this
    # stub only keeps the module importable when the SDK is absent.
    class Sandbox:  # type: ignore[no-redef]
        @staticmethod
        def create(*args, **kwargs):  # noqa: ARG004
            class MockCommands:
                def run(self, *a, **k):  # noqa: ARG002
                    class _R:
                        exit_code = 0
                        stdout = ""
                        stderr = ""

                    return _R()

            class _Mock:
                sandbox_id = "mock_sandbox_id"
                commands = MockCommands()

                def kill(self):
                    pass

                def pause(self, *a, **k):  # noqa: ARG002
                    pass

                def get_info(self):
                    return {"state": "running"}

            return _Mock()

        @staticmethod
        def list(*args, **kwargs):  # noqa: ARG004
            return []

        @staticmethod
        def connect(sandbox_id, *args, **kwargs):  # noqa: ARG004
            class MockCommands:
                def run(self, *a, **k):  # noqa: ARG002
                    class _R:
                        exit_code = 0
                        stdout = ""

                    return _R()

            class _Mock:
                sandbox_id = sandbox_id or "mock_sandbox_id"
                commands = MockCommands()

                def kill(self):
                    pass

            return _Mock()


from env_provider._base import BaseSandboxManager, BackendSandboxStatus

from .config import Config
from .schemas import CubeSandboxState

logger = logging.getLogger(__name__)

try:
    from requests.adapters import HTTPAdapter

    class _TimeoutAdapter(HTTPAdapter):
        """HTTPAdapter that fills a default timeout when the caller omits one.

        ``requests`` blocks forever when ``timeout`` is None (its default), and
        the cube SDK's control-plane calls (kill / pause / get_info / snapshots)
        omit it -- a single blackholed DELETE hangs the caller indefinitely.
        Mounting this on a handle's session bounds every call without patching
        the pip-installed SDK.
        """

        def __init__(self, timeout: float, **kwargs: Any) -> None:
            self._default_timeout = timeout
            super().__init__(**kwargs)

        def send(self, request: Any, **kwargs: Any) -> Any:
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = self._default_timeout
            return super().send(request, **kwargs)

except ImportError:  # requests absent (only when the cube SDK is absent)
    _TimeoutAdapter = None  # type: ignore[assignment,misc]


def pin_session_timeout(handle: Any, timeout: float) -> None:
    """Best-effort: bound the cube SDK handle's control-plane HTTP calls.

    Mounts a :class:`_TimeoutAdapter` on the handle's ``requests.Session`` so a
    ``kill()`` with no ``timeout=`` cannot block forever. Idempotent per
    session; degrades to a debug log if the SDK's private layout differs -- never
    fails a create/connect. The bench-side backstop when the pin is unavailable
    is ``cleanup_all``'s overall deadline.
    """
    if _TimeoutAdapter is None:
        return
    session = getattr(handle, "_session", None)
    if session is None or getattr(session, "_timeout_pinned", False):
        return
    try:
        adapter = _TimeoutAdapter(timeout)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session._timeout_pinned = True
    except Exception as e:
        logger.debug(f"Session timeout pin skipped: {str(e)[:80]}")


def _call_bounded(fn: Callable[[], Any], timeout: float) -> Any:
    """Run ``fn()`` on a daemon thread; raise TimeoutError if it outlives ``timeout``.

    :func:`pin_session_timeout` only covers instance handles (their session
    exists before the first call). The SDK's classmethods -- ``Sandbox.create``,
    ``connect``, ``list`` -- build a throwaway ``requests.Session`` per call
    with no timeout, so a blackholed control-plane POST blocks the calling thread forever and there is no session to pin ahead of time.
    join(timeout) bounds the wait bench-side; the worker is a daemon thread, so
    an abandoned call can never block process exit.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"cube SDK call did not return within {timeout}s; abandoned")
    if "error" in box:
        raise box["error"]
    return box["result"]


class CubesandboxManager(BaseSandboxManager):
    """CubeSandbox lifecycle: SDK seams over the shared lifecycle template.

    Shared stress params (total_count, create_batch_*, workflow_type) are read
    from ``kernel_config``; backend knobs (template, timeout, ids_file) from
    ``cube_config``. Readiness is delegated to :class:`ReadyChecker` via the
    base (provider-transparent ``_ready_config`` -- no cube readiness knobs).
    """

    _handle_attr = "cube_sandbox"
    _noun = "Sandbox"
    _id_attr = "sandbox_id"
    # Keep the original creation status for stats; don't overwrite with KILLED.
    _set_killed_on_cleanup = False

    def __init__(self, kernel_config: Any, cube_config: Config, stop_event: Event) -> None:
        super().__init__(kernel_config, stop_event)
        self.config = cube_config

    # --------------------------------------------------------- subclass seams
    def _new_state(
        self,
        index: int,
        *,
        batch_id: int = -1,
        external_id: str = "",  # noqa: ARG002
    ) -> CubeSandboxState:
        return CubeSandboxState(
            sandbox_id=index,
            batch_id=batch_id,
            workflow_type=self.kernel_config.workflow_type,
        )

    def _create_single(
        self, state: CubeSandboxState, *, metadata: dict[str, str] | None = None, template: str | None = None
    ) -> dict:
        """Create one sandbox; preserve the handle in ``state.cube_sandbox``.

        Records submit->create timing; the base runs the readiness probe after
        this returns success and maps the result onto ``creation_metrics``.
        ``metadata`` is forwarded to the SDK for operator visibility (labels
        only, not an idempotency key -- spec G3 deferred). ``template`` overrides
        the config default (trajectory-mode multi-template routing).

        v1 passes no ``envs``: CubeSandbox NUMA binding is host-level
        (``distribution_scope``), not the guest ``FC_BIND`` env convention; the
        latter is unverified for cube, so it is omitted rather than guessed.
        """
        state.creation_metrics.status = BackendSandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        resolved = template or self.config.template

        try:
            sbx = _call_bounded(
                lambda: Sandbox.create(
                    resolved,
                    timeout=self.config.timeout,
                    envs=None,
                    metadata=metadata,
                ),
                self.config.request_timeout,
            )
            pin_session_timeout(sbx, self.config.request_timeout)
            state.cube_sandbox = sbx
            state.creation_metrics.create_ready_time = time.time()
            state.creation_metrics.create_elapsed = (
                state.creation_metrics.create_ready_time - state.creation_metrics.submit_time
            )
            state.creation_metrics.status = BackendSandboxStatus.CREATED
            return {
                "success": True,
                "create_elapsed": state.creation_metrics.create_elapsed,
                "error": "",
                "template": resolved,
            }
        except Exception as e:
            state.creation_metrics.create_ready_time = time.time()
            return {
                "success": False,
                "create_elapsed": 0.0,
                "error": str(e),
                "template": resolved,
            }

    def _list_existing(self) -> list:
        """List running sandboxes (CubeSandbox returns a flat list of dicts)."""
        return _call_bounded(Sandbox.list, self.config.request_timeout)

    def _external_id(self, listed: Any) -> str:
        """The stable cube sandbox ID of a listed sandbox (a dict, key sandboxID)."""
        if isinstance(listed, dict):
            return listed.get("sandboxID") or ""
        return str(getattr(listed, "sandbox_id", listed) or "")

    def _attach(self, listed: Any) -> Any:
        sbx = _call_bounded(lambda: Sandbox.connect(self._external_id(listed)), self.config.request_timeout)
        pin_session_timeout(sbx, self.config.request_timeout)
        return sbx

    def _kill_one(self, state: CubeSandboxState) -> None:
        state.cube_sandbox.kill()

    def _exec_probe(self, handle: Any, cmd: str, timeout: int) -> tuple[int, str, str]:
        result = handle.commands.run(cmd, user="root", timeout=timeout)
        return result.exit_code, result.stdout, result.stderr

    # ------------------------------------------------------------ cleanup override
    def cleanup_all(self) -> None:
        """Bounded-concurrent kill of every tracked sandbox.

        Overrides the base serial loop: serial is too slow at oversub scale
        (1000+ sandboxes), but *unbounded* parallel DELETEs overflow cubemaster's
        accept backlog and blackhole kills (observed at ratio-04). Two bounds
        stack:

        * ``cleanup_concurrency`` workers cap the request storm;
        * the pinned session timeout (:func:`pin_session_timeout`) caps each
          HTTP call, and the overall deadline below -- worst case every kill
          rides its full timeout -- guarantees teardown returns even if the pin
          is unavailable (SDK internals changed). Abandoned kills are logged,
          not retried.
        """
        logger.info(f"\nKilling all {self._noun.lower()}s...")
        states = [s for s in self._states.values() if self._handle_of(s)]
        if not states:
            logger.info(f"Killed 0 {self._noun.lower()}s")
            return
        workers = max(1, min(self.config.cleanup_concurrency, len(states)))
        overall = (len(states) / workers + 1) * self.config.request_timeout
        logger.info(f"  {len(states)} {self._noun.lower()}s, {workers} concurrent, {overall:.0f}s deadline")

        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {executor.submit(self._kill_tracked, s): s for s in states}
            done, not_done = wait(futures, timeout=overall)
            killed = sum(1 for f in done if f.result())
        finally:
            # Never join workers here: a kill blocked despite the pinned timeout
            # must not hang teardown (the report and subsequent trials proceed).
            executor.shutdown(wait=False, cancel_futures=True)
        for f in not_done:
            logger.warning(f"[{self._label(futures[f])}] kill unfinished after {overall:.0f}s, abandoning")
        logger.info(f"Killed {killed} of {len(states)} {self._noun.lower()}s")

    # ----------------------------------------------------- cube-specific methods
    def detect_from_file(self, ids_file: str) -> dict[int, CubeSandboxState]:
        """Detect sandboxes from an ID file: list running, match, attach.

        Reuses the base's per-item loop (:meth:`_detect_each`) for the
        attach->ready-check->status mapping; only the ID-file matching is
        cube-specific. CubeSandbox's ``Sandbox.list()`` returns a flat list of
        dicts (keyed ``sandboxID``), so matching is a set intersection.
        """
        logger.info(f"\n{'=' * 60}")
        logger.info("Detect Sandboxes from ID File")
        logger.info(f"{'=' * 60}")
        logger.info(f"  ID file: {ids_file}")

        if not os.path.exists(ids_file):
            raise FileNotFoundError(f"Sandbox IDs file not found: {ids_file}")

        target_ids: set[str] = set()
        with open(ids_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    target_ids.add(line)

        if not target_ids:
            logger.warning(f"  No IDs found in {ids_file}")
            return {}

        logger.info(f"  Target IDs from file: {len(target_ids)}")

        try:
            running = self._list_existing()
            logger.info(f"  Running sandboxes: {len(running)}")
        except Exception as e:
            logger.error(f"  Failed to list sandboxes: {e}")
            return {}

        if not running:
            logger.info("  No running sandboxes found")
            return {}

        # Match: only keep sandboxes in both sets.
        matched: list = []
        found_ids: set[str] = set()
        for listed in running:
            cube_id = self._external_id(listed)
            if cube_id in target_ids:
                matched.append(listed)
                found_ids.add(cube_id)

        not_found = target_ids - found_ids
        if not_found:
            logger.warning(f"  {len(not_found)} IDs not found or stopped")
            for sid in list(not_found)[:5]:
                logger.info(f"    - {sid}")
            if len(not_found) > 5:
                logger.info(f"    ... and {len(not_found) - 5} more")

        logger.info(f"  Matched sandboxes: {len(matched)}")
        if not matched:
            logger.info("  No matched sandboxes to benchmark")
            return {}

        return self._detect_each(matched)

    def check_alive(self, state: CubeSandboxState) -> bool:
        """Liveness via the control-plane ``get_info`` (no envd exec round-trip).

        CubeSandbox exposes ``get_info()`` (GET /sandboxes/:id) which raises
        ``SandboxNotFoundError`` (HTTP 404) when the sandbox is gone; a broad
        ``Exception`` catch mirrors the e2b manager's defensive liveness probe
        and treats any failure as not-alive (so a transient control-plane blip
        does not crash the kernel).
        """
        sbx = state.cube_sandbox
        if not sbx or not state.is_alive:
            return False
        try:
            sbx.get_info()
            return True
        except Exception:
            return False

    # ----------------------------------------------------- adapter alias (state)
    @property
    def sandbox_states(self) -> dict[int, CubeSandboxState]:
        """Adapter-facing alias for the base state registry."""
        return self._states
