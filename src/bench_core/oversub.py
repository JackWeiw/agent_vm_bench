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

Admission-free modes (``exec_ephemeral`` / ``exec_only``) run PROCESS-SHARDED
by default (``--shards auto`` -> P = ratio): a single CPython process saturates
one GIL (~1 core, ~50-55 steps/s at N=384) and high-ratio trials turn
client-bound, so each trial splits into P kernel subprocesses of ~N slots --
the mechanism lives in :mod:`bench_core.oversub_shards`; this driver composes
it and merges the shard artifacts back into the single-trial
``run_summary.json`` / ``trajectories/index.json`` contract, so every
downstream consumer (validity, CSVs, compare, reuse) is shard-unaware.

The driver imports no kernel data-path internals (lifecycle / admission /
stats / observability) -- only the CLI, the YAML schema, the
``run_summary.json`` schema, and the shared ``setup_logging`` CLI helper.
Pure helpers are module-level so tests import them directly.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import statistics
import subprocess
import time
from pathlib import Path

import yaml

from bench_core.oversub_shards import (
    ShardGroupRunner,
    ShardPlan,
    build_shard_command,
    coerce_shards,
    collect_shard_artifacts,
    merge_run_summaries,
    merge_trajectory_indexes,
    plan_shards,
    write_merged_artifacts,
    write_shard_configs,
)
from bench_core.utils import format_duration, setup_logging

logger = logging.getLogger(__name__)


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


def _coerce_ratios(v) -> list[int]:
    """YAML list ``[1, 2, 3]`` or comma string ``"1,2,3"`` -> ``[1, 2, 3]``.

    A sweep-config may carry either form (lists are idiomatic in YAML, strings
    match the ``--ratios`` CLI flag); normalize to a validated list of ints.
    """
    if isinstance(v, str):
        return parse_ratios(v)
    if isinstance(v, list | tuple):
        vals = [int(x) for x in v]
        if not vals or any(x < 1 for x in vals):
            raise ValueError(f"ratios must be positive, got {vals}")
        return vals
    raise ValueError(f"ratios must be a list or string, got {type(v).__name__}")


def _coerce_modes(v) -> list[str]:
    """YAML list or comma string -> stripped, non-empty mode names."""
    if isinstance(v, str):
        return [m.strip() for m in v.split(",") if m.strip()]
    if isinstance(v, list | tuple):
        return [str(m).strip() for m in v if str(m).strip()]
    raise ValueError(f"modes must be a list or string, got {type(v).__name__}")


def _coerce_cmd(v) -> list[str]:
    """Normalize a ``bench_core_bin`` value to a list of argv elements.

    A YAML scalar (``bench_core_bin: bench-core``) is ONE argv element -- the
    binary name -- not a shell line, so it is wrapped rather than split. Without
    this, ``[*args.bench_core_bin, ...]`` would spread a scalar string into
    chars (``['b', 'e', ...]``) and the driver would try to exec ``b``. Use a
    list (``[python, -m, stub]``) for a multi-element command.
    """
    if isinstance(v, str):
        return [v]
    if isinstance(v, list | tuple):
        return [str(x) for x in v]
    raise ValueError(f"bench_core_bin must be a string or list, got {type(v).__name__}")


def default_running_concurrency(base: dict) -> int:
    """N = ``replay.running_concurrency``, falling back to ``sandbox.total_count``."""
    replay = base.get("replay") or {}
    n = replay.get("running_concurrency")
    if n is None:
        n = (base.get("sandbox") or {}).get("total_count") or 0
    if n < 1:
        raise ValueError(f"running_concurrency must be >= 1, got {n}")
    return n


# Default outer wall-clock per trial (seconds). A defense-in-depth safety net: a
# hung kernel subprocess (e.g. a pathological post-kill render) cannot block the
# sweep indefinitely. 21600s = 6h -- generous enough that a legitimate large
# lifecycle trial (1152 trajectories, replay + render) completes well within it,
# while still catching a multi-hour hang instead of waiting forever. Override per
# sweep (--trial-timeout-sec) or disable with 0. The render-path caps (Gantt
# segment cap, per-step/overhead downsample) are the primary fix; this is the
# backstop so a future uncapped path cannot wedge the sweep again.
DEFAULT_TRIAL_TIMEOUT_SEC = 21600

# True defaults for the sweep knobs (used when neither a CLI flag nor a
# sweep-config value is given). Mirrors build_arg_parser; main() resolves
# CLI flag > sweep-config > these.
_TRUE_DEFAULTS: dict[str, object] = {
    "provider": "aenv",
    "running_concurrency": None,
    "ratios": "1,2,3",
    "modes": "lifecycle,exec_ephemeral",
    "repeats": 1,
    "test_duration": None,
    "failure_tolerance": 0.0,
    "cooldown_sec": 30,
    "cleanup_between_trials": "on",
    "trial_timeout_sec": DEFAULT_TRIAL_TIMEOUT_SEC,
    "output_root": None,
    "reuse": False,
    "stop_on_failure": False,
    "dry_run": False,
    "no_vm_monitor": False,
    "multiplier": None,
    "shards": "auto",
    "bench_core_bin": ["bench-core"],
    "auto_compare": True,
}


