"""Streaming-reader tests: chunking must not change the numbers.

The long raw CSVs are reduced chunk by chunk (see ``frames._stream_vm`` and
``frames._stream_numa_cpu``). A chunk boundary that splits a sample is the
subtle case: the last row per VM is carried into the next chunk so the delta
across the boundary survives. These tests pin that property by deriving the
same file twice with a tiny and a huge chunk size and requiring identical
output (including identical NaN placement).
"""
from __future__ import annotations

import csv
import math

import pandas as pd

from vm_monitor.export.raw import frames
from vm_monitor.export.raw.report import _derive_host, _derive_numa, _derive_per_vm

_CPU_HDR = [
    "timestamp",
    "cpu_user",
    "cpu_nice",
    "cpu_system",
    "cpu_idle",
    "cpu_iowait",
    "cpu_irq",
    "cpu_softirq",
    "cpu_steal",
    "cpu_guest",
    "cpu_guest_nice",
    "ctxt",
    "btime",
    "processes",
    "procs_running",
    "procs_blocked",
    "softirq_total",
]


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _seed(tmp_path, vms=5, samples=7, memory_mb=True):
    """Host CPU + a sample-major VM CSV wide enough to split mid-sample."""
    _write(
        tmp_path / "host_cpu_raw.csv",
        _CPU_HDR,
        [
            [
                str(i),
                str(100 * i),
                "0",
                str(50 * i),
                str(8000 * i),
                str(10 * i),
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
            for i in range(samples)
        ],
    )
    vm_hdr = ["timestamp", "pid", "vm_name", "utime", "stime", "pss_mb"]
    if memory_mb:
        vm_hdr.append("memory_mb")
    rows = []
    for i in range(samples):
        for v in range(vms):
            row = [str(i * 2), str(100 + v), f"vm{v}", str(i * (v + 1) * 100), str(i * (v + 1) * 20), str(100 + v)]
            if memory_mb:
                row.append(str(110 + v))
            rows.append(row)
    _write(tmp_path / "vm_cpu_raw.csv", vm_hdr, rows)


def _same(left: pd.DataFrame, right: pd.DataFrame) -> None:
    assert list(left.columns) == list(right.columns)
    assert left.shape == right.shape
    for column in left.columns:
        a, b = left[column].to_numpy(float), right[column].to_numpy(float)
        assert (pd.isna(a) == pd.isna(b)).all(), column
        both = ~pd.isna(a)
        assert pd.Series(a[both]).equals(pd.Series(b[both])), column


def test_vm_stream_chunk_boundary_matches_single_chunk(tmp_path, monkeypatch):
    """A sample split across chunks must produce the same Host/Per-VM sheets."""
    _seed(tmp_path, vms=5, samples=7)

    monkeypatch.setattr(frames, "_CHUNK_ROWS", 10**6)
    host_ref, (cores_ref, pss_ref) = _derive_host(tmp_path), _derive_per_vm(tmp_path)

    # 3 rows per chunk splits every sample (5 VMs) and every boundary.
    monkeypatch.setattr(frames, "_CHUNK_ROWS", 3)
    host_chunked, (cores_chunked, pss_chunked) = _derive_host(tmp_path), _derive_per_vm(tmp_path)

    _same(host_ref, host_chunked)
    _same(cores_ref, cores_chunked)
    _same(pss_ref, pss_chunked)


def test_vm_stream_birth_and_death_across_chunks(tmp_path, monkeypatch):
    """Method A must survive a chunk boundary: a VM that starts or dies mid-run
    is excluded from vm_cores on intervals it does not span, however the rows
    were chunked, while still counting toward concurrent_vms."""
    _write(
        tmp_path / "host_cpu_raw.csv",
        _CPU_HDR,
        [
            [
                str(i * 2),
                str(100 * i),
                "0",
                str(50 * i),
                str(8000 * i),
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
            for i in range(5)
        ],
    )
    rows = [
        ["0", "1", "vm0", "0", "0", "100.0"],
        ["0", "2", "vm1", "0", "0", "100.0"],
        ["2", "1", "vm0", "100", "0", "100.0"],
        ["2", "2", "vm1", "120", "0", "100.0"],
        ["4", "1", "vm0", "200", "0", "100.0"],
        ["4", "2", "vm1", "240", "0", "100.0"],  # vm1 dies after t=4
        ["6", "1", "vm0", "400", "0", "100.0"],
        ["6", "3", "vm2", "80", "0", "100.0"],  # vm2 born at t=6
        ["8", "1", "vm0", "500", "0", "100.0"],
        ["8", "3", "vm2", "160", "0", "100.0"],
    ]
    _write(tmp_path / "vm_cpu_raw.csv", ["timestamp", "pid", "vm_name", "utime", "stime", "pss_mb"], rows)

    def build(chunk_rows):
        monkeypatch.setattr(frames, "_CHUNK_ROWS", chunk_rows)
        return _derive_host(tmp_path)

    ref = build(10**6)
    chunked = build(2)  # cuts every sample and every birth/death boundary
    _same(ref, chunked)

    # t=2: vm0 +100 and vm1 +120 over dt=2 -> 1.1 cores.
    assert math.isclose(ref.iloc[1]["vm_cores"], 220 / 100 / 2, abs_tol=1e-9)
    # t=4: vm1's last interval -> its +120 still counts; vm0 +100.
    assert math.isclose(ref.iloc[2]["vm_cores"], 220 / 100 / 2, abs_tol=1e-9)
    # t=6: vm1 gone, vm2 has no predecessor -> only vm0's +200 over dt=2.
    assert math.isclose(ref.iloc[3]["vm_cores"], 200 / 100 / 2, abs_tol=1e-9)
    assert ref.iloc[3]["concurrent_vms"] == 2  # vm0 + vm2, the newcomer still counted
    # t=8: vm0 +100 and vm2 +80.
    assert math.isclose(ref.iloc[4]["vm_cores"], 180 / 100 / 2, abs_tol=1e-9)


def test_vm_stream_values_survive_chunking(tmp_path, monkeypatch):
    """Spot-check the streamed numbers against hand arithmetic, not just parity."""
    _seed(tmp_path, vms=2, samples=3)
    monkeypatch.setattr(frames, "_CHUNK_ROWS", 3)  # splits the 2-VM sample
    cores, pss = _derive_per_vm(tmp_path)

    # vm0: utime+stime = 0 -> 120 -> 240 jiffies, dt=2 -> 0.6 cores.
    assert math.isclose(cores[cores["t_s"] == 2.0]["vm0"].iloc[0], 120 / 100 / 2, abs_tol=1e-9)
    assert math.isclose(cores[cores["t_s"] == 4.0]["vm0"].iloc[0], 120 / 100 / 2, abs_tol=1e-9)
    # vm1: 120 jiffies per sample (utime v+1=2 * 100, plus stime 40).
    assert math.isclose(cores[cores["t_s"] == 2.0]["vm1"].iloc[0], 240 / 100 / 2, abs_tol=1e-9)
    # PSS is a gauge: valid on the first sample too.
    assert math.isclose(pss["vm0"].iloc[0], 100 / 1024, abs_tol=1e-9)


def test_vm_stream_without_memory_column(tmp_path):
    """A capture predating memory_mb must not invent a vm_mem_gb column."""
    _seed(tmp_path, memory_mb=False)
    host = _derive_host(tmp_path)
    assert "vm_mem_gb" not in host.columns
    assert "fc_pss_gb" in host.columns


def test_numa_stream_chunk_boundary_matches_single_chunk(tmp_path, monkeypatch):
    """Per-node sums must not depend on where the percpu chunks break."""
    _write(tmp_path / "numa_cpu_map.csv", ["node", "cpu"], [["0", "0"], ["0", "1"], ["1", "2"], ["1", "3"]])
    rows = []
    for sample in range(6):
        for cpu in range(4):
            busy = (cpu + 1) * 10 * sample
            idle = 1000 * sample
            rows.append([str(sample * 2), str(cpu), str(busy), "0", str(busy), str(idle), "0", "0", "0", "0"])
    _write(
        tmp_path / "host_percpu_raw.csv",
        ["timestamp", "cpu_id", "user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"],
        rows,
    )
    _write(
        tmp_path / "host_cpu_raw.csv",
        _CPU_HDR,
        [
            [str(i * 2), "0", "0", "0", str(i), "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"]
            for i in range(6)
        ],
    )

    monkeypatch.setattr(frames, "_CHUNK_ROWS", 10**6)
    ref = _derive_numa(tmp_path)
    monkeypatch.setattr(frames, "_CHUNK_ROWS", 3)
    chunked = _derive_numa(tmp_path)

    _same(ref, chunked)
    # Node 0 at sample 1: user+system = 2*(10+20) = 60, idle = 2000.
    assert math.isclose(ref.iloc[1]["NUMA0 CPU (%)"], 100 * (1 - 2000 / 2060), abs_tol=1e-6)


def test_numa_derivation_does_not_mutate_shared_frames(tmp_path):
    """MB -> GB scaling must stay local, so a second derive sees the same numbers.

    ``resource_report`` and ``host_resources`` both derive from frames loaded
    once; an in-place /1024 would halve the values on the second call.
    """
    _write(
        tmp_path / "host_cpu_raw.csv",
        _CPU_HDR,
        [[str(i), "0", "0", "0", str(i), "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"] for i in range(3)],
    )
    _write(
        tmp_path / "numa_mem_raw.csv",
        ["timestamp", "node", "used_mb", "free_mb"],
        [[str(i), "0", "2048", "1024"] for i in range(3)],
    )
    shared = frames._load_frames(tmp_path)
    first = _derive_numa(frames=shared)
    second = _derive_numa(frames=shared)

    pd.testing.assert_frame_equal(first, second)
    # 2048 MB -> 2 GB, not 2/1024 after a second pass.
    assert math.isclose(second["NUMA0 Used (GB)"].iloc[0], 2.0, abs_tol=1e-9)
