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
import json
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
