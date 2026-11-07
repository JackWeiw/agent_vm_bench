"""SandboxSched sandbox manager (SDK seams over :class:`BaseSandboxManager`).

The workflow-agnostic create/detect/cleanup skeleton is inherited from
:class:`env_provider._base.BaseSandboxManager`; this module supplies only the
OpenSandbox SDK seams (``Sandbox.create`` / ``SandboxManager.list`` /
``Sandbox.connect`` / ``Sandbox.kill`` + the exec probe) and the two
sandboxsched-specific methods the base can't own: ``detect_from_file`` (ID-file
matching) and ``check_alive`` (exec-based liveness). Readiness probing is
delegated to :class:`env_provider._ready.ReadyChecker` via the base.

The OpenSandbox SDK's primary client is async (``opensandbox.Sandbox`` +
``opensandbox.manager.SandboxManager``, httpx.AsyncClient underneath -- async
transports are loop-bound per the SDK's own docs), while every kernel entry
point is synchronous. The bridge is a single private asyncio loop running on a
daemon thread (:class:`_EventLoopThread`): each SDK seam wraps its call in a
coroutine and blocks on ``asyncio.run_coroutine_threadsafe(...).result()``
(:meth:`SandboxManager.submit`). One loop for the manager's lifetime keeps
every SDK handle / transport bound to the same loop, which is exactly what the
SDK's ConnectionConfig transport-lifecycle note requires.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import timedelta
from typing import Any

try:
    from opensandbox import Sandbox
    from opensandbox.config import ConnectionConfig
    from opensandbox.exceptions import SandboxException
    from opensandbox.manager import SandboxManager as SdkSandboxManager
    from opensandbox.models.execd import RunCommandOpts
    from opensandbox.models.sandboxes import SandboxFilter, SandboxState as SdkSandboxState
except ImportError:  # pragma: no cover - SDK not installed in dev/test
    # Mock for development/testing without the opensandbox SDK. Mirrors the
    # e2b manager's mock shape so the base lifecycle + unit tests run without
    # the SDK. The real seams use the async SDK classes above (coroutine
    # methods -- the manager's submit() drives them on the bridge loop).

    logging.getLogger(__name__).warning(
        "opensandbox SDK not installed: sandboxsched falls back to the inline "
        "mock (sandbox ids come out as 'mock_sandbox_id', no real server "
        "calls). Install with: pip install 'agent-vm-bench[sandboxsched]'"
    )

    class _MockCommands:  # type: ignore[no-redef]
        async def run(self, command, *, opts=None, handlers=None):  # noqa: ARG002
            class _Result:
                exit_code = 0

                @property
                def stdout(self):
                    return ""

                @property
                def stderr(self):
                    return ""

            return _Result()

    class _MockSandbox:  # type: ignore[no-redef]
        id = "mock_sandbox_id"
        commands = _MockCommands()

        async def kill(self):
            pass

        async def pause(self):
            pass

        async def close(self):
            pass

    class Sandbox:  # type: ignore[no-redef]
        @staticmethod
        async def create(
            image=None,
            *,
            snapshot_id=None,
            timeout=None,
            env=None,
            metadata=None,
            resource=None,
            extensions=None,
            connection_config=None,
            skip_health_check=False,
            **_kwargs,
        ):  # noqa: ARG004
            return _MockSandbox()

        @staticmethod
        async def connect(sandbox_id, connection_config=None, skip_health_check=False, **_kwargs):  # noqa: ARG004
            m = _MockSandbox()
            m.id = sandbox_id
            return m

        @staticmethod
        async def resume(sandbox_id, connection_config=None, skip_health_check=False, **_kwargs):  # noqa: ARG004
            m = _MockSandbox()
            m.id = sandbox_id
            return m

    class SdkSandboxManager:  # type: ignore[no-redef]
        @staticmethod
        async def create(connection_config=None, **_kwargs):  # noqa: ARG004
            return SdkSandboxManager()

        async def list_sandbox_infos(self, filter):  # noqa: ARG002
            class _Page:
                class pagination:
                    has_next_page = False

                sandbox_infos = []

            return _Page()

        async def kill_sandbox(self, sandbox_id):  # noqa: ARG002
            pass

        async def close(self):
            pass

    class ConnectionConfig:  # type: ignore[no-redef]
        def __init__(self, api_key=None, domain=None, protocol="http", **kwargs):  # noqa: ARG002
            self.api_key = api_key
            self.domain = domain
            self.protocol = protocol

    class SandboxException(Exception):  # type: ignore[no-redef]
        pass

    class RunCommandOpts:  # type: ignore[no-redef]
        def __init__(self, *, timeout=None, **kwargs):  # noqa: ARG002
            self.timeout = timeout

    class SandboxFilter:  # type: ignore[no-redef]
        def __init__(self, *, states=None, page_size=None, page=None):  # noqa: ARG002
            self.states = states
            self.page = page

    class SdkSandboxState:  # type: ignore[no-redef]
        RUNNING = "Running"
        PAUSED = "Paused"
        TERMINATED = "Terminated"


from env_provider._base import BaseSandboxManager, BackendSandboxStatus

from .config import Config
from .schemas import SandboxState

logger = logging.getLogger(__name__)

# SandboxState.state values (capitalized) -> kept/not-running for listing. The
# SDK's SandboxState uses capitalized strings ("Running", "Paused", ...). We
# list non-terminated sandboxes so detect/cleanup sees live + paused ones.
_LIVE_STATES = (SdkSandboxState.RUNNING, SdkSandboxState.PAUSED)


class _EventLoopThread:
    """A private asyncio loop on a daemon thread for the SDK's async client.

    Started lazily on the first SDK call; lives for the process (daemon, so an
    exiting kernel never blocks on it). All SDK handles the manager creates are
    used on this one loop -- async transports are loop-bound, so sharing one
    loop across create/connect/exec/kill is what makes handle reuse safe.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        threading.Thread(target=self._spin, daemon=True, name="opensandbox-asyncio").start()
        self._ready.wait()

    def _spin(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: Any) -> Any:
        """Drive ``coro`` on the bridge loop; block until it finishes."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


class SandboxManager(BaseSandboxManager):
    """SandboxSched sandbox lifecycle: SDK seams over the shared lifecycle template.

    Shared stress params (total_count, create_batch_*, workflow_type) are read
    from ``kernel_config``; backend knobs (template, sandbox_group, create_timeout,
    connection) from ``config``. Readiness is delegated to :class:`ReadyChecker`
    via the base (provider-transparent ``_ready_config`` -- no sandboxsched
    readiness knobs).
    """

    _handle_attr = "sandbox_obj"
    _noun = "Sandbox"
    # Keep the original creation status for stats; don't overwrite with KILLED.
    _set_killed_on_cleanup = False

    def __init__(self, kernel_config, config: Config, stop_event: Event) -> None:
        super().__init__(kernel_config, stop_event)
        self.config = config
        self._connection_config: ConnectionConfig | None = None
        self._loop_thread: _EventLoopThread | None = None

    # --------------------------------------------------------- async bridge
    def submit(self, coro: Any) -> Any:
        """Run an SDK coroutine on the bridge loop; block until it completes.

        The one entry point every seam (and the adapter's exec/pause/resume/
        snapshot routing) uses to reach the async SDK from sync code. The loop
        thread starts on first use, so a manager that never talks to the SDK
        never pays for a thread.
        """
        if self._loop_thread is None:
            self._loop_thread = _EventLoopThread()
        return self._loop_thread.run(coro)

    # --------------------------------------------------------- connection
    def _connection(self) -> ConnectionConfig:
        """The shared ConnectionConfig (built lazily, cached).

        Built once from the Config's resolved api_key / domain / protocol. The
        SDK's ``with_transport_if_missing`` is applied inside Sandbox.create /
        connect / resume -- on the bridge loop -- so we pass the bare config
        and let the SDK own its transport.
        """
        if self._connection_config is None:
            self._connection_config = ConnectionConfig(
                api_key=self.config.resolved_api_key() or None,
                domain=self.config.domain,
                protocol=self.config.protocol,
            )
        return self._connection_config

    # --------------------------------------------------------- subclass seams
    def _new_state(self, index: int, *, batch_id: int = -1, external_id: str = "") -> SandboxState:
        return SandboxState(
            sandbox_id=index,
            batch_id=batch_id,
            workflow_type=self.kernel_config.workflow_type,
        )

    def _create_single(
        self, state: SandboxState, *, metadata: dict[str, str] | None = None, template: str | None = None
    ) -> dict:
        """Create one sandbox; preserve the handle in ``state.sandbox_obj``.

        Records submit->create timing; the base runs the readiness probe after
        this returns success and maps the result onto ``creation_metrics``.
        ``metadata`` is forwarded to the SDK for operator visibility (labels
        only, not an idempotency key). The OpenSandbox Server requires
        ``extensions.sandboxGroup`` -> a missing ``sandbox_group`` fails fast
        here with a clear error rather than a 400 at the SDK call.
        """
        state.creation_metrics.status = BackendSandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        resolved = template or self.config.template

        if not self.config.sandbox_group:
            err = "sandbox_group is required (extensions.sandboxGroup); set it under the sandboxsched: block"
            state.creation_metrics.create_ready_time = time.time()
            return {"success": False, "create_elapsed": 0.0, "error": err, "template": resolved}

        try:
            sbx = self.submit(
                Sandbox.create(
                    resolved,
                    timeout=timedelta(seconds=self.config.create_timeout),
                    env=None,
                    metadata=metadata,
                    resource=self.config.resource,
                    extensions={"sandboxGroup": self.config.sandbox_group},
                    connection_config=self._connection(),
                    skip_health_check=self.config.skip_health_check,
                )
            )
            state.sandbox_obj = sbx
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
                "error": _create_error_detail(e),
                "template": resolved,
            }

    async def _list_existing_async(self) -> list:
        """Async body of ``_list_existing`` (manager create + paginate + close).

        One coroutine so the admin manager and its transport are created, used,
        and closed on a single loop trip. ``list_sandbox_infos`` returns a
        ``PagedSandboxInfos`` with ``.sandbox_infos`` +
        ``.pagination.has_next_page``; iterate pages by bumping
        ``SandboxFilter.page`` until no next page. Filters to RUNNING + PAUSED
        so detect/cleanup sees live sandboxes (terminated ones are gone).
        """
        manager = await SdkSandboxManager.create(connection_config=self._connection())
        try:
            listed: list = []
            page = 1
            page_size = 100
            while True:
                flt = SandboxFilter(states=list(_LIVE_STATES), page_size=page_size, page=page)
                paged = await manager.list_sandbox_infos(flt)
                listed.extend(paged.sandbox_infos)
                if not paged.pagination.has_next_page:
                    break
                page += 1
            return listed
        finally:
            await manager.close()

    def _list_existing(self) -> list:
        return self.submit(self._list_existing_async())

    def _external_id(self, listed: Any) -> str:
        return listed.id if hasattr(listed, "id") else str(listed)

    def _attach(self, listed: Any) -> Any:
        return self.submit(
            Sandbox.connect(
                self._external_id(listed),
                connection_config=self._connection(),
                skip_health_check=self.config.skip_health_check,
            )
        )

    def _kill_one(self, state: SandboxState) -> None:
        self.submit(state.sandbox_obj.kill())

    def _exec_probe(self, handle: Any, cmd: str, timeout: int) -> tuple[int, str, str]:
        result = self.submit(handle.commands.run(cmd, opts=RunCommandOpts(timeout=timedelta(seconds=timeout))))
        return _execution_to_result(result)

    # ----------------------------------------------------- handle ops (adapter-facing)
    def pause_handle(self, state: SandboxState) -> None:
        """Server-side pause of the sandbox behind ``state`` (keeps the handle)."""
        self.submit(state.sandbox_obj.pause())

    def resume_handle(self, sandbox_id: str) -> Any:
        """Resume a paused sandbox by id; return the fresh async Sandbox handle.

        ``Sandbox.resume`` performs the server-side resume and re-resolves the
        execd endpoint (it may change across pause/resume), so the caller must
        swap the returned handle in.
        """
        return self.submit(
            Sandbox.resume(
                sandbox_id,
                connection_config=self._connection(),
                skip_health_check=self.config.skip_health_check,
            )
        )

    def snapshot_handle(self, state: SandboxState) -> Any:
        """Create a persistent snapshot from the sandbox behind ``state``."""
        return self.submit(state.sandbox_obj.create_snapshot())

    # ----------------------------------------------------- sandboxsched-specific
    def detect_from_file(self, ids_file: str) -> dict[int, SandboxState]:
        """Detect sandboxes from an ID file: list running, match, attach.

        Reuses the base's per-item loop (:meth:`_detect_each`) for the
        attach->ready-check->status mapping; only the ID-file matching is
        sandboxsched-specific (mirrors e2b's ``detect_from_file``).
        """
        logger.info(f"\n{'=' * 60}")
        logger.info("Detect Sandboxes from ID File")
        logger.info(f"{'=' * 60}")
        logger.info(f"  ID file: {ids_file}")

        if not os.path.exists(ids_file):
            raise FileNotFoundError(f"Sandbox IDs file not found: {ids_file}")

        target_ids: set[str] = set()
        id_to_template: dict[str, str | None] = {}
        with open(ids_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # "<id>\t<template>" (multi-template replay) or bare "<id>".
                if "\t" in line:
                    sid, _, template = line.partition("\t")
                    sid = sid.strip()
                    template = template.strip() or None
                else:
                    sid, template = line, None
                if sid:
                    target_ids.add(sid)
                    id_to_template[sid] = template

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
            sbx_id = self._external_id(listed)
            if sbx_id in target_ids:
                matched.append(listed)
                found_ids.add(sbx_id)

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

        return self._detect_each(matched, id_to_template=id_to_template)

    def check_alive(self, state: SandboxState) -> bool:
        """Liveness via an exec probe (sandboxsched has no cheaper reload)."""
        sbx = state.sandbox_obj
        if not sbx or not state.is_alive:
            return False
        try:
            result = self.submit(sbx.commands.run("echo alive", opts=RunCommandOpts(timeout=timedelta(seconds=10))))
            exit_code, _, _ = _execution_to_result(result)
            return exit_code == 0
        except Exception:
            return False

    # ----------------------------------------------------- adapter alias (state)
    @property
    def sandbox_states(self) -> dict[int, SandboxState]:
        """Adapter-facing alias for the base state registry."""
        return self._states


def _create_error_detail(e: Exception) -> str:
    """str(e) + the server's status/body when the SDK carries them.

    ``SandboxApiException`` (HTTP 4xx/5xx) holds ``response_body`` -- the
    server's actual complaint (e.g. why a create 500s); without it the log
    only shows "create sandbox failed: http 500" and the root cause stays
    invisible. Truncated to keep the metrics row readable.
    """
    parts = [str(e)]
    status = getattr(e, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    body = getattr(e, "response_body", None)
    if body:
        text = body.decode(errors="replace") if isinstance(body, bytes) else str(body)
        parts.append(f"body={text[:2000]}")
    return " | ".join(parts)


def _execution_to_result(execution: Any) -> tuple[int, str, str]:
    """Translate an OpenSandbox ``Execution`` -> (exit_code, stdout, stderr).

    The SDK's ``Commands.run`` returns an ``Execution`` whose stdout is the
    ``.text`` property (combined stdout log + result text) and whose stderr is
    the joined ``logs.stderr`` messages; ``exit_code`` is None when the command
    was interrupted/timed out server-side. Map a None exit_code to 124 (the
    ``timeout(1)`` convention) so the runner can bucket it as timed_out, and
    coalesce stderr from the ``error`` field when logs.stderr is empty.
    """
    stdout = getattr(execution, "text", "") or ""
    logs = getattr(execution, "logs", None)
    stderr = ""
    if logs is not None:
        stderr_msgs = getattr(logs, "stderr", None)
        if stderr_msgs:
            stderr = "\n".join(getattr(m, "text", "").rstrip("\n") for m in stderr_msgs)
    exit_code = getattr(execution, "exit_code", None)
    if exit_code is None:
        err = getattr(execution, "error", None)
        if err is not None and not stderr:
            stderr = f"{getattr(err, 'name', 'error')}: {getattr(err, 'value', '')}"
        exit_code = 124  # timed out / interrupted with no integer exit code
    return int(exit_code), stdout, stderr
