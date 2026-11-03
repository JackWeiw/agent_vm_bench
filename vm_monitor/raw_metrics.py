"""raw_data frames -> derived timelines (host, per-VM, NUMA, disk, host-proc).

Every derivation is pure file input and vectorized: cumulative counters are
differenced with ``groupby(...).shift(1)`` (or a numpy pivot diff), so a 2-hour
capture costs one pass per series rather than one Python row-iteration per
sample. The shared delta/guard contract lives in :mod:`vm_monitor.raw_frames`.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from vm_monitor.base import _BYTES_PER_MIB, _CLK_TCK, _DISK_FIELDS, _compute_pressure_rates
from vm_monitor.raw_frames import (
    MAX_INTERVAL_S,
    _CPU_FIELDS,
    _MEMINFO_MB_PER_GIB,
    _VMSTAT_FIELDS,
    _asof_join,
    _column,
    _f,
    _frame_by_epoch,
    _guarded,
    _load_frames,
    _rows_by_epoch,
    _ts_to_epoch,
)

logger = logging.getLogger(__name__)

MAX_VM_SERIES = 8  # per-VM chart series cap (Excel render perf)

# /sys/block/<dev>/stat fields that are cumulative monotonic counters.
# `inflight` (current I/Os in progress) is a GAUGE -- normal 3->1 fluctuation
# must NOT trip the reset guard, else busy-disk intervals get NaN-holed.
_DISK_GUARD_FIELDS = tuple(f for f in _DISK_FIELDS if f != "inflight")

# Disk I/O columns derived per device (r_mb_s/w_mb_s/util_pct + depth metrics).
_DISK_RATE_FIELDS = (
    "r_mb_s",
    "w_mb_s",
    "util_pct",
    "inflight",
    "avg_queue_depth",
    "read_await_ms",
    "write_await_ms",
    "r_iops",
    "w_iops",
    "avg_rq_sz",
)

# Serial cap for the Disk IO sheet (10 columns per device): keeps it far below
# Excel's 16,384-column limit while staying legible.
_MAX_DISK_DEVICES = 16

# VM-fleet core count sanity bound: aggregate jiffies deltas are bounded by the
# host's busy cores, so exceeding it (with slack) means the two clocks disagree.
_SANITY_EPS = 1.02


# --- shared counter helpers -------------------------------------------------


def _sort_by(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return frame.sort_values(keys, kind="stable")


def _per_key_deltas(frame: pd.DataFrame, key) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(dt, du, ds) per row, grouped by ``key``, aligned to ``frame``'s index.

    ``frame`` must carry ``__e`` (epoch), ``__u`` (utime) and ``__s`` (stime).
    Rows without a group predecessor get NaN deltas.
    """
    keys = key if isinstance(key, list) else [key]
    ordered = _sort_by(frame, [*keys, "__e"])
    group = ordered.groupby(key, sort=False)
    return (
        ordered["__e"] - group["__e"].shift(1),
        ordered["__u"] - group["__u"].shift(1),
        ordered["__s"] - group["__s"].shift(1),
    )


