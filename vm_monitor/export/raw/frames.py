"""Raw-data CSV loading and the delta/counter contract every derivation shares.

Why this module exists: the derived timelines all answer the same two
questions -- "what did this CSV contain" and "is this interval a valid delta".
Both answers live here so a capture is read exactly once (:func:`_load_frames`)
and every metric applies the same reset/gap rule (:func:`_guarded`), instead of
each derivation re-implementing its own.

Memory is the other half of the contract. A 2-hour capture at 2 s is ~3M rows
for the two long-format files; a 24-hour one is ~33M (~4 GB if materialized).
The long files are therefore *streamed* in bounded chunks and reduced to the
per-sample aggregates the sheets actually plot (:func:`_stream_vm`,
:func:`_stream_numa_cpu`), so peak RSS is set by the chunk size and the wide
output, not by the capture length.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from vm_monitor.base import _CLK_TCK

logger = logging.getLogger(__name__)

# Calibration knobs (ponytail: physical world needs tuning, not hard-coded).
MAX_INTERVAL_S = 10.0  # gap guard: per-key delta over a gap longer than this -> NaN

# Rows per streaming chunk. 250k x ~10 columns is ~20 MB of transient frames:
# large enough that per-chunk overhead disappears, small enough that 24 h of
# capture costs the same memory as 2 h.
_CHUNK_ROWS = 250_000

# host_mem_raw.csv holds MB (base._read_meminfo converts kB -> MB before the
# writer sees it); /1024 -> GiB.
_MEMINFO_MB_PER_GIB = 1024

# /proc/stat cpu fields summed into the host/NUMA busy percentage. guest is
# already folded into user by the kernel, so it is NOT added again.
_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

_VMSTAT_FIELDS = [
    "pswpin",
    "pswpout",
    "pgscan_kswapd",
    "pgscan_direct",
    "pgscan_direct_throttle",
    "pgsteal_kswapd",
    "pgsteal_direct",
    "workingset_refault_file",
    # allocstall is per-zone-type in /proc/vmstat (no plain sum); list every
    # known variant so the reset guard covers them. Absent variants -> NaN via
    # _f -> NaN<NaN is False -> no spurious reset on configs that lack a zone.
    "allocstall_dma",
    "allocstall_dma32",
    "allocstall_normal",
    "allocstall_movable",
    "allocstall_high",
    "allocstall_device",
]

_VM_DTYPES = {
    "pid": "int64",
    "utime": "float64",
    "stime": "float64",
    "pss_mb": "float64",
    "rss_mb": "float64",
    "memory_mb": "float64",
}


@dataclass
class VmStream:
    """Reduced result of one streaming pass over ``vm_cpu_raw.csv``.

    ``delta_jiffies`` / ``pss_sum`` / ``rss_sum`` / ``mem_sum`` / ``count`` are
    per-episode aggregates for the Host sheet; the two wide frames are the
    per-VM timelines the Per-VM sheet pivots (and top-K caps). All are indexed
    by ``__e``.
    """

    delta_jiffies: pd.Series
    pss_sum: pd.Series | None
    rss_sum: pd.Series | None
    mem_sum: pd.Series | None
    count: pd.Series
    cores_wide: pd.DataFrame
    pss_wide: pd.DataFrame
    skipped: int = 0
    skipped_example: str | None = None


def _f(d: dict, k: str) -> float:
    """Best-effort float of a CSV cell (NaN if empty/non-numeric)."""
    v = d.get(k)
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _gauge_int(d: dict, k: str):
    """Instantaneous count cell: int when present, NaN when absent."""
    v = _f(d, k)
    return math.nan if math.isnan(v) else int(v)


def _ts_to_epoch(series: pd.Series) -> pd.Series:
    """Normalize a raw-CSV timestamp column to float epoch seconds for delta math.

    Numeric input passes straight through. The always-on dumper writes
    ``datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")``; parsing with that exact
    format first is ~3x faster than inference on the multi-million-row VM CSV.
    Rows that still fail (fixtures with whole-second timestamps) fall back to
    inference; NaT becomes NaN so a bad row NaN-holes its interval instead of
    crashing the build.
    """
    try:
        return series.astype(float)
    except (TypeError, ValueError):
        pass
    parsed = pd.to_datetime(series, format="%Y-%m-%d %H:%M:%S.%f", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed = parsed.copy()
        parsed[missing] = pd.to_datetime(series[missing], errors="coerce")
    # Force microsecond resolution then view as int64: unit-agnostic (pandas may
    # store datetime64[ns] or [us]) and preserves sub-second precision, which
    # the 1-second disk/ublk sub-samples depend on (whole-second truncation
    # collapsed distinct samples into dt=0 and NaN-holed the rate columns).
    out = pd.Series(math.nan, index=series.index, dtype="float64")
    mask = parsed.notna()
    if mask.any():
        out[mask] = parsed[mask].to_numpy().astype("datetime64[us]").astype("int64").astype("float64") / 1e6
    return out


def _guarded(
    cur: dict, prev: dict, dt: float, key: str, fields: list[str], max_interval: float = MAX_INTERVAL_S
) -> bool:
    """True if this delta must be NaNed: dt<=0, dt>max_interval (gap), or any
    cumulative field cur<prev (counter reset -- e.g. VM pid restart zeroes
    utime/stime, device remount zeroes disk stat). Logs a WARNING per case.

    The single-interval reference implementation; the vectorized derivations
    apply the same predicate in bulk.
    """
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


def _read_csv(raw_dir: Path, fname: str, dtype: dict | None = None) -> pd.DataFrame | None:
    p = Path(raw_dir) / fname
    if not p.exists() or p.stat().st_size == 0:
        logger.warning("raw CSV missing/empty: %s -> skipping derived sheet", p)
        return None
    try:
        return pd.read_csv(p, dtype=dtype)
    except (pd.errors.ParserError, OSError, ValueError) as e:
        logger.warning("raw CSV unparseable: %s (%s) -> skipping", p, e)
        return None


def _read_csv_optional(raw_dir: Path, fname: str, dtype: dict | None = None) -> pd.DataFrame | None:
    """Like :func:`_read_csv` but silent on absence -- the file is provider-dependent."""
    p = Path(raw_dir) / fname
    if not p.exists() or p.stat().st_size == 0:
        return None
    return _read_csv(raw_dir, fname, dtype=dtype)


def _with_epoch(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Parse ``timestamp`` once into ``__e`` and drop the string column.

    The raw string column is the single biggest object-dtype block in memory
    (2.7M Python strings on a 2h/384-VM capture); every derivation works from
    the float epoch instead. The display clock lives on the host CPU frame,
    which :func:`_load_frames` keeps verbatim.
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    df["__e"] = _ts_to_epoch(df["timestamp"])
    df.drop(columns=["timestamp"], inplace=True)
    return df


def _load_frames(raw_dir) -> dict[str, object | None]:
    """Read every raw CSV once, reducing the two long files while streaming.

    Returns the small per-sample frames verbatim plus:

    - ``vm``     -> :class:`VmStream` (fleet aggregates + per-VM wide timelines)
    - ``percpu`` -> per-(epoch, NUMA node) counter sums
    - ``cpu``    -> keeps its ``timestamp`` text column as the display clock
    """
    raw_dir = Path(raw_dir)
    cpu = _read_csv(raw_dir, "host_cpu_raw.csv")
    if cpu is not None and not cpu.empty:
        cpu = cpu.assign(__e=_ts_to_epoch(cpu["timestamp"]))
    numa_map = _read_csv_optional(raw_dir, "numa_cpu_map.csv", {"node": "int64", "cpu": "int64"})
    return {
        "cpu": cpu,
        "mem": _with_epoch(_read_csv(raw_dir, "host_mem_raw.csv")),
        "vmstat": _with_epoch(_read_csv(raw_dir, "host_vmstat_raw.csv")),
        "ublk": _with_epoch(_read_csv_optional(raw_dir, "ublk_cpu_raw.csv", {"pid": "int64"})),
        "vm": _stream_vm(raw_dir, _host_epochs(cpu)),
        "disk": _with_epoch(_read_csv(raw_dir, "disk_io_raw.csv")),
        "percpu": _stream_numa_cpu(raw_dir, numa_map),
        "numa_map": numa_map,
        "numa_mem": _with_epoch(_read_csv_optional(raw_dir, "numa_mem_raw.csv", {"node": "int64"})),
        "api": _with_epoch(_read_csv_optional(raw_dir, "api_state_raw.csv")),
        "hostproc": _with_epoch(_read_csv_optional(raw_dir, "hostproc_cpu_raw.csv", {"pid": "int64"})),
    }


def _host_epochs(cpu: pd.DataFrame | None) -> list[float]:
    """Host clock in sample order (the reference every other CSV joins onto)."""
    if cpu is None or cpu.empty:
        return []
    ordered = cpu.sort_values("timestamp", kind="stable")["__e"]
    return [stamp for stamp in ordered.tolist() if not math.isnan(stamp)]


def _stream_vm(raw_dir, host_epochs: list[float]) -> VmStream | None:
    """One streaming pass over ``vm_cpu_raw.csv``.

    Reduces a row per (VM, sample) into the two shapes the workbook needs --
    per-sample fleet aggregates and per-VM wide timelines -- while keeping only
    one chunk of rows alive. The last row per VM is carried into the next chunk
    so a delta spanning the chunk boundary is not lost.

    Method A (fleet cores): a VM contributes at e_cur only if its own previous
    sample landed on the immediately preceding host epoch, so a VM born/died
    mid-interval is excluded rather than dividing a two-interval delta by one.
    """
    p = Path(raw_dir) / "vm_cpu_raw.csv"
    if not p.exists() or p.stat().st_size == 0:
        logger.warning("raw CSV missing/empty: %s -> skipping derived sheet", p)
        return None
    host_prev = dict(zip(host_epochs[1:], host_epochs[:-1]))

    carry: pd.DataFrame | None = None
    fleet, pss_sum, rss_sum, mem_sum, count = [], [], [], [], []
    cores_wide, pss_wide = [], []
    has_memory = True
    has_pss = False  # sticky: saw at least one non-empty pss_mb value in the file
    has_rss = False  # sticky: saw at least one non-empty rss_mb value in the file
    skipped = 0
    skipped_example: str | None = None
    try:
        for chunk in pd.read_csv(p, chunksize=_CHUNK_ROWS, dtype=_VM_DTYPES):
            stamps = chunk.pop("timestamp")
            chunk["__e"] = _ts_to_epoch(stamps)
            chunk["__j"] = pd.to_numeric(chunk["utime"], errors="coerce") + pd.to_numeric(
                chunk["stime"], errors="coerce"
            )
            chunk["__pss"] = pd.to_numeric(chunk["pss_mb"], errors="coerce")
            chunk["pss_gb"] = chunk["__pss"] / 1024
            has_pss = has_pss or bool(chunk["__pss"].notna().any())
            has_memory = has_memory and "memory_mb" in chunk.columns
            chunk["__mem"] = pd.to_numeric(chunk["memory_mb"], errors="coerce") if has_memory else math.nan
            chunk["__rss"] = pd.to_numeric(chunk["rss_mb"], errors="coerce") if "rss_mb" in chunk.columns else math.nan
            has_rss = has_rss or bool(chunk["__rss"].notna().any())
            chunk["__carry"] = False
            if carry is not None:
                chunk = pd.concat([carry, chunk], ignore_index=True)

            chunk = chunk.sort_values(["vm_name", "__e"], kind="stable")
            group = chunk.groupby("vm_name", sort=False)
            prev_e, prev_j = group["__e"].shift(1), group["__j"].shift(1)
            dt, dj = chunk["__e"] - prev_e, chunk["__j"] - prev_j
            # Fleet: both endpoints must sit on consecutive host epochs.
            fleet_dj = dj.where((chunk["__e"].map(host_prev) == prev_e) & (dj >= 0))
            # Per-VM: the ordinary interval guard (dt>0, no gap, no reset).
            ok = (dt > 0) & (dt <= MAX_INTERVAL_S) & (dj >= 0)
            chunk["cores"] = (dj / _CLK_TCK / dt).where(ok)
            # Intervals dropped by the guard, excluding by-design first samples.
            skipped_mask = (~ok) & prev_e.notna()
            skipped += int(skipped_mask.sum())
            if skipped_example is None and skipped_mask.any():
                skipped_example = str(chunk.loc[skipped_mask, "vm_name"].iloc[0])

            fresh = chunk[~chunk["__carry"]]
            if fresh.empty:
                carry = chunk.groupby("vm_name", sort=False).tail(1).assign(__carry=True)
                continue
            epochs = fresh["__e"].to_numpy()
            fleet.append(fleet_dj[~chunk["__carry"]].groupby(epochs).sum(min_count=1))
            pss_sum.append(fresh["__pss"].groupby(epochs).sum(min_count=1))
            rss_sum.append(fresh["__rss"].groupby(epochs).sum(min_count=1))
            mem_sum.append(fresh["__mem"].groupby(epochs).sum(min_count=1))
            count.append(fresh.groupby("__e")["vm_name"].count())
            cores_wide.append(
                fresh.pivot_table(index="__e", columns="vm_name", values="cores", aggfunc="first", dropna=False)
            )
            pss_wide.append(
                fresh.pivot_table(index="__e", columns="vm_name", values="pss_gb", aggfunc="first", dropna=False)
            )
            carry = chunk.groupby("vm_name", sort=False).tail(1).assign(__carry=True)
    except (pd.errors.ParserError, OSError, ValueError) as e:
        logger.warning("vm_cpu_raw.csv unparseable: %s -> per-VM sheet skipped", e)
        return None

    if not fleet:
        return None
    return VmStream(
        delta_jiffies=_merge_epoch_series(fleet),
        pss_sum=_merge_epoch_series(pss_sum) if has_pss else None,
        rss_sum=_merge_epoch_series(rss_sum) if has_rss else None,
        mem_sum=_merge_epoch_series(mem_sum) if has_memory else None,
        count=_merge_epoch_series(count).fillna(0),
        cores_wide=_merge_wide(cores_wide),
        pss_wide=_merge_wide(pss_wide),
        skipped=skipped,
        skipped_example=skipped_example,
    )


def _merge_epoch_series(parts: list[pd.Series]) -> pd.Series:
    """Concatenate per-chunk epoch aggregates; a split sample overlaps by epoch."""
    return pd.concat(parts).groupby(level=0, sort=True).sum(min_count=1)


def _merge_wide(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate per-chunk pivots; a VM missing from a chunk keeps its gap."""
    return pd.concat(parts).groupby(level=0, sort=True).first()


