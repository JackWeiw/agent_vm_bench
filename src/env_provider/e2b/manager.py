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
from contextlib import nullcontext
from datetime import timezone
from threading import Event, Lock
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
        # Serializes create+pin when pin_cpus is set (single-FC cold-start perf
        # mode). The MAC-based lookup in _pin_and_verify_fc is per-sandbox, so
        # this is no longer needed for PID correctness; kept to avoid concurrent
        # pin-mode creates racing on the same host CPUs. Inert otherwise.
        self._pin_lock = Lock()

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

        When ``pin_cpus`` is set, create+pin run under ``_pin_lock``.
        ``_pin_and_verify_fc`` locates the FC by the guest's eth0 MAC (not by
        spawn order), so the lock is only there to keep pin-mode creates from
        racing on the same host CPUs; it is not required for PID correctness.
        Serializing is acceptable here -- pin_cpus is single-FC cold-start perf
        mode, not oversub concurrency (which leaves pin_cpus unset).
        """
        state.creation_metrics.status = BackendSandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        resolved = template or self.config.template
        pin_cpus = self.config.pin_cpus
        lock = self._pin_lock if pin_cpus else nullcontext()

        try:
            with lock:
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
                pin_err = self._pin_and_verify_fc(sbx) if pin_cpus else None
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

        taskset prints comma-separated hex words (one per ``unsigned long`` =
        64 bits on Linux), first word = cpus 0..w-1, each later word continuing
        the bit range. Words are zero-padded by taskset but not to a fixed
        width, so a word's width is its own digit count x 4 and the offset for
        word i is the cumulative bit count of words 0..i-1. 'c' -> {2,3};
        '0000000c,00000000' -> {2,3}; '00000000,0000000c' -> {34,35};
        '0000000000000000,0000000300000000' -> {96,97}.
        """
        cpus: set[int] = set()
        offset = 0
        for word in hexmask.replace(" ", "").split(","):
            if not word:
                continue
            value = int(word, 16)
            width = 4 * len(word)
            for bit in range(width):
                if value & (1 << bit):
                    cpus.add(offset + bit)
            offset += width
        return cpus

    def _pin_and_verify_fc(self, sbx: Any) -> str | None:
        """Pin THIS sandbox's Firecracker process to ``config.pin_cpus`` and verify.

        aenv keeps a warm Firecracker pool: creating a sandbox claims an
        already-running FC while the pool refills by spawning a new one. A
        ``pgrep`` for the firecracker binary therefore returns the refill
        process, not the sandbox's FC, and the pin silently lands on the wrong
        PID. Instead we read the guest's eth0 MAC through the SDK and match it
        against the ``tap0`` neighbour table inside each FC network namespace;
        only a PID that also has an ``fc_vcpu`` thread is accepted. Sandboxes
        created from the same template can share the snapshot's guest MAC, so
        when several FCs match we pick the one whose work-dir claim time
        (``agentenv_volume_slot_0.img`` mtime) is closest to the sandbox's
        ``started_at``; a single match is accepted without that window because
        a warm-pool FC can predate the sandbox by minutes.

        Cold-start create blocks until envd ready, so vCPU threads already
        exist at the moment this runs. The main thread is pinned with
        ``taskset -pc`` and each existing thread individually (new threads
        inherit); then ``taskset -ap`` reads every thread's mask back and
        asserts each equals the requested set and at least one ``fc_vcpu``
        thread exists. Returns None on verified success (logs pid for
        perf/devkit attach) or an error string on failure -- the caller returns
        ``success=False`` so ``--create-only`` aborts before any ``--detect``
        stress runs on an unpinned FC.

        ``pin_cmd_prefix`` ("ssh user@host" remote, "" local) routes the
        host-side lookup / taskset commands.
        """
        cpus = self.config.pin_cpus
        if not cpus:
            return None
        prefix = shlex.split(self.config.pin_cmd_prefix or "")
        where = self.config.pin_cmd_prefix or "local"
        want = self._parse_cpulist(cpus)

        def _run(args: list[str], timeout: float = 10) -> str:
            proc = subprocess.run(prefix + args, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
            return proc.stdout.strip()

        # guest eth0 MAC (unique per sandbox, independent of FC spawn order)
        try:
            result = sbx.commands.run("cat /sys/class/net/eth0/address", user="root", timeout=10)
            mac = (getattr(result, "stdout", "") or "").strip().lower()
        except Exception as e:
            return f"read guest MAC failed: {e}"
        if not mac:
            return "read guest MAC failed: empty stdout"

        # locate the FC PID whose netns tap0 neighbour table has this MAC.
        try:
            info = sbx.get_info()
            started_at = info.started_at
            if started_at.tzinfo is None:
                # AENV may send an offset-less timestamp; treat it as UTC so
                # .timestamp() does not shift it by the collector's local zone.
                started_at = started_at.replace(tzinfo=timezone.utc)
            started_epoch = started_at.timestamp()
        except Exception:
            started_epoch = time.time()

        lookup = (
            "MAC=" + shlex.quote(mac) + "\n"
            "for p in $(pgrep -f firecracker); do\n"
            '  [ "$(basename "$(readlink -f /proc/$p/exe)")" = firecracker ] || continue\n'
            "  tr '\\0' ' ' < /proc/$p/cmdline | grep -q -- --api-sock || continue\n"
            '  nsenter -t "$p" -n ip neigh show dev tap0 2>/dev/null '
            '| grep -qi "lladdr $MAC" || continue\n'
            "  grep -q 'fc_vcpu' /proc/$p/task/*/comm 2>/dev/null || continue\n"
            "  m=$(find -L /proc/$p/cwd/ -maxdepth 1 "
            "-name agentenv_volume_slot_0.img -printf '%T@\\n' 2>/dev/null | head -1)\n"
            '  [ -z "$m" ] && m=$(stat -L -c %Y /proc/$p/cwd 2>/dev/null)\n'
            '  [ -n "$m" ] && echo "$p $m"\n'
            "done\n"
        )
        try:
            out = _run(["sh", "-c", lookup])
        except Exception as e:
            return f"locate FC by guest MAC {mac} failed: {e}"
        candidates: list[tuple[int, float]] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                try:
                    candidates.append((int(parts[0]), float(parts[1])))
                except ValueError:
                    pass
        if not candidates:
            return f"no FC process with fc_vcpu thread for guest MAC {mac}"
        pid, mtime = min(candidates, key=lambda x: abs(x[1] - started_epoch))
        pid = str(pid)
        diff = abs(mtime - started_epoch)
        if len(candidates) > 1 and diff > 30:
            return (
                f"FC candidates {candidates} for guest MAC {mac} but none matches "
                f"sandbox start {started_epoch:.3f} (closest pid {pid}, diff {diff:.3f}s)"
            )
        logger.info(
            "[aenv-pin] located pid=%s mac=%s via work-dir mtime (diff=%.3fs, candidates=%d)",
            pid,
            mac,
            diff,
            len(candidates),
        )

        # pin all current + future threads, then verify every thread's mask.
        apply_script = (
            "taskset -pc " + shlex.quote(cpus) + " " + shlex.quote(pid) + " >/dev/null || exit 1\n"
            "for t in /proc/" + shlex.quote(pid) + "/task/*; do\n"
            "  taskset -pc " + shlex.quote(cpus) + ' "${t##*/}" >/dev/null 2>&1 || true\n'
            "done\n"
        )

        def _apply_pin() -> None:
            _run(["sh", "-c", apply_script])

        last_err: Exception | None = None
        for _ in range(5):
            try:
                _apply_pin()
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        if last_err is not None:
            return f"taskset failed for pid {pid} (mac {mac}): {last_err}"

        deadline = time.time() + 10
        while True:
            try:
                out = _run(["taskset", "-ap", pid])
                vcpu_count = int(
                    _run(["sh", "-c", f"grep -l fc_vcpu /proc/{pid}/task/*/comm 2>/dev/null | wc -l"]) or "0"
                )
            except Exception as e:
                if time.time() >= deadline:
                    return f"verify read failed for pid {pid} (mac {mac}): {e}"
                time.sleep(0.5)
                continue
            masks = [line.rsplit(":", 1)[1].strip() for line in out.splitlines() if "affinity mask:" in line]
            sets = [self._mask_to_cpus(m) for m in masks]
            if sets and vcpu_count >= 1 and all(s == want for s in sets):
                logger.info(
                    "[aenv-pin] PINNED OK pid=%s mac=%s cpus=%s threads=%d vcpu=%d (%s); "
                    "attach perf: perf stat -p %s ...",
                    pid,
                    mac,
                    cpus,
                    len(sets),
                    vcpu_count,
                    where,
                    pid,
                )
                return None
            if time.time() >= deadline:
                got = ", ".join(sorted({str(s) for s in sets})) or "<none>"
                logger.error(
                    "[aenv-pin] PIN FAILED pid=%s mac=%s want=%s got=[%s] threads=%d vcpu=%d (%s); "
                    "recheck: taskset -ap %s",
                    pid,
                    mac,
                    cpus,
                    got,
                    len(sets),
                    vcpu_count,
                    where,
                    pid,
                )
                return (
                    f"verify failed: pid {pid} mac {mac} want {cpus}, got [{got}] "
                    f"({len(sets)} threads, {vcpu_count} vcpu)"
                )
            # A new vCPU thread may have appeared after the initial taskset;
            # re-apply before the next read.
            try:
                _apply_pin()
            except Exception:
                pass
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

        return self._detect_each(matched, id_to_template=id_to_template)

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
