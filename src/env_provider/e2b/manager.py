"""E2B sandbox manager (SDK seams over :class:`BaseSandboxManager`).

The workflow-agnostic create/detect/cleanup skeleton is inherited from
:class:`env_provider._base.BaseSandboxManager`; this module supplies only the
e2b SDK seams (Sandbox.create / list / connect / kill + the exec probe) and the
two e2b-specific methods the base can't own: ``detect_from_file`` (ID-file
matching) and ``check_alive`` (exec-based liveness). Readiness probing is
delegated to :class:`env_provider._ready.ReadyChecker` via the base.
"""

from __future__ import annotations

import logging
import os
import time
from threading import Event
from typing import Any

try:
    from e2b import Sandbox
except ImportError:
    # Mock for development/testing without E2B SDK
    class Sandbox:
        @staticmethod
        def create(template, timeout=86400, envs=None, metadata=None):  # noqa: ARG001 - mock ignores args
            class MockSandbox:
                sandbox_id = "mock_sandbox_id"

                class MockCommands:
                    def run(self, cmd, timeout=60, user="root"):
                        class Result:
                            exit_code = 0
                            stdout = ""

                        return Result()

                commands = MockCommands()

                def kill(self):
                    pass

            return MockSandbox()

        @staticmethod
        def kill(sandbox_id):
            pass

        @staticmethod
        def list():
            """Mock list() for testing - returns Paginator-like object"""

            class MockPaginator:
                has_next = True
                _items = [type("MockListedSandbox", (), {"sandbox_id": "mock_sandbox_1"})()]

                def next_items(self):
                    if self.has_next:
                        self.has_next = False
                        return self._items
                    return []

            return MockPaginator()

        @staticmethod
        def connect(sandbox_id):
            """Mock connect() for testing"""

            class MockSandbox:
                sandbox_id = sandbox_id

                class MockCommands:
                    def run(self, cmd, timeout=60, user="root"):
                        class Result:
                            exit_code = 0
                            stdout = ""

                        return Result()

                commands = MockCommands()

            return MockSandbox()


from env_provider._base import BaseSandboxManager, BackendSandboxStatus

from .config import Config, numa_node_for_index
from .schemas import SandboxState

logger = logging.getLogger(__name__)


class SandboxManager(BaseSandboxManager):
    """E2B sandbox lifecycle: SDK seams over the shared lifecycle template.

    Shared stress params (total_count, create_batch_*, workflow_type) are read
    from ``kernel_config``; backend knobs (template, create_timeout, numa_bind)
    from ``e2b_config``. Readiness is delegated to :class:`ReadyChecker` via the
    base (provider-transparent ``_ready_config`` -- no e2b readiness knobs).
    """

    _handle_attr = "sandbox_obj"
    _noun = "Sandbox"
    # Keep the original creation status for stats; don't overwrite with KILLED.
    _set_killed_on_cleanup = False

    def __init__(self, kernel_config, e2b_config: Config, stop_event: Event) -> None:
        super().__init__(kernel_config, stop_event)
        self.config = e2b_config

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

        Records submit→create timing; the base runs the readiness probe after
        this returns success and maps the result onto ``creation_metrics``.
        ``metadata`` is forwarded to the SDK for operator visibility (labels
        only, not an idempotency key -- spec G3 deferred).
        """
        state.creation_metrics.status = BackendSandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        resolved = template or self.config.template

        try:
            # Build envs with NUMA binding if configured. numa_bind is a
            # normalized list of nodes (or None); round-robin across them by
            # sandbox index so sandboxes spread evenly.
            numa_node = numa_node_for_index(state.sandbox_id - 1, self.config.numa_bind)
            envs: dict[str, str] = {}
            if numa_node is not None:
                envs["FC_BIND"] = str(numa_node)

            sbx = Sandbox.create(
                resolved,
                timeout=self.config.create_timeout,
                envs=envs if envs else None,
                metadata=metadata,
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
                "error": str(e),
                "template": resolved,
            }

    def _list_existing(self) -> list:
        """List running sandboxes (flatten the E2B paginator)."""
        paginator = Sandbox.list()
        listed: list = []
        while paginator.has_next:
            listed.extend(paginator.next_items())
        return listed

    def _external_id(self, listed: Any) -> str:
        return listed.sandbox_id if hasattr(listed, "sandbox_id") else str(listed)

    def _attach(self, listed: Any) -> Any:
        return Sandbox.connect(self._external_id(listed))

    def _kill_one(self, state: SandboxState) -> None:
        state.sandbox_obj.kill()

    def _exec_probe(self, handle: Any, cmd: str, timeout: int) -> tuple[int, str, str]:
        result = handle.commands.run(cmd, user="root", timeout=timeout)
        return result.exit_code, result.stdout, result.stderr

    # ----------------------------------------------------- e2b-specific methods
    def detect_from_file(self, ids_file: str) -> dict[int, SandboxState]:
        """Detect sandboxes from an ID file: list running, match, attach.

        Reuses the base's per-item loop (:meth:`_detect_each`) for the
        attach→ready-check→status mapping; only the ID-file matching is e2b.
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
            e2b_id = self._external_id(listed)
            if e2b_id in target_ids:
                matched.append(listed)
                found_ids.add(e2b_id)

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

    def check_alive(self, state: SandboxState) -> bool:
        """Liveness via an exec probe (e2b has no container.reload)."""
        sbx = state.sandbox_obj
        if not sbx or not state.is_alive:
            return False
        try:
            result = sbx.commands.run("echo alive", timeout=10, user="root")
            return result.exit_code == 0
        except Exception:
            return False

    # ----------------------------------------------------- adapter alias (state)
    @property
    def sandbox_states(self) -> dict[int, SandboxState]:
        """Adapter-facing alias for the base state registry."""
        return self._states
