"""Tests for the user-specified host-process collector (collect_host_procs).

The collector is a generalized sibling of collect_ublk_daemon: it resolves a
pid lazily by comm (+ optional cmdline substring), reads /proc/<pid>/stat
utime+stime + smaps_rollup PSS, enqueues raw jiffies to hostproc_cpu_raw.csv,
and keeps a derived cores history. These tests patch the /proc seams so they
run on any host (no real agentENV server needed).
"""
from __future__ import annotations

import logging

import vm_monitor.base as base
from vm_monitor.base import VMMonitorBase


class _StubMonitor(VMMonitorBase):
    """Concrete VMMonitorBase that touches no real /proc at runtime."""

    def get_process_names(self):  # noqa: N802 - ABC seam
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):  # noqa: N802 - ABC seam
        return "vm0"

    def get_vms_realtime(self):  # noqa: N802 - ABC seam
        return []

    def get_monitor_title(self):
        return "StubMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "stub_monitor"


def test_set_host_proc_specs_parses_comm_and_cmdline():
    mon = _StubMonitor()
    mon.set_host_proc_specs(["server:release/server", "uvm-ublk-daemon", "  ", ":bad"])
    assert mon._hostproc_specs == [
        ("server", "server", "release/server"),
        ("uvm-ublk-daemon", "uvm-ublk-daemon", None),
    ]
    # empty comm dropped; blank spec dropped.


def test_collect_host_procs_derives_cores_from_jiffy_delta(monkeypatch):
    """Two samples: jiffy delta / CLK_TCK / dt -> cores; raw jiffies enqueued."""
    monkeypatch.setattr(base.VMMonitorBase, "_resolve_hostproc_pid", staticmethod(lambda comm, sub: (700, 1)))
    monkeypatch.setattr(base.VMMonitorBase, "_read_pid_jiffies", lambda self, pid: (110, 10))
    monkeypatch.setattr(base.VMMonitorBase, "_read_pss_mb", lambda self, pid: 512.0)
    ticks = iter([10.0, 11.0])
    monkeypatch.setattr(base.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(base.time, "monotonic", lambda: 11.0)

    mon = _StubMonitor()
    mon.set_host_proc_specs(["server:release/server"])
    # prime the pid cache + prev on first call
    mon.collect_host_procs()
    # second call: same pid, prev set -> cores = (120)/CLK_TCK/dt
    monkeypatch.setattr(base.VMMonitorBase, "_read_pid_jiffies", lambda self, pid: (220, 20))
    ticks2 = iter([11.0, 12.0])
    monkeypatch.setattr(base.time, "perf_counter", lambda: next(ticks2))
    mon.collect_host_procs()
    assert len(mon.hostproc_history) == 2
    rec = mon.hostproc_history[1]
    assert rec["name"] == "server"
    assert rec["pss_mb"] == 512.0
    # jiffies went (110+10)=120 -> (220+20)=240, dt=1.0, CLK_TCK=100 -> 1.2 cores
    assert abs(rec["cores"] - 1.2) < 1e-6


def test_collect_host_procs_warns_on_multi_match(monkeypatch, caplog):
    """A spec matching >1 pid logs ONE warning (per spec, once); takes the first."""
    monkeypatch.setattr(base.VMMonitorBase, "_resolve_hostproc_pid", staticmethod(lambda comm, sub: (700, 2)))
    monkeypatch.setattr(base.VMMonitorBase, "_read_pid_jiffies", lambda self, pid: (5, 1))
    monkeypatch.setattr(base.VMMonitorBase, "_read_pss_mb", lambda self, pid: 0.0)
    monkeypatch.setattr(base.time, "perf_counter", lambda: 1.0)

    mon = _StubMonitor()
    mon.set_host_proc_specs(["server:release/server"])
    with caplog.at_level(logging.WARNING, logger="vm_monitor.base"):
        mon.collect_host_procs()  # resolves -> multi-match warning
        mon.collect_host_procs()  # pid cached -> NO second warning
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "matched" in r.message]
    assert len(warns) == 1, "multi-match warning must fire once per spec, not per sample"
    assert mon._hostproc_pids["server"] == 700


def test_collect_host_procs_warns_after_missing_streak(monkeypatch, caplog):
    """A never-seen spec fires a WARNING after the miss threshold; DEBUG otherwise."""
    monkeypatch.setattr(base.VMMonitorBase, "_resolve_hostproc_pid", staticmethod(lambda comm, sub: (None, 0)))
    monkeypatch.setattr(base.time, "perf_counter", lambda: 1.0)

    mon = _StubMonitor()
    mon._HOSTPROC_MISS_THRESHOLD = 3  # speed up the threshold
    mon.set_host_proc_specs(["typo:badcmd"])
    with caplog.at_level(logging.DEBUG, logger="vm_monitor.base"):
        for _ in range(3):
            mon.collect_host_procs()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "not found after" in r.message]
    assert len(warns) == 1, "missing-spec WARNING fires once at threshold, not per sample"
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debugs, "transient misses log DEBUG"


def test_collect_host_procs_reresolves_after_pid_death(monkeypatch):
    """When /proc/<pid>/stat becomes unreadable (process died), the cached pid
    is cleared and re-resolved next sample -- same restart handling as ublk.

    _resolve_hostproc_pid is called only when the pid is NOT cached (initial
    resolve + after a death), so its sequence is 2 entries; _read_pid_jiffies
    is called every sample (4 entries, the 3rd None simulates the death)."""
    states = iter([(700, 1), (800, 1)])  # initial resolve, post-death re-resolve
    monkeypatch.setattr(base.VMMonitorBase, "_resolve_hostproc_pid", staticmethod(lambda comm, sub: next(states)))
    jiffies = iter([(10, 1), (20, 2), None, (5, 1)])  # stable, stable, gone, restarted
    monkeypatch.setattr(base.VMMonitorBase, "_read_pid_jiffies", lambda self, pid: next(jiffies))
    monkeypatch.setattr(base.VMMonitorBase, "_read_pss_mb", lambda self, pid: 0.0)
    monkeypatch.setattr(base.time, "perf_counter", lambda: 1.0)

    mon = _StubMonitor()
    mon.set_host_proc_specs(["server:release/server"])
    mon.collect_host_procs()  # resolve 700
    mon.collect_host_procs()  # stable 700
    mon.collect_host_procs()  # jiffies None -> clear pid, re-resolve next
    assert mon._hostproc_pids["server"] is None  # cleared
    mon.collect_host_procs()  # re-resolve 800
    assert mon._hostproc_pids["server"] == 800


def test_collect_host_procs_no_specs_is_noop():
    mon = _StubMonitor()
    mon.collect_host_procs()  # no specs -> immediate return
    assert mon.hostproc_history == []
