"""Overlaybd snapshot size scan (inode-dedup), ported from the reference
``spotbox_monitor.py::snapshot_dir_stats``.

The persisted-sandboxes dir holds one subdirectory per snapshot *generation*
(UUID-v7 names, sorted-by-name = creation order). Each generation hardlinks
the previous commit's layers as ``inherited-layers/``. To avoid double-counting
physical blocks, each inode's ``st_blocks * 512`` is attributed only to the
first generation that contains it (a ``seen`` set of ``(st_dev, st_ino)``).

Returns the newest generation's row plus the whole-tree cumulative disk bytes
(matches ``du``). Returns ``None`` when the dir is absent/unreadable.

**Incremental scan (no re-walk).** Generations are immutable (overlaybd
snapshots are COW hardlinks, append-only -- a pause adds one generation dir,
it never modifies an existing one). So the scan caches the ``seen``-set, the
per-generation unique-disk, and the set of already-scanned generation names
per sandbox dir. Each call after the first walks ONLY the generation dirs not
yet scanned -> O(1) per step instead of O(K), total O(steps) not O(steps^2).
The cache is module-level (one bench-core process = one run; the oversub
driver spawns a fresh subprocess per trial so it starts empty). Call
:func:`reset_snapshot_cache` between runs in the same process (tests).

Thread safety: the module lock guards only the cache dict get/create. The
walk mutates the per-sandbox ``_ScanState`` (``seen``/``cumulative``) WITHOUT
the lock -- safe because exactly one thread touches a given sandbox's state:
the single drain thread in :class:`bench_core.observability.snapshot_scanner.
SnapshotSizeScanner` (replay lifecycle/trajectory), or one sandbox thread per
dir in the no-scanner sync fallback (each sandbox scans its own, distinct
dir). No two threads ever share a ``_ScanState``.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()


class _ScanState:
    """Per-sandbox incremental scan cache. Mutated only by the scan call(s)
    for that one sandbox dir (see module docstring: single-access invariant)."""

    __slots__ = ("seen", "scanned", "cumulative", "newest")

    def __init__(self) -> None:
        self.seen: set[tuple[int, int]] = set()
        self.scanned: set[str] = set()  # generation dir names already walked
        self.cumulative: int = 0
        self.newest: dict | None = None


_SCAN_CACHE: dict[str, _ScanState] = {}


def reset_snapshot_cache() -> None:
    """Clear the incremental scan cache. Call between runs in the same
    process (each oversub trial is a fresh subprocess, so production never
    needs this; tests do, since pytest shares one process)."""
    with _CACHE_LOCK:
        _SCAN_CACHE.clear()


def scan_snapshot_sizes(sandbox_dir: Path) -> dict | None:
    """Stat one sandbox's snapshot tree with inode dedup (incremental).

    Returns ``{logical_bytes, disk_bytes, inherited_bytes, cumulative_bytes,
    generations, files}`` for the newest generation, or ``None`` on error.
    Walks only generation dirs not yet scanned for this sandbox (cached across
    calls); the first call primes the cache by walking all generations.
    """
    root = Path(sandbox_dir)
    key = str(root)
    try:
        gen_dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        # Dir absent (sandbox killed in trajectory mode) -> drop any stale
        # cache entry so it can't leak across the sandbox's lifetime.
        with _CACHE_LOCK:
            _SCAN_CACHE.pop(key, None)
        return None  # absent / unreadable -> caller (provider) skips
    if not gen_dirs:
        return None

    with _CACHE_LOCK:
        state = _SCAN_CACHE.get(key)
        if state is None:
            state = _ScanState()
            _SCAN_CACHE[key] = state

    # Walk ONLY generations not yet scanned (immutable; created in order).
    for gd in (g for g in gen_dirs if g.name not in state.scanned):
        apparent = 0
        unique_disk = 0
        inherited = 0
        n_files = 0
        for _dirpath, _dirs, files in os.walk(gd):
            for name in files:
                full = os.path.join(_dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                n_files += 1
                apparent += st.st_size
                ikey = (st.st_dev, st.st_ino)
                if ikey in state.seen:
                    inherited += st.st_size
                else:
                    state.seen.add(ikey)
                    # st_blocks unavailable on Windows; fall back to 0.
                    unique_disk += getattr(st, "st_blocks", 0) * 512
        state.scanned.add(gd.name)
        state.cumulative += unique_disk
        state.newest = {
            "apparent": apparent,
            "unique_disk": unique_disk,
            "inherited": inherited,
            "files": n_files,
        }

    if state.newest is None:
        return None
    newest = state.newest
    return {
        "logical_bytes": newest["apparent"],
        "disk_bytes": newest["unique_disk"],
        "inherited_bytes": newest["inherited"],
        "cumulative_bytes": state.cumulative,
        "generations": len(state.scanned),
        "files": newest["files"],
    }
