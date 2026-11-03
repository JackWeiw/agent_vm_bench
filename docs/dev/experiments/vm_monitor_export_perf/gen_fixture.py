"""Generate a large raw_data fixture by tiling a real short capture.

The template directory must contain the raw CSVs written by a real
``vm-monitor`` run (host_cpu_raw.csv / host_mem_raw.csv / host_vmstat_raw.csv /
host_percpu_raw.csv / disk_io_raw.csv / numa_cpu_map.csv).  This script replays
the template at the target sample count, advancing every cumulative counter
monotonically and jittering gauges, then synthesizes vm_cpu_raw.csv (a fleet of
N VMs), ublk_cpu_raw.csv, hostproc_cpu_raw.csv and api_state_raw.csv.

Why tile a real capture: the column set is exactly what the monitor writes
(kernel-version dependent), so the fixture cannot silently miss a field the
exporter needs.  Values are synthetic, but their shape/units are real.

Usage:
  python gen_fixture.py --template /tmp/vmcap/raw_data --out /tmp/fixture2h \
      --samples 3600 --interval 2 --vms 384 --ublk 8 --hostproc 1
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

_T0 = "2026-11-03 00:00:00.000000"


def _stamps(n: int, interval: int) -> list[str]:
    base = pd.Timestamp("2026-11-03 00:00:00")
    return [str(base + pd.Timedelta(seconds=i * interval)) for i in range(n)]


def _counter_step(df: pd.DataFrame, col: str) -> float:
    """Per-sample increment for a cumulative column (median of the template)."""
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if len(s) < 2:
        return 1.0
    d = s.diff().dropna().to_numpy()
    d = d[np.isfinite(d)]
    return max(float(np.median(d)), 0.0)


def _tile_wide(src: Path, dst: Path, n: int, interval: int, counter_cols: list[str]) -> None:
    df = pd.read_csv(src)
    base = df.iloc[[0]].copy()
    steps = np.array([_counter_step(df, c) for c in counter_cols], dtype=float)
    stamps = _stamps(n, interval)
    out = pd.DataFrame(np.repeat(base.to_numpy(), n, axis=0), columns=base.columns, dtype=object)
    out["timestamp"] = stamps
    for j, c in enumerate(counter_cols):
        out[c] = (base[c].to_numpy(dtype=float)[0] + np.arange(n, dtype=float) * steps[j]).astype(object)
    dst.write_text("", encoding="utf-8")
    out.to_csv(dst, index=False)


def _tile_long(src: Path, dst: Path, n: int, interval: int, key: str, counter_cols: list[str]) -> None:
    df = pd.read_csv(src)
    first_ts = df["timestamp"].iloc[0]
    base = df[df["timestamp"] == first_ts].copy()
    keys = base[key].to_numpy()
    steps = {c: np.array([_counter_step(df[df[key] == k], c) for k in keys]) for c in counter_cols}
    stamps = _stamps(n, interval)
    chunk = max(1, min(n, 400_000 // max(1, len(base))))
    for start in range(0, n, chunk):
        end = min(n, start + chunk)
        m = end - start
        block = pd.DataFrame({k: np.tile(base[k].to_numpy(), m) for k in base.columns if k not in counter_cols})
        block["timestamp"] = np.repeat(stamps[start:end], len(base))
        for c in counter_cols:
            # sample-major rows: sample s, entity k -> base[k] + s*step[k]
            vals = np.tile(base[c].to_numpy(dtype=float), (m, 1)) + (
                np.arange(start, end, dtype=float)[:, None] * steps[c][None, :]
            )
            block[c] = vals.ravel().astype(object)
        block = block[list(base.columns)]
        block.to_csv(dst, index=False, mode="a", header=(start == 0))


def _gauge_jitter(n: int, base: float, amp: float = 0.02, period: int = 90) -> np.ndarray:
    phase = np.arange(n, dtype=float)
    return base * (1.0 + amp * np.sin(phase / period))


def _write_vm(dst: Path, n: int, interval: int, vms: int) -> None:
    stamps = _stamps(n, interval)
    names = [f"fc-vm-{i:03d}" for i in range(vms)]
    pids = [20000 + i for i in range(vms)]
    # per-VM activity: 0.08 .. 1.0 cores, sinusoidal by vm index
    cores = 0.08 + 0.92 * (np.arange(vms) % 13) / 12.0
    step = cores * interval * 100.0  # jiffies per sample (CLK_TCK=100)
    pss = 1024.0 + 96.0 * np.sin(np.arange(vms) / 7.0)
    mem = pss * 1.05
    chunk = max(1, min(n, 200_000 // max(1, vms)))
    first = True
    for start in range(0, n, chunk):
        end = min(n, start + chunk)
        m = end - start
        t = np.arange(start, end, dtype=float)
        block = {
            "timestamp": np.repeat(stamps[start:end], vms),
            "pid": np.tile(np.array(pids), m),
            "vm_name": np.tile(np.array(names), m),
            "utime": np.tile(step * 0.7, m) * np.repeat(t, vms),
            "stime": np.tile(step * 0.3, m) * np.repeat(t, vms),
            "pss_mb": np.tile(pss, m),
            "memory_mb": np.tile(mem, m),
        }
        pd.DataFrame(block).to_csv(dst, index=False, mode="a", header=first)
        first = False


def _write_ublk(dst: Path, n: int, interval: int, daemons: int) -> None:
    # 1s sub-sample cadence, like the real writer.
    sub = n * 2
    stamps = _stamps(sub, max(1, interval // 2))
    rows_per = daemons
    chunk = 200_000 // max(1, rows_per)
    first = True
    for start in range(0, sub, chunk):
        end = min(sub, start + chunk)
        m = end - start
        t = np.arange(start, end, dtype=float)
        block = {
            "timestamp": np.repeat(stamps[start:end], rows_per),
            "pid": np.tile(np.arange(30000, 30000 + daemons), m),
            "utime": (np.tile(np.arange(daemons, dtype=float) + 1.0, m) * 12.0 * np.repeat(t, daemons)),
            "stime": (np.tile(np.arange(daemons, dtype=float) + 1.0, m) * 4.0 * np.repeat(t, daemons)),
        }
        pd.DataFrame(block).to_csv(dst, index=False, mode="a", header=first)
        first = False


def _write_hostproc(dst: Path, n: int, interval: int, procs: int) -> None:
    sub = n * 2
    stamps = _stamps(sub, max(1, interval // 2))
    chunk = 200_000 // max(1, procs)
    first = True
    for start in range(0, sub, chunk):
        end = min(sub, start + chunk)
        m = end - start
        t = np.arange(start, end, dtype=float)
        block = {
            "timestamp": np.repeat(stamps[start:end], procs),
            "name": np.tile(np.array([f"srv-{i}" for i in range(procs)]), m),
            "pid": np.tile(np.arange(40000, 40000 + procs), m),
            "utime": np.tile(np.full(procs, 18.0), m) * np.repeat(t, procs),
            "stime": np.tile(np.full(procs, 6.0), m) * np.repeat(t, procs),
            "pss_mb": np.tile(np.full(procs, 512.0), m),
        }
        pd.DataFrame(block).to_csv(dst, index=False, mode="a", header=first)
        first = False


def _write_api_state(dst: Path, n: int, interval: int) -> None:
    stamps = _stamps(n, interval)
    t = np.arange(n, dtype=float)
    pd.DataFrame(
        {
            "timestamp": stamps,
            "sandboxes_running": (300 + 40 * np.sin(t / 120)).astype(int),
            "sandboxes_paused": (80 + 30 * np.cos(t / 150)).astype(int),
            "sandboxes_other": np.full(n, 4),
            "sandboxes_total": (384 + 0 * t).astype(int),
        }
    ).to_csv(dst, index=False)


def _write_numa_mem(dst: Path, n: int, interval: int, nodes: list[int]) -> None:
    """Synthesize numa_mem_raw.csv: one row per node per sample, MB gauges.

    The template predates this file (it was added with the NUMA timeline), so
    the node set comes from numa_cpu_map.csv and the values are synthetic.
    """
    stamps = _stamps(n, interval)
    per_node_total = 1024.0 * 1024 * 0.75 / max(1, len(nodes))  # MB, 75% of 1 TiB
    t = np.arange(n, dtype=float)
    chunk = max(1, min(n, 200_000 // max(1, len(nodes))))
    first = True
    for start in range(0, n, chunk):
        end = min(n, start + chunk)
        m, block = end - start, {"timestamp": [], "node": []}
        for node in nodes:
            phase = t[start:end] + node * 37.0
            used = per_node_total * (0.62 + 0.05 * np.sin(phase / 240.0))
            block["timestamp"].append(stamps[start:end])
            block["node"].append(np.full(m, node))
            block["total_mb"] = block.get("total_mb", []) + [np.full(m, per_node_total)]
            for field, values in (
                ("used_mb", used),
                ("free_mb", per_node_total - used),
                ("available_mb", per_node_total - used * 0.86),
                ("active_mb", used * 0.58),
                ("inactive_mb", used * 0.34),
                ("anon_pages_mb", used * 0.41),
                ("file_pages_mb", used * 0.47),
                ("swap_cached_mb", 1024.0 * 2 + 1024.0 * 0.5 * np.cos(phase / 170.0)),
            ):
                block[field] = block.get(field, []) + [values]
        frame = pd.DataFrame(
            {
                "timestamp": np.concatenate(block["timestamp"]),
                "node": np.concatenate(block["node"]),
                **{
                    key: np.concatenate(values).astype(object)
                    for key, values in block.items()
                    if key not in ("timestamp", "node")
                },
            }
        )
        frame["usage_pct"] = 100.0 * frame["used_mb"] / frame["total_mb"]
        frame.to_csv(dst, index=False, mode="a", header=first)
        first = False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples", type=int, default=3600)
    ap.add_argument("--interval", type=int, default=2)
    ap.add_argument("--vms", type=int, default=384)
    ap.add_argument("--ublk", type=int, default=8)
    ap.add_argument("--hostproc", type=int, default=1)
    a = ap.parse_args()
    src, dst = Path(a.template), Path(a.out)
    dst.mkdir(parents=True, exist_ok=True)

    tpl_cpu = pd.read_csv(src / "host_cpu_raw.csv")
    cpu_counters = [
        c
        for c in tpl_cpu.columns
        if c.startswith("cpu_") or c in ("ctxt", "processes", "softirq_total") or c.startswith("softirq_")
    ]
    _tile_wide(src / "host_cpu_raw.csv", dst / "host_cpu_raw.csv", a.samples, a.interval, cpu_counters)

    _tile_wide(src / "host_mem_raw.csv", dst / "host_mem_raw.csv", a.samples, a.interval, [])
    # give the fixture a real swap partition so the swap timeline is non-zero
    mem = pd.read_csv(dst / "host_mem_raw.csv")
    t = np.arange(len(mem), dtype=float)
    mem["SwapTotal"] = 1024.0 * 64
    mem["SwapFree"] = 1024.0 * 64 - (1024.0 * 20 + 1024.0 * 8 * np.sin(t / 200))
    mem["SwapCached"] = 1024.0 * 4 + 1024.0 * 2 * np.cos(t / 170)
    mem["MemAvailable"] = _gauge_jitter(len(mem), 259891.91, amp=0.01)
    mem.to_csv(dst / "host_mem_raw.csv", index=False)

    tpl_vmstat = pd.read_csv(src / "host_vmstat_raw.csv")
    vmstat_counters = [
        c
        for c in tpl_vmstat.columns
        if c
        in (
            "pswpin",
            "pswpout",
            "pgscan_kswapd",
            "pgscan_direct",
            "pgscan_direct_throttle",
            "pgsteal_kswapd",
            "pgsteal_direct",
            "workingset_refault_file",
            "allocstall_dma",
            "allocstall_dma32",
            "allocstall_normal",
            "allocstall_movable",
            "allocstall_high",
            "allocstall_device",
        )
    ]
    _tile_wide(src / "host_vmstat_raw.csv", dst / "host_vmstat_raw.csv", a.samples, a.interval, vmstat_counters)
    vs = pd.read_csv(dst / "host_vmstat_raw.csv")
    vs["pswpin"] = np.arange(len(vs), dtype=float) * 37.0
    vs["pswpout"] = np.arange(len(vs), dtype=float) * 53.0
    vs["pgscan_kswapd"] = np.arange(len(vs), dtype=float) * 900.0
    vs["pgscan_direct"] = np.arange(len(vs), dtype=float) * 120.0
    vs["pgsteal_kswapd"] = np.arange(len(vs), dtype=float) * 800.0
    vs["pgsteal_direct"] = np.arange(len(vs), dtype=float) * 110.0
    vs["workingset_refault_file"] = np.arange(len(vs), dtype=float) * 640.0
    vs["allocstall_normal"] = np.arange(len(vs), dtype=float) * 9.0
    vs.to_csv(dst / "host_vmstat_raw.csv", index=False)

    _tile_long(
        src / "host_percpu_raw.csv",
        dst / "host_percpu_raw.csv",
        a.samples,
        a.interval,
        "cpu_id",
        ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice"],
    )
    _tile_long(
        src / "disk_io_raw.csv",
        dst / "disk_io_raw.csv",
        a.samples * 2,
        max(1, a.interval // 2),
        "device",
        [c for c in pd.read_csv(src / "disk_io_raw.csv").columns if c not in ("timestamp", "device", "inflight")],
    )
    (dst / "numa_cpu_map.csv").write_text((src / "numa_cpu_map.csv").read_text(), encoding="utf-8")
    nodes = sorted(pd.read_csv(src / "numa_cpu_map.csv")["node"].unique().astype(int))
    _write_numa_mem(dst / "numa_mem_raw.csv", a.samples, a.interval, nodes)

    _write_vm(dst / "vm_cpu_raw.csv", a.samples, a.interval, a.vms)
    if a.ublk:
        _write_ublk(dst / "ublk_cpu_raw.csv", a.samples, a.interval, a.ublk)
    if a.hostproc:
        _write_hostproc(dst / "hostproc_cpu_raw.csv", a.samples, a.interval, a.hostproc)
    _write_api_state(dst / "api_state_raw.csv", a.samples, a.interval)
    print(f"fixture written: {dst} ({a.samples} samples x {a.interval}s, {a.vms} VMs)")


if __name__ == "__main__":
    main()
