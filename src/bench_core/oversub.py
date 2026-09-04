"""Oversubscription benchmark sweep driver.

Runs the bench-core replay kernel across a matrix of memory/CPU
oversubscription ratios: ``running_concurrency`` (N) stays fixed (from the
base config), ``total_count = N * ratio (k)`` scales per trial. One
``bench-core`` invocation per trial; the driver reads each trial's
machine-readable ``run_summary.json`` (the only contract with the kernel) and
aggregates per-trial + per-ratio degradation curves.

Inspired by ``replay-aenv-main/scripts/run_pause_resume_oversubscription_benchmark.py``
but leaner (~400 lines) because the kernel owns the per-trial lifecycle,
admission, observability, trajectory export, and vm_monitor orchestration.

The driver imports no kernel internals -- only the CLI, the YAML schema, and
the ``run_summary.json`` schema. Pure helpers are module-level so tests import
them directly.
"""
from __future__ import annotations

import copy
import csv
import json
import statistics
import time
from pathlib import Path


def parse_ratios(text: str) -> list[int]:
    """Parse ``"1,2,3"`` -> ``[1, 2, 3]``; reject non-positive / empty."""
    vals: list[int] = []
    for tok in text.replace(" ", "").split(","):
        if not tok:
            continue
        v = int(tok)
        if v < 1:
            raise ValueError(f"ratio must be >= 1, got {v}")
        vals.append(v)
    if not vals:
        raise ValueError("no ratios given")
    return vals


def default_running_concurrency(base: dict) -> int:
    """N = ``replay.running_concurrency``, falling back to ``sandbox.total_count``."""
    replay = base.get("replay") or {}
    n = replay.get("running_concurrency")
    if n is None:
        n = (base.get("sandbox") or {}).get("total_count") or 0
    if n < 1:
        raise ValueError(f"running_concurrency must be >= 1, got {n}")
    return n


def build_trial_config(
    base: dict,
    *,
    mode: str,
    ratio: int,
    n: int,
    test_duration: int,
    trial_dir: str,
    prefix: str,
) -> dict:
    """Deep-copy the base YAML and merge the per-trial oversub overrides.

    Overrides: ``sandbox.total_count = ratio*N``; ``replay.running_concurrency = N``
    (stays fixed); ``replay.mode``; ``test.round_size = ratio*N`` (so all k*N
    sandboxes run concurrently in one group -- without this, k>=2 silently
    runs multiple sequential groups of N, corrupting the oversubscription
    dynamics); ``test.round_count = 0`` (sustained until ``test.duration``);
    ``test.duration``; ``report.output_dir``; ``report.filename_prefix``.
    Everything else (backend blocks, create_batch, trajectory_dir, monitor,
    task_batch) passes through unchanged.
    """
    cfg = copy.deepcopy(base)
    target = ratio * n
    cfg.setdefault("sandbox", {})["total_count"] = target
    cfg.setdefault("replay", {})["running_concurrency"] = n
    cfg["replay"]["mode"] = mode
    cfg.setdefault("test", {})
    cfg["test"]["round_size"] = target
    cfg["test"]["round_count"] = 0
    cfg["test"]["duration"] = test_duration
    cfg.setdefault("report", {})
    cfg["report"]["output_dir"] = trial_dir
    cfg["report"]["filename_prefix"] = prefix
    return cfg