def _cores_and_guard(dt: pd.Series, du: pd.Series, ds: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Cores = delta(utime+stime) / _CLK_TCK / dt, plus the guard mask applied.

    The mask is returned so callers can report *skipped* intervals separately
    from the by-design NaN first sample.
    """
    ok = (dt > 0) & (dt <= MAX_INTERVAL_S) & (du >= 0) & (ds >= 0)
    return ((du + ds) / _CLK_TCK / dt).where(ok), ok


def _prepare_counter_series(frame: pd.DataFrame, key: str = "pid") -> dict:
    """Per-key (ts, utime, stime) arrays for the ublk sub-sample aggregation.

    Collapses duplicate whole-second timestamps first (the 1s sub-sampler can
    write two samples into one wall-second); the last snapshot per distinct
    timestamp wins, matching the old ``drop_duplicates(keep="last")`` behavior.
    Precomputing the arrays turns each host sample's lookup into a
    ``searchsorted`` instead of a full-table ``groupby`` (the O(n^2) hotspot).
    """
    out: dict = {}
    for pid, group in frame.groupby(key, sort=False):
        group = group.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        out[pid] = (
            group["timestamp"].to_numpy(dtype=float),
            pd.to_numeric(group["utime"], errors="coerce").to_numpy(dtype=float),
            pd.to_numeric(group["stime"], errors="coerce").to_numpy(dtype=float),
        )
    return out


def _aggregate_cores_at(series: dict, cur_ts: float) -> float:
    """Sum ublk-daemon cores at ``cur_ts`` from precomputed per-pid series.

    For each pid, the last distinct snapshot <= cur_ts is differenced against
    its predecessor. A pid with <2 snapshots, a reset (negative jiffies), or a
    gap contributes 0 (not NaN) so one daemon restart cannot blank the fleet;
    NaN only when NO pid is rateable.
    """
    if not series:
        return math.nan
    total = 0.0
    saw = False
    for timestamps, utime, stime in series.values():
        i = int(np.searchsorted(timestamps, cur_ts, side="right")) - 1
        if i < 1:
            continue
        dt = timestamps[i] - timestamps[i - 1]
        delta = (utime[i] - utime[i - 1]) + (stime[i] - stime[i - 1])
        if dt <= 0 or dt > MAX_INTERVAL_S or delta < 0:
            continue
        total += delta / _CLK_TCK / dt
        saw = True
    return total if saw else math.nan


# --- host timeline ----------------------------------------------------------


def derive_host(raw_dir=None, *, frames: dict | None = None) -> pd.DataFrame:
    """Host-wide rates/gauges, one row per host cpu timestamp.

    Assembled from independent blocks (cpu / memory / vmstat / fleet / API) so
    each metric family is readable and testable on its own. Rate columns need a
    predecessor and stay NaN on row 0; gauges are valid on row 0.
    """
    f = frames if frames is not None else _load_frames(raw_dir)
    cpu = f.get("cpu")
    if cpu is None or cpu.empty:
        return pd.DataFrame()
    cpu = _sort_by(cpu, ["timestamp"]).reset_index(drop=True)
    epochs = _ts_to_epoch(cpu["timestamp"])

    df = _cpu_block(cpu, epochs)
    _warn_on_gaps(df["__dt"], cpu["timestamp"])
    for block in (
        _memory_block(f, epochs),
        _vmstat_block(f, epochs, df["__dt"]),
        _fleet_block(f, epochs),
        _api_block(f, epochs),
    ):
        df = df.join(block)
    _warn_if_cores_exceed_host(df, cpu["timestamp"])
    df = df.drop(columns=["__dt", "__host_cores"])
    df.insert(0, "t_s", epochs - epochs.iloc[0])
    df.insert(1, "timestamp", cpu["timestamp"].to_numpy())
    return df


def _cpu_block(cpu: pd.DataFrame, epochs: pd.Series) -> pd.DataFrame:
    """Host CPU busy/iowait/user/sys, procs gauges, and the shared interval dt."""
    counters = pd.DataFrame({field: _column(cpu, f"cpu_{field}", cpu.index) for field in _CPU_FIELDS}, index=cpu.index)
    deltas = counters.diff()
    interval = epochs.diff()

    total = deltas.sum(axis=1)
    idle_iowait = deltas["idle"] + deltas["iowait"]
    valid = (interval > 0) & (interval <= MAX_INTERVAL_S) & (deltas >= 0).all(axis=1) & (total != 0)

    def share(field: str) -> pd.Series:
        return (100.0 * deltas[field] / total).where(valid)

    block = pd.DataFrame(index=cpu.index)
    block["cpu_busy_pct"] = (100.0 * (1.0 - idle_iowait / total)).where(valid)
    block["cpu_iowait_pct"] = share("iowait")
    block["cpu_user_pct"] = share("user")
    block["cpu_sys_pct"] = share("system")
    block["procs_running"] = _column(cpu, "procs_running", cpu.index)
    block["procs_blocked"] = _column(cpu, "procs_blocked", cpu.index)
    block["__dt"] = interval
    block["__host_cores"] = ((total - idle_iowait) / _CLK_TCK / interval).where(valid)
    return block


def _memory_block(frames: dict, epochs: pd.Series) -> pd.DataFrame:
    """Memory/swap gauges from /proc/meminfo (MB in the CSV)."""
    mem = _frame_by_epoch(frames.get("mem"), epochs)
    index = epochs.index

    def column(name: str) -> pd.Series:
        return _column(mem, name, index)

    cached = column("Cached")
    buffers = column("Buffers")
    shmem = column("Shmem").fillna(0.0)
    # Page cache excludes tmpfs pages (Shmem is already in Cached) and adds
    # Buffers. Missing Shmem means 0; a missing Cached/Buffers stays NaN.
    file_cache = (cached + buffers - shmem).clip(lower=0.0).where(cached.notna())

    swap_total, swap_free, swap_cached = column("SwapTotal"), column("SwapFree"), column("SwapCached")
    swap_used = swap_total - swap_free

    block = pd.DataFrame(index=index)
    block["mem_used_gb"] = (column("MemTotal") - column("MemAvailable")) / _MEMINFO_MB_PER_GIB
    block["mem_cache_gb"] = (cached + column("SReclaimable")) / _MEMINFO_MB_PER_GIB
    block["buffers_mb"] = buffers
    block["dirty_mb"] = column("Dirty")
    block["writeback_mb"] = column("Writeback")
    block["sreclaimable_mb"] = column("SReclaimable")
    block["anon_pages_mb"] = column("AnonPages")
    block["file_cache_mb"] = file_cache
    block["swap_used_gb"] = swap_used / _MEMINFO_MB_PER_GIB
    block["swap_free_gb"] = swap_free / _MEMINFO_MB_PER_GIB
    block["swap_cached_mb"] = swap_cached
    # Share of the used swap sitting in the cache: 0 when nothing is swapped,
    # NaN when the source fields are absent (not a real zero).
    block["swap_cache_ratio_pct"] = (100.0 * swap_cached / swap_used).mask(swap_used == 0, 0.0)
    return block


def _vmstat_block(frames: dict, epochs: pd.Series, interval: pd.Series) -> pd.DataFrame:
    """Swap in/out and page-cache pressure rates from /proc/vmstat.

    Both endpoints must be present at consecutive host epochs; the delta math
    is delegated to ``base._compute_pressure_rates`` so the live monitor and the
    post-hoc report cannot drift apart.
    """
    names = (
        "swap_in_mib_s",
        "swap_out_mib_s",
        "page_scan_mib_s",
        "page_reclaim_mib_s",
        "file_refault_mib_s",
        "direct_reclaim_mib_s",
        "allocstall_per_s",
    )
    block = pd.DataFrame(math.nan, index=epochs.index, columns=names, dtype=float)
    rows = _rows_by_epoch(frames.get("vmstat"))
    if not rows:
        return block
    stamps = epochs.to_numpy()
    for i in range(1, len(stamps)):
        cur, prev = rows.get(stamps[i]), rows.get(stamps[i - 1])
        dt = interval.iloc[i]
        if not cur or not prev or not (0 < dt <= MAX_INTERVAL_S):
            continue
        if _guarded(cur, prev, dt, "host_vmstat", _VMSTAT_FIELDS):
            continue
        pressure = _compute_pressure_rates(cur, prev, dt)
        for name in names[2:]:
            block.iat[i, block.columns.get_loc(name)] = pressure[name]
        block.iat[i, block.columns.get_loc("swap_in_mib_s")] = (
            max(0.0, _f(cur, "pswpin") - _f(prev, "pswpin")) * 4096 / _BYTES_PER_MIB / dt
        )
        block.iat[i, block.columns.get_loc("swap_out_mib_s")] = (
            max(0.0, _f(cur, "pswpout") - _f(prev, "pswpout")) * 4096 / _BYTES_PER_MIB / dt
        )
    return block


def _fleet_block(frames: dict, epochs: pd.Series) -> pd.DataFrame:
    """ublk-daemon cores, VM fleet cores/memory/PSS, and concurrent VM count."""
    block = pd.DataFrame(index=epochs.index)
    ublk = frames.get("ublk")
    if ublk is not None and not ublk.empty:
        ublk = ublk.assign(timestamp=_ts_to_epoch(ublk["timestamp"])).sort_values("timestamp")
        series = _prepare_counter_series(ublk)
        block["ublk_cores"] = [_aggregate_cores_at(series, stamp) for stamp in epochs]
    else:
        block["ublk_cores"] = math.nan

    vm = frames.get("vm")
    if vm is None or vm.empty:
        block["vm_cores"] = math.nan
        block["fc_pss_gb"] = math.nan
        block["concurrent_vms"] = 0
        return block

    aggregates = _vm_fleet_aggregates(vm, epochs)
    block["vm_cores"] = aggregates["cores"]
    block["fc_pss_gb"] = aggregates["pss_gb"]
    block["concurrent_vms"] = aggregates["concurrent"]
    if aggregates["memory_gb"] is not None:
        block["vm_mem_gb"] = aggregates["memory_gb"]
    return block


def _vm_fleet_aggregates(vm: pd.DataFrame, epochs: pd.Series) -> dict:
    """Per-host-sample VM fleet aggregates.

    Method A: a VM contributes at e_cur only if its own previous sample landed
    on the immediately preceding host epoch (both endpoints). A VM born/died
    mid-interval has no baseline -> excluded (contributes 0) rather than
    dividing a two-interval delta by one interval. The fleet cores value is a
    single aggregate jiffies delta, bounded by construction by the host's busy
    cores -- never a sum of per-VM normalized rates.
    """
    vm = vm.copy()
    vm["__e"] = _ts_to_epoch(vm["timestamp"])
    vm["__j"] = pd.to_numeric(vm["utime"], errors="coerce") + pd.to_numeric(vm["stime"], errors="coerce")
    vm["__pss"] = _column(vm, "pss_mb", vm.index)
    has_memory = "memory_mb" in vm.columns
    vm["__mem"] = _column(vm, "memory_mb", vm.index) if has_memory else math.nan

    host_epochs = [stamp for stamp in epochs.tolist() if not math.isnan(stamp)]
    host_prev = dict(zip(host_epochs[1:], host_epochs[:-1]))

    vm = _sort_by(vm, ["vm_name", "__e"])
    group = vm.groupby("vm_name", sort=False)
    delta_jiffies = vm["__j"] - group["__j"].shift(1)
    both_endpoints = vm["__e"].map(host_prev) == group["__e"].shift(1)
    delta_jiffies = delta_jiffies.where(both_endpoints & (delta_jiffies >= 0))

    interval = epochs.diff().to_numpy()
    rateable = (interval > 0) & (interval <= MAX_INTERVAL_S)
    by_epoch = vm.groupby("__e")
    return {
        "cores": _jiffies_to_cores(delta_jiffies.groupby(vm["__e"]).sum(min_count=1), interval, rateable, epochs),
        "pss_gb": _host_aligned(by_epoch["__pss"].sum(min_count=1), epochs) / 1024,
        "concurrent": _host_aligned(by_epoch["vm_name"].count(), epochs).fillna(0).astype(int),
        "memory_gb": _host_aligned(by_epoch["__mem"].sum(min_count=1), epochs) / 1024 if has_memory else None,
    }


def _host_aligned(aggregated: pd.Series, epochs: pd.Series) -> pd.Series:
    """Reindex an epoch-keyed aggregate onto the host clock's positional index."""
    return pd.Series(aggregated.reindex(epochs.to_numpy()).to_numpy(), index=epochs.index)


def _jiffies_to_cores(aggregated, interval, rateable, epochs) -> pd.Series:
    """Aggregate jiffies delta -> cores per host interval (NaN where unusable)."""
    values = _host_aligned(aggregated, epochs).to_numpy(dtype=float) / _CLK_TCK / interval
    values[~rateable] = math.nan
    return pd.Series(values, index=epochs.index)


def _api_block(frames: dict, epochs: pd.Series) -> pd.DataFrame:
    """Provider-reported sandbox states, backward-as-of joined onto host samples."""
    api = frames.get("api")
    if api is None or api.empty or not {"sandboxes_running", "sandboxes_paused"} <= set(api.columns):
        return pd.DataFrame(index=epochs.index)
    stamps = _ts_to_epoch(api["timestamp"]).to_numpy()
    order = stamps.argsort(kind="stable")
    block = pd.DataFrame(index=epochs.index)
    for column in ("sandboxes_running", "sandboxes_paused"):
        values = pd.to_numeric(api[column], errors="coerce").to_numpy()[order]
        block[column] = _asof_join(epochs.to_numpy(), stamps[order], values)
    return block


def _warn_on_gaps(interval: pd.Series, timestamps: pd.Series) -> None:
    """Surface capture gaps once per run instead of per interval (they NaN-hole rates)."""
    gaps = interval[interval > MAX_INTERVAL_S]
    if len(gaps):
        logger.warning(
            "gap detected for host_cpu (%d interval(s) > %.1fs, first dt=%.1fs at %s)",
            len(gaps),
            MAX_INTERVAL_S,
            gaps.iloc[0],
            timestamps.iloc[gaps.index[0]],
        )


def _warn_if_cores_exceed_host(df: pd.DataFrame, timestamps: pd.Series) -> None:
    """Surface a VM/CPU clock-skew inconsistency instead of silently plotting it.

    The fleet core count is one aggregate jiffies delta, so it is bounded by the
    host's busy cores; exceeding that bound means the two clocks disagree.
    """
    if "vm_cores" not in df.columns:
        return
    host_cores = df["__host_cores"]
    over = (df["vm_cores"] > host_cores * _SANITY_EPS).fillna(False)
    if not over.any():
        return
    worst = (df["vm_cores"] / host_cores)[over].idxmax()
    logger.warning(
        "vm_cores exceeded host_busy_cores*%.2f at %d timestamp(s) (worst %.2fx at %s) "
        "-- bounded by construction; investigate VM jiffies vs /proc/stat skew",
        _SANITY_EPS,
        int(over.sum()),
        df.loc[worst, "vm_cores"] / host_cores[worst],
        timestamps.iloc[worst],
    )


# --- per-VM ----------------------------------------------------------------


def derive_per_vm(raw_dir=None, max_series: int = MAX_VM_SERIES, *, frames: dict | None = None):
    """Per-VM CPU cores + PSS GB, long -> wide (rows=epoch, cols=vm_name).

    VM count > ``max_series`` -> top-K by mean CPU busy + ``other`` = sum of the
    rest; both CPU and PSS charts share that VM set (ranked by CPU, NOT PSS) so
    the two charts stay aligned.
    """
    f = frames if frames is not None else _load_frames(raw_dir)
    vm = f.get("vm")
    if vm is None or vm.empty:
        return pd.DataFrame(), pd.DataFrame()
    vm = vm.copy()
    vm["__e"] = _ts_to_epoch(vm["timestamp"])
    vm["__u"] = pd.to_numeric(vm["utime"], errors="coerce")
    vm["__s"] = pd.to_numeric(vm["stime"], errors="coerce")
    vm["cores"], guard = _cores_and_guard(*_per_key_deltas(vm, "vm_name"))
    vm["pss_gb"] = _column(vm, "pss_mb", vm.index) / 1024

    skipped = (~guard) & (vm.groupby("vm_name", sort=False)["__e"].shift(1).notna())
    if skipped.any():
        logger.warning(
            "%d per-VM intervals skipped (counter reset or gap), e.g. vm=%s",
            int(skipped.sum()),
            vm.loc[skipped, "vm_name"].iloc[0],
        )
    return _top_k_pivot(vm, "cores", "pss_gb", max_series, "vm_name")


def _top_k_pivot(vm: pd.DataFrame, cores_col: str, gauge_col: str, max_series: int | None, key: str):
    """Wide cores + gauge frames sharing the top-K series set and a t_s column."""
    cores = vm.pivot_table(index="__e", columns=key, values=cores_col, aggfunc="first", dropna=False)
    gauge = vm.pivot_table(index="__e", columns=key, values=gauge_col, aggfunc="first", dropna=False)
    if max_series is not None and len(cores.columns) > max_series:
        top = list(cores.mean().sort_values(ascending=False).head(max_series).index)
        rest = [name for name in cores.columns if name not in top]
        out_cores, out_gauge = cores[top].copy(), gauge[top].copy()
        out_cores["other"] = cores[rest].sum(axis=1, skipna=True, min_count=1)
        out_gauge["other"] = gauge[rest].sum(axis=1, skipna=True, min_count=1)
    else:
        out_cores, out_gauge = cores.copy(), gauge.copy()
    out_cores, out_gauge = out_cores.sort_index(), out_gauge.sort_index()
    first_epoch = out_cores.index.min()
    out_cores.insert(0, "t_s", out_cores.index - first_epoch)
    out_gauge.insert(0, "t_s", out_gauge.index - first_epoch)
    return out_cores, out_gauge


# --- disk ------------------------------------------------------------------


def derive_disk(raw_dir=None, *, frames: dict | None = None) -> pd.DataFrame:
    """Per-device throughput / util / depth, vectorized over the long CSV.

    Each ``/sys/block`` counter is pivoted to [timestamp x device]; one diff
    per column yields every interval at once. A device whose counters decrease
    (remount) or whose interval is a gap is NaNed for that interval;
    ``inflight`` is a gauge and stays valid.
    """
    f = frames if frames is not None else _load_frames(raw_dir)
    disk = f.get("disk")
    if disk is None or disk.empty:
        return pd.DataFrame()
    disk = disk.assign(__e=_ts_to_epoch(disk["timestamp"]))
    pivots = {
        field: disk.pivot_table(index="__e", columns="device", values=field, aggfunc="last", dropna=False)
        for field in _DISK_FIELDS
        if field in disk.columns
    }
    if not pivots:
        return pd.DataFrame()
    devices = _cap_disk_devices(pivots)
    stamps = next(iter(pivots.values())).index.to_numpy(dtype=float)
    interval = np.full(len(stamps), math.nan)
    interval[1:] = np.diff(stamps)
    valid = (interval > 0) & (interval <= MAX_INTERVAL_S)

    def delta(field: str) -> np.ndarray:
        if field not in pivots:
            return np.zeros((len(stamps), len(devices)))
        values = pivots[field].reindex(columns=devices).to_numpy(dtype=float)
        out = np.full_like(values, np.nan)
        out[1:] = values[1:] - values[:-1]
        return out

    ok = np.repeat(valid[:, None], len(devices), axis=1)
    resets = np.zeros_like(ok)
    for field in _DISK_GUARD_FIELDS:
        if field in pivots:
            # NaN compares False: a missing cell is not a reset (parity with
            # `_guarded`, whose `_f(cur) < _f(prev)` is also NaN-tolerant); the
            # NaN then propagates into the rate columns anyway.
            resets |= delta(field) < 0
    ok &= ~resets
    if resets.any():
        _warn_disk_resets(devices, resets)

    spread = interval[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        rates = {
            "r_mb_s": np.round(delta("sectors_read") * 512 / _BYTES_PER_MIB / spread, 2),
            "w_mb_s": np.round(delta("sectors_written") * 512 / _BYTES_PER_MIB / spread, 2),
            "util_pct": np.round(np.clip(delta("ms_io") / 1000.0 / spread * 100, 0.0, 100.0), 1),
            "avg_queue_depth": np.round(delta("weighted_ms") / 1000.0 / spread, 3),
            "r_iops": np.round(delta("reads_completed") / spread, 2),
            "w_iops": np.round(delta("writes_completed") / spread, 2),
        }
        reads, writes = delta("reads_completed"), delta("writes_completed")
        rates["read_await_ms"] = np.where(reads > 0, np.round(delta("read_ms") / reads, 3), 0.0)
        rates["write_await_ms"] = np.where(writes > 0, np.round(delta("write_ms") / writes, 3), 0.0)
        rates["avg_rq_sz"] = np.where(
            (reads + writes) > 0,
            np.round((delta("sectors_read") + delta("sectors_written")) / (reads + writes), 2),
            0.0,
        )

    inflight = (
        pivots["inflight"].reindex(columns=devices).to_numpy(dtype=float)
        if "inflight" in pivots
        else np.full((len(stamps), len(devices)), math.nan)
    )
    columns = {}
    for j, device in enumerate(devices):
        for field in _DISK_RATE_FIELDS:
            values = inflight[:, j] if field == "inflight" else np.where(ok[:, j], rates[field][:, j], math.nan)
            columns[f"{device}_{field}"] = values

    frame = pd.DataFrame(columns, index=stamps)
    frame.insert(0, "t_s", stamps - stamps[0])
    return frame


def _warn_disk_resets(devices: list[str], resets: np.ndarray) -> None:
    """One WARNING per run naming the devices whose counters went backwards."""
    affected = sorted({devices[index] for index in np.where(resets.any(axis=0))[0]})
    logger.warning(
        "counter reset for disk device(s) %s (%d interval(s)) -> rate columns NaNed for those intervals",
        ", ".join(affected),
        int(resets.any(axis=1).sum()),
    )


def _cap_disk_devices(pivots: dict) -> list[str]:
    """Busiest-N device list; drops the remainder from the pivots in place."""
    devices = sorted(next(iter(pivots.values())).columns)
    if len(devices) <= _MAX_DISK_DEVICES:
        return devices

    def final_io(device: str) -> float:
        total = 0.0
        for field in ("sectors_read", "sectors_written"):
            matrix = pivots.get(field)
            if matrix is not None and len(matrix):
                value = matrix[device].iloc[-1]
                total += 0.0 if math.isnan(value) else value
        return total

    by_activity = sorted(devices, key=lambda device: (-final_io(device), device))
    kept, dropped = by_activity[:_MAX_DISK_DEVICES], by_activity[_MAX_DISK_DEVICES:]
    logger.warning(
        "disk_io_raw.csv: %d devices exceed the %d-series sheet cap; keeping the busiest by I/O, dropped: %s%s",
        len(devices),
        _MAX_DISK_DEVICES,
        ", ".join(dropped[:5]),
        ", ..." if len(dropped) > 5 else "",
    )
    for matrix in pivots.values():
        matrix.drop(columns=[device for device in matrix.columns if device not in kept], inplace=True)
    return sorted(kept)


# --- NUMA ------------------------------------------------------------------


def derive_numa(raw_dir=None, *, frames: dict | None = None) -> pd.DataFrame:
    """Per-NUMA-node CPU% + memory timeline.

    CPU: each cpu's /proc/stat jiffies are differenced, summed per node via
    ``numa_cpu_map.csv``, then busy% = 1 - (idle+iowait)/total -- the same
    definition as the host CPU column. Memory comes straight from
    ``numa_mem_raw.csv`` (long, one row per node per sample).
    """
    f = frames if frames is not None else _load_frames(raw_dir)
    series: dict[tuple[int, str], pd.Series] = {}
    series.update(_numa_cpu_series(f))
    series.update(_numa_memory_series(f))
    if not series:
        return pd.DataFrame()

    index = None
    for values in series.values():
        index = values.index if index is None else index.union(values.index)
    index = index.sort_values()

    frame = pd.DataFrame(index=index)
    frame["t_s"] = (index - index.min()).to_numpy(dtype=float)
    frame["timestamp"] = _epochs_to_text(f.get("cpu"), index)
    for node in sorted({node for node, _ in series}):
        for field, label in _NUMA_FIELD_LABELS.items():
            values = series.get((node, field))
            if values is not None:
                frame[f"NUMA{node} {label}"] = values.reindex(index).to_numpy(dtype=float)
    return frame.reset_index(drop=True)


# NUMA memory fields written by _ProcRawWriter._write_numa_mem; the MB -> GB
# conversion applies to the capacity/pages fields, swap_cached_mb and
# usage_pct already being in their display unit.
_NUMA_GB_FIELDS = ("used_mb", "free_mb", "available_mb", "anon_pages_mb", "file_pages_mb")

# Column order for the NUMA sheet: CPU first, then memory, one block per node.
_NUMA_FIELD_LABELS = {
    "cpu_pct": "CPU (%)",
    "used_mb": "Used (GB)",
    "free_mb": "Free (GB)",
    "available_mb": "Available (GB)",
    "anon_pages_mb": "Anon (GB)",
    "file_pages_mb": "File (GB)",
    "swap_cached_mb": "SwapCache (MB)",
    "usage_pct": "Usage (%)",
}


def _numa_cpu_series(frames: dict) -> dict[tuple[int, str], pd.Series]:
    percpu, numa_map = frames.get("percpu"), frames.get("numa_map")
    if percpu is None or percpu.empty or numa_map is None or numa_map.empty:
        return {}
    frame = percpu.assign(__e=_ts_to_epoch(percpu["timestamp"]))
    frame = _sort_by(frame, ["cpu_id", "__e"])
    group = frame.groupby("cpu_id", sort=False)
    interval = frame["__e"] - group["__e"].shift(1)
    ok = (interval > 0) & (interval <= MAX_INTERVAL_S)

    total = idle_iowait = None
    for field in _CPU_FIELDS:
        if field not in frame.columns:
            continue
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        delta = (frame[field] - group[field].shift(1)).where(ok)
        total = delta if total is None else total + delta
        if field in ("idle", "iowait"):
            idle_iowait = delta if idle_iowait is None else idle_iowait + delta

    node_of = dict(zip(numa_map["cpu"].astype(int), numa_map["node"].astype(int)))
    frame["__node"] = frame["cpu_id"].map(node_of)
    frame["__total"], frame["__idle_iowait"] = total, idle_iowait
    frame = frame.dropna(subset=["__node"])
    if frame.empty:
        return {}
    summed = frame.groupby(["__e", "__node"])[["__total", "__idle_iowait"]].sum(min_count=1)
    busy = ((1.0 - summed["__idle_iowait"] / summed["__total"]) * 100.0).unstack("__node")
    return {(int(node), "cpu_pct"): busy[node] for node in busy.columns}


def _numa_memory_series(frames: dict) -> dict[tuple[int, str], pd.Series]:
    raw = frames.get("numa_mem")
    if raw is None or raw.empty:
        return {}
    frame = raw.assign(__e=_ts_to_epoch(raw["timestamp"]))
    frame["node"] = pd.to_numeric(frame["node"], errors="coerce")
    for field in _NUMA_GB_FIELDS:
        if field in frame.columns:
            frame[field] = pd.to_numeric(frame[field], errors="coerce") / 1024.0
    series: dict[tuple[int, str], pd.Series] = {}
    for field in _NUMA_FIELD_LABELS:
        if field == "cpu_pct" or field not in frame.columns:
            continue
        pivot = frame.pivot_table(index="__e", columns="node", values=field, aggfunc="last", dropna=False)
        for node in pivot.columns:
            series[(int(node), field)] = pivot[node]
    return series


def _epochs_to_text(cpu_df, index) -> list[str]:
    """Human-readable timestamp for each epoch index entry (host CPU CSV is the
    reference clock; falls back to the raw epoch value)."""
    if cpu_df is not None and not cpu_df.empty:
        epochs = _ts_to_epoch(cpu_df["timestamp"]).to_numpy()
        table = dict(zip(epochs, cpu_df["timestamp"].astype(str)))
        return [table.get(value, str(value)) for value in index]
    return [str(value) for value in index]


# --- host processes --------------------------------------------------------


def derive_hostproc(raw_dir=None, *, frames: dict | None = None):
    """Per named host-process CPU cores + PSS GB.

    Cores are grouped by ``(name, pid)`` so a pid restart resets the delta
    baseline; segments are pivoted onto one curve per ``name`` (at any timestamp
    only one pid per name is live, so ``aggfunc="first"`` is unambiguous).
    """
    f = frames if frames is not None else _load_frames(raw_dir)
    hp = f.get("hostproc")
    if hp is None or hp.empty:
        return pd.DataFrame(), pd.DataFrame()
    hp = hp.copy()
    hp["__e"] = _ts_to_epoch(hp["timestamp"])
    hp["__u"] = pd.to_numeric(hp["utime"], errors="coerce")
    hp["__s"] = pd.to_numeric(hp["stime"], errors="coerce")
    hp["cores"], _ = _cores_and_guard(*_per_key_deltas(hp, ["name", "pid"]))
    hp["pss_gb"] = _column(hp, "pss_mb", hp.index) / 1024
    return _top_k_pivot(hp, "cores", "pss_gb", None, "name")
