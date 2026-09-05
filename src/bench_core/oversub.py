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

from bench_core.utils import setup_logging

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


# True defaults for the sweep knobs (used when neither a CLI flag nor a
# sweep-config value is given). Mirrors build_arg_parser; main() resolves
# CLI flag > sweep-config > these.
_TRUE_DEFAULTS: dict[str, object] = {
    "provider": "aenv",
    "running_concurrency": None,
    "ratios": "1,2,3",
    "modes": "lifecycle,exec_only",
    "repeats": 1,
    "test_duration": None,
    "failure_tolerance": 0.0,
    "cooldown_sec": 30,
    "cleanup_between_trials": "on",
    "trial_timeout_sec": 0,
    "output_root": None,
    "reuse": False,
    "stop_on_failure": False,
    "dry_run": False,
    "no_vm_monitor": False,
    "bench_core_bin": ["bench-core"],
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


def write_outputs(trials: list[dict], *, output_root: Path, configuration: dict | None = None) -> None:
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
        "configuration": configuration or {},
        "trials": trials,
        "ratio_summary": ratio_rows,
        "trajectory_details": traj_rows,
    }
    (output_root / "benchmark-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


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
    p.add_argument("--modes", default=None, help="comma-separated replay modes (default lifecycle,exec_only)")
    p.add_argument("--repeats", type=int, default=None, help="repeats per (mode,ratio) (default 1)")
    p.add_argument(
        "--test-duration", type=int, default=None, help="sustained window per trial (default = base test.duration)"
    )
    p.add_argument("--failure-tolerance", type=float, default=None, help="max failure_rate for valid (default 0.0)")
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
        help="outer wall-clock per trial; terminate->10s->kill (0 = off, default 0)",
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
    return p


def _run_subprocess(cmd: list[str], log_path: Path, timeout_sec: int) -> int:
    """Run cmd, stream stdout+stderr to log_path; portable hard-kill on timeout.

    ``terminate`` -> 10s grace -> ``kill`` (works on both Unix and Windows;
    the bench targets Linux hosts, but the driver may run on a Windows dev box).
    """
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=timeout_sec if timeout_sec else None)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except KeyboardInterrupt:
            # Tear down the trial subprocess so Ctrl-C does not orphan a run
            # managing hundreds of sandboxes + a vm_monitor host collector.
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            raise
        return proc.returncode if proc.returncode is not None else -1


def _run_trial(
    args: argparse.Namespace,
    base_yaml: dict,
    mode: str,
    ratio: int,
    repeat: int,
    n: int,
    output_root: Path,
    *,
    is_first: bool = False,
) -> dict | None:
    """Run one (mode,ratio,repeat) trial; return a trial_row dict or None (dry-run)."""
    prefix = (base_yaml.get("report") or {}).get("filename_prefix", "replay_bench")
    trial_dir = output_root / mode / f"ratio-{ratio:02d}" / f"repeat-{repeat:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    test_duration = args.test_duration or (base_yaml.get("test") or {}).get("duration", 600)

    summary_glob = f"{prefix}_*/{prefix}_run_summary.json"

    # --reuse: skip completed-valid trials (restart safety).
    if args.reuse:
        hits = sorted(trial_dir.glob(summary_glob))
        if hits:
            try:
                s = parse_run_summary(hits[-1])
                if compute_valid(s, 0, n=n, test_duration=test_duration, failure_tolerance=args.failure_tolerance):
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
                        run_summary_path=str(hits[-1]),
                    )
            except Exception:
                pass  # corrupt summary -> re-run

    # Cooldown between trials (skip the very first trial; skip in dry-run).
    if args.cooldown_sec and not args.dry_run and not is_first:
        time.sleep(args.cooldown_sec)

    # Pre-trial cleanup: tear down leftovers so they don't corrupt the ratio.
    if args.cleanup_between_trials == "on" and not args.dry_run:
        # Dedicated cleanup timeout: cap at 300s so a hung `bench-core --cleanup`
        # cannot block the sweep indefinitely; shrink to trial_timeout if smaller.
        cleanup_timeout = min(args.trial_timeout_sec or 300, 300)
        result = subprocess.run(
            [*args.bench_core_bin, "--config", args.config, "--provider", args.provider, "--cleanup"],
            capture_output=True,
            timeout=cleanup_timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "pre-trial cleanup returned %s; leftover sandboxes may corrupt the ratio",
                result.returncode,
            )

    cfg = build_trial_config(
        base_yaml,
        mode=mode,
        ratio=ratio,
        n=n,
        test_duration=test_duration,
        trial_dir=str(trial_dir),
        prefix=prefix,
    )
    trial_yaml = trial_dir / "trial.yaml"
    trial_yaml.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    cmd = [*args.bench_core_bin, "--config", str(trial_yaml), "--provider", args.provider]
    if args.no_vm_monitor:
        cmd.append("--no-vm-monitor")

    if args.dry_run:
        logger.info("[dry-run] %s", " ".join(str(c) for c in cmd))
        return None  # aggregated below; no trial row.

    rc = _run_subprocess(cmd, trial_dir / "driver.log", args.trial_timeout_sec)

    hits = sorted(trial_dir.glob(summary_glob))
    if not hits:
        return trial_row(
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
    summary = parse_run_summary(hits[-1])
    valid = compute_valid(summary, rc, n=n, test_duration=test_duration, failure_tolerance=args.failure_tolerance)
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
    args.failure_tolerance = float(resolve("failure_tolerance"))
    args.cooldown_sec = int(resolve("cooldown_sec"))
    args.cleanup_between_trials = resolve("cleanup_between_trials")
    args.trial_timeout_sec = int(resolve("trial_timeout_sec"))
    args.reuse = resolve("reuse")
    args.stop_on_failure = resolve("stop_on_failure")
    args.dry_run = resolve("dry_run")
    args.no_vm_monitor = resolve("no_vm_monitor")
    args.bench_core_bin = _coerce_cmd(resolve("bench_core_bin"))

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
        "failure_tolerance": args.failure_tolerance,
        "cooldown_sec": args.cooldown_sec,
        "trial_timeout_sec": args.trial_timeout_sec,
    }

    # Natural trial order: for mode, for ratio, for repeat.
    matrix = [(mode, ratio, repeat) for mode in modes for ratio in ratios for repeat in range(repeats)]

    trials: list[dict] = []
    is_first = True
    try:
        for mode, ratio, repeat in matrix:
            row = _run_trial(args, base_yaml, mode, ratio, repeat, n, output_root, is_first=is_first)
            is_first = False
            if row is not None:
                trials.append(row)
            write_outputs(trials, output_root=output_root, configuration=configuration)
            if args.stop_on_failure and row is not None and not row["valid"]:
                logger.warning("[stop-on-failure] invalid trial %s/ratio-%s/repeat-%s; halting", mode, ratio, repeat)
                break
    except KeyboardInterrupt:
        write_outputs(trials, output_root=output_root, configuration=configuration)
        logger.warning("[interrupted] partial results were preserved")
        return 130

    write_outputs(trials, output_root=output_root, configuration=configuration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