def _stream_numa_cpu(raw_dir, numa_map: pd.DataFrame | None) -> pd.DataFrame | None:
    """Stream ``host_percpu_raw.csv`` into per-(epoch, node) counter sums.

    Summing per cpu and differencing per node are interchangeable (both are
    linear over the counters, and jiffies are exact in float64), so the
    320-CPU file never has to be materialized to plot 4 NUMA lines.
    """
    if numa_map is None or numa_map.empty:
        return None
    p = Path(raw_dir) / "host_percpu_raw.csv"
    if not p.exists() or p.stat().st_size == 0:
        return None
    node_of = dict(zip(numa_map["cpu"].astype(int), numa_map["node"].astype(int)))
    parts = []
    try:
        for chunk in pd.read_csv(p, chunksize=_CHUNK_ROWS, dtype={"cpu_id": "int64"}):
            stamps = chunk.pop("timestamp")
            chunk["__e"] = _ts_to_epoch(stamps)
            chunk["__node"] = chunk["cpu_id"].map(node_of)
            chunk = chunk.dropna(subset=["__node"])
            fields = [field for field in _CPU_FIELDS if field in chunk.columns]
            if chunk.empty or not fields:
                continue
            parts.append(chunk.groupby(["__e", "__node"], sort=False)[fields].sum(min_count=1))
    except (pd.errors.ParserError, OSError, ValueError) as e:
        logger.warning("host_percpu_raw.csv unparseable: %s -> NUMA CPU columns skipped", e)
        return None
    if not parts:
        return None
    return pd.concat(parts).groupby(level=[0, 1], sort=True).sum(min_count=1).reset_index()


