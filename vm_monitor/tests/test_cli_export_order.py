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

import sys
from unittest.mock import MagicMock


def test_cli_exports_svg_before_xlsx(monkeypatch, tmp_path):
    """export_svg_reports must run before export_to_excel so the xlsx is the
    last artifact (the "fully done" signal orchestrators reap on)."""
    import vm_monitor.cli as cli

    call_order: list[str] = []

    def fake_svg(monitor, log_dir):
        call_order.append("svg")
        return []

    def fake_xlsx(monitor, log_dir, numa_nodes, output_file, capture_results=None):
        call_order.append("xlsx")

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
