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

The SDK covers the control plane only (create / connect / pause / resume /
kill / list / endpoint resolution). E2B-mode sandboxes serve the DATA plane
through envd -- Connect RPC on port 49983 -- on which the SDK's execd REST
contract (``commands.run`` -> ``POST /command``) does not exist and 404s by
design. Command execution therefore goes through the vendored
:mod:`env_provider.sandboxsched.envd_client` (the upstream reference client
from the opensandbox repo's ``examples/flux-sandbox-e2b/``), addressed either
through the server proxy or the sandbox CNI IP per ``use_server_proxy``.
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
    from opensandbox.manager import SandboxManager as SdkSandboxManager
    from opensandbox.models.sandboxes import (
        SandboxFilter,
        SnapshotFilter,
        SandboxState as SdkSandboxState,
    )
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
from .envd_client import ENVD_PORT, EnvdClient
from .schemas import SandboxState


class _UserEnvdClient(EnvdClient):
    """EnvdClient that selects the executing OS user via Basic auth.

    envd picks the OS user from the Basic auth username (the password is
    never checked -- packages/envd/internal/permissions/authenticate.go).
    The server proxy strips the Authorization header, so this is effective
    in direct mode only; e2b/aenv parity (root execution) needs it.
    """

    def _request(self, method, path, *, data=None, headers=None, timeout=None):
        import base64

        merged = dict(headers or {})
        if self.user:
            token = base64.b64encode(f"{self.user}:".encode()).decode()
            merged["Authorization"] = f"Basic {token}"
        return super()._request(method, path, data=data, headers=merged, timeout=timeout)

logger = logging.getLogger(__name__)

# The SDK logs its own INFO chatter ("Creating sandbox with startup source:
# ...", "Connecting to sandbox ...") which duplicates the base lifecycle
# logs line-for-line at create time; keep warnings and errors only.
logging.getLogger("opensandbox").setLevel(logging.WARNING)

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
        # sandbox_id -> envd base URL; resolved on first use, dropped on
        # resume (the endpoint may change across pause/resume) and kill.
        self._envd_urls: dict[str, str] = {}

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
                use_server_proxy=self.config.use_server_proxy,
            )
            # One-shot fingerprint so a failing run shows exactly which
            # server/template/credentials the provider resolved -- the top
            # suspects when the same create succeeds from a hand-written
            # script but fails under the kernel.
            logger.info(
                "sandboxsched connect fingerprint: %s://%s | api_key=%s | use_server_proxy=%s",
                self.config.protocol,
                self.config.domain,
                "set" if self.config.resolved_api_key() else "none",
                self.config.use_server_proxy,
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
                    ready_timeout=timedelta(seconds=self.config.ready_timeout),
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
                connect_timeout=timedelta(seconds=self.config.ready_timeout),
            )
        )

    def _kill_one(self, state: SandboxState) -> None:
        self._envd_urls.pop(str(getattr(state.sandbox_obj, "id", "")), None)
        self.submit(state.sandbox_obj.kill())

    def _envd_client_for(self, handle: Any) -> EnvdClient:
        """An envd client for the sandbox behind ``handle`` (URL cached per id).

        The URL is resolved once per sandbox through the control plane; the
        envd client itself is plain blocking urllib -- no bridge needed.
        """
        sandbox_id = str(handle.id)
        url = self._envd_urls.get(sandbox_id)
        if url is None:
            endpoint = self.submit(handle.get_endpoint(ENVD_PORT))
            url = getattr(endpoint, "endpoint", "") or ""
            if not url:
                raise RuntimeError(f"sandbox {sandbox_id} has no published endpoint yet")
            self._envd_urls[sandbox_id] = url
        return _UserEnvdClient(url, user=self.config.envd_user, timeout=60.0)

    def _exec_probe(self, handle: Any, cmd: str, timeout: int) -> tuple[int, str, str]:
        result = self._envd_client_for(handle).run(cmd, timeout=float(timeout))
        return (
            int(result.exit_code or 0),
            result.stdout.decode("utf-8", "replace"),
            result.stderr.decode("utf-8", "replace"),
        )

    def envd_exec(
        self,
        state: SandboxState,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run ``command`` through the envd data plane (adapter-facing exec).

        ``cwd``/``env`` are natively supported by envd's Start request (the
        SDK's RunCommandOpts detour is gone with it).
        """
        if state.sandbox_obj is None:
            raise RuntimeError(f"No sandboxsched handle for sandbox index {state.sandbox_id}")
        result = self._envd_client_for(state.sandbox_obj).run(
            command,
            envs=env,
            cwd=cwd,
            timeout=float(timeout) if timeout is not None else None,
        )
        return (
            int(result.exit_code or 0),
            result.stdout.decode("utf-8", "replace"),
            result.stderr.decode("utf-8", "replace"),
        )

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
        self._envd_urls.pop(str(sandbox_id), None)  # endpoint may change across pause/resume
        return self.submit(
            Sandbox.resume(
                sandbox_id,
                connection_config=self._connection(),
                skip_health_check=self.config.skip_health_check,
                resume_timeout=timedelta(seconds=self.config.ready_timeout),
            )
        )

    async def _latest_snapshot_async(self, sandbox_id: str) -> Any | None:
        manager = await SdkSandboxManager.create(connection_config=self._connection())
        try:
            paged = await manager.list_snapshots(
                SnapshotFilter(sandbox_id=sandbox_id, page_size=1, page=1)
            )
            infos = paged.snapshot_infos
            return infos[0] if infos else None
        finally:
            await manager.close()

    def latest_snapshot(self, state: SandboxState) -> Any | None:
        """The sandbox's most recent server-side snapshot (None if absent).

        Pausing a sandbox produces a server-side snapshot (the next resume
        rebuilds from it), so this reports the pause-generated snapshot for
        the lifecycle snapshot-size series. Snapshots cannot be CREATED from
        a paused sandbox (server rejects: "snapshot can only be created from
        a running sandbox"), which is why this queries instead of creating.
        """
        sandbox_id = str(getattr(state.sandbox_obj, "id", "") or "")
        if not sandbox_id:
            return None
        return self.submit(self._latest_snapshot_async(sandbox_id))

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
        """Liveness via the envd health probe (``GET /health`` -> 204)."""
        sbx = state.sandbox_obj
        if not sbx or not state.is_alive:
            return False
        try:
            return self._envd_client_for(sbx).health(timeout=5.0)
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

