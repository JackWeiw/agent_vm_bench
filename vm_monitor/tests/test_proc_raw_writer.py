"""Smoke test for the background raw dumper (_ProcRawWriter).

The collectors/structure tests cover the sampling pipeline and the xlsx sheet
set, but the dumper's wide + long CSV serialization (header-from-first-row,
per-cpu numeric sort, stat-aggregate field layout, per-VM long rows, one-shot
numa map) is not otherwise exercised. One check that fails if that logic breaks.
"""
from __future__ import annotations

import csv
import os
import tempfile
import threading
import time

from vm_monitor.base import _ProcRawWriter


def test_proc_raw_writer_dumps_all_sources():
    meminfo = {"MemTotal": 100, "MemFree": 50}
    vmstat = {"pswpin": 1, "pgscan_kswapd": 2}
    stat = {
        "cpu": [10, 0, 5, 80, 1, 0, 0, 0, 0, 0],
        # cpu ids deliberately non-sorted as strings ("10" < "2" lexicographically)
        # to pin the int-key numeric sort in _write_stat.
        "cpus": {
            "0": [1, 0, 0, 8, 0, 0, 0, 0, 0, 0],
            "10": [3, 0, 0, 6, 0, 0, 0, 0, 0, 0],
            "2": [2, 0, 0, 7, 0, 0, 0, 0, 0, 0],
        },
        "ctxt": 123,
        "btime": 999,
        "processes": 5,
        "procs_running": 2,
        "procs_blocked": 1,
        "softirq": [100, 1, 2, 3],
    }
    vms = [
        {"pid": 111, "name": "firecracker-vm0", "utime": 500, "stime": 50},
        {"pid": 222, "name": "firecracker-vm1", "utime": 900, "stime": 90},
        # unreadable VM (utime/stime None) -> row skipped, not a crash
        {"pid": 333, "name": "firecracker-vm2", "utime": None, "stime": None},
    ]
    numa_map = {0: [0, 1, 2], 5: [10, 11]}
    # raw /sys/block/<dev>/stat splits: 11 fields (old kernel) + 17 fields (new).
    dev_raw = {
        "sda": ["100", "5", "2000", "40", "200", "10", "4000", "80", "2", "120", "121"],
        "nvme0n1": [
            "10",
            "0",
            "100",
            "1",
            "20",
            "0",
            "200",
            "2",
            "0",
            "3",
            "3",
            "1",
            "0",
            "50",
            "1",
            "5",
            "0",  # discards (11-14) + flush (15-16)
        ],
    }

    with tempfile.TemporaryDirectory() as d:
        w = _ProcRawWriter(d)
        w.enqueue_proc("2026-01-01 00:00:00", meminfo, vmstat, stat)
        w.enqueue_vm("2026-01-01 00:00:00", vms)
        w.enqueue_disk("2026-01-01 00:00:00", dev_raw)
        w.enqueue_numa_map(numa_map)
        w.close()

        for fname in (
            "host_mem_raw.csv",
            "host_vmstat_raw.csv",
            "host_cpu_raw.csv",
            "host_percpu_raw.csv",
            "vm_cpu_raw.csv",
            "disk_io_raw.csv",
            "numa_cpu_map.csv",
        ):
            assert os.path.exists(os.path.join(d, "raw_data", fname)), f"{fname} not written"

        # host meminfo wide: header from first row, one row, timestamp prepended.
        with open(os.path.join(d, "raw_data", "host_mem_raw.csv")) as f:
            rows = list(csv.DictReader(f))
        assert rows == [{"timestamp": "2026-01-01 00:00:00", "MemTotal": "100", "MemFree": "50"}]

        # host per-cpu long: numeric cpu_id sort (0, 2, 10 -- NOT 0, 10, 2).
        with open(os.path.join(d, "raw_data", "host_percpu_raw.csv")) as f:
            rows = list(csv.DictReader(f))
        assert [r["cpu_id"] for r in rows] == ["0", "2", "10"]
        assert rows[0]["user"] == "1" and rows[1]["user"] == "2" and rows[2]["user"] == "3"

        # host cpu aggregate wide: cpu_<field> + ctxt + softirq_total/sub land.
        with open(os.path.join(d, "raw_data", "host_cpu_raw.csv")) as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["cpu_user"] == "10"
        assert rows[0]["cpu_iowait"] == "1"
        assert rows[0]["ctxt"] == "123"
        assert rows[0]["procs_running"] == "2"
        assert rows[0]["softirq_total"] == "100"
        assert rows[0]["softirq_1"] == "1"

        # per-VM long: one row per VM, None-jiffies VM skipped.
        with open(os.path.join(d, "raw_data", "vm_cpu_raw.csv")) as f:
            rows = list(csv.DictReader(f))
        assert [r["pid"] for r in rows] == ["111", "222"]
        assert rows[0]["utime"] == "500" and rows[0]["stime"] == "50"
        assert rows[0]["vm_name"] == "firecracker-vm0"

        # disk raw: one row per device; devices sorted; old-kernel (11-field)
        # row pads discards/flush to "" while new-kernel (17-field) keeps them.
        with open(os.path.join(d, "raw_data", "disk_io_raw.csv")) as f:
            rows = list(csv.DictReader(f))
        assert [r["device"] for r in rows] == ["nvme0n1", "sda"]  # sorted
        sda = next(r for r in rows if r["device"] == "sda")
        nvme = next(r for r in rows if r["device"] == "nvme0n1")
        assert sda["sectors_read"] == "2000" and sda["inflight"] == "2"
        assert sda["discards_completed"] == ""  # old kernel -> padded
        assert nvme["discards_completed"] == "1" and nvme["flush_requests"] == "5"

        # numa map: static long (node, cpu); idempotent -- second enqueue is a no-op.
        w2 = _ProcRawWriter(d)
        w2.enqueue_numa_map(numa_map)
        w2.enqueue_numa_map({99: [0]})  # second call must NOT overwrite
        w2.close()
        with open(os.path.join(d, "raw_data", "numa_cpu_map.csv")) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["node", "cpu"]
        body = [tuple(r) for r in rows[1:]]
        assert body == [("0", "0"), ("0", "1"), ("0", "2"), ("5", "10"), ("5", "11")]


