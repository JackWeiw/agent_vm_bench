"""Tests for the host PSI collector (_read_psi / collect_psi / host_psi_raw.csv).

PSI files (/proc/pressure/{cpu,memory}) exist only on kernels built with
CONFIG_PSI (and not disabled at boot), so every test feeds fixture files through
the ``psi_dir`` seam instead of the host's /proc.
"""

from __future__ import annotations

import vm_monitor.base as base
from vm_monitor.base import VMMonitorBase, _psi_flat_row

CPU_BOTH = """some avg10=12.34 avg300=5.67 avg600=2.00 total=123456
full avg10=1.00 avg300=0.50 avg600=0.10 total=1000
"""
MEM_BOTH = """some avg10=3.00 avg300=1.00 avg600=0.50 total=999
full avg10=0.50 avg300=0.20 avg600=0.10 total=40
"""
CPU_SOME_ONLY = """some avg10=0.00 avg300=0.00 avg600=0.00 total=0
"""
CPU_BAD_VALUE = """some avg10=zzz avg300=0 avg600=0 total=0
"""
CPU_BAD_TOKEN = """some avg10 avg300=0 avg600=0 total=0
"""
CPU_NO_SOME = """full avg10=1.0 avg300=0 avg600=0 total=5
"""


class _PsiMonitor(VMMonitorBase):
    """Concrete monitor whose PSI reads come from a fixture dir; nothing else runs."""

    def __init__(self, psi_dir):
        super().__init__()
        self._psi_dir = psi_dir

    def _read_psi(self, psi_dir="/proc/pressure"):  # noqa: N802 - seam
        return super()._read_psi(self._psi_dir)

    # ABC seams:
    def get_vms_realtime(self):  # noqa: N802 - ABC seam
        return []

    def get_process_names(self):  # noqa: N802 - ABC seam
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):  # noqa: N802 - ABC seam
        return "vm0"

    def get_monitor_title(self):
        return "PsiMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "psi_monitor"


def _write_psi(tmp_path, cpu=CPU_BOTH, mem=MEM_BOTH):
    d = tmp_path / "pressure"
    d.mkdir(parents=True, exist_ok=True)
    if cpu is not None:
        (d / "cpu").write_text(cpu)
    if mem is not None:
        (d / "memory").write_text(mem)
    return str(d)


def test_read_psi_parses_some_and_full(tmp_path):
    mon = _PsiMonitor(_write_psi(tmp_path))
    psi = mon._read_psi()
    assert psi["cpu"]["some"] == {"avg10": 12.34, "avg300": 5.67, "avg600": 2.0, "total": 123456}
    assert psi["cpu"]["full"] == {"avg10": 1.0, "avg300": 0.5, "avg600": 0.1, "total": 1000}
    assert psi["memory"]["some"]["total"] == 999
    assert psi["memory"]["full"]["avg10"] == 0.5
    # avg* are gauges (float); total is a cumulative int in microseconds.
    assert isinstance(psi["cpu"]["some"]["avg10"], float)
    assert isinstance(psi["cpu"]["some"]["total"], int)


def test_read_psi_cpu_without_full_line(tmp_path):
    # /proc/pressure/cpu has no `full` line on kernels < 5.13: parse anyway.
    mon = _PsiMonitor(_write_psi(tmp_path, cpu=CPU_SOME_ONLY))
    psi = mon._read_psi()
    assert "full" not in psi["cpu"]
    assert psi["cpu"]["some"]["total"] == 0
    assert "full" in psi["memory"]


def test_read_psi_missing_dir_returns_none(tmp_path):
    mon = _PsiMonitor(str(tmp_path / "nope"))
    assert mon._read_psi() is None


def test_read_psi_missing_memory_file_returns_none(tmp_path):
    mon = _PsiMonitor(_write_psi(tmp_path, mem=None))
    assert mon._read_psi() is None


def test_read_psi_malformed_returns_none(tmp_path):
    # Bad value, non key=value token, and a file with no `some` line -> None.
    mon = _PsiMonitor(_write_psi(tmp_path, cpu=CPU_BAD_VALUE))
    assert mon._read_psi() is None
    mon2 = _PsiMonitor(_write_psi(tmp_path / "2", cpu=CPU_BAD_TOKEN))
    assert mon2._read_psi() is None
    mon3 = _PsiMonitor(_write_psi(tmp_path / "3", cpu=CPU_NO_SOME))
    assert mon3._read_psi() is None


def test_psi_flat_row_layout():
    psi = {
        "cpu": {
            "some": {"avg10": 12.34, "avg300": 5.67, "avg600": 2.0, "total": 123456},
            "full": {"avg10": 1.0, "avg300": 0.5, "avg600": 0.1, "total": 1000},
        },
        "memory": {
            "some": {"avg10": 3.0, "avg300": 1.0, "avg600": 0.5, "total": 999},
            "full": {"avg10": 0.5, "avg300": 0.2, "avg600": 0.1, "total": 40},
        },
    }
    row = _psi_flat_row(psi)
    assert list(row) == [
        "cpu_some_avg10",
        "cpu_some_avg300",
        "cpu_some_avg600",
        "cpu_some_total",
        "cpu_full_avg10",
        "cpu_full_avg300",
        "cpu_full_avg600",
        "cpu_full_total",
        "memory_some_avg10",
        "memory_some_avg300",
        "memory_some_avg600",
        "memory_some_total",
        "memory_full_avg10",
        "memory_full_avg300",
        "memory_full_avg600",
        "memory_full_total",
    ]
    assert row["cpu_some_avg10"] == 12.34
    assert row["memory_full_total"] == 40


def test_collect_psi_enqueues_flattened_row_with_shared_ts(monkeypatch, tmp_path):
    mon = _PsiMonitor(_write_psi(tmp_path))
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = {}
    monkeypatch.setattr(base._ProcRawWriter, "enqueue_psi", lambda self, ts, row: captured.update(ts=ts, row=row))
    try:
        mon.collect_psi(ts="2026-01-01 00:00:00.123456")
    finally:
        mon._proc_raw.close()
    assert captured["ts"] == "2026-01-01 00:00:00.123456"
    assert captured["row"]["cpu_some_total"] == 123456
    assert captured["row"]["memory_some_avg10"] == 3.0


def test_collect_psi_mints_ts_when_standalone(monkeypatch, tmp_path):
    mon = _PsiMonitor(_write_psi(tmp_path))
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = {}
    monkeypatch.setattr(base._ProcRawWriter, "enqueue_psi", lambda self, ts, row: captured.update(ts=ts))
    try:
        mon.collect_psi()
    finally:
        mon._proc_raw.close()
    # standalone calls mint their own timestamp with microseconds (26 chars, one dot)
    assert captured["ts"].count(".") == 1 and len(captured["ts"]) == 26


def test_collect_psi_warns_once_when_psi_missing(caplog):
    import logging

    mon = _PsiMonitor("/nonexistent/pressure")
    with caplog.at_level(logging.WARNING):
        mon.collect_psi(ts="t1")
        mon.collect_psi(ts="t2")
    warns = [r for r in caplog.records if "PSI collection skipped" in r.message]
    assert len(warns) == 1
