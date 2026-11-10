"""raw_data/*.csv -> host_resources.xlsx (timelines only, post-hoc rerunnable).

Every per-sample timeline lives in this workbook: host CPU/memory/pressure/
swap, per-VM CPU + PSS, per-NUMA CPU + memory, disk I/O and named host
processes. ``resource_report.xlsx`` keeps only the timeline-less summary sheets.

The work is split by responsibility so each layer stays readable on its own:

- :mod:`vm_monitor.export.raw.frames`  -- raw CSV contract (one read, delta rules)
- :mod:`vm_monitor.export.raw.metrics` -- derivations (host / per-VM / NUMA / disk / proc)
- :mod:`vm_monitor.export.raw.workbook`    -- xlsxwriter rendering, capacity guards
- this module                    -- orchestration, atomic write, degradation

Output is built at ``<out>.build.xlsx`` and atomically promoted with
``os.replace``: a failed build never leaves a corrupt file behind, and a single
failing sheet costs only that sheet.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from vm_monitor.export.raw.frames import (  # noqa: F401  (re-exported for callers/tests)
    MAX_INTERVAL_S,
    _guarded,
    _load_frames,
)
from vm_monitor.export.raw.metrics import (  # noqa: F401  (re-exported for callers/tests)
    _MAX_DISK_DEVICES,
    MAX_VM_SERIES,
    _aggregate_cores_at,
    derive_disk as _derive_disk,
    derive_host as _derive_host,
    derive_hostproc as _derive_hostproc,
    derive_numa as _derive_numa,
    derive_per_vm as _derive_per_vm,
)
from vm_monitor.export.raw.workbook import (  # noqa: F401  (re-exported for callers/tests)
    _CHART_MAX_ROWS,
    _EXCEL_MAX_COLS,
    _EXCEL_MAX_ROWS,
    SheetFailed as _SheetFailed,
    build_disk_sheet as _build_disk_sheet,
    build_host_sheet as _build_host_sheet,
    build_hostproc_sheet as _build_hostproc_sheet,
    build_numa_sheet as _build_numa_sheet,
    build_per_vm_sheet as _build_per_vm_sheet,
    render_workbook,
)

logger = logging.getLogger(__name__)


def _derive_or_fallback(source_name: str, derive_fn, fallback, frames):
    """Run one derivation, falling back with a WARNING when it raises -- a bad
    input must not cost the whole workbook."""
    try:
        return derive_fn(frames=frames)
    except Exception as exc:
        logger.warning("%s derivation failed: %s", source_name, exc)
        return fallback


def build_host_resources_xlsx(raw_dir, out_path) -> Path:
    """Read raw_data/*.csv once -> derive -> write host_resources.xlsx.

    Sheets: Host resources / NUMA / Per-VM / Disk IO / HostProc. Independent of
    resource_report.xlsx (separate filename preserves bench-core's reap signal)
    and of in-memory history (post-hoc rerunnable). Degrades: a missing CSV
    skips its sheet (WARNING), a failed sheet is rebuilt away, never raises.
    """
    raw_dir, out_path = Path(raw_dir), Path(out_path)
    tmp = out_path.with_name(out_path.name + ".build.xlsx")
    try:
        if tmp.exists():
            tmp.unlink()
        frames = _load_frames(raw_dir)
        per_vm = _derive_or_fallback("per-VM", _derive_per_vm, (pd.DataFrame(), pd.DataFrame()), frames)
        hostproc = _derive_or_fallback("hostproc", _derive_hostproc, (pd.DataFrame(), pd.DataFrame()), frames)
        plans = (
            ("Host resources", _build_host_sheet, (_derive_or_fallback("host", _derive_host, pd.DataFrame(), frames),)),
            ("NUMA", _build_numa_sheet, (_derive_or_fallback("NUMA", _derive_numa, pd.DataFrame(), frames),)),
            ("Per-VM", _build_per_vm_sheet, per_vm),
            ("Disk IO", _build_disk_sheet, (_derive_or_fallback("disk", _derive_disk, pd.DataFrame(), frames),)),
            ("HostProc", _build_hostproc_sheet, hostproc),
        )
        built = _render_isolating_failures(plans, tmp)
        if not built:
            logger.warning("no raw CSV data found in raw_dir=%s; host_resources.xlsx not written", raw_dir)
            if tmp.exists():
                tmp.unlink()
            return out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, out_path)
        logger.info("host_resources.xlsx written: %s", out_path)
        return out_path
    except Exception as exc:
        logger.warning("host_resources.xlsx build failed for raw_dir=%s: %s", raw_dir, exc)
        _discard(tmp)
        return out_path


def _render_isolating_failures(plans, tmp: Path) -> int:
    """Build the workbook, dropping any sheet whose builder raises.

    xlsxwriter cannot remove a worksheet, so isolation means rebuilding from
    scratch with the failed sheet skipped (normally never happens: capacity is
    checked before add_worksheet).
    """
    skipped: set[str] = set()
    while True:
        try:
            return render_workbook(plans, tmp, skipped)
        except _SheetFailed as exc:
            logger.warning("%s sheet failed: %s", exc.name, exc.cause)
            skipped.add(exc.name)
            _discard(tmp)
            if len(skipped) >= len(plans):
                return 0


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
