"""CCL CPU-affinity binding for AENV sandboxes (host-side firecracker pinning).

AENV maintains a warm firecracker pool, so each firecracker exists *before*
``create()`` with only a random ``--api-sock`` path (no sandbox_id in its
cmdline). To pin a sandbox's firecracker to a dedicated silicon domain (AMD
CCD / Kunpeng CL -- both mean "the CPUs sharing one L3 cache instance") we
scan ``/proc`` for firecracker processes, connect to each one's MMDS unix
socket and read ``instanceID`` (= sandbox_id) to map sandbox_id -> pid, then
bind the process.

Domains are detected from sysfs at runtime (``cache/index3/shared_cpu_list``
-- the same path on x86_64 and aarch64), never hardcoded: the legacy
contiguous-block arithmetic (``ccl_total_count`` x ``ccl_vcpus_per_ccl``)
survives only as the fallback when detection finds nothing. The arithmetic is
actively wrong on SMT hosts (Linux numbers physical cores 0..N-1 and their
siblings N..2N-1, so a "contiguous block of 16" spans TWO zen4/zen5 CCDs and
contains no sibling threads). See :func:`select_ccl_domains`.

The bind is one-shot and covers EVERY thread (:func:`bind_process_all_threads`):
main thread first (any thread spawned later inherits its mask -- clone()
copies the creating thread's affinity), then a ``/proc/<pid>/task`` sweep
repeated until no new tid appears. No polling is needed: the bind runs after
the sandbox is ready, when firecracker's thread set (1 VMM + N vCPU threads)
already exists and stays fixed for the run -- CCL binding only operates in
exec_only / exec_ephemeral, the modes without pause/resume respawns.

Best-effort: every failure (non-Linux host, no root, pid unresolved,
setaffinity error) degrades to a warning + skip -- it never blocks an
already-successful create. Requires the bench to run as root on the AENV host
(MMDS socket access + ``sched_setaffinity`` on another process).

Cache: a module-level sandbox_id -> pid map, populated by the first scan
(``setdefault`` -- never overwritten), plus a process-level topology cache. A
cache hit short-circuits without a rescan. Correct in both bound modes:
exec_only (the firecracker pid is stable for the whole run, no pause/resume)
and exec_ephemeral (each ``create_one`` yields a fresh e2b sandbox_id, so
``setdefault`` never collides and a killed sandbox's entry is never
re-resolved). Tests call :func:`reset_ccl_cache` between runs.
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

# Sysfs root for topology detection. Module attribute (not a default argument)
# so tests can monkeypatch it; production is always /sys.
_SYSFS_ROOT = Path("/sys")


# ---------------------------------------------------------------------------
# Topology detection (pure sysfs, architecture-independent: AMD CCD, Kunpeng
# CL and any other L3-domain machine expose the same paths).
# ---------------------------------------------------------------------------
def parse_cpu_list(cpu_list: str) -> set[int]:
    """Parse a kernel cpulist string (``'0-7,192-199'``) into a set of CPU ids."""
    cpus: set[int] = set()
    for part in cpu_list.split(","):
        part = part.strip()
        if not part:
            continue
        bounds = part.split("-", 1)
        try:
            lo = int(bounds[0])
            hi = int(bounds[1]) if len(bounds) > 1 else lo
        except ValueError:
            continue
        cpus.update(range(lo, hi + 1))
    return cpus


def format_cpu_list(cpus: set[int]) -> str:
    """Format CPU ids as a kernel-style cpulist (``{0,1,2,192}`` -> ``'0-2,192'``)."""
    parts: list[str] = []
    start = prev = -1
    for cpu in sorted(cpus) + [-2]:  # sentinel flushes the last range
        if start < 0:
            start = prev = cpu
        elif cpu == prev + 1:
            prev = cpu
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = cpu
    return ",".join(parts)


def _read_cpulist_file(path: Path) -> set[int] | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return parse_cpu_list(text) or None


def detect_l3_domains(sysfs_root: Path) -> list[set[int]]:
    """Detect L3 cache domains (AMD = CCD, Kunpeng = CL) from sysfs.

    Reads ``cache/index3/shared_cpu_list`` for every CPU and dedupes (all
    members of one domain report the same list). Sorted by first CPU id so the
    sandbox_index -> domain mapping is deterministic across runs.
    """
    domains: dict[int, set[int]] = {}
    try:
        entries = list((sysfs_root / "devices/system/cpu").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith("cpu") or not entry.name[3:].isdigit():
            continue
        cpus = _read_cpulist_file(entry / "cache" / "index3" / "shared_cpu_list")
        if cpus:
            domains.setdefault(min(cpus), cpus)
    return [domains[k] for k in sorted(domains)]


def detect_numa_domains(sysfs_root: Path) -> list[set[int]]:
    """Detect NUMA node CPU sets from sysfs, sorted by node id."""
    domains: dict[int, set[int]] = {}
    try:
        entries = list((sysfs_root / "devices/system/node").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith("node") or not entry.name[4:].isdigit():
            continue
        cpus = _read_cpulist_file(entry / "cpulist")
        if cpus:
            domains[int(entry.name[4:])] = cpus
    return [domains[k] for k in sorted(domains)]


def detect_socket_domains(sysfs_root: Path) -> list[set[int]]:
    """Detect socket CPU sets via ``topology/physical_package_id`` (sysfs has
    no socket cpulist file), sorted by package id."""
    packages: dict[int, set[int]] = {}
    try:
        entries = list((sysfs_root / "devices/system/cpu").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith("cpu") or not entry.name[3:].isdigit():
            continue
        try:
            pkg = int((entry / "topology" / "physical_package_id").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        packages.setdefault(pkg, set()).add(int(entry.name[3:]))
    return [packages[k] for k in sorted(packages)]


_DETECTORS = {"l3": detect_l3_domains, "numa": detect_numa_domains, "socket": detect_socket_domains}

# Process-level cache: one bench process = one host. Tests clear it via
# reset_ccl_cache(). Concurrent select() calls (exec_ephemeral create_one from
# multiple runner threads) may detect twice -- idempotent, so no lock.
_TOPOLOGY_CACHE: tuple[list[set[int]], str] | None = None


def get_ccl_cpu_affinity(sandbox_index: int, total_count: int, vcpus_per_ccl: int) -> list[int]:
    """Return the vCPU id list for the CCL this sandbox is pinned to.

    Legacy contiguous-block arithmetic, used only as the fallback when sysfs
    detection finds nothing (see :func:`select_ccl_domains`): ``ccl =
    sandbox_index % total_count``; each CCL owns ``vcpus_per_ccl`` consecutive
    vCPUs starting at ``ccl * vcpus_per_ccl``. Only correct on hosts whose
    silicon domains happen to be contiguous in Linux CPU numbering (no SMT).
    """
    ccl = sandbox_index % total_count
    base = ccl * vcpus_per_ccl
    return list(range(base, base + vcpus_per_ccl))


def _arithmetic_domains(config: Config) -> list[set[int]]:
    """Fallback domains: ``ccl_total_count`` contiguous blocks of
    ``ccl_vcpus_per_ccl`` CPUs (the legacy pre-detection behavior)."""
    total = max(1, int(getattr(config, "ccl_total_count", 24)))
    per = int(getattr(config, "ccl_vcpus_per_ccl", 16))
    return [set(get_ccl_cpu_affinity(i, total, per)) for i in range(total)]


def select_ccl_domains(config: Config, sysfs_root: Path | None = None) -> tuple[list[set[int]], str]:
    """Pick the binding domains per ``config.ccl_granularity``. Cached per process.

    - ``l3`` / ``numa`` / ``socket``: force that sysfs level; arithmetic
      fallback (warned) if detection finds nothing.
    - ``auto`` (default): L3 domains when every domain holds at least
      ``ccl_vcpus_per_ccl`` CPUs; else NUMA when present (the Kunpeng case: a
      4-CPU CL is too small to bind usefully, so take the coarser node --
      ``ccl_vcpus_per_ccl`` then acts as the minimum acceptable domain size);
      else L3 anyway; else NUMA; else arithmetic.
    - ``config``: skip detection, arithmetic blocks only (legacy behavior).

    Returns ``(domains sorted by first cpu, source label for logging)``; the
    label names the detection level actually used.
    """
    global _TOPOLOGY_CACHE
    if _TOPOLOGY_CACHE is not None:
        return _TOPOLOGY_CACHE
    root = _SYSFS_ROOT if sysfs_root is None else sysfs_root
    granularity = str(getattr(config, "ccl_granularity", "auto") or "auto").lower()
    min_size = int(getattr(config, "ccl_vcpus_per_ccl", 16))
    if granularity not in ("auto", "config") and granularity not in _DETECTORS:
        logger.warning("Unknown ccl_granularity %r; falling back to 'auto'.", granularity)
        granularity = "auto"
    domains: list[set[int]] = []
    source = "config-arithmetic"
    if granularity in _DETECTORS:
        domains = _DETECTORS[granularity](root)
        source = f"sysfs-{granularity}"
    elif granularity == "auto":
        l3 = detect_l3_domains(root)
        if l3 and min(len(d) for d in l3) >= min_size:
            domains, source = l3, "sysfs-l3(auto)"
        elif l3:
            smallest = min(len(d) for d in l3)
            numa = detect_numa_domains(root)
            if numa:
                domains = numa
                source = f"sysfs-numa(auto: l3 domains of {smallest} cpus < ccl_vcpus_per_ccl={min_size})"
            else:
                domains, source = l3, f"sysfs-l3(auto: {smallest} cpus/domain, no numa fallback)"
        else:
            numa = detect_numa_domains(root)
            if numa:
                domains, source = numa, "sysfs-numa(auto: no l3 domains)"
    if not domains:
        if granularity != "config":
            logger.warning(
                "CCL detection (%s) found no domains under %s; falling back to config arithmetic "
                "(%d CCLs x %d vCPUs -- only correct on no-SMT hosts).",
                granularity,
                root,
                getattr(config, "ccl_total_count", 24),
                min_size,
            )
        domains = _arithmetic_domains(config)
        source = "config-arithmetic"
    logger.info(
        "CCL topology source=%s domains=%d cpus_per_domain(min/max)=%d/%d sample=%s",
        source,
        len(domains),
        min(map(len, domains)),
        max(map(len, domains)),
        format_cpu_list(domains[0]),
    )
    _TOPOLOGY_CACHE = (domains, source)
    return _TOPOLOGY_CACHE


def _task_tids(pid: int) -> list[int]:
    """Every thread id of a process (``/proc/<pid>/task/*``, includes pid itself)."""
    try:
        entries = (Path("/proc") / str(pid) / "task").iterdir()
        return sorted(int(e.name) for e in entries if e.name.isdigit())
    except OSError:
        return [pid]


def bind_process_all_threads(pid: int, cpus: set[int], max_passes: int = 3) -> tuple[bool, str]:
    """One-shot pin of EVERY thread of ``pid`` to ``cpus`` (no polling).

    Order matters: the main thread is bound first, so any thread spawned
    afterwards inherits the mask (Linux clone() copies the creating thread's
    affinity). Then every ``/proc/<pid>/task`` tid is swept, repeating until a
    re-list shows no unbund tid (normally one pass -- firecracker's thread set
    is fixed after boot: 1 VMM + N vCPU threads).

    Verification compares every live thread against the kernel-honoured
    effective mask of the main thread (``sched_getaffinity`` right after the
    set -- the kernel may narrow the request to online/cgroup-allowed CPUs; a
    narrowing is reported in the detail string, not failed). Returns
    ``(verified, detail)``.
    """
    try:
        os.sched_setaffinity(pid, cpus)
    except OSError as err:
        return False, f"setaffinity(main tid={pid}) failed: {err!r}"
    try:
        effective = os.sched_getaffinity(pid)
    except OSError:
        effective = set(cpus)
    bound = {pid}
    for _ in range(max_passes):
        for tid in _task_tids(pid):
            if tid in bound:
                continue
            try:
                os.sched_setaffinity(tid, cpus)
                bound.add(tid)
            except OSError:
                pass  # thread exited between listing and binding
        if set(_task_tids(pid)) <= bound:
            break
    mismatched = []
    for tid in _task_tids(pid):
        try:
            if os.sched_getaffinity(tid) != effective:
                mismatched.append(tid)
        except OSError:
            pass  # thread exited; nothing left to verify
    detail = f"threads={len(bound)}"
    if effective != set(cpus):
        detail += f" kernel_narrowed_to={format_cpu_list(effective)}"
    if mismatched:
        detail += f" mismatched_tids={mismatched}"
    return not mismatched, detail


class LocalFirecrackerPidResolver:
    """Map sandbox_id -> firecracker pid by reading MMDS instanceID locally.

    Sync port of the reference's async resolver. A cache hit short-circuits
    without a rescan -- correct in exec_only (the firecracker pid is stable for
    the whole run, no pause/resume) and exec_ephemeral (each ``create_one``
    yields a fresh sandbox_id, so the cache only grows and a killed sandbox's
    entry is never re-resolved).
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
            # setdefault: never overwrite. Correct in exec_only (the pid is
            # stable for the whole run) and exec_ephemeral (each create_one
            # yields a fresh sandbox_id, so this never collides with a prior
            # entry; a killed sandbox's id is never re-resolved).
            self._pid_map.setdefault(instance_id, pid)
        return len(processes), identified

    def resolve_firecracker_pid(self, sandbox_id: str, *, attempts: int, timeout_sec: float) -> int | None:
        """Resolve a sandbox_id to its firecracker pid, with retry+cache.

        A cache hit short-circuits without a rescan -- correct in exec_only (the
        pid is stable for the whole run) and exec_ephemeral (each ``create_one``
        yields a fresh sandbox_id, so the cache only grows). On a miss, scan
        /proc + MMDS under the lock; a few short retries cover the
        just-created-sandbox-not-yet-in-warm-pool race.
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
    """Clear the pid-resolution and topology caches. Call between runs in the
    same process (production never needs this -- each oversub trial is a fresh
    subprocess)."""
    global _TOPOLOGY_CACHE
    RESOLVER._pid_map.clear()
    _TOPOLOGY_CACHE = None


def bind_ccl_affinity(sandbox_id: str, sandbox_index: int, config: Config) -> bool:
    """Pin one sandbox's firecracker -- every thread -- to its CCL domain.

    Best-effort. Returns True on a verified all-thread bind, False on any
    skip/failure. **Never raises** -- callers hook this after a successful
    create where a binding failure must not unwind an already-working sandbox.

    The domain list comes from :func:`select_ccl_domains` (sysfs-detected
    L3/NUMA/socket domains; config arithmetic as fallback); the sandbox maps
    round-robin to ``domains[sandbox_index % len(domains)]``. One-shot, no
    polling: the bind runs after the sandbox is ready, so firecracker's thread
    set already exists and the task sweep covers it; later-spawned threads
    inherit the pinned main thread's mask.
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
    domains, source = select_ccl_domains(config)
    domain_idx = sandbox_index % len(domains)
    domain = domains[domain_idx]
    logger.info(
        "CCL bind intent sandbox=%s sandbox_index=%d domain=%d/%d source=%s cpus=%s firecracker_pid=%d",
        sandbox_id,
        sandbox_index,
        domain_idx,
        len(domains),
        source,
        format_cpu_list(domain),
        firecracker_pid,
    )
    verified, detail = bind_process_all_threads(firecracker_pid, domain)
    logger.log(
        logging.INFO if verified else logging.WARNING,
        "CCL bind result sandbox=%s domain=%d source=%s requested=%s %s verified=%s",
        sandbox_id,
        domain_idx,
        source,
        format_cpu_list(domain),
        detail,
        verified,
    )
    return verified
