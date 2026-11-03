"""Per-VM sampling cost controls: /proc discovery, jiffies CPU%, heavy stride.

The heavy memory reads (numa_maps + smaps_rollup) walk the target's page
tables -- ~35 ms per GiB with 4 KiB pages. These tests pin the behavior of the
CPU-budgeted refresh so a large fleet gets a bounded, measured value instead
of either a runaway monitor or an invented one.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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


def _monitor(per_vm_cost=0.0001):
    mon = _StubMonitor()
    mon._read_pid_jiffies = lambda pid: (0, 0)
    mon._read_pss_mb = MagicMock(return_value=1.0)
    # each pid reports its own memory so carry-forward can be told apart
    mon.get_vm_memory_from_numastat = MagicMock(side_effect=lambda pid: {"total_mb": float(pid), "per_node": {}})
    mon.heavy_refresh.cost_per_vm = per_vm_cost
    return mon


def _candidates(pids):
    return [{"pid": pid, "vm_name": f"vm-{pid}", "status": "sleeping"} for pid in pids]


def _fresh_pids(mon):
    """Pids whose heavy files were read in the last collection."""
    return [call.args[0] for call in mon.get_vm_memory_from_numastat.call_args_list]


def test_small_cheap_fleet_reads_every_vm_every_cycle():
    mon = _monitor(per_vm_cost=0.0001)
    cands = _candidates([101, 102, 103, 104])

    for _ in range(3):
        mon.get_vm_memory_from_numastat.reset_mock()
        mon._collect_vm_metrics_serial(cands)
        assert _fresh_pids(mon) == [101, 102, 103, 104]
        assert mon.heavy_refresh.stride == 1


def test_stride_refreshes_each_vm_once_per_stride_cycles():
    mon = _monitor()
    mon.heavy_refresh.stride = 4
    # Keep the synthetic stride: real heavy cost here is ~0 and would reset it.
    mon.heavy_refresh.end_cycle = lambda n: None
    cands = _candidates([101, 102, 103, 104, 105, 106, 107, 108])

    seen = []
    for cycle in range(5):  # cold start + one full 4-cycle rotation
        mon.get_vm_memory_from_numastat.reset_mock()
        vms = mon._collect_vm_metrics_serial(cands)
        fresh = _fresh_pids(mon)
        if cycle == 0:
            assert sorted(fresh) == [101, 102, 103, 104, 105, 106, 107, 108]  # cold start
            continue
        assert len(fresh) == 2  # 8 VMs over a stride of 4
        seen.extend(fresh)
        # Every record still carries ITS OWN last measured value
        assert {vm["pid"]: vm["memory_mb"] for vm in vms}[fresh[0]] == float(fresh[0])

    assert sorted(seen) == [101, 102, 103, 104, 105, 106, 107, 108]


def test_new_pid_is_measured_immediately_not_waited_for_its_slot():
    mon = _monitor()
    mon.heavy_refresh.stride = 8
    mon.heavy_refresh.end_cycle = lambda n: None
    cands = _candidates([101, 102, 103, 104])

    mon._collect_vm_metrics_serial(cands)  # all cold: all read
    mon.get_vm_memory_from_numastat.reset_mock()

    vms = mon._collect_vm_metrics_serial(cands + _candidates([105]))

    assert 105 in _fresh_pids(mon)
    assert {vm["pid"]: vm["memory_mb"] for vm in vms}[105] == 105.0


def test_stride_derived_from_measured_cost_and_budget():
    mon = _monitor()
    mon.interval = 2.0  # budget = 0.25 * 2.0 = 0.5 CPU-s per cycle

    mon.heavy_refresh.cost_per_vm = 0.040  # 40 ms/VM x 384 = 15.36 s full pass
    mon.heavy_refresh.end_cycle(384)
    assert mon.heavy_refresh.stride == 31

    mon.heavy_refresh.cost_per_vm = 0.0005  # cheap (THP-backed): fits the budget
    mon.heavy_refresh.end_cycle(384)
    assert mon.heavy_refresh.stride == 1


def test_stride_ewma_tracks_refresh_cost():
    mon = _monitor(per_vm_cost=0.0)
    cands = _candidates([101, 102])
    # Two fresh reads costing 4 ms and 8 ms; the cycle's estimate is their mean.
    ticks = iter([0.0, 0.004, 0.010, 0.018])

    with patch("vm_monitor.sampling.time.thread_time", side_effect=lambda: next(ticks)):
        mon._collect_vm_metrics_serial(cands)

    assert round(mon.heavy_refresh.cost_per_vm, 6) == 0.006


def test_parallel_path_honours_stride():
    mon = _monitor()
    mon.heavy_refresh.stride = 4
    mon.heavy_refresh.end_cycle = lambda n: None
    cands = _candidates([101, 102, 103, 104, 105, 106, 107, 108])

    mon._collect_vm_metrics_parallel(cands)  # cold start: everything measured
    mon.get_vm_memory_from_numastat.reset_mock()
    vms = mon._collect_vm_metrics_parallel(cands)

    assert len(_fresh_pids(mon)) == 2  # 8 VMs over a stride of 4
    assert len(vms) == 8  # ...and every VM is still reported


def test_recycled_pid_remeasures_and_resets_cpu_baseline():
    """A recycled pid is a different VM: no carry-forward, no stale tick base."""
    mon = _monitor()
    mon.heavy_refresh.stride = 8
    mon.heavy_refresh.end_cycle = lambda n: None

    mon._collect_vm_metrics_serial([{"pid": 101, "pid_start": 111, "vm_name": "vm-101", "status": "sleeping"}])
    mon._proc_cpu_prev[101] = (500, 0.0)  # ticks of the VM that held the pid before
    mon.get_vm_memory_from_numastat.reset_mock()

    vms = mon._collect_vm_metrics_serial([{"pid": 101, "pid_start": 222, "vm_name": "vm-101", "status": "sleeping"}])

    assert _fresh_pids(mon) == [101]  # its slot was not up, but the identity changed
    assert vms[0]["memory_mb"] == 101.0
    assert vms[0]["cpu_percent"] == 0.0
    assert mon._proc_cpu_prev[101][0] == 0  # re-seeded from the stub jiffies


def test_cube_shim_discovery_reads_cmdline_for_truncated_comm(monkeypatch):
    """comm 'containerd-shim' is shared with runc; only the cmdline tells them apart."""
    from vm_monitor.vmm.cubesandbox import CubeSandboxMonitor

    procs = {
        100: ("containerd-shim", "S", "/usr/bin/containerd-shim-cube-rs -id sandbox-a -namespace cube"),
        101: ("containerd-shim", "S", "/usr/bin/containerd-shim-runc-v2 -namespace k8s.io"),
        102: ("sshd", "S", "sshd: root"),
    }
    monkeypatch.setattr("vm_monitor.base.os.listdir", lambda _p: [str(p) for p in procs])
    monkeypatch.setattr("vm_monitor.base.read_comm_state", lambda pid: (*procs[pid][:2], pid))
    monkeypatch.setattr("vm_monitor.base.read_cmdline", lambda pid: procs[pid][2])

    cands = CubeSandboxMonitor()._discover_vm_processes()

    assert [c["pid"] for c in cands] == [100]
    assert cands[0]["vm_name"] == "sandbox-a"
    assert cands[0]["status"] == "sleeping"
