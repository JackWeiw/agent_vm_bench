"""Raw-data CSV loading and the delta/counter contract every derivation shares.

Why this module exists: the derived timelines all answer the same two
questions -- "what did this CSV contain" and "is this interval a valid delta".
Both answers live here so a 2-hour capture is read exactly once
(:func:`_load_frames`) and every metric applies the same reset/gap rule
(:func:`_guarded`), instead of each derivation re-implementing its own.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Calibration knobs (ponytail: physical world needs tuning, not hard-coded).
MAX_INTERVAL_S = 10.0  # gap guard: per-key delta over a gap longer than this -> NaN

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


def _load_frames(raw_dir) -> dict[str, pd.DataFrame | None]:
    """Read every raw CSV exactly once. Derivation functions take this dict.

    dtypes are declared for the huge long-format files (vm_cpu_raw is ~1.4M
    rows on a 2h/384-VM capture) so pandas does not infer or upcast them.
    """
    raw_dir = Path(raw_dir)
    return {
        "cpu": _read_csv(raw_dir, "host_cpu_raw.csv"),
        "mem": _read_csv(raw_dir, "host_mem_raw.csv"),
        "vmstat": _read_csv(raw_dir, "host_vmstat_raw.csv"),
        "ublk": _read_csv_optional(raw_dir, "ublk_cpu_raw.csv", {"pid": "int64"}),
        "vm": _read_csv(
            raw_dir,
            "vm_cpu_raw.csv",
            {"pid": "int64", "utime": "float64", "stime": "float64", "pss_mb": "float64", "memory_mb": "float64"},
        ),
        "disk": _read_csv(raw_dir, "disk_io_raw.csv"),
        "percpu": _read_csv_optional(raw_dir, "host_percpu_raw.csv", {"cpu_id": "int64"}),
        "numa_map": _read_csv_optional(raw_dir, "numa_cpu_map.csv", {"node": "int64", "cpu": "int64"}),
        "numa_mem": _read_csv_optional(raw_dir, "numa_mem_raw.csv", {"node": "int64"}),
        "api": _read_csv_optional(raw_dir, "api_state_raw.csv"),
        "hostproc": _read_csv_optional(raw_dir, "hostproc_cpu_raw.csv", {"pid": "int64"}),
    }


def _rows_by_epoch(df: pd.DataFrame | None) -> dict:
    """Epoch -> row dict, for the exact-timestamp joins (gauges + vmstat)."""
    if df is None or df.empty:
        return {}
    epochs = _ts_to_epoch(df["timestamp"]).to_numpy()
    return {e: row for e, row in zip(epochs, df.to_dict("records"))}


def _frame_by_epoch(df: pd.DataFrame | None, epochs: pd.Series) -> pd.DataFrame | None:
    """Reindex a per-sample frame onto the host clock (exact epoch join).

    Duplicate epochs collapse to the last snapshot, matching the old
    ``{ts: row}`` dict build. The returned frame carries the host clock's
    POSITIONAL index so it can be joined column-wise with the other blocks.
    """
    if df is None or df.empty:
        return None
    stamps = _ts_to_epoch(df["timestamp"])
    keep = ~stamps.duplicated(keep="last")
    aligned = df.loc[keep].assign(__e=stamps[keep]).set_index("__e").reindex(epochs.to_numpy())
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