def load_sweep_config(path: Path | str) -> dict:
    """Load a sweep-config YAML (mirrors the CLI knobs + ``base_config``).

    Returns the raw mapping; ``ratios``/``modes`` stay in their YAML form and
    are coerced later by main(). Unknown keys are rejected so a typo
    (``repeat:`` vs ``repeats:``) fails loudly instead of being silently ignored.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"sweep config must be a mapping, got {type(raw).__name__}")
    known = set(_TRUE_DEFAULTS) | {"base_config"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown sweep-config key(s): {sorted(unknown)}")
    return raw


def build_trial_config(
    base: dict,
    *,
    mode: str,
    ratio: int,
    n: int,
    test_duration: int,
    trial_dir: str,
    prefix: str,
    multiplier: int | None = None,
) -> dict:
    """Deep-copy the base YAML and merge the per-trial oversub overrides.

    Overrides: ``sandbox.total_count = ratio*N``; ``replay.running_concurrency = N``
    (stays fixed); ``replay.trajectory_limit = N`` (fixes the issue set to the
    first N trajectories PER TEMPLATE across all ratios so only concurrency
    varies); ``replay.mode``; ``test.round_size = ratio*N`` (so all k*N
    sandboxes run concurrently in one group -- without this, k>=2 silently
    runs multiple sequential groups of N, corrupting the oversubscription
    dynamics); ``test.duration`` (the time ceiling); ``report.output_dir``;
    ``report.filename_prefix``. Everything else passes through unchanged --
    notably ``test.round_count`` (the work-amount knob: how many passes of the
    fleet). A trial ends at whichever comes first: ``round_count`` rounds OR
    ``test.duration`` seconds (the kernel's own ``round_count``/``duration``
    whichever-first semantics; see config/common/replay.yaml). Set
    ``round_count: 1`` (base default) for a bounded one-pass trial, ``0`` for a
    sustained-until-duration window.
    """
    cfg = copy.deepcopy(base)
    target = ratio * n
    cfg.setdefault("sandbox", {})["total_count"] = target
    cfg.setdefault("replay", {})["running_concurrency"] = n
    # Fix the issue set across ratios so only concurrency varies. Sets
    # trajectory_limit=N (the single-template strategy: first N per template).
    # For multi-template (T>1) the kernel's ``load_pool`` further clamps this to
    # N//T (the ratio-1 per-template fleet share under the balanced fleet) so
    # count_t <= fleet_t(ratio 1) and the running issue set stays fixed across
    # ratios -- without that clamp, capping to N lets only N/T issues per
    # template run at ratio 1 and the set grows with ratio until k>=T, drifting
    # the baseline. The driver cannot compute T itself (the trajectory pool /
    # manifest live on the test host, not necessarily the driver's box), so the
    # clamp is kernel-side. Per-template (not global first-N) keeps every
    # template covered in a multi-template manifest.
    cfg["replay"]["trajectory_limit"] = n
    cfg["replay"]["mode"] = mode
    cfg.setdefault("test", {})
    cfg["test"]["round_size"] = target
    cfg["test"]["duration"] = test_duration
    cfg.setdefault("report", {})
    cfg["report"]["output_dir"] = trial_dir
    cfg["report"]["filename_prefix"] = prefix
    # When multiplier (M) is set, switch this trial to fixed+exec_ephemeral+M:
    # each of the ratio*N sandboxes runs exactly M trajectories then exits
    # (stop-when-done). total_count/round_size/trajectory_limit are retained
    # for config-object compatibility but unused under fixed+M -- the per-slot
    # bound is replay.multiplier; round_count/round_size are round_robin knobs.
    if multiplier is not None:
        cfg["replay"]["multiplier"] = multiplier
        cfg["test"]["benchmark_mode"] = "fixed"
    return cfg


def parse_run_summary(path: Path | str) -> dict:
    """Read a trial's ``run_summary.json`` (the kernel's raw-facts contract)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compute_valid(
    summary: dict,
    return_code: int,
    *,
    n: int,
    failure_tolerance: float,
    multiplier: int | None = None,
    ratio: int | None = None,
) -> bool:
    """Run-to-completion validity (one pass of the k*N fleet).

    A trial runs ONE round (``round_count = 1``) of ``round_size = k*N``
    trajectories and is valid when it actually ran (close to) the full running
    fleet without over-admitting:

      * ``return_code == 0`` (the kernel subprocess succeeded),
      * ``throughput.total >= 0.9 * expected``. ``expected = N`` by default
        (mode-agnostic: a non-M trial runs ONE round, so ``total`` -- the
        trajectory-attempt count since run_summary schema 2 -- ~= k*N >= 0.9*N
        on a healthy run). When ``multiplier`` (M) is set
        (fixed+exec_ephemeral+M), each of the k*N sandboxes runs M trajectories,
        so ``expected = M * k * N`` and the floor reads ``throughput.succeeded``
        (the trajectory-completion count = ``sum(trajectory_completions)`` =
        M*k*N on a healthy run) -- NOT ``throughput.total`` (attempts shrink
        when a slot gives up on persistent create-failure, so only completions
        measure the work actually done). This catches a
        silent under-run where slots crashed / exited early before finishing
        their M (mirrors reference B's ``total == target_count`` check).
        Per-trajectory completion is inspectable in trajectory-detail.csv --
        ``valid`` is the gross-failure / over-admission gate, not a "every
        trajectory succeeded" bar,
      * ``admission.peak_active <= N`` (never ran more than N concurrent --
        dropped for exec_only, which has no admission controller),
      * ``failure_rate <= failure_tolerance`` (configurable wrap-noise allowance;
        1:1 modes only -- skipped when ``multiplier`` is set: the M succeeded-floor
        above is the sole gate there, and ``failure_tolerance`` is tuned for
        single-round wrap noise, not M-pass fleets).
    """
    if return_code != 0:
        return False
    tp = summary.get("throughput") or {}
    # M-aware floor: with multiplier each sandbox runs M trajectories, so the
    # trajectory-completion count is throughput.succeeded (= M*ratio*N on a
    # healthy run). throughput.total counts ATTEMPTS (schema 2), which under-
    # counts when slots give up early -- read succeeded so the floor measures
    # completed work against M*ratio*N. (Schema-1 summaries had total = slot
    # count and a dimensionally broken negative failed; the gates below read
    # the same fields, so rescoring an old file behaves as it did then.)
    if multiplier:
        actual = tp.get("succeeded", 0)
        expected = multiplier * (ratio or 1) * n
    else:
        actual = tp.get("total", 0)
        expected = n
    if actual < 0.9 * expected:
        return False
    adm = summary.get("admission")
    if adm and adm.get("peak_active") is not None and adm["peak_active"] > n:
        return False
    if not multiplier:
        # failure_rate is the 1:1 (single-round) gate; for M>1 the succeeded
        # floor above is the sole gate (step failures already reduce
        # completions).
        failed = tp.get("failed", 0)
        failure_rate = failed / actual if actual else 1.0
        if failure_rate > failure_tolerance:
            return False
    return True


