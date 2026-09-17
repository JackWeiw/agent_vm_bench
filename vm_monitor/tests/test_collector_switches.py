"""Tests for the per-collector enable/disable switches in vm_monitor/base.py.

A disabled collector must NOT be called from collect_sample / _disk_subsample_sleep;
its history stays empty so downstream sheets/SVGs omit it. The shared /proc/meminfo
+ /proc/vmstat read is skipped when none of its consumers (swap / host_mem_detail /
host_pressure) are enabled.
"""
from __future__ import annotations

import vm_monitor.base as base
from vm_monitor.base import VMMonitorBase


class _RecorderMonitor(VMMonitorBase):
    """VMMonitorBase that records which collectors ran instead of touching /proc."""

    def __init__(self):
        super().__init__()
        self.called: set[str] = set()

    def _record(self, name: str) -> None:
        self.called.add(name)

    # The 8 collectors invoked from collect_sample:
    def collect_hugepage_stats(self):  # noqa: D401, N802 - recorder seam
        self._record("hugepage")

    def collect_numa_cpu(self):  # noqa: N802 - recorder seam
        self._record("numa_cpu")

    def collect_host_stats(self):  # noqa: N802 - recorder seam
        self._record("host_stats")

    def collect_swap_stats(self, meminfo=None, vmstat=None):  # noqa: N802 - recorder seam
        self._record("swap")

    def collect_host_mem_detail(self, meminfo=None):  # noqa: N802 - recorder seam
        self._record("host_mem_detail")

    def collect_host_pressure(self, meminfo=None, vmstat=None):  # noqa: N802 - recorder seam
        self._record("pressure")

    def get_numa_nodes_memory(self):  # noqa: N802 - recorder seam
        self._record("numa_memory")

    def collect_vm_total_memory(self, vms):  # noqa: N802 - recorder seam
        self._record("vm_total")

    # The 2 collectors invoked from _disk_subsample_sleep:
    def collect_disk_stats(self):  # noqa: N802 - recorder seam
        self._record("disk")

    def collect_ublk_count(self):  # noqa: N802 - recorder seam
        self._record("ublk")

    # Shared /proc reads -- counted so we can assert the skip:
    def _read_meminfo(self):  # noqa: N802 - recorder seam
        self._record("meminfo")
        return {}

    def _read_vmstat(self):  # noqa: N802 - recorder seam
        self._record("vmstat")
        return {}

    # ABC seams:
    def get_vms_realtime(self):  # noqa: N802 - ABC seam
        return []

    def get_process_names(self):  # noqa: N802 - ABC seam
        return ("test_process",)

    def extract_vm_id(self, pid, cmdline):  # noqa: N802 - ABC seam
        return "vm0"

    def get_monitor_title(self):
        return "RecorderMonitor"

    def get_no_vm_message(self):
        return "No VMs detected"

    def get_csv_filename_prefix(self):
        return "recorder_monitor"


# collect_sample drives these 8 + the shared meminfo/vmstat reads (disk/ublk are
# sub-sampled separately by _disk_subsample_sleep).
_SAMPLE_DRIVEN = {
    "hugepage",
    "numa_cpu",
    "host_stats",
    "swap",
    "host_mem_detail",
    "pressure",
    "numa_memory",
    "vm_total",
    "meminfo",
    "vmstat",
}


def test_default_all_collectors_enabled():
    mon = _RecorderMonitor()
    mon.collect_sample()
    assert mon.called == _SAMPLE_DRIVEN


def test_disable_subset_skips_only_named_collectors():
    mon = _RecorderMonitor()
    mon.disable_collectors({"swap", "disk", "ublk"})
    mon.collect_sample()
    assert "swap" not in mon.called
    assert "host_mem_detail" in mon.called
    assert "numa_memory" in mon.called
    assert "vm_total" in mon.called
    assert "meminfo" in mon.called  # host_mem_detail still needs the shared read


def test_shared_proc_read_skipped_when_all_consumers_disabled():
    # Disabling swap + host_mem_detail + pressure means no consumer of the
    # shared /proc/meminfo + /proc/vmstat read is left -> the read is skipped.
    mon = _RecorderMonitor()
    mon.disable_collectors({"swap", "host_mem_detail", "pressure"})
    mon.collect_sample()
    assert "meminfo" not in mon.called
    assert "vmstat" not in mon.called
    assert "swap" not in mon.called
    assert "host_mem_detail" not in mon.called
    assert "hugepage" in mon.called  # other collectors still run


def test_disk_subsample_skips_disabled_disk(monkeypatch):
    mon = _RecorderMonitor()
    mon.running = True
    mon.disable_collectors({"disk"})
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    ticks = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(ticks, 999.0))
    mon._disk_subsample_sleep(2.0)
    assert "disk" not in mon.called
    assert "ublk" in mon.called  # ublk is independent and still enabled


def test_disk_subsample_skips_disabled_ublk(monkeypatch):
    mon = _RecorderMonitor()
    mon.running = True
    mon.disable_collectors({"ublk"})
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    ticks = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(ticks, 999.0))
    mon._disk_subsample_sleep(2.0)
    assert "ublk" not in mon.called
    assert "disk" in mon.called


def test_disable_unknown_name_silently_ignored():
    mon = _RecorderMonitor()
    before = set(mon.enabled_collectors)
    mon.disable_collectors({"bogus", "swap"})
    assert "swap" not in mon.enabled_collectors
    # bogus is not a known collector -> ignored, no crash, no other change
    assert mon.enabled_collectors == before - {"swap"}
