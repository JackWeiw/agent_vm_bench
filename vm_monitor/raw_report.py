"""raw_data/*.csv -> derived-rate DataFrames (spotbox-style host_resources consumer).

Reads the always-on raw /proc + per-VM + per-disk CSV dumps and derives rates
via delta (reusing base._compute_pressure_rates; disk rates via base.
_compute_disk_io_rates arrive in a later task). Pure file input -- does not
depend on in-memory history, post-hoc rerunnable. The xlsx assembly (3 sheets,
openpyxl LineCharts) is added in a later task; this module currently exposes
_derive_host (host-wide) + the shared _guarded counter-reset/gap guard.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import pandas as pd

from vm_monitor.base import (
    _BYTES_PER_MIB,
    _CLK_TCK,
    _compute_pressure_rates,
)

logger = logging.getLogger(__name__)

# Calibration knobs (ponytail: physical world needs tuning, not hard-coded).
MAX_INTERVAL_S = 10.0  # gap guard: per-key delta over a gap longer than this -> NaN
MAX_VM_SERIES = 8  # per-VM chart series cap (Excel render perf); used in Task 3+

_MEMINFO_KB_PER_GIB = 2**20  # /proc/meminfo fields are in kB; /2**20 -> GiB

_VMSTAT_FIELDS = [
    "pswpin",
    "pswpout",
    "pgscan_kswapd",
    "pgscan_direct",
    "pgscan_direct_throttle",
    "pgsteal_kswapd",
    "pgsteal_direct",
    "workingset_refault_file",
]


def _f(d: dict, k: str) -> float:
    """Best-effort float of a CSV cell (NaN if empty/non-numeric)."""
    v = d.get(k)
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _guarded(
    cur: dict, prev: dict, dt: float, key: str, fields: list[str], max_interval: float = MAX_INTERVAL_S
) -> bool:
    """True if this delta must be NaNed: dt<=0, dt>max_interval (gap), or any
    cumulative field cur<prev (counter reset -- e.g. VM pid restart zeroes
    utime/stime, device remount zeroes disk stat). Logs a WARNING per case."""
    if dt <= 0:
        logger.warning("counter reset/gap for %s (dt<=0)", key)
        return True
    if dt > max_interval:
        logger.warning("gap detected for %s (dt=%.1fs > %.1fs)", key, dt, max_interval)
        return True
    for f in fields:
        if _f(cur, f) < _f(prev, f):
            logger.warning("counter reset for %s at %s (cur<prev in %s)", key, cur.get("timestamp"), f)
            return True
    return False


def _read_csv(raw_dir: Path, fname: str) -> pd.DataFrame | None:
    p = raw_dir / fname
    if not p.exists() or p.stat().st_size == 0:
        logger.warning("raw CSV missing/empty: %s -> skipping derived sheet", p)
        return None
    try:
        return pd.read_csv(p)
    except (pd.errors.ParserError, OSError, ValueError) as e:
        logger.warning("raw CSV unparseable: %s (%s) -> skipping", p, e)
        return None


def _aggregate_cores(ublk_df: pd.DataFrame, cur_ts: float, dt: float) -> float:
    """Sum ublk-daemon cores at sample boundary cur_ts (ublk is 1s sub-sampled;
    take the last row <= cur_ts per pid, delta vs its predecessor)."""
    if ublk_df is None or ublk_df.empty or not dt:
        return math.nan
    ublk_df = ublk_df.sort_values("timestamp")
    ublk_df["timestamp"] = ublk_df["timestamp"].astype(float)
    total = 0.0
    nans = 0
    for pid, g in ublk_df.groupby("pid"):
        g = g[g["timestamp"] <= cur_ts]
        if len(g) < 2:
            nans += 1
            continue
        prev, cur = g.iloc[-2], g.iloc[-1]
        d = (
            _f(cur.to_dict(), "utime")
            + _f(cur.to_dict(), "stime")
            - _f(prev.to_dict(), "utime")
            - _f(prev.to_dict(), "stime")
        )
        sub_dt = cur["timestamp"] - prev["timestamp"]
        if _guarded(cur.to_dict(), prev.to_dict(), sub_dt, f"ublk pid={pid}", ["utime", "stime"]):
            nans += 1
            continue
        total += d / _CLK_TCK / sub_dt
    return total if nans == 0 else math.nan


def _derive_host(raw_dir: Path) -> pd.DataFrame:
    """Derive host-wide rates from host_mem_raw + host_cpu_raw + host_vmstat_raw
    + ublk_cpu_raw (aggregate). One row per timestamp (sorted). First row NaN."""
    cpu = _read_csv(raw_dir, "host_cpu_raw.csv")
    mem = _read_csv(raw_dir, "host_mem_raw.csv")
    vmstat = _read_csv(raw_dir, "host_vmstat_raw.csv")
    ublk = _read_csv(raw_dir, "ublk_cpu_raw.csv")
    if cpu is None or cpu.empty:
        return pd.DataFrame()
    cpu = cpu.sort_values("timestamp").reset_index(drop=True)
    ts = cpu["timestamp"].astype(float).to_numpy()
    out = {
        c: [math.nan]
        for c in (
            "cpu_busy_pct",
            "cpu_iowait_pct",
            "mem_used_gb",
            "mem_cache_gb",
            "buffers_mb",
            "dirty_mb",
            "swap_in_mib_s",
            "swap_out_mib_s",
            "page_scan_mib_s",
            "page_reclaim_mib_s",
            "file_refault_mib_s",
            "ublk_cores",
            "vm_cores",
        )
    }

    _CPU8 = ["cpu_user", "cpu_nice", "cpu_system", "cpu_idle", "cpu_iowait", "cpu_irq", "cpu_softirq", "cpu_steal"]
    for i in range(1, len(cpu)):
        dt = ts[i] - ts[i - 1]
        cur_c, prev_c = cpu.iloc[i].to_dict(), cpu.iloc[i - 1].to_dict()
        if not _guarded(cur_c, prev_c, dt, "host_cpu", _CPU8):
            d_total = sum(_f(cur_c, f) - _f(prev_c, f) for f in _CPU8)
            d_idle_iowait = (
                _f(cur_c, "cpu_idle") + _f(cur_c, "cpu_iowait") - _f(prev_c, "cpu_idle") - _f(prev_c, "cpu_iowait")
            )
            out["cpu_busy_pct"].append(100.0 * (1 - d_idle_iowait / d_total) if d_total else math.nan)
            out["cpu_iowait_pct"].append(
                100.0 * (_f(cur_c, "cpu_iowait") - _f(prev_c, "cpu_iowait")) / d_total if d_total else math.nan
            )
        else:
            out["cpu_busy_pct"].append(math.nan)
            out["cpu_iowait_pct"].append(math.nan)
        # mem is a gauge (no delta); aligned by row index to cpu since host_*_raw
        # are written together per-sample in one enqueue_proc call. If a /proc
        # read ever intermittently fails, switch to pd.merge_asof on timestamp.
        if mem is not None and i < len(mem):
            md = mem.iloc[i].to_dict()
            out["mem_used_gb"].append((_f(md, "MemTotal") - _f(md, "MemAvailable")) / _MEMINFO_KB_PER_GIB)
            out["mem_cache_gb"].append((_f(md, "Cached") + _f(md, "SReclaimable")) / _MEMINFO_KB_PER_GIB)
            out["buffers_mb"].append(_f(md, "Buffers") / 1024)
            out["dirty_mb"].append(_f(md, "Dirty") / 1024)
        else:
            for k in ("mem_used_gb", "mem_cache_gb", "buffers_mb", "dirty_mb"):
                out[k].append(math.nan)
        if vmstat is not None and i < len(vmstat):
            cur_v, prev_v = vmstat.iloc[i].to_dict(), vmstat.iloc[i - 1].to_dict()
            if _guarded(cur_v, prev_v, dt, "host_vmstat", _VMSTAT_FIELDS):
                for k in (
                    "swap_in_mib_s",
                    "swap_out_mib_s",
                    "page_scan_mib_s",
                    "page_reclaim_mib_s",
                    "file_refault_mib_s",
                ):
                    out[k].append(math.nan)
            else:
                d_pswpin = _f(cur_v, "pswpin") - _f(prev_v, "pswpin")
                d_pswpout = _f(cur_v, "pswpout") - _f(prev_v, "pswpout")
                out["swap_in_mib_s"].append(max(0, d_pswpin) * 4096 / _BYTES_PER_MIB / dt if dt else math.nan)
                out["swap_out_mib_s"].append(max(0, d_pswpout) * 4096 / _BYTES_PER_MIB / dt if dt else math.nan)
                pr = _compute_pressure_rates(cur_v, prev_v, dt)
                out["page_scan_mib_s"].append(pr["page_scan_mib_s"])
                out["page_reclaim_mib_s"].append(pr["page_reclaim_mib_s"])
                out["file_refault_mib_s"].append(pr["file_refault_mib_s"])
        else:
            for k in ("swap_in_mib_s", "swap_out_mib_s", "page_scan_mib_s", "page_reclaim_mib_s", "file_refault_mib_s"):
                out[k].append(math.nan)
        out["ublk_cores"].append(_aggregate_cores(ublk, ts[i], dt) if ublk is not None else math.nan)
        out["vm_cores"].append(math.nan)  # filled by per-VM aggregate in the orchestrator (Task 5)
    df = pd.DataFrame(out)
    df.insert(0, "t_s", [0.0] + [ts[i] - ts[0] for i in range(1, len(ts))])
    df.insert(1, "timestamp", cpu["timestamp"].tolist())
    return df


def _derive_per_vm(raw_dir: Path, max_series: int = MAX_VM_SERIES) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-VM CPU cores (delta of utime+stime /_CLK_TCK/dt, guarded) + PSS GB
    (gauge, plotted directly -- no delta). long -> wide pivot (rows indexed by
    timestamp, cols=vm_name). VM count > max_series -> top-K by CPU busy mean
    (descending) + 'other' = sum(skipna, min_count=1) of the rest. Both the CPU
    and PSS charts share this VM set (ranked by CPU busy, NOT PSS) so the two
    side-by-side charts show the same VMs -- ranking each by its own metric
    would misalign the sets and confuse the reader."""
    vm = _read_csv(raw_dir, "vm_cpu_raw.csv")
    if vm is None or vm.empty:
        return pd.DataFrame(), pd.DataFrame()
    vm = vm.sort_values(["vm_name", "timestamp"]).copy()
    vm["timestamp"] = vm["timestamp"].astype(float)
    vm["cores"] = math.nan
    vm["pss_gb"] = math.nan
    for name, g in vm.groupby("vm_name"):
        g = g.sort_values("timestamp")
        prev = None
        c_col, p_col = [], []
        for _, row in g.iterrows():
            rd = row.to_dict()
            if prev is None:
                c_col.append(math.nan)
            else:
                dt = float(row["timestamp"]) - float(prev["timestamp"])
                if _guarded(rd, prev, dt, f"vm={name}", ["utime", "stime"]):
                    c_col.append(math.nan)
                else:
                    d = _f(rd, "utime") + _f(rd, "stime") - _f(prev, "utime") - _f(prev, "stime")
                    c_col.append(d / _CLK_TCK / dt if dt else math.nan)
            p_col.append(_f(rd, "pss_mb") / 1024)
            prev = rd
        vm.loc[g.index, "cores"] = c_col
        vm.loc[g.index, "pss_gb"] = p_col

    p_cores = vm.pivot_table(index="timestamp", columns="vm_name", values="cores", aggfunc="first", dropna=False)
    p_pss = vm.pivot_table(index="timestamp", columns="vm_name", values="pss_gb", aggfunc="first", dropna=False)
    # rank VMs by mean CPU busy (cores) -- shared set for both charts
    means = p_cores.mean().sort_values(ascending=False)
    top = list(means.head(max_series).index)
    rest = [v for v in p_cores.columns if v not in top]
    out_cores = p_cores[top].copy()
    out_pss = p_pss[top].copy()
    if rest:
        out_cores["other"] = p_cores[rest].sum(axis=1, skipna=True, min_count=1)
        out_pss["other"] = p_pss[rest].sum(axis=1, skipna=True, min_count=1)
    out_cores = out_cores.sort_index()
    out_pss = out_pss.sort_index()
    t0 = out_cores.index.min()
    out_cores.insert(0, "t_s", out_cores.index - t0)
    out_pss.insert(0, "t_s", out_pss.index - t0)
    return out_cores, out_pss
