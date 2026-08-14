"""SVG time-curve exporter for vm_monitor.

Renders monitor histories as dark-themed SVG line charts, mirroring the
dependency-free SVG design from scripts/agentenv_monitor.py (dark #07101f
canvas, #101827 panels, <polyline> curves, dashed threshold lines, legend).
No external libraries -- pure string building -- so it runs anywhere the
monitor runs.

Each report bundles several charts (1- or 2-column grid) and is written to a
single SVG file in output_dir. Reports whose source histories are empty are
skipped silently. Safe to call on QEMU / Firecracker / test-dummy monitors.
"""

import math
import os
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

# Slate-tailwind accent palette (matches the agentenv reference look).
_PALETTE = [
    "#60a5fa",
    "#f59e0b",
    "#fb7185",
    "#4ade80",
    "#c084fc",
    "#22c55e",
    "#f97316",
    "#38bdf8",
]


def _svg_escape(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _finite(values: Iterable[float]) -> List[float]:
    return [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]


def _nice_max(value: float) -> float:
    """Round up to a pleasant axis maximum (1, 2, 5, 10 * 10^k)."""
    if value <= 0 or not math.isfinite(value):
        return 1.0
    exponent = 10 ** math.floor(math.log10(value))
    scaled = value / exponent
    step = 1 if scaled <= 1 else 2 if scaled <= 2 else 5 if scaled <= 5 else 10
    return step * exponent


def _elapsed_series(history: Sequence[dict], interval: float) -> List[float]:
    """Return per-sample elapsed-seconds from a history's ``ts`` strings.

    Falls back to ``index * interval`` (interval clamped to >= 1s) when
    timestamps are absent or unparseable, so reports still render from
    plain-list histories (e.g. host_cpu_history has no ts).
    """
    if not history:
        return []
    parsed: List[datetime] = []
    fmt = "%Y-%m-%d %H:%M:%S"
    ok = True
    for row in history:
        ts = row.get("ts")
        if not ts:
            ok = False
            break
        try:
            parsed.append(datetime.strptime(ts, fmt))
        except (TypeError, ValueError):
            ok = False
            break
    if ok and parsed:
        base = parsed[0]
        return [(p - base).total_seconds() for p in parsed]
    step = interval if interval and interval > 0 else 1.0
    return [i * step for i in range(len(history))]


def _line_chart(
    rows: List[dict],
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    unit: str,
    series: Sequence[Tuple[str, str, str]],
    threshold: Optional[Tuple[float, str]] = None,
) -> str:
    """Render one chart panel as an SVG <g> string.

    rows: list of dicts each carrying ``elapsed_s`` plus the series fields.
    series: list of (field_name, legend_label, color_hex).
    threshold: optional (value, label) drawn as a dashed amber line.
    """
    left, right, top, bottom = 58, 16, 38, 82
    plot_x, plot_y = x + left, y + top
    plot_w, plot_h = width - left - right, height - top - bottom
    elapsed = [float(row["elapsed_s"]) for row in rows]
    x_max = max(elapsed, default=1.0) or 1.0
    all_values: List[float] = []
    for field, _label, _color in series:
        all_values.extend(_finite(float(row.get(field, math.nan)) for row in rows))
    if threshold:
        all_values.append(threshold[0])
    raw_min = min(all_values, default=0.0)
    raw_max = max(all_values, default=1.0)
    y_min = -_nice_max(abs(raw_min) * 1.05) if raw_min < 0 else 0.0
    y_max = _nice_max(raw_max * 1.05) if raw_max > 0 else 1.0
    y_span = max(1e-12, y_max - y_min)

    parts = [
        f'<g><rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" ' f'fill="#101827" stroke="#26354a"/>',
        f'<text x="{x + 16}" y="{y + 25}" class="chart-title">{_svg_escape(title)}</text>',
    ]
    for tick in range(6):
        py = plot_y + plot_h * tick / 5
        value = y_max - y_span * tick / 5
        parts.append(f'<line x1="{plot_x}" y1="{py:.1f}" x2="{plot_x + plot_w}" y2="{py:.1f}" class="grid"/>')
        parts.append(f'<text x="{plot_x - 8}" y="{py + 4:.1f}" text-anchor="end" class="axis">{value:.1f}</text>')
    for tick in range(5):
        px = plot_x + plot_w * tick / 4
        value = x_max * tick / 4
        parts.append(
            f'<text x="{px:.1f}" y="{plot_y + plot_h + 18}" text-anchor="middle" class="axis">{value:.0f}</text>'
        )
    parts.append(
        f'<text x="{plot_x + plot_w / 2}" y="{y + height - 35}" text-anchor="middle" '
        f'class="axis">Elapsed (s)</text>'
    )
    parts.append(
        f'<text x="{x + 14}" y="{plot_y + plot_h / 2}" '
        f'transform="rotate(-90 {x + 14} {plot_y + plot_h / 2})" text-anchor="middle" '
        f'class="axis">{_svg_escape(unit)}</text>'
    )

    if threshold and y_min <= threshold[0] <= y_max:
        py = plot_y + plot_h * (y_max - threshold[0]) / y_span
        parts.append(
            f'<line x1="{plot_x}" y1="{py:.1f}" x2="{plot_x + plot_w}" y2="{py:.1f}" '
            f'stroke="#f59e0b" stroke-dasharray="7 5"/>'
        )
        parts.append(
            f'<text x="{plot_x + plot_w - 4}" y="{py - 5:.1f}" text-anchor="end" '
            f'fill="#fbbf24" class="legend">{_svg_escape(threshold[1])}</text>'
        )

    legend_x = plot_x
    legend_y = y + height - 14
    for field, label, color in series:
        points: List[str] = []
        for row in rows:
            value = float(row.get(field, math.nan))
            if not math.isfinite(value):
                continue
            px = plot_x + plot_w * float(row["elapsed_s"]) / x_max
            clipped = min(y_max, max(y_min, value))
            py = plot_y + plot_h * (y_max - clipped) / y_span
            points.append(f"{px:.1f},{py:.1f}")
        if len(points) >= 2:
            parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"/>')
        parts.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 18}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        parts.append(f'<text x="{legend_x + 23}" y="{legend_y + 4}" class="legend">{_svg_escape(label)}</text>')
        legend_x += max(120, len(label) * 13)
    parts.append("</g>")
    return "".join(parts)


