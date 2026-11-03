"""xlsxwriter rendering of the derived timelines into host_resources.xlsx.

Rendering is deliberately separate from derivation: a table is written with
``constant_memory`` (bounded RSS on the measured host), charts are native
xlsxwriter LineCharts bound to worksheet ranges, and every sheet is capacity
checked against Excel's limits before it is created.

Failure isolation lives here too: xlsxwriter cannot delete a worksheet, so
:func:`render_workbook` raises :class:`SheetFailed` on the first broken builder
and the caller rebuilds without it.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd
import xlsxwriter

logger = logging.getLogger(__name__)

# Excel hard limits (row/column counts are 1-based; a header row is included).
_EXCEL_MAX_ROWS = 1_048_576
_EXCEL_MAX_COLS = 16_384

# Past this many data rows a line chart is unreadable and slow to open; the
# table above it still carries every sample.
_CHART_MAX_ROWS = 500_000

# Chart footprint in default (64 px) columns: 640 px ~= 10 columns. Used to seat
# a second table block past a chart's right edge.
_CHART_COL_FOOTPRINT = 10

# Vertical pitch between stacked charts: 300 px chart height < 18 rows x 20 px.
_CHART_ROW_STEP = 18

# (df column, display label), paired so a reorder cannot silently mislabel.
_HOST_COLUMNS = (
    ("t_s", "t (s)"),
    ("timestamp", "Timestamp"),
    ("cpu_busy_pct", "CPU busy (%)"),
    ("cpu_iowait_pct", "CPU iowait (%)"),
    ("cpu_user_pct", "CPU user (%)"),
    ("cpu_sys_pct", "CPU sys (%)"),
    ("mem_used_gb", "Mem used (GB)"),
    ("mem_cache_gb", "Mem cache (GB)"),
    ("buffers_mb", "Buffers (MB)"),
    ("dirty_mb", "Dirty (MB)"),
    ("writeback_mb", "Writeback (MB)"),
    ("anon_pages_mb", "Anon pages (MB)"),
    ("file_cache_mb", "File cache (MB)"),
    ("sreclaimable_mb", "SReclaimable (MB)"),
    ("swap_used_gb", "Swap used (GB)"),
    ("swap_free_gb", "Swap free (GB)"),
    ("swap_cached_mb", "Swap cached (MB)"),
    ("swap_cache_ratio_pct", "Swap cache ratio (%)"),
    ("swap_in_mib_s", "Swap in (MiB/s)"),
    ("swap_out_mib_s", "Swap out (MiB/s)"),
    ("page_scan_mib_s", "Page scan (MiB/s)"),
    ("page_reclaim_mib_s", "Page reclaim (MiB/s)"),
    ("file_refault_mib_s", "File refault (MiB/s)"),
    ("ublk_cores", "ublk cores"),
    ("vm_cores", "VM cores"),
    ("vm_mem_gb", "VM memory (GB)"),
    ("fc_pss_gb", "FC total PSS (GB)"),
    ("direct_reclaim_mib_s", "Direct reclaim (MiB/s)"),
    ("allocstall_per_s", "Alloc stalls (/s)"),
    ("concurrent_vms", "Concurrent VMs"),
    ("procs_running", "Procs running"),
    ("procs_blocked", "Procs blocked"),
    ("sandboxes_running", "Sandboxes running (API)"),
    ("sandboxes_paused", "Sandboxes paused (API)"),
)

# (title, y-axis title, series columns). Skips any series whose column is
# absent (older raw dirs: no memory_mb -> no vm_mem_gb, no API state -> no last).
_HOST_CHARTS = (
    ("Host memory", "GB", ("mem_used_gb", "mem_cache_gb")),
    ("Buffers / dirty / writeback", "MB", ("buffers_mb", "dirty_mb", "writeback_mb")),
    ("Anon vs file cache", "MB", ("anon_pages_mb", "file_cache_mb", "sreclaimable_mb")),
    ("Swap usage", "GB", ("swap_used_gb", "swap_free_gb")),
    ("Swap cache", "MB", ("swap_cached_mb",)),
    ("FC memory (total PSS)", "GB", ("fc_pss_gb",)),
    ("VM memory vs FC PSS", "GB", ("vm_mem_gb", "fc_pss_gb")),
    ("Page-cache pressure", "MiB/s", ("page_scan_mib_s", "page_reclaim_mib_s", "file_refault_mib_s")),
    ("Alloc stalls", "/s", ("allocstall_per_s",)),
    ("Swap in/out", "MiB/s", ("swap_in_mib_s", "swap_out_mib_s")),
    ("Host CPU", "%", ("cpu_busy_pct", "cpu_iowait_pct", "cpu_user_pct", "cpu_sys_pct")),
    ("CPU cores (ublk + VM)", "cores", ("ublk_cores", "vm_cores")),
    ("Procs running/blocked", "count", ("procs_running", "procs_blocked")),
    ("Concurrent VMs", "count", ("concurrent_vms",)),
    ("Sandbox states (API)", "count", ("sandboxes_running", "sandboxes_paused")),
)

# Disk I/O display labels, keyed by df-column suffix ("<device>_<field>"). Keeps
# the table header + chart legend readable ("sda Read (MB/s)") while the df
# keeps the suffix names the chart column matcher expects.
_DISK_FIELD_LABELS = {
    "r_mb_s": "Read (MB/s)",
    "w_mb_s": "Write (MB/s)",
    "util_pct": "Util (%)",
    "inflight": "Inflight",
    "avg_queue_depth": "Queue depth",
    "read_await_ms": "Read await (ms)",
    "write_await_ms": "Write await (ms)",
    "r_iops": "Read IOPS",
    "w_iops": "Write IOPS",
    "avg_rq_sz": "Avg rq sz (sectors)",
}

_DISK_CHARTS = (
    ("Disk r/w MB/s", "MB/s", ("_r_mb_s", "_w_mb_s")),
    ("Disk util %", "%", ("_util_pct",)),
    ("Disk await", "ms", ("_read_await_ms", "_write_await_ms")),
    ("Disk queue depth / inflight", "count", ("_avg_queue_depth", "_inflight")),
    ("Disk IOPS", "IOPS", ("_r_iops", "_w_iops")),
)

_NUMA_CHARTS = (
    ("NUMA CPU", "%", ("CPU (%)",)),
    ("NUMA memory used/free", "GB", ("Used (GB)", "Free (GB)")),
    ("NUMA anon vs file", "GB", ("Anon (GB)", "File (GB)")),
    ("NUMA swap cache", "MB", ("SwapCache (MB)",)),
)


class SheetFailed(Exception):
    """A builder raised; carries the sheet name so the retry can skip it."""

    def __init__(self, name: str, cause: Exception):
        super().__init__(f"{name} sheet failed: {cause}")
        self.name = name
        self.cause = cause


class _Formats:
    """Workbook-scoped cell formats (xlsxwriter objects are workbook-bound)."""

    def __init__(self, workbook):
        self.header = workbook.add_format({"bold": True, "font_color": "#F9FAFB", "bg_color": "#1F2937"})


def _cell(value):
    """xlsxwriter accepts no NaN/INF: render non-finite floats as blank cells.

    Numpy scalars are unwrapped because write_row's type dispatch does not
    recognise np.float64/np.int64 on every build.
    """
    if isinstance(value, float | np.floating):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _write_table(ws, frame, headers, columns=None, start_col=0, header_format=None) -> int:
    """Write ``frame`` under a styled header at row 0; return the data-row count."""
    ws.write_row(0, start_col, list(headers), header_format)
    for row, record in enumerate(frame[list(columns or headers)].itertuples(index=False, name=None), start=1):
        ws.write_row(row, start_col, [_cell(value) for value in record])
    return len(frame)


def _add_line_chart(
    workbook,
    ws,
    title: str,
    y_title: str,
    category_col: int,
    series_cols,
    header_row: int,
    data_rows: int,
    anchor_row: int,
    anchor_col: int,
    legend_bottom: bool = False,
) -> None:
    """Line chart bound to worksheet cells (0-based rows/cols).

    Skipped past ``_CHART_MAX_ROWS`` points: the table keeps every sample, but
    an unreadable 500k-point line only slows Excel down.
    """
    if data_rows <= 0:
        return
    if data_rows > _CHART_MAX_ROWS:
        logger.warning("chart %r skipped: %d data points exceeds %d", title, data_rows, _CHART_MAX_ROWS)
        return
    sheet_name = ws.get_name()
    chart = workbook.add_chart({"type": "line"})
    chart.set_title({"name": title})
    chart.set_y_axis({"name": y_title})
    chart.set_x_axis({"name": "t (s)"})
    chart.set_size({"width": 640, "height": 300})
    if legend_bottom:
        chart.set_legend({"position": "bottom"})
    last_row = header_row + data_rows
    for column in series_cols:
        chart.add_series(
            {
                "name": [sheet_name, header_row, column],
                "categories": [sheet_name, header_row + 1, category_col, last_row, category_col],
                "values": [sheet_name, header_row + 1, column, last_row, column],
            }
        )
    ws.insert_chart(anchor_row, anchor_col, chart)


def _sheet_fits(data_rows: int, columns: int) -> bool:
    return data_rows + 1 <= _EXCEL_MAX_ROWS and columns <= _EXCEL_MAX_COLS


def _chart_rows(workbook, ws, charts, data_rows, anchor_col, legend_bottom=False) -> None:
    """Stack the given chart specs from row 1 at ``anchor_col``."""
    for index, (title, y_title, series_cols) in enumerate(charts):
        _add_line_chart(
            workbook,
            ws,
            title,
            y_title,
            0,
            series_cols,
            0,
            data_rows,
            1 + index * _CHART_ROW_STEP,
            anchor_col,
            legend_bottom=legend_bottom,
        )


def _columns_ending_with(columns, *suffixes) -> tuple[int, ...]:
    return tuple(i for i, name in enumerate(columns) if name.endswith(tuple(suffixes)))


def _columns_with_data(frame) -> list[bool]:
    """Per-column: does it hold at least one finite number?

    A column that is absent or all-NaN would render an empty chart line (the
    raw dir predates that field), so charts built from it are dropped instead
    of shipped blank -- a 2h workbook must not carry empty decoration.
    """
    numeric = frame.apply(pd.to_numeric, errors="coerce").astype("float64")
    return np.isfinite(numeric.to_numpy()).any(axis=0).tolist()


def _chart_specs(charts, populated: list[bool]):
    """Keep charts that still have data, and drop ones that duplicate an
    earlier chart's series set (e.g. "VM memory vs FC PSS" falls back to bare
    fc_pss_gb when the raw dir has no memory_mb)."""
    specs, seen = [], set()
    for title, y_title, columns in charts:
        selected = tuple(c for c in columns if populated[c])
        if not selected or selected in seen:
            continue
        seen.add(selected)
        specs.append((title, y_title, selected))
    return specs


# --- sheets ----------------------------------------------------------------


def build_host_sheet(workbook, fmt, frame) -> bool:
    if frame is None or frame.empty:
        return False
    if not _sheet_fits(len(frame), len(frame.columns)):
        logger.warning(
            "Host resources sheet skipped: %d rows x %d cols exceeds Excel limits", len(frame), len(frame.columns)
        )
        return False
    # Older raw dirs lack memory_mb / API state -> those columns simply do not
    # exist in the frame; keep the label table in sync with what is present.
    columns = [(name, label) for name, label in _HOST_COLUMNS if name in frame.columns]
    names, headers = zip(*columns)
    position = {name: index for index, name in enumerate(names)}

    ws = workbook.add_worksheet("Host resources")
    _write_table(ws, frame, headers, columns=names, header_format=fmt.header)
    ws.freeze_panes(1, 0)

    charts = [
        (title, y_title, tuple(position[name] for name in series if name in position))
        for title, y_title, series in _HOST_CHARTS
    ]
    charts = _chart_specs(charts, _columns_with_data(frame[list(names)]))
    _chart_rows(workbook, ws, charts, len(frame), len(headers) + 1)
    return True


def build_per_vm_sheet(workbook, fmt, cores, pss) -> bool:
    """CPU table + chart on top, PSS table + chart below (shared VM set)."""
    if cores is None or cores.empty:
        return False
    headers = ["t_s", *cores.columns[1:]]
    if not _sheet_fits(len(cores) + len(pss) + 3, len(headers) + 2):
        logger.warning("Per-VM sheet skipped: table exceeds Excel limits")
        return False

    ws = workbook.add_worksheet("Per-VM")
    data_rows = _write_table(ws, cores, headers, header_format=fmt.header)
    ws.freeze_panes(1, 0)
    anchor_col = len(headers) + 1
    _add_line_chart(workbook, ws, "Per-VM CPU cores", "cores", 0, range(1, len(headers)), 0, data_rows, 1, anchor_col)

    pss_headers = ["t_s", *pss.columns[1:]]
    pss_header_row = data_rows + 3
    ws.write_row(pss_header_row, 0, pss_headers, fmt.header)
    for row, record in enumerate(pss.itertuples(index=False, name=None), start=pss_header_row + 1):
        ws.write_row(row, 0, [_cell(value) for value in record])
    _add_line_chart(
        workbook,
        ws,
        "Per-VM PSS",
        "GB",
        0,
        range(1, len(pss_headers)),
        pss_header_row,
        len(pss),
        max(pss_header_row + 1, _CHART_ROW_STEP + 1),
        anchor_col,
    )
    return True


def _disk_display_header(column_name: str) -> str:
    if column_name == "t_s":
        return "t (s)"
    for suffix, label in _DISK_FIELD_LABELS.items():
        if column_name.endswith(f"_{suffix}"):
            return f"{column_name[: -len(suffix) - 1]} {label}"
    return column_name


def build_disk_sheet(workbook, fmt, frame) -> bool:
    if frame is None or frame.empty:
        return False
    if not _sheet_fits(len(frame), len(frame.columns)):
        logger.warning("Disk IO sheet skipped: table exceeds Excel limits")
        return False
    ws = workbook.add_worksheet("Disk IO")
    columns = list(frame.columns)
    _write_table(ws, frame, [_disk_display_header(name) for name in columns], columns=columns, header_format=fmt.header)
    ws.freeze_panes(1, 0)

    charts = [(title, y_title, _columns_ending_with(columns, *suffixes)) for title, y_title, suffixes in _DISK_CHARTS]
    charts = _chart_specs(charts, _columns_with_data(frame))
    _chart_rows(workbook, ws, charts, len(frame), len(columns) + 1, legend_bottom=True)
    return True


def build_numa_sheet(workbook, fmt, frame) -> bool:
    if frame is None or frame.empty:
        return False
    if not _sheet_fits(len(frame), len(frame.columns)):
        logger.warning("NUMA sheet skipped: table exceeds Excel limits")
        return False
    ws = workbook.add_worksheet("NUMA")
    headers = list(frame.columns)
    _write_table(ws, frame, headers, header_format=fmt.header)
    ws.freeze_panes(1, 0)

    charts = [(title, y_title, _columns_ending_with(headers, *suffixes)) for title, y_title, suffixes in _NUMA_CHARTS]
    charts = _chart_specs(charts, _columns_with_data(frame))
    _chart_rows(workbook, ws, charts, len(frame), len(headers) + 1)
    return True


def build_hostproc_sheet(workbook, fmt, cores, pss) -> bool:
    """Host-process CPU cores + PSS side by side.

    constant_memory requires increasing row order, so the two blocks are
    interleaved row by row (cores on the left, PSS past the cores chart).
    """
    cores_empty, pss_empty = cores is None or cores.empty, pss is None or pss.empty
    if cores_empty and pss_empty:
        return False
    cores_headers = [] if cores_empty else ["t_s", *cores.columns[1:]]
    pss_headers = [] if pss_empty else ["t_s", *pss.columns[1:]]
    pss_start_col = 0 if not cores_headers else len(cores_headers) + 1 + _CHART_COL_FOOTPRINT
    total_cols = len(cores_headers) + (pss_start_col + len(pss_headers) if pss_headers else 0)
    total_rows = max(len(cores) if not cores_empty else 0, len(pss) if not pss_empty else 0)
    if not _sheet_fits(total_rows, total_cols):
        logger.warning("HostProc sheet skipped: table exceeds Excel limits")
        return False

    ws = workbook.add_worksheet("HostProc")
    if cores_headers:
        ws.write_row(0, 0, cores_headers, fmt.header)
    if pss_headers:
        ws.write_row(0, pss_start_col, pss_headers, fmt.header)
    cores_rows = list(cores.itertuples(index=False, name=None)) if not cores_empty else []
    pss_rows = list(pss.itertuples(index=False, name=None)) if not pss_empty else []
    for row in range(total_rows):
        if row < len(cores_rows):
            ws.write_row(row + 1, 0, [_cell(value) for value in cores_rows[row]])
        if row < len(pss_rows):
            ws.write_row(row + 1, pss_start_col, [_cell(value) for value in pss_rows[row]])
    ws.freeze_panes(1, 0)

    if cores_headers:
        _add_line_chart(
            workbook,
            ws,
            "Host-proc CPU cores",
            "cores",
            0,
            range(1, len(cores_headers)),
            0,
            len(cores),
            1,
            len(cores_headers) + 1,
        )
    if pss_headers:
        _add_line_chart(
            workbook,
            ws,
            "Host-proc PSS",
            "GB",
            pss_start_col,
            range(pss_start_col + 1, pss_start_col + len(pss_headers)),
            0,
            len(pss),
            1,
            pss_start_col + len(pss_headers) + 1,
        )
    return True


# (sheet name, builder) roster; the caller supplies each builder's args.
SHEET_BUILDERS = (
    ("Host resources", build_host_sheet),
    ("NUMA", build_numa_sheet),
    ("Per-VM", build_per_vm_sheet),
    ("Disk IO", build_disk_sheet),
    ("HostProc", build_hostproc_sheet),
)


def render_workbook(plans, path, skip: set[str]) -> int:
    """Build one workbook at ``path``; raise SheetFailed on the first failure.

    Returns the number of sheets that actually received data.
    """
    workbook = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    fmt = _Formats(workbook)
    built = 0
    try:
        for name, build_sheet, args in plans:
            if name in skip:
                continue
            try:
                built += 1 if build_sheet(workbook, fmt, *args) else 0
            except Exception as exc:
                raise SheetFailed(name, exc) from exc
    finally:
        workbook.close()
    return built