def _rows_by_epoch(df: pd.DataFrame | None) -> dict:
    """Epoch -> row dict, for the exact-timestamp joins (gauges + vmstat)."""
    if df is None or df.empty:
        return {}
    return {e: row for e, row in zip(df["__e"].to_numpy(), df.to_dict("records"))}


def _frame_by_epoch(df: pd.DataFrame | None, epochs: pd.Series) -> pd.DataFrame | None:
    """Reindex a per-sample frame onto the host clock (exact epoch join).

    Duplicate epochs collapse to the last snapshot, matching the old
    ``{ts: row}`` dict build. The returned frame carries the host clock's
    POSITIONAL index so it can be joined column-wise with the other blocks.
    """
    if df is None or df.empty:
        return None
    stamps = df["__e"]
    keep = ~stamps.duplicated(keep="last")
    aligned = df.loc[keep].set_index("__e").reindex(epochs.to_numpy())
    aligned.index = epochs.index
    return aligned


def _asof_join(sample_ts, value_ts, values):
    """Last value sampled at or before each sample ts (backward as-of, gauges only)."""
    index = np.searchsorted(value_ts, sample_ts, side="right") - 1
    out = np.full(len(sample_ts), math.nan)
    found = index >= 0
    out[found] = values[index[found]]
    return out


def _column(frame: pd.DataFrame | None, name: str, index: pd.Index) -> pd.Series:
    """Numeric column of ``frame`` aligned to ``index``; all-NaN when absent.

    Keeps the "one absent field must not blank an unrelated column" contract:
    callers that need a missing field to mean NaN get NaN, not a KeyError.
    """
    if frame is None or name not in frame.columns:
        return pd.Series(math.nan, index=index)
    return pd.to_numeric(frame[name], errors="coerce")