def test_proc_raw_writer_dumps_ublk_daemon_jiffies():
    """ublk daemon raw utime+stime jiffies land in ublk_cpu_raw.csv (long, one
    row per 1s sub-sample) so an agent derives ublk-daemon CPU% via delta --
    the derived 'cores' rate in ublk_daemon_history alone is lossy. A pid
    restart (new pid) is just another row keyed by pid."""
    with tempfile.TemporaryDirectory() as d:
        w = _ProcRawWriter(d)
        w.enqueue_ublk("2026-01-01 00:00:00", 4242, 100, 10)
        w.enqueue_ublk("2026-01-01 00:00:01", 4242, 150, 15)  # pid stable
        w.enqueue_ublk("2026-01-01 00:00:02", 9999, 5, 1)  # pid restarted
        w.close()
        with open(os.path.join(d, "raw_data", "ublk_cpu_raw.csv")) as f:
            rows = list(csv.DictReader(f))
    assert [r["pid"] for r in rows] == ["4242", "4242", "9999"]
    assert rows[0]["utime"] == "100" and rows[0]["stime"] == "10"
    assert rows[1]["utime"] == "150" and rows[1]["stime"] == "15"
    assert rows[2]["utime"] == "5" and rows[2]["stime"] == "1"


def test_numa_map_retries_after_failed_first_write(monkeypatch):
    """A failed first numa_cpu_map write must NOT mark itself written, so a
    later cycle retries. Bug: flag set in finally -> flag True after failure ->
    every later call short-circuits -> the topology map is never written."""
    attempts = {"n": 0}
    real_open = open

    def flaky_open(path, *a, **k):
        if os.path.basename(str(path)) == "numa_cpu_map.csv":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("simulated disk full")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", flaky_open)
    with tempfile.TemporaryDirectory() as d:
        w = _ProcRawWriter(d)
        # Drain thread is idle (queue empty); exercise the writer's numa-map
        # path directly. Bug: 1st call raises + sets flag -> 2nd is a no-op.
        for _ in range(2):
            try:
                w._write_numa_map({0: [0, 1]})
            except OSError:
                pass  # bug path raises (flag wrongly set); fix swallows it
        w.close()
        assert attempts["n"] == 2, "numa map must retry after the first failure"
        with real_open(os.path.join(d, "raw_data", "numa_cpu_map.csv")) as f:
            rows = list(csv.reader(f))
    assert rows[0] == ["node", "cpu"]
    assert ("0", "0") in [tuple(r) for r in rows[1:]]


def test_close_drains_slow_writer_before_closing_handles(monkeypatch):
    """close() must wait for the drain thread to finish writing before closing
    file handles. A bounded join (join(timeout=5)) closes the handle mid-write
    when a single row takes longer than the timeout, losing the row."""
    opened = threading.Event()

    def slow_write_wide(self, key, fname, ts, row):
        # Replicate _write_wide's open-on-first-call so the handle exists, then
        # delay the row write past the old 5s join timeout.
        sink = self._wide.get(key)
        if sink is None:
            fh = open(os.path.join(self._dir, fname), "w", newline="", encoding="utf-8")
            dw = csv.DictWriter(fh, fieldnames=["timestamp", *row.keys()], restval="", extrasaction="ignore")
            dw.writeheader()
            self._wide[key] = (fh, dw)
            opened.set()
        fh, dw = self._wide[key]
        time.sleep(5.5)
        dw.writerow({"timestamp": ts, **row})
        fh.flush()

    monkeypatch.setattr(_ProcRawWriter, "_write_wide", slow_write_wide)
    with tempfile.TemporaryDirectory() as d:
        w = _ProcRawWriter(d)
        w.enqueue_proc("2026-01-01 00:00:00", {"MemTotal": 1}, {}, {})
        assert opened.wait(5), "drain did not open the handle in time"
        w.close()
        with open(os.path.join(d, "raw_data", "host_mem_raw.csv")) as f:
            rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["MemTotal"] == "1"