# trial-summary.csv column order (mirrors the reference TRIAL_COLUMNS + the
# sustained-mode failure_rate / test_duration columns).
#
# Interrupted trials: a trial halted mid-run by Ctrl-C / driver SIGTERM (the
# kernel's SIGTERM-cooperative `finally` flushes a PARTIAL run_summary.json +
# trajectories/index.json) surfaces here as ``return_code == 130`` and
# ``valid == False`` -- its ``total`` / ``wall_sec`` reflect only what ran
# before the interrupt, which is itself the degradation signal (a ratio that
# stalls at 200/1152 trajectories is exactly the oversub breakdown). An
# interrupted trial is the END of the sweep (the driver halts); a timed-out
# trial (non-zero rc, != 130) is NOT -- the sweep continues to the next ratio.
# ``_interrupted`` is an internal row sentinel (not a column; DictWriter's
# extrasaction="ignore" drops it) that `main` reads to decide halt-vs-continue.
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
    "avg_cmd_exec_sec",
    "median_cmd_exec_sec",
    "cmd_exec_degradation_vs_1_1_pct",
    "time_degradation_vs_1_1_pct",
    "throughput_gain_vs_1_1_pct",
]

# Per-trajectory timing breakdown columns. Sourced from the kernel-emitted
# ``time_breakdown_sec`` sub-dict in trajectories/index.json; together they
# decompose ``elapsed_sec`` into exec / lifecycle-API (resume+pause) /
# inter-step think delay / create+kill cost / queueing waits, so a reader can
# attribute where each trajectory's wall time went without opening replay_result.json.
# ``stress_sec`` is the stress window (elapsed - create - kill, clamped at 0):
# ``elapsed_sec = create_sec + kill_sec + stress_sec`` holds per run row.
# index.json short key -> CSV column name (kept aligned with trajectory_export._index_row).
_TRAJECTORY_BREAKDOWN_COLUMNS = [
    "slice_total_sec",
    "exec_sec",
    "resume_sec",
    "pause_sec",
    "requested_delay_sec",
    "create_sec",
    "kill_sec",
    "stress_sec",
    "interaction_total_sec",
    "slot_contention_wait_sec",
    # Wait-decoupling split (the four independent components + per-phase inflight).
    "natural_delay_sec",
    "capacity_wait_sec",
    "rate_pacing_wait_sec",
    "inflight_wait_sec",
    "resume_rate_pacing_wait_sec",
    "pause_rate_pacing_wait_sec",
    "resume_inflight_wait_sec",
    "pause_inflight_wait_sec",
    "running_slot_held_sec",
]
_BREAKDOWN_KEY_TO_COL = {
    "slice_total": "slice_total_sec",
    "exec": "exec_sec",
    "resume": "resume_sec",
    "pause": "pause_sec",
    "requested_delay": "requested_delay_sec",
    "create": "create_sec",
    "kill": "kill_sec",
    "stress": "stress_sec",
    "interaction_total": "interaction_total_sec",
    "slot_contention_wait": "slot_contention_wait_sec",
    "natural_delay": "natural_delay_sec",
    "capacity_wait": "capacity_wait_sec",
    "rate_pacing_wait": "rate_pacing_wait_sec",
    "inflight_wait": "inflight_wait_sec",
    "resume_rate_pacing_wait": "resume_rate_pacing_wait_sec",
    "pause_rate_pacing_wait": "pause_rate_pacing_wait_sec",
    "resume_inflight_wait": "resume_inflight_wait_sec",
    "pause_inflight_wait": "pause_inflight_wait_sec",
    "running_slot_held": "running_slot_held_sec",
}

