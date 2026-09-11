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
import shlex
import subprocess
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


from env_provider._base import BackendSandboxStatus, BaseSandboxManager

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
            pin_err = self._pin_and_verify_fc()
            if pin_err:
                return {
                    "success": False,
                    "create_elapsed": state.creation_metrics.create_elapsed,
                    "error": f"firecracker cpu pin not verified: {pin_err}",
                    "template": resolved,
                }
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

    # ----------------------------------------------------- fc cpu pin (aenv cold-start perf)
    @staticmethod
    def _parse_cpulist(s: str) -> set[int]:
        """Parse a CPU list ('2,3' / '2-3' / '0,2-3') into a set of ints."""
        out: set[int] = set()
        for part in s.strip().split(","):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(part))
        return out

    @staticmethod
    def _mask_to_cpus(hexmask: str) -> set[int]:
        """taskset hex affinity mask -> set of cpu ints.

        taskset prints comma-separated 32-bit hex words, little-endian (first
        word = cpus 0-31). '0c' -> {2,3}; '0000000c,00000000' -> {2,3};
        '00000000,0000000c' -> {34,35}.
        """
        cpus: set[int] = set()
        for word_idx, w in enumerate(hexmask.replace(" ", "").split(",")):
            v = int(w, 16)
            for bit in range(32):
                if v & (1 << bit):
                    cpus.add(word_idx * 32 + bit)
        return cpus

    def _pin_and_verify_fc(self) -> str | None:
        """Pin the just-created Firecracker process to ``config.pin_cpus`` and verify.

        Cold-start create blocks until envd ready, so vCPU threads already
        exist at the moment this runs (immediate verify). ``taskset -a -pc``
        sets all current threads + future vCPU threads (inheritance); then
        ``taskset -ap`` reads every thread's mask back and asserts each equals
        the requested set. Returns None on verified success (logs pid for
        perf/devkit attach) or an error string on failure -- the caller returns
        ``success=False`` so ``--create-only`` aborts before any ``--detect``
        stress runs on an unpinned FC.

        ponytail: single-FC assumption (pgrep -fn = newest firecracker).
        Concurrent sandboxes need per-sandbox PID match by api-sock path.
        ``pin_cmd_prefix`` ("ssh user@host" remote, "" local) routes commands.
        """
        cpus = self.config.pin_cpus
        if not cpus:
            return None
        prefix = shlex.split(self.config.pin_cmd_prefix or "")
        where = self.config.pin_cmd_prefix or "local"
        want = self._parse_cpulist(cpus)

        def _run(args: list[str], timeout: float = 10) -> str:
            return subprocess.run(prefix + args, capture_output=True, text=True, timeout=timeout).stdout.strip()

        try:
            pid = _run(["pgrep", "-fn", "firecracker"])  # -f cmdline, -n newest
            if not pid.isdigit():
                return f"pgrep firecracker -> {pid[:80]!r}, no PID"
            _run(["taskset", "-a", "-pc", cpus, pid])  # -a: all threads + inherit
        except Exception as e:
            return f"pin cmd failed: {e}"

        # Verify: every thread's affinity mask == requested set. Poll briefly
        # in case vCPU threads are still coming up on a slow boot.
        deadline = time.time() + 10
        while True:
            try:
                out = _run(["taskset", "-ap", pid])
            except Exception as e:
                return f"verify read failed: {e}"
            masks = [line.rsplit(":", 1)[1].strip() for line in out.splitlines() if "affinity mask:" in line]
            sets = [self._mask_to_cpus(m) for m in masks]
            if sets and all(s == want for s in sets) and len(sets) >= 2:
                logger.info(
                    "[aenv-pin] PINNED OK pid=%s cpus=%s threads=%d (%s); " "attach perf: perf stat -p %s ...",
                    pid,
                    cpus,
                    len(sets),
                    where,
                    pid,
                )
                return None
            if time.time() >= deadline:
                got = ", ".join(sorted({str(s) for s in sets})) or "<none>"
                logger.error(
                    "[aenv-pin] PIN FAILED pid=%s want=%s got=[%s] threads=%d (%s); " "recheck: taskset -ap %s",
                    pid,
                    cpus,
                    got,
                    len(sets),
                    where,
                    pid,
                )
                return f"verify failed: want {cpus}, got [{got}] ({len(sets)} threads)"
            time.sleep(0.5)

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
