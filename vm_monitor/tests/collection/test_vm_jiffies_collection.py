"""Per-VM raw jiffies must be attached by BOTH collection paths.

vm_cpu_raw.csv (the per-VM CPU series) is written from utime/stime on each VM
dict. The serial path (<4 VMs) used to omit them, leaving the CSV header-only
on small fleets while the parallel path worked.
"""
from __future__ import annotations

import os

from vm_monitor.base import VMMonitorBase


class _StubMonitor(VMMonitorBase):
    """Touches no real /proc or /sys; only the collection paths under test."""

    def get_vms_realtime(self):
        return []

    def get_process_names(self):
        return ("stub_process",)

    def extract_vm_id(self, pid, cmdline):
        return f"vm-{pid}"

    def get_monitor_title(self):
        return "StubMonitor"

    def get_no_vm_message(self):
        return "No VMs"

    def get_csv_filename_prefix(self):
        return "stub_monitor"


def _monitor(jiffies):
    mon = _StubMonitor()
    mon.get_vm_memory_from_numastat = lambda pid: {}
    mon._extract_numastat_fields = lambda mem: {"memory_mb": 1.0}
    mon._read_pss_mb = lambda pid: 0.0
    mon._read_pid_jiffies = lambda pid: jiffies
    mon._finalize_vm_collection = lambda *args, **kwargs: None
    return mon


def _candidates(n):
    return [{"pid": os.getpid(), "vm_name": f"vm{i}", "status": "running"} for i in range(n)]


def test_serial_path_attaches_jiffies():
    vms = _monitor((100, 20))._collect_vm_metrics_serial(_candidates(1))
    assert (vms[0]["utime"], vms[0]["stime"]) == (100, 20)


def test_parallel_path_attaches_jiffies():
    vms = _monitor((100, 20))._collect_vm_metrics_parallel(_candidates(4))
    assert len(vms) == 4
    assert all((v["utime"], v["stime"]) == (100, 20) for v in vms)


def test_unreadable_pid_leaves_jiffies_unset():
    # Writers skip the row rather than emitting a None; keys stay off the dict.
    vms = _monitor(None)._collect_vm_metrics_serial(_candidates(1))
    assert "utime" not in vms[0] and "stime" not in vms[0]
