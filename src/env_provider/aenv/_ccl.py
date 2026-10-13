"""CCL CPU-affinity binding for AENV sandboxes (host-side firecracker pinning).

Ported from the reference ``replay_agent_no_lifecycle.py``: AENV maintains a
warm firecracker pool, so each firecracker exists *before* ``create()`` with
only a random ``--api-sock`` path (no sandbox_id in its cmdline). To pin a
sandbox's firecracker to a dedicated CCL vCPU set (so concurrent VMs do not
contend for the same physical cores), we scan ``/proc`` for firecracker
processes, connect to each one's MMDS unix socket and read ``instanceID``
(= sandbox_id) to map sandbox_id -> pid, then ``os.sched_setaffinity`` the
firecracker to its CCL.

Best-effort: every failure (non-Linux host, no root, pid unresolved,
setaffinity error) degrades to a warning + skip -- it never blocks an
already-successful create. Requires the bench to run as root on the AENV host
(MMDS socket access + ``sched_setaffinity`` on another process).

The reference is async (its replay runner is async); this is a synchronous
port because the repo's provider stack (``E2BProvider``/``SandboxManager``) is
fully synchronous (the E2B SDK ``Sandbox.create`` is a blocking call).

Cache: a module-level sandbox_id -> pid map, populated by the first scan
(``setdefault`` -- never overwritten). A cache hit short-circuits without a
rescan. This is correct only because binding is ``exec_only``-only: the
firecracker pid is stable for the whole run (no pause/resume), so a cached pid
never goes stale. Mirrors the reference's incremental-cache pattern.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from env_provider.e2b.config import Config

logger = logging.getLogger(__name__)

# Resolve retries / per-scan timeout. The firecracker may take a moment to be
# registered in the warm pool after create() returns; a few short scans cover
# that race without stalling the create path (best-effort).
CCL_PID_RESOLVE_ATTEMPTS = 8
CCL_PID_RESOLVE_TIMEOUT_SEC = 1.0


def get_ccl_cpu_affinity(sandbox_index: int, total_count: int, vcpus_per_ccl: int) -> list[int]:
    """Return the vCPU id list for the CCL this sandbox is pinned to.

    Round-robins: ``ccl = sandbox_index % total_count``; each CCL owns a
    contiguous block of ``vcpus_per_ccl`` vCPUs starting at ``ccl *
    vcpus_per_ccl``. Default topology (384 vCPU / 16 per CCL = 24 CCLs) comes
    from the config; override per host.
    """
    ccl = sandbox_index % total_count
    base = ccl * vcpus_per_ccl
    return list(range(base, base + vcpus_per_ccl))


def read_kernel_cpu_affinity(pid: int) -> str | None:
    """Read the kernel-reported CPU affinity mask for a process.

    Returns the ``Cpus_allowed_list`` field (e.g. ``"0-15"``) from
    ``/proc/<pid>/status``. This is the authoritative post-binding check:
    whatever ``os.sched_setaffinity`` requested, this is what the kernel
    actually honoured. ``None`` if the field/process is unavailable (e.g. not
    Linux, or pid already gone).
    """
    try:
        status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


class LocalFirecrackerPidResolver:
    """Map sandbox_id -> firecracker pid by reading MMDS instanceID locally.

    Sync port of the reference's async resolver. A cache hit short-circuits
    without a rescan -- correct because binding is ``exec_only``-only, where the
    firecracker pid is stable for the whole run (no pause/resume), so a cached
    pid never goes stale.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid_map: dict[str, int] = {}

    @staticmethod
    def _live_firecrackers() -> list[tuple[int, Path]]:
        """List (pid, api-sock-path) for every live firecracker on this host."""
        processes: list[tuple[int, Path]] = []
        try:
            proc_dirs = list(Path("/proc").iterdir())
        except OSError:
            return processes
        for proc_dir in proc_dirs:
            if not proc_dir.name.isdigit():
                continue
            try:
                arguments = [
                    part.decode("utf-8", errors="replace")
                    for part in (proc_dir / "cmdline").read_bytes().split(b"\0")
                    if part
                ]
                socket_index = arguments.index("--api-sock") + 1
                socket_path = Path(arguments[socket_index])
            except (OSError, ValueError, IndexError):
                continue
            if not arguments or Path(arguments[0]).name != "firecracker":
                continue
            if socket_path.is_socket():
                processes.append((int(proc_dir.name), socket_path))
        processes.sort(reverse=True)
        return processes

    @staticmethod
    def _mmds_instance_id(socket_path: Path, timeout_sec: float) -> str | None:
        """Read MMDS locally and return only the non-secret instanceID field."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout_sec)
                client.connect(str(socket_path))
                client.sendall(b"GET /mmds HTTP/1.1\r\n" b"Host: localhost\r\n" b"Connection: close\r\n\r\n")
                response = http.client.HTTPResponse(client)
                response.begin()
                if response.status != 200:
                    return None
                payload = json.loads(response.read())
        except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        instance_id = payload.get("instanceID")
        return instance_id if isinstance(instance_id, str) and instance_id else None

    def _scan_pids(self, timeout_sec: float) -> tuple[int, int]:
        """Populate the cache from a /proc + MMDS sweep. Returns (live, identified)."""
        processes = self._live_firecrackers()
        identified = 0
        for pid, socket_path in processes:
            instance_id = self._mmds_instance_id(socket_path, timeout_sec)
            if instance_id is None:
                continue
            identified += 1
            # setdefault: never overwrite. Correct because binding is exec_only-
            # only (the firecracker pid is stable for the whole run); a stale-pid
            # refresh has no motivating case once resume-rebind is gone.
            self._pid_map.setdefault(instance_id, pid)
        return len(processes), identified

    def resolve_firecracker_pid(self, sandbox_id: str, *, attempts: int, timeout_sec: float) -> int | None:
        """Resolve a sandbox_id to its firecracker pid, with retry+cache.

        A cache hit short-circuits without a rescan -- correct because binding
        is exec_only-only (the pid is stable for the whole run, so a cached pid
        never goes stale). On a miss, scan /proc + MMDS under the lock; a few
        short retries cover the just-created-sandbox-not-yet-in-warm-pool race.
        """
        last_scan = "scan did not run"
        for attempt in range(1, attempts + 1):
            # Fast path: cached -> return without scanning.
            cached = self._pid_map.get(sandbox_id)
            if cached is not None:
                logger.info(
                    "CCL pid resolve sandbox=%s attempt=%d cache=hit pid=%d",
                    sandbox_id,
                    attempt,
                    cached,
                )
                return cached
            if attempt == 1:
                # Let a just-created sandbox register in the warm pool first.
                time.sleep(0.05)
            with self._lock:
                cached = self._pid_map.get(sandbox_id)
                if cached is not None:
                    return cached
                live, identified = self._scan_pids(min(max(timeout_sec, 0.2), 3.0))
                last_scan = f"live_firecrackers={live} identified={identified}"
                cached = self._pid_map.get(sandbox_id)
                if cached is not None:
                    logger.info(
                        "CCL pid resolve sandbox=%s attempt=%d cache=hit-after-scan " "pid=%d %s",
                        sandbox_id,
                        attempt,
                        cached,
                        last_scan,
                    )
                    return cached
                logger.info(
                    "CCL pid resolve sandbox=%s attempt=%d/%d cache=miss %s",
                    sandbox_id,
                    attempt,
                    attempts,
                    last_scan,
                )
            if attempt < attempts:
                time.sleep(min(0.2 * attempt, 1.0))
        logger.warning(
            "CCL pid resolve sandbox=%s failed after=%d attempts last_scan=%s",
            sandbox_id,
            attempts,
            last_scan,
        )
        return None


# Module-level singleton: one bench-core process = one run; the cache persists
# across sandboxes (the first bind primes it for the rest). The oversub driver
# spawns a fresh subprocess per trial so it starts empty. Tests call
# :func:`reset_ccl_cache` between runs.
RESOLVER = LocalFirecrackerPidResolver()


def reset_ccl_cache() -> None:
    """Clear the pid-resolution cache. Call between runs in the same process
    (production never needs this -- each oversub trial is a fresh subprocess)."""
    RESOLVER._pid_map.clear()


def bind_ccl_affinity(sandbox_id: str, sandbox_index: int, config: Config) -> bool:
    """Pin one sandbox's firecracker to its CCL vCPU set. Best-effort.

    Returns True on a verified bind, False on any skip/failure. **Never
    raises** -- callers hook this after a successful create where a binding
    failure must not unwind an already-working sandbox.
    """
    if not hasattr(os, "sched_setaffinity"):
        logger.warning(
            "Skip CCL affinity for sandbox %s: os.sched_setaffinity unavailable " "(need Linux host)",
            sandbox_id,
        )
        return False
    if not sandbox_id:
        logger.warning("Skip CCL affinity: sandbox_id not set")
        return False
    try:
        firecracker_pid = RESOLVER.resolve_firecracker_pid(
            sandbox_id,
            attempts=CCL_PID_RESOLVE_ATTEMPTS,
            timeout_sec=CCL_PID_RESOLVE_TIMEOUT_SEC,
        )
    except Exception as resolve_err:  # noqa: BLE001 -- best-effort, never raise
        logger.warning(
            "CCL pid resolve failed for sandbox %s: %s: %r",
            sandbox_id,
            type(resolve_err).__name__,
            resolve_err,
        )
        return False
    if not firecracker_pid:
        logger.warning(
            "Skip CCL affinity for sandbox %s: could not resolve firecracker pid "
            "via MMDS scan (need root on AENV host)",
            sandbox_id,
        )
        return False
    cpu_list = get_ccl_cpu_affinity(sandbox_index, config.ccl_total_count, config.ccl_vcpus_per_ccl)
    ccl_idx = sandbox_index % config.ccl_total_count
    expected_mask = f"{cpu_list[0]}-{cpu_list[-1]}"
    logger.info(
        "CCL bind intent sandbox=%s sandbox_index=%d target_ccl=%d "
        "expected_vCPU_range=%s vcpu_count=%d firecracker_pid=%d",
        sandbox_id,
        sandbox_index,
        ccl_idx,
        expected_mask,
        len(cpu_list),
        firecracker_pid,
    )
    try:
        os.sched_setaffinity(firecracker_pid, set(cpu_list))
    except OSError as setaff_err:
        logger.warning(
            "CCL setaffinity failed sandbox=%s pid=%d: %s: %r",
            sandbox_id,
            firecracker_pid,
            type(setaff_err).__name__,
            setaff_err,
        )
        return False
    actual_mask = read_kernel_cpu_affinity(firecracker_pid)
    verified = actual_mask is not None and actual_mask == expected_mask
    logger.log(
        logging.INFO if verified else logging.WARNING,
        "CCL bind result sandbox=%s ccl=%d requested=%s kernel_cpulist=%s verified=%s",
        sandbox_id,
        ccl_idx,
        expected_mask,
        actual_mask,
        verified,
    )
    return verified
