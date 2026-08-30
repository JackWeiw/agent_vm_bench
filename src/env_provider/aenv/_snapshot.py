"""Overlaybd snapshot size scan (inode-dedup), ported from the reference
``spotbox_monitor.py::snapshot_dir_stats``.

The persisted-sandboxes dir holds one subdirectory per snapshot *generation*
(UUID-v7 names, sorted-by-name = creation order). Each generation hardlinks
the previous commit's layers as ``inherited-layers/``. To avoid double-counting
physical blocks, each inode's ``st_blocks * 512`` is attributed only to the
first generation that contains it (a ``seen`` set of ``(st_dev, st_ino)``).

Returns the newest generation's row plus the whole-tree cumulative disk bytes
(matches ``du``). Returns ``None`` when the dir is absent/unreadable.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def scan_snapshot_sizes(sandbox_dir: Path) -> dict | None:
    """Stat one sandbox's snapshot tree with inode dedup.

    Returns ``{logical_bytes, disk_bytes, inherited_bytes, cumulative_bytes,
    generations, files}`` for the newest generation, or ``None`` on error.
    """
    root = Path(sandbox_dir)
    try:
        gen_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return None  # absent / unreadable -> caller (provider) skips
    if not gen_dirs:
        return None

    seen: set[tuple[int, int]] = set()
    gen_stats: list[dict] = []
    for gd in gen_dirs:
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
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    inherited += st.st_size
                else:
                    seen.add(key)
                    # st_blocks unavailable on Windows; fall back to 0.
                    unique_disk += getattr(st, "st_blocks", 0) * 512
        gen_stats.append(
            {
                "apparent": apparent,
                "unique_disk": unique_disk,
                "inherited": inherited,
                "files": n_files,
            }
        )
    newest = gen_stats[-1]
    cumulative = sum(g["unique_disk"] for g in gen_stats)
    return {
        "logical_bytes": newest["apparent"],
        "disk_bytes": newest["unique_disk"],
        "inherited_bytes": newest["inherited"],
        "cumulative_bytes": cumulative,
        "generations": len(gen_dirs),
        "files": newest["files"],
    }