TRAJECTORY_COLUMNS = [
    "mode",
    "ratio",
    "repeat",
    "trajectory_id",
    "sandbox_index",
    "round_id",
    "n_steps",
    "n_failed",
    "n_timeout",
    "success_rate",
    "elapsed_sec",
    *_TRAJECTORY_BREAKDOWN_COLUMNS,
    "create_error_type",
    "kill_error_type",
    # action_series (the trajectory's newline-joined command sequence) lives in
    # replay_result.json / index.json per trajectory and in benchmark-report.json;
    # omitted from the CSV so a multi-line cell doesn't bloat every Excel row.
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


def _cmd_exec_stats(traj_rows: list[dict]) -> dict[tuple[str, int], tuple[float, float]]:
    """Per ``(mode, ratio)``: ``(avg_cmd_exec_sec, median_cmd_exec_sec)``.

    Per-cmd exec time = a run's ``exec_sec / n_steps``, over runs that executed
    at least one step and had **no failed steps** (a failed run carries
    truncated / zero exec that would drag the mean down -- the same exclusion
    rule as the kernel's per-action latency stats). ``avg`` is the true
    per-cmd mean ``sum(exec) / sum(steps)``; ``median`` is the median of
    per-run means (robust to run-length outliers). Keys absent when a ratio
    has no qualifying run (callers default to 0.0, matching _median).
    """
    by_key: dict[tuple[str, int], list[tuple[float, int]]] = {}
    for r in traj_rows:
        n = r.get("n_steps") or 0
        ex = r.get("exec_sec")
        if not n or ex is None or (r.get("n_failed") or 0) > 0:
            continue
        by_key.setdefault((r["mode"], r["ratio"]), []).append((float(ex), int(n)))
    out: dict[tuple[str, int], tuple[float, float]] = {}
    for key, pairs in by_key.items():
        total_exec = sum(e for e, _ in pairs)
        total_steps = sum(n for _, n in pairs)
        per_run = [e / n for e, n in pairs]
        out[key] = (round(total_exec / total_steps, 6), round(statistics.median(per_run), 6))
    return out


def aggregate_ratio_summary(trials: list[dict], traj_rows: list[dict] | None = None) -> list[dict]:
    """One row per ``(mode, ratio)``: medians across repeats + degradation vs k=1.

    Degradation is computed **within a mode** vs that mode's ``k=1`` baseline,
    so lifecycle and exec_only each get their own curve (memory-overcommit
    overhead vs CPU-oversubscription degradation). When a mode has no k=1
    trial, the degradation columns default to ``0.0`` (no baseline to compare).

    ``traj_rows`` (the trajectory-detail rows) enables the per-cmd exec
    columns; without them those columns stay ``0.0`` (older callers).
    """
    cmd_stats = _cmd_exec_stats(traj_rows or [])
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
        avg_cmd, med_cmd = cmd_stats.get((mode, ratio), (0.0, 0.0))
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
                "avg_cmd_exec_sec": avg_cmd,
                "median_cmd_exec_sec": med_cmd,
                "cmd_exec_degradation_vs_1_1_pct": 0.0,
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
            bc = base["avg_cmd_exec_sec"] or 0
            r["cmd_exec_degradation_vs_1_1_pct"] = round(((r["avg_cmd_exec_sec"] - bc) / bc * 100) if bc else 0.0, 3)
    return rows


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _trajectory_rows(trials: list[dict]) -> list[dict]:
    """One row per (trajectory, sandbox, round) per trial.

    Expands each ``trajectories/index.json`` row's ``runs[]`` array into one
    CSV row per run so a reader gets per-run latency at any pool/N ratio (when
    pool < N, the round-robin wraps and each trajectory is run by ~N/pool
    sandboxes -- those are distinct runs, not a sum). The per-run
    ``time_breakdown_sec`` (that run's own sums) is surfaced as flat columns so
    a reader can attribute each run's ``elapsed_sec``; a ``groupby
    trajectory_id`` over the per-run rows yields the per-trajectory median.

    Backward compat: an older index.json without ``runs[]`` yields one row per
    trajectory from the top-level fields (a 1:1 pool>=N run), not a crash.
    """
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
            runs = tr.get("runs")
            sources = runs if runs else [tr]  # new shape: runs[]; old shape: [tr]
            for src in sources:
                row = {
                    "mode": t["mode"],
                    "ratio": t["ratio"],
                    "repeat": t["repeat"],
                    "trajectory_id": tr.get("trajectory_id"),
                    "sandbox_index": src.get("sandbox_index"),
                    "round_id": src.get("round_id"),
                    "n_steps": src.get("n_steps"),
                    "n_failed": src.get("n_failed"),
                    "n_timeout": src.get("n_timeout"),
                    "success_rate": src.get("success_rate"),
                    "elapsed_sec": src.get("elapsed_sec"),
                    "create_error_type": src.get("create_error_type"),
                    "kill_error_type": src.get("kill_error_type"),
                    # Trajectory-level (same for every run of this trajectory);
                    # blank for an older index.json without it (backward compat).
                    "action_series": tr.get("action_series"),
                }
                bd = src.get("time_breakdown_sec") or {}
                for short_key, col in _BREAKDOWN_KEY_TO_COL.items():
                    row[col] = bd.get(short_key)
                out.append(row)
    return out


def write_outputs(trials: list[dict], *, output_root: Path, configuration: dict | None = None) -> None:
    """Write trial/ratio/trajectory CSVs + benchmark-report.json (restart-safe).

    Call after every trial so a killed driver leaves a complete partial set.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    _write_csv(output_root / "trial-summary.csv", TRIAL_COLUMNS, trials)
    traj_rows = _trajectory_rows(trials)
    ratio_rows = aggregate_ratio_summary(trials, traj_rows)
    _write_csv(output_root / "ratio-summary.csv", RATIO_COLUMNS, ratio_rows)
    _write_csv(output_root / "trajectory-detail.csv", TRAJECTORY_COLUMNS, traj_rows)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "configuration": configuration or {},
        "trials": trials,
        "ratio_summary": ratio_rows,
        "trajectory_details": traj_rows,
    }
    (output_root / "benchmark-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _auto_compare(output_root: Path) -> None:
    """Post-sweep: run compare_sweeps on this single sweep dir.

    Produces ``<output_root>/comparison/`` (per-stat p50/p90/p99/avg degradation
    vs k=1 + the same-trajectory cross-ratio table + workbook) without a manual
    ``oversub-compare`` invocation -- a single-series manifest where the
    cross-series deltas degenerate to zero and the within-series cross-ratio
    comparison is the point. NEVER fails the sweep: any error degrades to a
    warning (the raw CSVs remain the source of truth; ``oversub-compare`` can
    always be re-run by hand).
    """
    from bench_core.compare_sweeps import run_comparison

    manifest = {
        "baseline_series": "sweep",
        "series": [{"label": "sweep", "arch": "", "dir": str(output_root), "caps": {}}],
    }
    try:
        rc = run_comparison(manifest, output_root / "comparison")
    except Exception as exc:
        logger.warning("auto-compare skipped (%s); sweep CSVs are unaffected", exc)
        return
    if rc == 0:
        logger.info("auto-compare written to %s", output_root / "comparison")
    else:
        logger.warning("auto-compare returned %s; sweep CSVs are unaffected", rc)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oversub-bench",
        description="Sweep the bench-core replay kernel across oversubscription ratios.",
    )
    p.add_argument(
        "--sweep-config", default=None, help="sweep-config YAML (all knobs + base_config); CLI flags override it"
    )
    p.add_argument("--config", default=None, help="base replay.yaml (required unless sweep-config sets base_config)")
    p.add_argument("--provider", default=None, help="provider name (default aenv)")
    p.add_argument(
        "--running-concurrency",
        type=int,
        default=None,
        help="N running slots (default = base replay.running_concurrency)",
    )
    p.add_argument("--ratios", default=None, help="comma-separated k values (default 1,2,3)")
    p.add_argument("--modes", default=None, help="comma-separated replay modes (default lifecycle,exec_ephemeral)")
    p.add_argument("--repeats", type=int, default=None, help="repeats per (mode,ratio) (default 1)")
    p.add_argument(
        "--test-duration", type=int, default=None, help="sustained window per trial (default = base test.duration)"
    )
    p.add_argument(
        "--multiplier",
        type=int,
        default=None,
        help="per-sandbox trajectory count M for exec_ephemeral: each sandbox runs M "
        "trajectories then stops (stop-when-done); switches the trial to fixed. "
        "Default off (the trial uses the base benchmark_mode / total_count).",
    )
    p.add_argument("--failure-tolerance", type=float, default=None, help="max failure_rate for valid (default 0.0)")
    p.add_argument(
        "--shards",
        default=None,
        help="kernel processes per trial for admission-free modes (exec_ephemeral/exec_only): "
        "'auto' = one shard per ratio (each ~N slots, its own GIL) or an explicit P >= 1 "
        "(P=1 = legacy single-process). lifecycle/trajectory always run single-process "
        "(global admission). Default auto.",
    )
    p.add_argument("--cooldown-sec", type=int, default=None, help="settle time between trials (default 30)")
    p.add_argument(
        "--cleanup-between-trials",
        choices=["on", "off"],
        default=None,
        help="pre-trial teardown of leftovers (default on)",
    )
    p.add_argument(
        "--trial-timeout-sec",
        type=int,
        default=None,
        help=f"outer wall-clock per trial; terminate->{_TERMINATE_GRACE_SEC}s->kill "
        f"(0 = off; default {DEFAULT_TRIAL_TIMEOUT_SEC}s = {DEFAULT_TRIAL_TIMEOUT_SEC // 3600}h)",
    )
    p.add_argument("--output-root", default=None, help="default results/oversub/oversub-N{N}-{ts}/")
    p.add_argument(
        "--reuse", action=argparse.BooleanOptionalAction, default=None, help="skip completed-valid trials (default off)"
    )
    p.add_argument(
        "--stop-on-failure",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="halt on first invalid trial (default off)",
    )
    p.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="print trial.yaml + commands, write empty outputs (default off)",
    )
    p.add_argument(
        "--no-vm-monitor",
        action="store_true",
        default=None,
        help="pass --no-vm-monitor through to bench-core (default off)",
    )
    p.add_argument(
        "--bench-core-bin", nargs="+", default=None, help="command for the kernel subprocess (default bench-core)"
    )
    p.add_argument(
        "--auto-compare",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="run oversub-compare on the finished sweep (single-series cross-ratio "
        "comparison into <output_root>/comparison/) (default on)",
    )
    return p


# Grace given to bench-core after SIGTERM (terminate) before the hard kill.
# Must exceed the kernel's partial-flush wall time (series.close +
# export_trajectories + write_run_summary -- seconds for a 1152-trajectory
# fleet; larger fleets or vm_monitor-on may need more). Future: expose as a CLI
# flag so a heavy aenv+vm_monitor trial can bump it without code change.
_TERMINATE_GRACE_SEC = 30
# Reaper wait after SIGKILL (the process is already dead; this just collects).
_POSTKILL_GRACE_SEC = 5


def _run_subprocess(cmd: list[str], log_path: Path, timeout_sec: int) -> int:
    """Run cmd, stream stdout+stderr to log_path; portable hard-kill on timeout.

    ``terminate`` -> ``_TERMINATE_GRACE_SEC`` grace -> ``kill``. The grace must
    exceed the kernel's partial-flush wall time (``series.close`` +
    ``export_trajectories`` + ``write_run_summary`` -- seconds for a
    1152-trajectory fleet; larger fleets or vm_monitor-on may need more) so a
    SIGTERM-triggered flush completes before the hard ``kill``. The bench
    targets Linux hosts, but the driver may run on a Windows dev box.
    """
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=timeout_sec if timeout_sec else None)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_POSTKILL_GRACE_SEC)
        except KeyboardInterrupt:
            # Tear down the trial subprocess so Ctrl-C does not orphan a run
            # managing hundreds of sandboxes + a vm_monitor host collector. The
            # generous grace lets the kernel's SIGTERM-cooperative handler flush
            # partial artifacts (run_summary.json / trajectories/index.json)
            # before the hard kill -- _run_trial then captures them as a row.
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_POSTKILL_GRACE_SEC)
            raise
        return proc.returncode if proc.returncode is not None else -1


def _run_cleanup(args: argparse.Namespace, *, label: str, halt_on_interrupt: bool = True) -> None:
    """Tear down leftover sandboxes via ``bench-core --cleanup``.

    Idempotent (lists + kills whatever is still alive). Two callers:
      * pre-trial, so a prior run's leftovers don't corrupt the ratio;
      * on a Ctrl+C sweep halt, so an interrupted trial (kernel SIGTERM'd
        mid-stress, which leaves sandboxes for THIS reaper -- the kernel's
        SIGTERM path deliberately skips cleanup so the driver owns teardown)
        does not orphan them when the sweep halts with no next trial to run
        the pre-trial cleanup.

    Respects ``--cleanup-between-trials`` and ``--dry-run`` (the single auto-
    cleanup knob; a user who opted out manages sandboxes themselves). The
    dedicated cleanup timeout caps at 300s so a hung ``bench-core --cleanup``
    cannot block indefinitely; shrinks to ``trial_timeout_sec`` if smaller.

    ``halt_on_interrupt``: a SECOND Ctrl+C during cleanup. Pre-trial we
    propagate it (it halts the sweep, matching the prior behavior where a
    Ctrl+C during pre-trial cleanup unwound straight to main's handler). On
    the post-interrupt exit path we are already inside an interrupt unwind,
    so we swallow it (log + return) instead of replacing the propagating
    interrupt and traceback-ing the driver. Never raises otherwise: a cleanup
    failure or timeout is logged + downgraded to a warning.
    """
    if args.cleanup_between_trials != "on" or args.dry_run:
        return
    cleanup_timeout = min(args.trial_timeout_sec or 300, 300)
    cmd = [*args.bench_core_bin, "--config", args.config, "--provider", args.provider, "--cleanup"]
    logger.info("%s: tearing down leftovers (timeout %ss)...", label, cleanup_timeout)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=cleanup_timeout)
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "%s timed out after %ss; leftover sandboxes may remain\n%s",
            label,
            cleanup_timeout,
            _cleanup_output(exc.stdout, exc.stderr),
        )
        return
    except KeyboardInterrupt:
        logger.warning("%s interrupted again; leftover sandboxes may remain", label)
        if halt_on_interrupt:
            raise
        return
    if result.returncode != 0:
        logger.warning(
            "%s returned %s; leftover sandboxes may remain\n%s",
            label,
            result.returncode,
            _cleanup_output(result.stdout, result.stderr),
        )


def _cleanup_output(stdout, stderr, *, limit: int = 2000) -> str:
    """Decode + truncate captured ``bench-core --cleanup`` output for diagnostics.

    ``capture_output=True`` yields bytes (or None); decode defensively and tail
    to ``limit`` chars so a chatty or hung cleanup cannot flood the driver log.
    """
    parts = []
    for name, blob in (("stdout", stdout), ("stderr", stderr)):
        if not blob:
            continue
        text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
        if len(text) > limit:
            text = "..." + text[-limit:]
        parts.append(f"[{name}]\n{text}")
    return "\n".join(parts)


def _find_reusable_summary(trial_dir: Path, prefix: str) -> Path | None:
    """Locate a finished trial's summary for ``--reuse``.

    Sharded trials carry the driver-merged summary at a fixed path; legacy
    single-process trials keep the kernel's timestamped run dir. Merged first
    (it is the trial-level contract); either shape feeds the same
    ``compute_valid`` because the merge is schema-faithful.
    """
    merged = trial_dir / "merged" / f"{prefix}_run_summary.json"
    if merged.exists():
        return merged
    hits = sorted(trial_dir.glob(f"{prefix}_*/{prefix}_run_summary.json"))
    return hits[-1] if hits else None


def _run_trial(
    args: argparse.Namespace,
    base_yaml: dict,
    mode: str,
    ratio: int,
    repeat: int,
    n: int,
    output_root: Path,
    *,
    index: int,
    total: int,
) -> dict | None:
    """Run one (mode,ratio,repeat) trial; return a trial_row dict or None (dry-run)."""
    prefix = (base_yaml.get("report") or {}).get("filename_prefix", "replay_bench")
    trial_dir = output_root / mode / f"ratio-{ratio:02d}" / f"repeat-{repeat:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    test_duration = args.test_duration or (base_yaml.get("test") or {}).get("duration", 600)
    # multiplier (M) applies only to exec_ephemeral (each sandbox runs M
    # trajectories, stop-when-done); a no-op for lifecycle/trajectory.
    m = args.multiplier if mode == "exec_ephemeral" else None
    # P >= 2 only for admission-free modes (see oversub_shards); None = legacy
    # single-process path, byte-identical layout and behavior.
    plan = plan_shards(trial_dir=trial_dir, mode=mode, ratio=ratio, n=n, setting=args.shards)

    summary_glob = f"{prefix}_*/{prefix}_run_summary.json"

    logger.info(
        "[%s/%s] %s ratio=%d repeat=%d: target=%d, duration=%ss, shards=%d; log=%s",
        index,
        total,
        mode,
        ratio,
        repeat,
        ratio * n,
        test_duration,
        plan.count if plan else 1,
        trial_dir / "driver.log" if plan is None else str(trial_dir / "shard-*") + "/driver.log",
    )

    # --reuse: skip completed-valid trials (restart safety).
    if args.reuse:
        prior = _find_reusable_summary(trial_dir, prefix)
        if prior:
            try:
                s = parse_run_summary(prior)
                if compute_valid(s, 0, n=n, failure_tolerance=args.failure_tolerance, multiplier=m, ratio=ratio):
                    logger.info(
                        "[%s/%s] %s ratio=%d repeat=%d: reused completed-valid trial -- skipped",
                        index,
                        total,
                        mode,
                        ratio,
                        repeat,
                    )
                    return trial_row(
                        mode=mode,
                        ratio=ratio,
                        repeat=repeat,
                        running_concurrency=n,
                        target_count=ratio * n,
                        summary=s,
                        return_code=0,
                        valid=True,
                        reused=True,
                        trial_dir=str(trial_dir),
                        run_summary_path=str(prior),
                    )
            except Exception:
                pass  # corrupt summary -> re-run

    # Cooldown between trials (skip the very first trial; skip in dry-run).
    if args.cooldown_sec and not args.dry_run and index != 1:
        logger.info("cooldown %ds...", args.cooldown_sec)
        time.sleep(args.cooldown_sec)

    # Pre-trial cleanup: tear down leftovers so they don't corrupt the ratio.
    # A Ctrl+C during this window propagates (halt_on_interrupt=True) and unwinds
    # to main's interrupt handler, which then runs its own post-interrupt cleanup.
    _run_cleanup(args, label="pre-trial cleanup", halt_on_interrupt=True)

    cfg = build_trial_config(
        base_yaml,
        mode=mode,
        ratio=ratio,
        n=n,
        test_duration=test_duration,
        trial_dir=str(trial_dir),
        prefix=prefix,
        multiplier=m,
    )
    trial_yaml = trial_dir / "trial.yaml"
    trial_yaml.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    # Sharded path: trial.yaml stays as the logical (unsharded) provenance; the
    # per-shard configs are derived from it under trial_dir/shard-*/.
    if plan is not None:
        return _run_sharded_trial(
            args,
            cfg,
            plan,
            mode=mode,
            ratio=ratio,
            repeat=repeat,
            n=n,
            trial_dir=trial_dir,
            prefix=prefix,
            multiplier=m,
            test_duration=test_duration,
            index=index,
            total=total,
        )

    cmd = [*args.bench_core_bin, "--config", str(trial_yaml), "--provider", args.provider]
    if args.no_vm_monitor:
        cmd.append("--no-vm-monitor")

    if args.dry_run:
        logger.info("[dry-run] %s", " ".join(str(c) for c in cmd))
        return None  # aggregated below; no trial row.

    interrupted = False
    try:
        rc = _run_subprocess(cmd, trial_dir / "driver.log", args.trial_timeout_sec)
    except KeyboardInterrupt:
        # The kernel's SIGTERM-cooperative handler flushed partial artifacts
        # (run_summary.json / trajectories/index.json) during the generous
        # terminate-grace; fall through to capture them as an invalid row
        # instead of orphaning the trial from the CSVs.
        rc = 130
        interrupted = True

    hits = sorted(trial_dir.glob(summary_glob))
    if not hits:
        row = trial_row(
            mode=mode,
            ratio=ratio,
            repeat=repeat,
            running_concurrency=n,
            target_count=ratio * n,
            summary={"throughput": {}, "test_duration": test_duration, "error": "no run_summary.json"},
            return_code=rc,
            valid=False,
            reused=False,
            trial_dir=str(trial_dir),
            run_summary_path="",
        )
    else:
        summary = parse_run_summary(hits[-1])
        valid = compute_valid(summary, rc, n=n, failure_tolerance=args.failure_tolerance, multiplier=m, ratio=ratio)
        row = trial_row(
            mode=mode,
            ratio=ratio,
            repeat=repeat,
            running_concurrency=n,
            target_count=ratio * n,
            summary=summary,
            return_code=rc,
            valid=valid,
            reused=False,
            trial_dir=str(trial_dir),
            run_summary_path=str(hits[-1]),
        )
    wall = row.get("wall_sec")
    logger.info(
        "[%s/%s] %s ratio=%d repeat=%d done in %s: valid=%s, total=%s, succeeded=%s, failed=%s, peak_active=%s",
        index,
        total,
        mode,
        ratio,
        repeat,
        format_duration(wall) if wall else "n/a",
        row["valid"],
        row["total"],
        row["succeeded"],
        row["failed"],
        row.get("peak_active"),
    )
    # Sentinel for main(): a user-initiated interrupt (Ctrl-C/SIGTERM) halted
    # this trial mid-run -- distinguish it from a trial-timeout (non-zero rc,
    # but the sweep continues). Not a CSV column (extrasaction="ignore" drops
    # it from trial-summary.csv); see _run_subprocess / main for the contract.
    if interrupted:
        row["_interrupted"] = True
    return row


def _run_sharded_trial(
    args: argparse.Namespace,
    trial_cfg: dict,
    plan: ShardPlan,
    *,
    mode: str,
    ratio: int,
    repeat: int,
    n: int,
    trial_dir: Path,
    prefix: str,
    multiplier: int | None,
    test_duration: int,
    index: int,
    total: int,
) -> dict | None:
    """Run one trial as P kernel subprocesses; merge back to a single trial row.

    Thin composition: planning / per-shard config derivation / group supervision
    / artifact merging all live in :mod:`bench_core.oversub_shards`; validity
    and row shaping reuse the same driver functions as the single-process path,
    so a sharded trial is indistinguishable downstream (CSVs, compare, reuse).
    A shard that produced no summary is never backfilled -- it surfaces in the
    merged ``error`` and fails the validity floor (a crash must not masquerade
    as a successful measurement).
    """
    write_shard_configs(trial_cfg, plan)
    commands = [
        build_shard_command(args.bench_core_bin, spec, provider=args.provider, no_vm_monitor=args.no_vm_monitor)
        for spec in plan.specs
    ]
    if args.dry_run:
        for cmd in commands:
            logger.info("[dry-run] %s", " ".join(str(c) for c in cmd))
        return None

    result = ShardGroupRunner(commands, [spec.log_path for spec in plan.specs], args.trial_timeout_sec).run()
    artifacts = collect_shard_artifacts(plan, prefix)
    rc = result.trial_return_code()

    if all(s is None for s in artifacts.summaries):
        summary = {
            "throughput": {},
            "test_duration": test_duration,
            "error": f"no shard produced run_summary.json (rcs={result.returncodes})",
        }
        run_summary_path = ""
    else:
        merged_index = merge_trajectory_indexes(artifacts.catalogs) if artifacts.catalogs else None
        summary = merge_run_summaries(artifacts.summaries, plan=plan, artifacts=artifacts, result=result)
        run_summary_path = str(write_merged_artifacts(trial_dir, prefix, summary, merged_index))

    valid = compute_valid(
        summary, rc, n=n, failure_tolerance=args.failure_tolerance, multiplier=multiplier, ratio=ratio
    )
    row = trial_row(
        mode=mode,
        ratio=ratio,
        repeat=repeat,
        running_concurrency=n,
        target_count=ratio * n,
        summary=summary,
        return_code=rc,
        valid=valid,
        reused=False,
        trial_dir=str(trial_dir),
        run_summary_path=run_summary_path,
    )
    logger.info(
        "[%s/%s] %s ratio=%d repeat=%d done in %s (shards=%d, rcs=%s): valid=%s, total=%s, succeeded=%s, failed=%s",
        index,
        total,
        mode,
        ratio,
        repeat,
        format_duration(row.get("wall_sec")) if row.get("wall_sec") else "n/a",
        plan.count,
        result.returncodes,
        row["valid"],
        row["total"],
        row["succeeded"],
        row["failed"],
    )
    if result.interrupted:
        # Same sentinel as the single-process path: unwinds to main's interrupt
        # handler (partial outputs preserved + post-interrupt cleanup + rc 130).
        row["_interrupted"] = True
    return row


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_arg_parser().parse_args(argv)
    sweep = load_sweep_config(args.sweep_config) if args.sweep_config else {}

    def resolve(key: str):
        # CLI flag > sweep-config > built-in default. Sentinel ``None`` default on
        # every arg means "not passed", so an explicit --ratios 4 beats the YAML.
        cli = getattr(args, key, None)
        if cli is not None:
            return cli
        return sweep.get(key, _TRUE_DEFAULTS[key])

    # Base replay.yaml: --config wins, else sweep base_config, else fail.
    base_config_path = args.config or sweep.get("base_config")
    if not base_config_path:
        logger.error("must provide --config or a sweep-config with base_config")
        return 2
    with open(base_config_path, encoding="utf-8") as f:
        base_yaml = yaml.safe_load(f)

    n = resolve("running_concurrency")
    n = int(n) if n is not None else None
    n = n or default_running_concurrency(base_yaml)
    ratios = _coerce_ratios(resolve("ratios"))
    modes = _coerce_modes(resolve("modes"))
    # Breaking-change surface: the oversub default sweep modes changed from
    # exec_only to exec_ephemeral (per-round sandbox recreate). Warn loudly when
    # that default is *auto-applied* (no --modes, no sweep modes:) so operators
    # who relied on the long-lived exec_only baseline notice and can pin it back.
    _modes_cli = getattr(args, "modes", None)
    if _modes_cli is None and "modes" not in sweep and "exec_ephemeral" in modes:
        logger.warning(
            "oversub default modes now include exec_ephemeral (per-round sandbox "
            "recreate); behavior changed vs the old exec_only default. Pin "
            "--modes exec_only (or set modes: in the sweep config) to keep the "
            "long-lived baseline."
        )
    repeats = int(resolve("repeats"))
    td = resolve("test_duration")
    test_duration = int(td) if td is not None else None
    test_duration = test_duration or (base_yaml.get("test") or {}).get("duration", 600)

    # Write resolved values back so _run_trial / _run_subprocess read them unchanged.
    args.config = base_config_path
    args.provider = resolve("provider")
    args.ratios = ratios
    args.modes = modes
    args.repeats = repeats
    args.test_duration = test_duration
    args.multiplier = resolve("multiplier")
    args.shards = coerce_shards(resolve("shards"))
    args.failure_tolerance = float(resolve("failure_tolerance"))
    args.cooldown_sec = int(resolve("cooldown_sec"))
    args.cleanup_between_trials = resolve("cleanup_between_trials")
    args.trial_timeout_sec = int(resolve("trial_timeout_sec"))
    args.reuse = resolve("reuse")
    args.stop_on_failure = resolve("stop_on_failure")
    args.dry_run = resolve("dry_run")
    args.no_vm_monitor = resolve("no_vm_monitor")
    args.bench_core_bin = _coerce_cmd(resolve("bench_core_bin"))
    args.auto_compare = resolve("auto_compare")

    output_root_arg = resolve("output_root")
    if output_root_arg:
        output_root = Path(output_root_arg)
    else:
        output_root = Path(f"results/oversub/oversub-N{n}-{time.strftime('%Y%m%d-%H%M%S')}")
    output_root.mkdir(parents=True, exist_ok=True)

    configuration = {
        "base_config": str(base_config_path),
        "sweep_config": str(args.sweep_config) if args.sweep_config else None,
        "provider": args.provider,
        "running_concurrency": n,
        "ratios": ratios,
        "modes": modes,
        "repeats": repeats,
        "test_duration": test_duration,
        "multiplier": args.multiplier,
        "shards": args.shards,
        "failure_tolerance": args.failure_tolerance,
        "cooldown_sec": args.cooldown_sec,
        "trial_timeout_sec": args.trial_timeout_sec,
    }

    # Natural trial order: for mode, for ratio, for repeat.
    matrix = [(mode, ratio, repeat) for mode in modes for ratio in ratios for repeat in range(repeats)]

    trials: list[dict] = []
    total_trials = len(matrix)
    logger.info(
        "oversub sweep: provider=%s N=%d ratios=%s modes=%s repeats=%d -> %d trial(s); output=%s",
        args.provider,
        n,
        ratios,
        modes,
        repeats,
        total_trials,
        output_root,
    )
    logger.info(
        "shards=%s: admission-free modes (exec_ephemeral/exec_only) run one kernel "
        "process per shard ('auto' = P=ratio, ~N slots each); lifecycle/trajectory "
        "always single-process (global admission)",
        args.shards,
    )
    try:
        for index, (mode, ratio, repeat) in enumerate(matrix, 1):
            row = _run_trial(args, base_yaml, mode, ratio, repeat, n, output_root, index=index, total=total_trials)
            if row is not None:
                trials.append(row)
            write_outputs(trials, output_root=output_root, configuration=configuration)
            if args.stop_on_failure and row is not None and not row["valid"]:
                logger.warning("[stop-on-failure] invalid trial %s/ratio-%s/repeat-%s; halting", mode, ratio, repeat)
                break
            if row is not None and row.get("_interrupted"):
                logger.warning("[interrupted] partial results preserved; halting sweep")
                # The interrupted trial's kernel was SIGTERM'd mid-stress, so it
                # skipped cleanup (the driver owns teardown); with the sweep
                # halting there is no next trial to run the pre-trial cleanup,
                # so tear down the leftovers here.
                _run_cleanup(args, label="post-interrupt cleanup", halt_on_interrupt=False)
                return 130
    except KeyboardInterrupt:
        write_outputs(trials, output_root=output_root, configuration=configuration)
        logger.warning("[interrupted] partial results were preserved")
        # Same leak path: a Ctrl+C between trials leaves the just-SIGTERM'd
        # trial's sandboxes with no next pre-trial cleanup to reap them.
        _run_cleanup(args, label="post-interrupt cleanup", halt_on_interrupt=False)
        return 130

    write_outputs(trials, output_root=output_root, configuration=configuration)
    valid_count = sum(1 for t in trials if t.get("valid"))
    logger.info(
        "oversub sweep done: %d/%d trial(s) valid; report=%s",
        valid_count,
        len(trials),
        output_root / "benchmark-report.json",
    )
    if args.auto_compare and trials and not args.dry_run:
        _auto_compare(output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