def _render_report(
    target: str,
    title: str,
    charts: Sequence[Tuple[str, str, Sequence[Tuple[str, str, str]], Optional[Tuple[float, str]]]],
    rows_per_chart: Sequence[List[dict]],
) -> None:
    """Compose several line charts into one dark-themed SVG file.

    charts: list of (chart_title, unit, series, threshold).
    rows_per_chart: per-chart row list (each row carries elapsed_s + fields).
    """
    columns = 1 if len(charts) <= 1 else 2
    width = 1200 if columns == 1 else 1440
    panel_h = 320
    gap = 20
    panel_w = width - 60 if columns == 1 else 680
    top = 72
    chart_rows = math.ceil(len(charts) / columns)
    height = top + chart_rows * (panel_h + gap) + 20
    style = (
        ".title{font:700 28px system-ui,sans-serif;fill:#f8fafc}"
        ".chart-title{font:600 16px system-ui,sans-serif;fill:#e2e8f0}"
        ".axis{font:11px system-ui,sans-serif;fill:#94a3b8}"
        ".legend{font:11px system-ui,sans-serif;fill:#cbd5e1}"
        ".grid{stroke:#26354a;stroke-width:1}"
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f"<style>{style}</style>",
        '<rect width="100%" height="100%" fill="#07101f"/>',
        f'<text x="30" y="45" class="title">{_svg_escape(title)}</text>',
    ]
    for index, (ctitle, unit, series, thr) in enumerate(charts):
        rows = rows_per_chart[index] if index < len(rows_per_chart) else []
        x = 30 + (index % columns) * 700
        y = top + (index // columns) * (panel_h + gap)
        parts.append(_line_chart(rows, x, y, panel_w, panel_h, ctitle, unit, series, thr))
    parts.append("</svg>")
    with open(target, "w", encoding="utf-8") as f:
        f.write("".join(parts))


# ============================ row builders ============================


def _disk_io_rows(monitor) -> List[dict]:
    """Flatten disk_history + ublk_history into rows keyed by elapsed_s."""
    if not monitor.disk_history and not monitor.ublk_history:
        return []
    base = monitor.disk_history if monitor.disk_history else monitor.ublk_history
    elapsed = _elapsed_series(base, getattr(monitor, "interval", 0))
    disks = list(getattr(monitor, "target_disks", []) or [])
    rows: List[dict] = []
    n = len(base)
    for i in range(n):
        row: dict = {"elapsed_s": elapsed[i] if i < len(elapsed) else i}
        if monitor.disk_history and i < len(monitor.disk_history):
            for dev in disks:
                d = monitor.disk_history[i]["disks"].get(dev, {})
                row[f"{dev}_read_mb_s"] = d.get("r_mb_s", 0.0)
                row[f"{dev}_write_mb_s"] = d.get("w_mb_s", 0.0)
                row[f"{dev}_util_pct"] = d.get("util_pct", 0.0)
        if monitor.ublk_history and i < len(monitor.ublk_history):
            row["ublk_devices"] = monitor.ublk_history[i].get("ublk_devices", 0)
        rows.append(row)
    return rows


def _host_resource_rows(monitor) -> List[dict]:
    """Merge host_cpu_history + host_mem_history + host_mem_detail_history by index."""
    cpu = monitor.host_cpu_history
    mem = monitor.host_mem_history
    detail = monitor.host_mem_detail_history
    if not cpu and not mem and not detail:
        return []
    # Prefer a ts-bearing history for elapsed; fall back to index*interval.
    ts_source: Sequence[dict] = detail if detail else (mem if mem else [])
    elapsed = _elapsed_series(ts_source, getattr(monitor, "interval", 0))
    n = max(len(cpu), len(mem), len(detail))
    rows: List[dict] = []
    for i in range(n):
        row: dict = {"elapsed_s": elapsed[i] if i < len(elapsed) else i}
        if i < len(cpu):
            row["host_cpu_pct"] = float(cpu[i])
        if i < len(mem):
            row["host_mem_used_gb"] = round(mem[i].get("used_mb", 0.0) / 1024.0, 3)
        if i < len(detail):
            row["dirty_mb"] = detail[i].get("dirty_mb", 0.0)
            row["writeback_mb"] = detail[i].get("writeback_mb", 0.0)
            row["cached_mb"] = detail[i].get("cached_mb", 0.0)
            row["buffers_mb"] = detail[i].get("buffers_mb", 0.0)
        rows.append(row)
    return rows


def _swap_rows(monitor) -> List[dict]:
    if not monitor.swap_history:
        return []
    elapsed = _elapsed_series(monitor.swap_history, getattr(monitor, "interval", 0))
    rows: List[dict] = []
    for i, s in enumerate(monitor.swap_history):
        rows.append(
            {
                "elapsed_s": elapsed[i] if i < len(elapsed) else i,
                "swap_used_mb": s.get("capacity", {}).get("used_mb", 0.0),
                "swap_in_rate": s.get("activity", {}).get("swap_in_rate", 0.0),
                "swap_out_rate": s.get("activity", {}).get("swap_out_rate", 0.0),
                "swap_cached_mb": s.get("cache", {}).get("cached_mb", 0.0),
            }
        )
    return rows


def _numa_rows(monitor) -> Tuple[List[dict], List[int]]:
    """Return (rows, sorted_numa_node_ids) for NUMA memory + CPU."""
    mem_hist = monitor.numa_memory_history
    cpu_hist = monitor.numa_cpu_history
    if not mem_hist and not cpu_hist:
        return [], []
    node_ids = set()
    for entry in mem_hist:
        for n in entry.get("nodes", []):
            node_ids.add(n.get("node"))
    node_ids.update(cpu_hist.keys())
    nodes = sorted(n for n in node_ids if n is not None)
    elapsed = _elapsed_series(mem_hist, getattr(monitor, "interval", 0)) if mem_hist else []
    n = len(mem_hist) if mem_hist else max((len(v) for v in cpu_hist.values()), default=0)
    rows: List[dict] = []
    for i in range(n):
        row: dict = {"elapsed_s": elapsed[i] if i < len(elapsed) else i}
        if mem_hist and i < len(mem_hist):
            for node_obj in mem_hist[i].get("nodes", []):
                nid = node_obj.get("node")
                if nid is not None:
                    row[f"numa_{nid}_used_gb"] = round(node_obj.get("used_mb", 0.0) / 1024.0, 3)
        if cpu_hist:
            for nid in nodes:
                hist = cpu_hist.get(nid, [])
                if i < len(hist):
                    row[f"numa_{nid}_cpu_pct"] = float(hist[i])
        rows.append(row)
    return rows, nodes


def _vm_total_rows(monitor) -> List[dict]:
    if not monitor.vm_total_memory_history:
        return []
    elapsed = _elapsed_series(monitor.vm_total_memory_history, getattr(monitor, "interval", 0))
    rows: List[dict] = []
    for i, v in enumerate(monitor.vm_total_memory_history):
        rows.append(
            {
                "elapsed_s": elapsed[i] if i < len(elapsed) else i,
                "vm_total_gb": round(v.get("total_mb", 0.0) / 1024.0, 3),
                "vm_count": v.get("vm_count", 0),
            }
        )
    return rows


# ============================ report assembly ============================


def _disk_report(monitor, target, rows):
    disks = list(getattr(monitor, "target_disks", []) or [])
    read_series = [(f"{d}_read_mb_s", f"{d} Read", _PALETTE[i % len(_PALETTE)]) for i, d in enumerate(disks)]
    write_series = [(f"{d}_write_mb_s", f"{d} Write", _PALETTE[i % len(_PALETTE)]) for i, d in enumerate(disks)]
    util_series = [(f"{d}_util_pct", d, _PALETTE[i % len(_PALETTE)]) for i, d in enumerate(disks)]
    charts = [
        ("Disk Read Throughput", "MiB/s", read_series, None),
        ("Disk Write Throughput", "MiB/s", write_series, None),
        ("Disk Busy Utilization", "%", util_series, (100.0, "100%")),
        ("ublk Devices", "count", [("ublk_devices", "ublk", "#38bdf8")], None),
    ]
    _render_report(target, "Disk I/O Time Curves", charts, [rows] * len(charts))


def _host_report(target, rows):
    charts = [
        ("Host CPU Usage", "%", [("host_cpu_pct", "CPU", "#60a5fa")], None),
        ("Host Memory Used", "GiB", [("host_mem_used_gb", "Used", "#f59e0b")], None),
        ("Host Dirty Pages", "MiB", [("dirty_mb", "Dirty", "#fb7185")], None),
        (
            "Writeback / Cached / Buffers",
            "MiB",
            [
                ("writeback_mb", "Writeback", "#38bdf8"),
                ("cached_mb", "Cached", "#4ade80"),
                ("buffers_mb", "Buffers", "#c084fc"),
            ],
            None,
        ),
    ]
    _render_report(target, "Host Resource Time Curves", charts, [rows] * len(charts))


def _swap_report(target, rows):
    charts = [
        ("Swap Used", "MiB", [("swap_used_mb", "Used", "#fb7185")], None),
        ("Swap In/Out Rate", "MiB/s", [("swap_in_rate", "In", "#4ade80"), ("swap_out_rate", "Out", "#f97316")], None),
        ("Swap Cache", "MiB", [("swap_cached_mb", "Cached", "#60a5fa")], None),
        ("Swap Used (cumulative view)", "MiB", [("swap_used_mb", "Used", "#f59e0b")], None),
    ]
    _render_report(target, "Swap Activity Time Curves", charts, [rows] * len(charts))


def _numa_report(target, rows, nodes):
    mem_series = [(f"numa_{n}_used_gb", f"Node {n}", _PALETTE[i % len(_PALETTE)]) for i, n in enumerate(nodes)]
    cpu_series = [(f"numa_{n}_cpu_pct", f"Node {n}", _PALETTE[i % len(_PALETTE)]) for i, n in enumerate(nodes)]
    charts = [
        ("NUMA Memory Used", "GiB", mem_series, None),
        ("NUMA CPU Usage", "%", cpu_series, None),
    ]
    _render_report(target, "NUMA Time Curves", charts, [rows] * len(charts))


def _vm_total_report(target, rows):
    charts = [
        ("VM Total Memory", "GiB", [("vm_total_gb", "Total", "#60a5fa")], None),
        ("VM Count", "count", [("vm_count", "VMs", "#4ade80")], None),
    ]
    _render_report(target, "VM Total Memory Time Curves", charts, [rows] * len(charts))


def export_svg_reports(monitor, output_dir: str) -> List[str]:
    """Render all SVG time-curve reports for a monitor into output_dir.

    Returns the list of written file paths. Reports whose source histories
    are empty are skipped. Missing monitor attributes default to empty, so this
    is safe to call on QEMU / Firecracker / test-dummy monitors alike.
    """
    os.makedirs(output_dir, exist_ok=True)
    written: List[str] = []

    def _path(name: str) -> str:
        return os.path.join(output_dir, name)

    disk_rows = _disk_io_rows(monitor)
    if disk_rows:
        _disk_report(monitor, _path("disk_io.svg"), disk_rows)
        written.append(_path("disk_io.svg"))

    host_rows = _host_resource_rows(monitor)
    if host_rows:
        _host_report(_path("host_resources.svg"), host_rows)
        written.append(_path("host_resources.svg"))

    swap_rows = _swap_rows(monitor)
    if swap_rows:
        _swap_report(_path("swap.svg"), swap_rows)
        written.append(_path("swap.svg"))

    numa_rows, nodes = _numa_rows(monitor)
    if numa_rows and nodes:
        _numa_report(_path("numa.svg"), numa_rows, nodes)
        written.append(_path("numa.svg"))

    vm_rows = _vm_total_rows(monitor)
    if vm_rows:
        _vm_total_report(_path("vm_total.svg"), vm_rows)
        written.append(_path("vm_total.svg"))

    return written