def parse_run_summary(path: Path | str) -> dict:
    """Read a trial's ``run_summary.json`` (the kernel's raw-facts contract)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compute_valid(
    summary: dict,
    return_code: int,
    *,
    n: int,
    test_duration: int,
    failure_tolerance: float,
) -> bool:
    """Sustained-rotation validity (NOT the reference's ``succeeded==k*N`` bar).

    A sustained sweep (``round_count=0``) completes many more than ``k*N``
    trajectories and some may fail from dirty-state wrap (§6.1 of the spec);
    those failures are surfaced via ``failure_rate`` in the CSV, not hidden
    behind ``valid``. ``valid`` here means "the trial ran the full window,
    did work, and did not over-admit":

      * ``return_code == 0`` (the kernel subprocess succeeded),
      * ``throughput.total > 0`` (it actually replayed something),
      * ``wall_sec >= 0.9 * test_duration`` (it sustained the window, did not
        crash/exit early),
      * ``admission.peak_active <= N`` (it never ran more than N concurrent --
        dropped for exec_only, which has no admission controller),
      * ``failure_rate <= failure_tolerance`` (configurable wrap-noise allowance).
    """
    if return_code != 0:
        return False
    tp = summary.get("throughput") or {}
    total = tp.get("total", 0)
    if total <= 0:
        return False
    wall = summary.get("wall_sec") or 0
    if test_duration > 0 and wall < 0.9 * test_duration:
        return False
    adm = summary.get("admission")
    if adm and adm.get("peak_active") is not None and adm["peak_active"] > n:
        return False
    failed = tp.get("failed", 0)
    failure_rate = failed / total if total else 1.0
    if failure_rate > failure_tolerance:
        return False
    return True


# trial-summary.csv column order (mirrors the reference TRIAL_COLUMNS + the
# sustained-mode failure_rate / test_duration columns).
TRIAL_COLUMNS = [
    "mode",
    "ratio",
    "oversubscription_ratio",
    "repeat",
    "running_concurrency",
    "target_count",
    "total",
    "succeeded",
    "failed",
    "failure_rate",
    "peak_active",
    "granted",
    "avg_queue_wait_sec",
    "control_dispatched",
    "test_duration",
    "wall_sec",
    "tasks_per_sec",
    "steps_per_sec",
    "lifecycle_overhead_pct",
    "return_code",
    "valid",
    "reused",
    "started_at",
    "completed_at",
    "error",
    "trial_dir",
    "run_summary_path",
]

RATIO_COLUMNS = [
    "mode",
    "ratio",
    "attempted",
    "successful",
    "all_repeats_successful",
    "median_wall_sec",
    "median_tasks_per_sec",
    "median_peak_active",
    "median_queue_wait_sec",
    "time_degradation_vs_1_1_pct",
    "throughput_gain_vs_1_1_pct",
]

TRAJECTORY_COLUMNS = [
    "mode",
    "ratio",
    "repeat",
    "trajectory_id",
    "sandbox_index",
    "n_steps",
    "n_failed",
    "n_timeout",
    "success_rate",
    "elapsed_sec",
    "create_error_type",
    "kill_error_type",
]


def trial_row(
    *,
    mode: str,
    ratio: int,
    repeat: int,
    running_concurrency: int,
    target_count: int,
    summary: dict,
    return_code: int,
    valid: bool,
    reused: bool,
    trial_dir: str,
    run_summary_path: str,
) -> dict:
    """Build a trial-summary.csv row from a parsed run_summary + driver metadata."""
    tp = summary.get("throughput") or {}
    total = tp.get("total", 0)
    failed = tp.get("failed", 0)
    adm = summary.get("admission") or {}
    lc = summary.get("lifecycle_overhead") or {}
    return {
        "mode": mode,
        "ratio": ratio,
        "oversubscription_ratio": ratio,
        "repeat": repeat,
        "running_concurrency": running_concurrency,
        "target_count": target_count,
        "total": total,
        "succeeded": tp.get("succeeded", 0),
        "failed": failed,
        "failure_rate": round(failed / total, 4) if total else 0.0,
        "peak_active": adm.get("peak_active"),
        "granted": adm.get("granted"),
        "avg_queue_wait_sec": adm.get("avg_queue_wait_sec"),
        "control_dispatched": adm.get("control_dispatched"),
        "test_duration": summary.get("test_duration") or 0,
        "wall_sec": summary.get("wall_sec"),
        "tasks_per_sec": tp.get("tasks_per_sec"),
        "steps_per_sec": tp.get("steps_per_sec"),
        "lifecycle_overhead_pct": lc.get("pct_of_slice_total"),
        "return_code": return_code,
        "valid": valid,
        "reused": reused,
        "started_at": summary.get("started_at"),
        "completed_at": summary.get("completed_at"),
        "error": summary.get("error"),
        "trial_dir": trial_dir,
        "run_summary_path": run_summary_path,
    }


def _median(vals: list[float]) -> float:
    return statistics.median(vals) if vals else 0.0


def aggregate_ratio_summary(trials: list[dict]) -> list[dict]:
    """One row per ``(mode, ratio)``: medians across repeats + degradation vs k=1.

    Degradation is computed **within a mode** vs that mode's ``k=1`` baseline,
    so lifecycle and exec_only each get their own curve (memory-overcommit
    overhead vs CPU-oversubscription degradation). When a mode has no k=1
    trial, the degradation columns default to ``0.0`` (no baseline to compare).
    """
    by_key: dict[tuple[str, int], list[dict]] = {}
    for t in trials:
        by_key.setdefault((t["mode"], t["ratio"]), []).append(t)

    rows: list[dict] = []
    for (mode, ratio), group in sorted(by_key.items()):
        walls = [g["wall_sec"] for g in group if g.get("wall_sec") is not None]
        tps = [g["tasks_per_sec"] for g in group if g.get("tasks_per_sec") is not None]
        peaks = [g["peak_active"] for g in group if g.get("peak_active") is not None]
        waits = [g["avg_queue_wait_sec"] for g in group if g.get("avg_queue_wait_sec") is not None]
        attempted = len(group)
        successful = sum(1 for g in group if g["valid"])
        rows.append(
            {
                "mode": mode,
                "ratio": ratio,
                "attempted": attempted,
                "successful": successful,
                "all_repeats_successful": successful == attempted,
                "median_wall_sec": _median(walls),
                "median_tasks_per_sec": _median(tps),
                "median_peak_active": _median(peaks),
                "median_queue_wait_sec": _median(waits),
                "time_degradation_vs_1_1_pct": 0.0,
                "throughput_gain_vs_1_1_pct": 0.0,
            }
        )

    by_mode: dict[str, list[dict]] = {}
    for r in rows:
        by_mode.setdefault(r["mode"], []).append(r)
    for mrows in by_mode.values():
        base = next((r for r in mrows if r["ratio"] == 1), None)
        if not base:
            continue
        for r in mrows:
            bw = base["median_wall_sec"] or 0
            r["time_degradation_vs_1_1_pct"] = round(((r["median_wall_sec"] - bw) / bw * 100) if bw else 0.0, 3)
            bt = base["median_tasks_per_sec"] or 0
            r["throughput_gain_vs_1_1_pct"] = round(((r["median_tasks_per_sec"] - bt) / bt * 100) if bt else 0.0, 3)
    return rows


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _trajectory_rows(trials: list[dict]) -> list[dict]:
    """One row per trajectory per trial (from each trial's trajectories/index.json)."""
    out: list[dict] = []
    for t in trials:
        idx_path = t.get("run_summary_path")
        if not idx_path:
            continue
        # run_summary lives in <trial_stamp_dir>/; index.json in <trial_stamp_dir>/trajectories/
        idx = Path(idx_path).parent / "trajectories" / "index.json"
        if not idx.exists():
            continue
        catalog = json.loads(idx.read_text(encoding="utf-8"))
        for tr in catalog.get("trajectories", []):
            out.append(
                {
                    "mode": t["mode"],
                    "ratio": t["ratio"],
                    "repeat": t["repeat"],
                    "trajectory_id": tr.get("trajectory_id"),
                    "sandbox_index": tr.get("sandbox_index"),
                    "n_steps": tr.get("n_steps"),
                    "n_failed": tr.get("n_failed"),
                    "n_timeout": tr.get("n_timeout"),
                    "success_rate": tr.get("success_rate"),
                    "elapsed_sec": tr.get("elapsed_sec"),
                    "create_error_type": tr.get("create_error_type"),
                    "kill_error_type": tr.get("kill_error_type"),
                }
            )
    return out


def write_outputs(trials: list[dict], *, output_root: Path) -> None:
    """Write trial/ratio/trajectory CSVs + benchmark-report.json (restart-safe).

    Call after every trial so a killed driver leaves a complete partial set.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    _write_csv(output_root / "trial-summary.csv", TRIAL_COLUMNS, trials)
    ratio_rows = aggregate_ratio_summary(trials)
    _write_csv(output_root / "ratio-summary.csv", RATIO_COLUMNS, ratio_rows)
    traj_rows = _trajectory_rows(trials)
    _write_csv(output_root / "trajectory-detail.csv", TRAJECTORY_COLUMNS, traj_rows)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "configuration": {},
        "trials": trials,
        "ratio_summary": ratio_rows,
        "trajectory_details": traj_rows,
    }
    (output_root / "benchmark-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
