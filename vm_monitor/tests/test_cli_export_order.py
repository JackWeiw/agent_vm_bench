"""Regression for the CLI artifact-export ordering.

The bug: vm_monitor's CLI used to write the xlsx report BEFORE the SVG
time-curve reports. bench-core's MonitorController polls for
``resource_report.xlsx`` as the "all artifacts written" signal and reaps the
subprocess the moment it appears -- so with the old order, the SVG export
step (which ran AFTER xlsx) was killed, and every replay run came back with a
vm_monitor/ dir that had the xlsx + CSVs but no ``*.svg`` files.

The fix lives in ``cli.py``: write SVG first, xlsx LAST, so the xlsx's
appearance means CSV + SVG + xlsx are all written. This test locks that
ordering at the CLI level (the layer where the fix is), not at bench-core's
orchestration layer.
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock


def test_cli_exports_svg_before_xlsx(monkeypatch, tmp_path):
    """export_svg_reports must run before export_to_excel so the xlsx is the
    last artifact (the "fully done" signal orchestrators reap on)."""
    import vm_monitor.cli as cli

    call_order: list[str] = []
    recorded: dict = {}

    def fake_svg(monitor, log_dir):
        call_order.append("svg")
        return []

    def fake_xlsx(monitor, log_dir, numa_nodes, output_file, capture_results=None, skip_charts=False):
        call_order.append("xlsx")
        recorded["skip_charts"] = skip_charts

    monkeypatch.setattr(cli, "export_svg_reports", fake_svg)
    monkeypatch.setattr(cli, "export_to_excel", fake_xlsx)
    monkeypatch.setattr(cli, "PANDAS_AVAILABLE", True)

    # Fake monitor: real monitoring is a no-op; only the export phase matters.
    fake_m = MagicMock()
    fake_m.available_numa_nodes = [0]
    monkeypatch.setattr(cli, "FirecrackerMonitor", lambda: fake_m)

    # --disks "" avoids real block-device discovery; --time 0 short-circuits
    # monitoring (a no-op on the MagicMock monitor anyway).
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vm-monitor",
            "--vmm",
            "firecracker",
            "--time",
            "0",
            "--disks",
            "",
            "--log-dir",
            str(tmp_path),
        ],
    )
    cli.main()

    assert call_order == ["svg", "xlsx"], f"expected SVG before xlsx (xlsx must be the LAST artifact); got {call_order}"
    # Default (no --no-charts) builds charts: skip_charts forwarded as False.
    assert recorded.get("skip_charts") is False


def test_cli_no_charts_forwards_skip_charts(monkeypatch, tmp_path):
    """--no-charts forwards skip_charts=True to export_to_excel (skips the slow
    openpyxl chart phase on huge runs); without it the default is False (charts
    built)."""
    import vm_monitor.cli as cli

    recorded: dict = {}

    def fake_xlsx(monitor, log_dir, numa_nodes, output_file, capture_results=None, skip_charts=False):
        recorded["skip_charts"] = skip_charts

    monkeypatch.setattr(cli, "export_to_excel", fake_xlsx)
    monkeypatch.setattr(cli, "export_svg_reports", lambda m, d: [])
    monkeypatch.setattr(cli, "PANDAS_AVAILABLE", True)

    fake_m = MagicMock()
    fake_m.available_numa_nodes = [0]
    monkeypatch.setattr(cli, "FirecrackerMonitor", lambda: fake_m)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vm-monitor",
            "--vmm",
            "firecracker",
            "--time",
            "0",
            "--disks",
            "",
            "--log-dir",
            str(tmp_path),
            "--no-charts",
        ],
    )
    cli.main()
    assert recorded.get("skip_charts") is True


def test_auto_host_resources_hook_runs_even_with_no_charts(monkeypatch, tmp_path):
    """The export-step hook for host_resources.xlsx fires even under --no-charts
    (it reads raw CSVs, not the in-memory chart stack), runs AFTER svg and BEFORE
    the main xlsx, with raw_dir=<log_dir>/raw_data and out=<log_dir>/host_resources.xlsx."""
    import vm_monitor.cli as cli

    call_order: list[str] = []
    recorded: dict = {}

    def fake_build(raw_dir, out_path):
        call_order.append("host_resources")
        recorded["raw_dir"] = raw_dir
        recorded["out"] = out_path
        return out_path

    def fake_xlsx(monitor, log_dir, numa_nodes, output_file, capture_results=None, skip_charts=False):
        call_order.append("xlsx")

    monkeypatch.setattr(cli, "build_host_resources_xlsx", fake_build)
    monkeypatch.setattr(cli, "export_to_excel", fake_xlsx)
    monkeypatch.setattr(cli, "export_svg_reports", lambda m, d: [])
    monkeypatch.setattr(cli, "PANDAS_AVAILABLE", True)

    fake_m = MagicMock()
    fake_m.available_numa_nodes = [0]
    monkeypatch.setattr(cli, "FirecrackerMonitor", lambda: fake_m)

    monkeypatch.setattr(
        sys,
        "argv",
        ["vm-monitor", "--vmm", "firecracker", "--time", "0", "--disks", "", "--log-dir", str(tmp_path), "--no-charts"],
    )
    cli.main()
    assert "host_resources" in call_order  # hook fired under --no-charts
    assert call_order.index("host_resources") < call_order.index("xlsx")  # before main xlsx
    assert recorded["raw_dir"] == os.path.join(str(tmp_path), "raw_data")
    assert recorded["out"] == os.path.join(str(tmp_path), "host_resources.xlsx")


def test_auto_host_resources_hook_failure_does_not_block_xlsx(monkeypatch, tmp_path, caplog):
    """If build_host_resources_xlsx raises, the hook logs a WARNING naming the
    real raw_dir and the main resource_report.xlsx export still runs (degrade
    contract: WARNING + skip, never block main xlsx)."""
    import vm_monitor.cli as cli

    call_order: list[str] = []

    def boom(raw_dir, out_path):
        call_order.append("host_resources-boom")
        raise OSError("simulated disk full")

    def fake_xlsx(monitor, log_dir, numa_nodes, output_file, capture_results=None, skip_charts=False):
        call_order.append("xlsx")

    monkeypatch.setattr(cli, "build_host_resources_xlsx", boom)
    monkeypatch.setattr(cli, "export_to_excel", fake_xlsx)
    monkeypatch.setattr(cli, "export_svg_reports", lambda m, d: [])
    monkeypatch.setattr(cli, "PANDAS_AVAILABLE", True)

    fake_m = MagicMock()
    fake_m.available_numa_nodes = [0]
    monkeypatch.setattr(cli, "FirecrackerMonitor", lambda: fake_m)

    monkeypatch.setattr(
        sys,
        "argv",
        ["vm-monitor", "--vmm", "firecracker", "--time", "0", "--disks", "", "--log-dir", str(tmp_path)],
    )
    with caplog.at_level(logging.WARNING):
        cli.main()  # must NOT raise despite the hook's OSError
    assert "xlsx" in call_order  # main xlsx still ran
    # WARNING logged naming the REAL raw_dir (<log_dir>/raw_data), not the parent
    assert any("host_resources raw-report failed" in m for m in caplog.messages)
    assert any(str(tmp_path) in m and "raw_data" in m for m in caplog.messages)
