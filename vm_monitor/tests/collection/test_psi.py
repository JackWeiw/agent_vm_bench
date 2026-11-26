"""Tests for the host PSI collector (_read_psi / collect_psi / host_psi_raw.csv).

PSI files (/proc/pressure/{cpu,memory}) exist only on kernels built with
CONFIG_PSI (and not disabled at boot), so every test feeds fixture files through
the ``psi_dir`` seam instead of the host's /proc. Fixtures use the mainline PSI
field layout (Documentation/accounting/psi.rst): avg10/avg60/avg300.
"""
from __future__ import annotations

import vm_monitor.base as base
from vm_monitor.base import PSI_MIN_INTERVAL, VMMonitorBase, _psi_flat_row

CPU_BOTH = """some avg10=12.34 avg60=5.67 avg300=2.00 total=123456
full avg10=1.00 avg60=0.50 avg300=0.10 total=1000
"""
MEM_BOTH = """some avg10=3.00 avg60=1.00 avg300=0.50 total=999
full avg10=0.50 avg60=0.20 avg300=0.10 total=40
"""
CPU_SOME_ONLY = """some avg10=0.00 avg60=0.00 avg300=0.00 total=0
"""
CPU_NO_SOME = """full avg10=1.0 avg60=0 avg300=0 total=5
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
    assert psi["cpu"]["some"] == {"avg10": 12.34, "avg60": 5.67, "avg300": 2.0, "total": 123456}
    assert psi["cpu"]["full"] == {"avg10": 1.0, "avg60": 0.5, "avg300": 0.1, "total": 1000}
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


def test_read_psi_bad_field_skips_field_keeps_rest(tmp_path):
    # One unparseable field: that field is skipped, the rest of the resource parses.
    cpu = "some avg10=1.5 avg60=bogus avg300=0.2 total=7\n"
    mon = _PsiMonitor(_write_psi(tmp_path, cpu=cpu))
    psi = mon._read_psi()
    assert psi["cpu"]["some"] == {"avg10": 1.5, "avg300": 0.2, "total": 7}
    assert "memory" in psi


def test_read_psi_unusable_resource_keeps_the_other(tmp_path):
    # cpu has no `some` line; memory is fine -> memory snapshot survives.
    mon = _PsiMonitor(_write_psi(tmp_path, cpu=CPU_NO_SOME))
    psi = mon._read_psi()
    assert "cpu" not in psi
    assert psi["memory"]["some"]["total"] == 999


def test_read_psi_non_utf8_file_skips_resource_keeps_the_other(tmp_path):
    # A decode error in one file skips only that resource (ValueError is caught,
    # never escapes into collect_sample).
    d = tmp_path / "pressure"
    d.mkdir(parents=True)
    (d / "cpu").write_bytes(bytes([0xFF, 0xFE]) + b"some avg10=1.0")
    (d / "memory").write_text(MEM_BOTH)
    mon = _PsiMonitor(str(d))
    psi = mon._read_psi()
    assert "cpu" not in psi
    assert psi["memory"]["some"]["total"] == 999


def test_read_psi_garbage_both_files_returns_empty(tmp_path):
    # Readable but nothing usable -> {} (caller keeps sampling), never None.
    mon = _PsiMonitor(_write_psi(tmp_path, cpu=CPU_NO_SOME, mem=CPU_NO_SOME))
    assert mon._read_psi() == {}


def test_psi_flat_row_layout():
    psi = {
        "cpu": {
            "some": {"avg10": 12.34, "avg60": 5.67, "avg300": 2.0, "total": 123456},
            "full": {"avg10": 1.0, "avg60": 0.5, "avg300": 0.1, "total": 1000},
        },
        "memory": {
            "some": {"avg10": 3.0, "avg60": 1.0, "avg300": 0.5, "total": 999},
            "full": {"avg10": 0.5, "avg60": 0.2, "avg300": 0.1, "total": 40},
        },
    }
    row = _psi_flat_row(psi)
    assert list(row) == [
        "cpu_some_avg10",
        "cpu_some_avg60",
        "cpu_some_avg300",
        "cpu_some_total",
        "cpu_full_avg10",
        "cpu_full_avg60",
        "cpu_full_avg300",
        "cpu_full_total",
        "memory_some_avg10",
        "memory_some_avg60",
        "memory_some_avg300",
        "memory_some_total",
        "memory_full_avg10",
        "memory_full_avg60",
        "memory_full_avg300",
        "memory_full_total",
    ]
    assert row["cpu_some_avg10"] == 12.34
    assert row["memory_full_total"] == 40


def _capture_enqueue(monkeypatch):
    captured = {"calls": 0}
    monkeypatch.setattr(
        base._ProcRawWriter, "enqueue_psi", lambda self, ts, row: captured.__setitem__("calls", captured["calls"] + 1)
    )
    return captured


def test_collect_psi_throttles_to_avg10_window(monkeypatch, tmp_path):
    # One read per PSI_MIN_INTERVAL: reads at t=0 and t=10 enqueue; t=5 does not.
    mon = _PsiMonitor(_write_psi(tmp_path))
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = _capture_enqueue(monkeypatch)
    try:
        mon.collect_psi(ts="t0", now=0.0)
        mon.collect_psi(ts="t5", now=PSI_MIN_INTERVAL / 2)
        mon.collect_psi(ts="t10", now=PSI_MIN_INTERVAL)
    finally:
        mon._proc_raw.close()
    assert captured["calls"] == 2  # t0 + t10; t5 throttled away


def test_collect_psi_skips_reads_without_raw_writer(monkeypatch, tmp_path):
    # No log_dir -> no raw dumper -> PSI files are never even opened.
    mon = _PsiMonitor(_write_psi(tmp_path))
    mon._proc_raw = None
    calls = []

    def counting_read(self, psi_dir="/proc/pressure"):
        calls.append(1)
        return None

    monkeypatch.setattr(_PsiMonitor, "_read_psi", counting_read)
    mon.collect_psi(ts="t0")
    assert calls == []


def test_collect_psi_latches_missing_psi(monkeypatch, tmp_path, caplog):
    # Files missing -> one WARNING, then zero reads for the rest of the run.
    import logging

    mon = _PsiMonitor("/nonexistent/pressure")
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = _capture_enqueue(monkeypatch)
    reads = []
    real = _PsiMonitor._read_psi

    def counting_read(self, psi_dir="/proc/pressure"):
        reads.append(1)
        return real(self, psi_dir)

    monkeypatch.setattr(_PsiMonitor, "_read_psi", counting_read)
    try:
        with caplog.at_level(logging.WARNING):
            mon.collect_psi(ts="t0", now=0.0)
            mon.collect_psi(ts="t10", now=10.0)
            mon.collect_psi(ts="t20", now=20.0)
    finally:
        mon._proc_raw.close()
    assert len(reads) == 1  # latched: no reads after the first
    assert captured["calls"] == 0
    assert len([r for r in caplog.records if "PSI collection disabled" in r.message]) == 1


def test_collect_psi_rate_limits_unparseable(monkeypatch, tmp_path, caplog):
    # Readable but unparseable: keep sampling, warn at most once per warn window.
    import logging

    mon = _PsiMonitor(_write_psi(tmp_path, cpu=CPU_NO_SOME, mem=CPU_NO_SOME))
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = _capture_enqueue(monkeypatch)
    try:
        with caplog.at_level(logging.WARNING):
            mon.collect_psi(ts="t0", now=0.0)
            mon.collect_psi(ts="t10", now=10.0)
            mon.collect_psi(ts="t70", now=70.0)
    finally:
        mon._proc_raw.close()
    assert captured["calls"] == 0
    warns = [r for r in caplog.records if "nothing parsed" in r.message]
    assert len(warns) == 2  # t0 and t70; t10 is inside the warn window


def test_collect_psi_enqueues_flattened_row(monkeypatch, tmp_path):
    mon = _PsiMonitor(_write_psi(tmp_path))
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = {}
    monkeypatch.setattr(base._ProcRawWriter, "enqueue_psi", lambda self, ts, row: captured.update(ts=ts, row=row))
    try:
        mon.collect_psi(ts="2026-01-01 00:00:00.123456", now=0.0)
    finally:
        mon._proc_raw.close()
    assert captured["ts"] == "2026-01-01 00:00:00.123456"
    assert captured["row"]["cpu_some_total"] == 123456
    assert captured["row"]["memory_some_avg10"] == 3.0


def test_collect_psi_keeps_partial_snapshot(monkeypatch, tmp_path):
    # cpu unusable, memory fine -> memory-only row (never None-latched).
    mon = _PsiMonitor(_write_psi(tmp_path, cpu=CPU_NO_SOME))
    mon._proc_raw = base._ProcRawWriter(str(tmp_path))
    captured = {}
    monkeypatch.setattr(base._ProcRawWriter, "enqueue_psi", lambda self, ts, row: captured.update(ts=ts, row=row))
    try:
        mon.collect_psi(ts="t0", now=0.0)
    finally:
        mon._proc_raw.close()
    assert captured["row"] and all(k.startswith("memory_") for k in captured["row"])
