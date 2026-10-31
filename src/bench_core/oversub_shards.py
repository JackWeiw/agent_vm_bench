"""Process-sharded trial execution for the oversub driver (mechanism layer).

One CPython process saturates a single GIL: a thread-per-sandbox kernel pinned
at ~1 core caps step dispatch (~50-55 steps/s measured at N=384) regardless of
host vCPU count, so high-ratio trials become client-bound instead of host-bound
(inter-step gaps overshoot the recorded think time; server-side load stops
growing). The fix mirrors the reference agent's ``process-per-ratio`` topology:
split each trial's ``k*N`` slots into P shard subprocesses of ~N slots each --
every process carries the load profile already proven at 1:1, and the GIL
ceiling moves to ~P x single-process throughput.

Layering contract:

- This module is MECHANISM: shard planning, per-shard config derivation,
  subprocess-group supervision, and artifact merging. It imports nothing from
  :mod:`bench_core.oversub` (the driver composes it, never the reverse) and
  nothing from the kernel data path.
- The shard boundary sits BELOW the kernel artifact contract: merging produces
  a schema-faithful ``run_summary.json`` + ``trajectories/index.json`` pair, so
  every downstream consumer (``compute_valid``, ``trial_row``,
  ``_trajectory_rows``, ratio summaries, ``compare_sweeps``, ``--reuse``)
  works on a sharded trial unchanged.
- Only admission-free modes shard (``SHARDABLE_MODES``). ``lifecycle`` /
  ``trajectory`` enforce a GLOBAL ``running_concurrency`` ceiling; per-shard
  admission controllers would let the global peak reach P*N and silently
  change the oversubscription profile. Sharding them needs a parent-side
  permit server (the reference's LAUNCH/PERMIT channel) -- a kernel change,
  deliberately out of scope here.

Known simplifications (measured, not assumed away):

- No cross-process start barrier: shards self-start after their own setup.
  The skew is recorded (``shard_start_skew_sec`` in the merged summary) so it
  can be quantified; add a READY/START gate only if the data shows it matters.
- vm_monitor is owned by shard 0 alone (one host collector per host); its
  stress window is shard 0's, which approximates the trial window up to the
  recorded skew.
- ``launch_interval_sec`` is scaled by P per shard: each shard paces its own
  creates at ``P * interval``, preserving the GLOBAL average create rate (the
  control-plane protection the knob exists for) without cross-process IPC.
  Round-boundary bursts can still align across shards (up to P concurrent
  creates); a parent-side pacer would remove that if it ever hurts.
- Multi-template manifests with ``N % T != 0``: a group-aligned shard offset
  (a multiple of N) shifts the kernel's within-template rank, so the merged
  (sandbox_index -> trajectory) ASSOCIATION can differ from single-process even
  though per-trajectory run counts are preserved. Single-template (the oversub
  default) is exactly faithful.
"""
from __future__ import annotations

import copy
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Modes whose kernel runs have admission=None (bench.py builds the Admission
# controller only for lifecycle/trajectory), i.e. no cross-fleet coordination
# invariant -- each slot is independent, so slot sets compose across processes.
SHARDABLE_MODES = frozenset({"exec_ephemeral", "exec_only"})

# Grace after terminate() before kill(); mirrors oversub._TERMINATE_GRACE_SEC
# (kept local so this module has no driver dependency). Must exceed the
# kernel's SIGTERM partial-flush wall time.
_TERMINATE_GRACE_SEC = 30
_POSTKILL_GRACE_SEC = 5

# Run-scoped fields on an index.json trajectory entry: they describe ONE run
# and must never be inherited as merged-entry scalars (they live on runs[]).
_RUN_SCOPED_KEYS = (
    "sandbox_index",
    "round_id",
    "n_steps",
    "n_failed",
    "n_timeout",
    "success_rate",
    "success",
    "elapsed_sec",
    "time_breakdown_sec",
    "create_error_type",
    "kill_error_type",
)

# Entry-level AGGREGATES recomputed from the merged runs[] with the kernel's
# own rules (trajectory_summary.aggregate_trajectories + trajectory_export.
# _index_row). Never inherited from one shard's entry: that value describes
# only the shard's runs subset (inheriting shard 0's success_rate/error types/
# breakdown p50s would silently drop every other shard's data). ``file`` is
# re-set to None: the deep per-trajectory replay_result.json artifacts stay in
# each shard's run dir (shard_dirs in the merged summary is the provenance).
_AGGREGATE_KEYS = (
    "n_runs",
    "n_success",
    "n_failed",
    "success_rate",
    "elapsed_sec",
    "all_failed",
    "n_steps_max",
    "time_breakdown_sec",
    "create_error_type",
    "kill_error_type",
    "file",
)

# Additive count keys inside run_summary["throughput"].
_THROUGHPUT_COUNT_KEYS = ("total", "succeeded", "failed", "total_steps")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def coerce_shards(value: object) -> int | str:
    """Normalize a ``shards`` setting: ``"auto"`` or an int >= 1.

    Accepts the CLI string form and the YAML forms (int or string). Raises
    ``ValueError`` on anything else so a typo fails loudly at startup.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "auto":
            return "auto"
        value = int(text)  # ValueError on garbage
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"shards must be 'auto' or an int, got {value!r}")
    if value < 1:
        raise ValueError(f"shards must be >= 1, got {value}")
    return value


def resolve_shard_count(setting: int | str, *, mode: str, ratio: int, n: int) -> int:
    """P for one trial: ``auto`` -> ratio (reference topology), int -> as asked.

    Non-shardable modes always collapse to 1 (a global-admission mode cannot
    be split without a cross-process permit server); an explicit P > 1 on such
    a mode is downgraded with a warning, not an error, so one sweep config can
    span modes. P is capped at the fleet size (every shard needs >= 1 slot).
    """
    if mode not in SHARDABLE_MODES:
        if setting != "auto" and setting > 1:
            logger.warning(
                "shards=%s ignored for mode %s: global running_concurrency admission "
                "cannot be split across processes (would over-admit to P*N); running "
                "single-process. Shardable modes: %s",
                setting,
                mode,
                sorted(SHARDABLE_MODES),
            )
        return 1
    p = ratio if setting == "auto" else int(setting)
    return max(1, min(p, ratio * n))


def shard_sizes(total: int, count: int) -> list[int]:
    """Balanced split of ``total`` slots into ``count`` shards (remainder first)."""
    size, extra = divmod(total, count)
    return [size + (1 if i < extra else 0) for i in range(count)]


@dataclass(frozen=True)
class ShardSpec:
    """One shard's static identity: slot share, re-key offset, artifact dir."""

    index: int
    size: int
    slot_offset: int  # cumulative slots of prior shards; sandbox_index re-key
    directory: Path

    @property
    def log_path(self) -> Path:
        return self.directory / "driver.log"

    @property
    def config_path(self) -> Path:
        return self.directory / "shard.yaml"


@dataclass(frozen=True)
class ShardPlan:
    """The full split of one trial (P >= 2; P == 1 keeps the legacy path)."""

    mode: str
    ratio: int
    n: int
    specs: tuple[ShardSpec, ...]

    @property
    def count(self) -> int:
        return len(self.specs)


def plan_shards(*, trial_dir: Path, mode: str, ratio: int, n: int, setting: int | str) -> ShardPlan | None:
    """Build the shard plan for one trial, or ``None`` when P == 1 (legacy).

    For P <= ratio the split is GROUP-ALIGNED: the trial's ``ratio`` groups of
    N slots are distributed over the shards, so every shard size is an integer
    multiple of N. Each shard then replays the whole first-N pool a whole
    number of times, and the merged per-trajectory run counts are exactly the
    single-process counts (slot k -> pool[k % N] in every shard). P > ratio
    falls back to a slot-level split -- totals are preserved but per-trajectory
    run counts redistribute (a shard smaller than the pool cannot cover every
    trajectory), so it is flagged with a warning.
    """
    p = resolve_shard_count(setting, mode=mode, ratio=ratio, n=n)
    if p <= 1:
        return None
    if p <= ratio:
        sizes = [groups * n for groups in shard_sizes(ratio, p)]
    else:
        logger.warning(
            "shards=%d > ratio=%d: shard sizes fall below the N=%d trajectory pool, so "
            "per-trajectory run counts redistribute vs a single-process trial (totals "
            "preserved). Use shards <= ratio for exact per-trajectory parity.",
            p,
            ratio,
            n,
        )
        sizes = shard_sizes(ratio * n, p)
    specs, offset = [], 0
    for i, size in enumerate(sizes):
        specs.append(ShardSpec(index=i, size=size, slot_offset=offset, directory=trial_dir / f"shard-{i:02d}"))
        offset += size
    return ShardPlan(mode=mode, ratio=ratio, n=n, specs=tuple(specs))


# ---------------------------------------------------------------------------
# Per-shard config + command
# ---------------------------------------------------------------------------


def build_shard_config(trial_cfg: dict, spec: ShardSpec, *, count: int) -> dict:
    """Derive one shard's kernel config from the (unsharded) trial config.

    The shard is a self-contained kernel run over ``spec.size`` slots:
    ``total_count`` / ``round_size`` shrink to the shard's share, artifacts go
    to the shard dir, the docker container prefix is suffixed so parallel
    shards never collide on ``{prefix}-{index}`` names (the suffix keeps
    ``startswith(base)`` true, so prefix-based cleanup still reaches every
    shard), and the pacing knobs (``launch_interval_sec``, ``create_batch.interval``,
    ``task_batch.interval``) scale by P so P independently-pacing shards keep the
    GLOBAL average create/dispatch rate of the single-process trial.
    ``trajectory_limit`` / ``running_concurrency`` stay untouched: the issue pool
    is global by design (every shard replays the same first-N pool; with
    group-aligned shard sizes the merged run set matches the single-process
    trial per trajectory, not just in total).
    """
    cfg = copy.deepcopy(trial_cfg)
    cfg.setdefault("sandbox", {})["total_count"] = spec.size
    cfg.setdefault("test", {})["round_size"] = spec.size
    cfg.setdefault("report", {})["output_dir"] = str(spec.directory)
    docker = cfg.get("docker")
    if isinstance(docker, dict) and docker.get("container_prefix"):
        docker["container_prefix"] = f"{docker['container_prefix']}-s{spec.index}"
    replay = cfg.get("replay")
    if isinstance(replay, dict) and replay.get("launch_interval_sec"):
        replay["launch_interval_sec"] = replay["launch_interval_sec"] * count
    for section in ("create_batch", "task_batch"):
        block = cfg.get(section)
        if isinstance(block, dict) and block.get("interval"):
            block["interval"] = block["interval"] * count
    return cfg


def write_shard_configs(trial_cfg: dict, plan: ShardPlan) -> list[Path]:
    """Materialize every shard's ``shard.yaml``; returns the config paths."""
    paths = []
    for spec in plan.specs:
        spec.directory.mkdir(parents=True, exist_ok=True)
        cfg = build_shard_config(trial_cfg, spec, count=plan.count)
        spec.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        paths.append(spec.config_path)
    return paths


def build_shard_command(
    bench_core_bin: list[str],
    spec: ShardSpec,
    *,
    provider: str,
    no_vm_monitor: bool,
) -> list[str]:
    """One shard's bench-core argv. Shard 0 owns the host vm_monitor.

    A second collector on the same host would double-sample the same /proc
    counters and race on the tool lock dir, so every shard past the first runs
    with ``--no-vm-monitor`` (host metrics are host-wide: one collector sees
    all VMs, including the other shards').
    """
    cmd = [*bench_core_bin, "--config", str(spec.config_path), "--provider", provider]
    if no_vm_monitor or spec.index > 0:
        cmd.append("--no-vm-monitor")
    return cmd


# ---------------------------------------------------------------------------
# Subprocess-group supervision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupResult:
    """Outcome of one sharded launch: per-shard rc + how the group ended."""

    returncodes: list[int]
    interrupted: bool
    timed_out: bool

    def trial_return_code(self) -> int:
        """The rc a single-process trial would have shown (130 = user interrupt)."""
        if self.interrupted:
            return 130
        return next((rc for rc in self.returncodes if rc != 0), 0)


class ShardGroupRunner:
    """Launch P kernel subprocesses under one shared trial deadline.

    Same supervision semantics as the driver's single-process
    ``_run_subprocess`` -- terminate -> grace -> kill -- applied to the whole
    group: a deadline breach or Ctrl+C tears down EVERY shard (an orphaned
    shard would keep stressing the host and corrupt the next trial).
    """

    def __init__(self, commands: list[list[str]], log_paths: list[Path], timeout_sec: int) -> None:
        if len(commands) != len(log_paths):
            raise ValueError("commands and log_paths must align")
        self.commands = commands
        self.log_paths = log_paths
        self.timeout_sec = timeout_sec

    def run(self) -> GroupResult:
        procs: list[subprocess.Popen] = []
        logs = []
        interrupted = timed_out = False
        try:
            try:
                for cmd, log_path in zip(self.commands, self.log_paths):
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    logs.append(open(log_path, "w", encoding="utf-8"))
                    procs.append(subprocess.Popen(cmd, stdout=logs[-1], stderr=subprocess.STDOUT))
            except KeyboardInterrupt:
                # Ctrl+C inside the launch window: already-launched shards must
                # not be orphaned stressing the host while the driver exits.
                interrupted = True
                self._terminate_all(procs)
            if not interrupted:
                deadline = time.monotonic() + self.timeout_sec if self.timeout_sec else None
                for proc in procs:
                    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                    try:
                        proc.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        logger.warning("shard group deadline (%ss) breached; tearing down all shards", self.timeout_sec)
                        self._terminate_all(procs)
                        break
                    except KeyboardInterrupt:
                        interrupted = True
                        self._terminate_all(procs)
                        break
        finally:
            for stream in logs:
                stream.close()
        returncodes = [p.returncode if p.returncode is not None else -1 for p in procs]
        return GroupResult(returncodes=returncodes, interrupted=interrupted, timed_out=timed_out)

    @staticmethod
    def _terminate_all(procs: list[subprocess.Popen]) -> None:
        """terminate -> grace -> kill every still-alive process, then reap all."""
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=_TERMINATE_GRACE_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=_POSTKILL_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    logger.error("shard process %s did not exit after kill", proc.pid)


# ---------------------------------------------------------------------------
# Artifact collection + merge
# ---------------------------------------------------------------------------


@dataclass
class ShardArtifacts:
    """What each shard left behind (None = the shard produced no summary)."""

    summaries: list[dict | None] = field(default_factory=list)
    summary_paths: list[str | None] = field(default_factory=list)
    catalogs: list[tuple[int, dict]] = field(default_factory=list)  # (slot_offset, index.json)

    @property
    def missing(self) -> list[int]:
        return [i for i, s in enumerate(self.summaries) if s is None]


def collect_shard_artifacts(plan: ShardPlan, prefix: str) -> ShardArtifacts:
    """Glob each shard's run_summary.json + trajectories/index.json.

    A crashed shard simply has no summary -- it surfaces in ``missing`` and in
    the merged summary's ``error``; it is never backfilled with fabricated
    zeros (a crash must not masquerade as a successful measurement). Note the
    ``error`` field is provenance, not a validity gate: ``compute_valid`` gates
    on summed counts, so an (abnormal) rc-0 exit without a summary would only
    show up as a count shortfall + this error string.
    """
    out = ShardArtifacts()
    for spec in plan.specs:
        hits = sorted(spec.directory.glob(f"{prefix}_*/{prefix}_run_summary.json"))
        if not hits:
            out.summaries.append(None)
            out.summary_paths.append(None)
            continue
        out.summaries.append(json.loads(hits[-1].read_text(encoding="utf-8")))
        out.summary_paths.append(str(hits[-1]))
        idx_path = hits[-1].parent / "trajectories" / "index.json"
        if idx_path.exists():
            try:
                catalog = json.loads(idx_path.read_text(encoding="utf-8"))
                out.catalogs.append((spec.slot_offset, catalog))
            except (OSError, ValueError) as exc:
                logger.warning("shard %d: unreadable index.json (%s); trajectory rows will lack it", spec.index, exc)
    return out


def _merged_throughput(summaries: list[dict], wall_sec: float | None) -> dict:
    """Counts sum across concurrent shards; rates recompute over the trial wall.

    Summing per-shard rates would overstate the fleet rate whenever shard
    walls differ (setup skew, a straggler); ``count / max_wall`` is the honest
    trial-level rate and matches the kernel's own ``succeeded / wall_sec``.
    """
    tp = {k: sum((s.get("throughput") or {}).get(k) or 0 for s in summaries) for k in _THROUGHPUT_COUNT_KEYS}
    tp["tasks_per_sec"] = round(tp["succeeded"] / wall_sec, 3) if wall_sec else None
    tp["steps_per_sec"] = round(tp["total_steps"] / wall_sec, 3) if wall_sec else None
    return tp


def _merged_span(summaries: list[dict]) -> dict:
    """min start / max end across shards (the trial's wall-clock envelope).

    ``wall_sec`` is the ENVELOPE (max completed − min started), not max of the
    shard walls: the kernel's own ``wall_sec`` is measured over exactly its
    ``[started_epoch, completed_epoch]`` span, so the envelope is what a single
    process running k×N slots would have reported -- and every rate is divided
    by it. Max-of-walls would overstate fleet throughput whenever shards start
    or finish at different times. Falls back to max wall when epochs are
    incomplete (a partially-flushed shard summary).
    """
    started = [(s.get("started_epoch"), s.get("started_at")) for s in summaries if s.get("started_epoch")]
    completed = [(s.get("completed_epoch"), s.get("completed_at")) for s in summaries if s.get("completed_epoch")]
    walls = [s.get("wall_sec") for s in summaries if s.get("wall_sec") is not None]
    start_epoch = min(started)[0] if started else None
    end_epoch = max(completed)[0] if completed else None
    if start_epoch is not None and end_epoch is not None:
        wall = round(end_epoch - start_epoch, 3)
    else:
        wall = round(max(walls), 3) if walls else None
    return {
        "started_epoch": start_epoch,
        "started_at": min(started)[1] if started else None,
        "completed_epoch": end_epoch,
        "completed_at": max(completed)[1] if completed else None,
        "wall_sec": wall,
        "shard_start_skew_sec": (
            round(max(e for e, _ in started) - start_epoch, 3) if len(started) > 1 and start_epoch else None
        ),
    }


def _merged_admission(summaries: list[dict]) -> dict | None:
    """None when no shard had admission (the shardable modes); else merge.

    Defensive path only -- SHARDABLE_MODES run admission-free, so today this
    always returns None. Kept correct (peak = max, counts = sum, wait =
    grant-weighted) so a future permit-server design inherits sound algebra.
    """
    blocks = [s.get("admission") for s in summaries if s.get("admission")]
    if not blocks:
        return None
    granted = sum(b.get("granted") or 0 for b in blocks)
    wait = sum((b.get("avg_queue_wait_sec") or 0) * (b.get("granted") or 0) for b in blocks)
    peaks = [b.get("peak_active") for b in blocks if b.get("peak_active") is not None]
    return {
        "maximum": sum(b.get("maximum") or 0 for b in blocks),
        "peak_active": max(peaks) if peaks else None,
        "granted": granted,
        "avg_queue_wait_sec": round(wait / granted, 6) if granted else 0.0,
        "control_qps": blocks[0].get("control_qps"),
        "control_dispatched": sum(b.get("control_dispatched") or 0 for b in blocks),
    }


def _merged_lifecycle_overhead(summaries: list[dict]) -> dict | None:
    """Sums + recomputed pct; None when no shard reported it (exec modes)."""
    blocks = [s.get("lifecycle_overhead") for s in summaries if s.get("lifecycle_overhead")]
    if not blocks:
        return None
    pause = sum(b.get("pause_sec_sum") or 0 for b in blocks)
    resume = sum(b.get("resume_sec_sum") or 0 for b in blocks)
    # slice_total is not carried in the block; reconstruct it per shard from
    # pct. Unreachable for SHARDABLE_MODES (exec modes report no overhead) --
    # kept honest for a future permit-server design, not precise to the digit.
    slice_sum = sum(
        ((b.get("pause_sec_sum") or 0) + (b.get("resume_sec_sum") or 0)) * 100 / b["pct_of_slice_total"]
        for b in blocks
        if b.get("pct_of_slice_total")
    )
    return {
        "pause_sec_sum": round(pause, 6),
        "resume_sec_sum": round(resume, 6),
        "pct_of_slice_total": round((pause + resume) / slice_sum * 100, 3) if slice_sum else 0.0,
    }


def _merged_error(summaries: list[dict], artifacts: ShardArtifacts, result: GroupResult) -> str | None:
    """First real problem, aggregated: shard errors + missing summaries + bad rcs."""
    parts = [s.get("error") for s in summaries if s.get("error")]
    for i in artifacts.missing:
        parts.append(f"shard {i}: no run_summary.json (crashed or killed before flush)")
    for i, rc in enumerate(result.returncodes):
        if rc != 0:
            parts.append(f"shard {i}: exit code {rc}")
    if result.timed_out:
        parts.append("trial deadline breached; shards terminated")
    return "; ".join(str(p) for p in parts) if parts else None


def merge_run_summaries(
    summaries: list[dict],
    *,
    plan: ShardPlan,
    artifacts: ShardArtifacts,
    result: GroupResult,
) -> dict:
    """Compose P shard summaries into one schema-faithful trial summary.

    Schema-2 keys keep their exact meaning at trial level (counts summed,
    wall = envelope, rates recomputed over it, span = envelope); shard
    provenance rides in ADDITIONAL keys (``shard_*``) so a consumer that only
    knows schema 2 reads the merged file as if one kernel run of k*N slots had
    produced it.
    """
    present = [s for s in summaries if s is not None]
    if not present:
        return {
            "throughput": {},
            "test_duration": None,
            "error": _merged_error([], artifacts, result) or "no shard run_summary.json",
        }
    first = present[0]
    span = _merged_span(present)
    tp = _merged_throughput(present, span["wall_sec"])
    total_count = sum(s.get("total_count") or 0 for s in present) or None
    running_concurrency = next((s.get("running_concurrency") for s in present if s.get("running_concurrency")), None)
    vm_dirs = [
        (s.get("paths") or {}).get("vm_monitor_dir") for s in present if (s.get("paths") or {}).get("vm_monitor_dir")
    ]
    return {
        "schema_version": first.get("schema_version", 2),
        "workflow_type": first.get("workflow_type"),
        "replay_mode": first.get("replay_mode"),
        "provider": first.get("provider"),
        "started_at": span["started_at"],
        "completed_at": span["completed_at"],
        "started_epoch": span["started_epoch"],
        "completed_epoch": span["completed_epoch"],
        "test_duration": first.get("test_duration"),
        "wall_sec": span["wall_sec"],
        "total_count": total_count,
        "running_concurrency": running_concurrency,
        "overcommit_ratio": (
            round(total_count / running_concurrency, 3)
            if total_count and running_concurrency
            else first.get("overcommit_ratio")
        ),
        "throughput": tp,
        "admission": _merged_admission(present),
        "lifecycle_overhead": _merged_lifecycle_overhead(present),
        "paths": {
            # The real txt/xlsx/series artifacts live under shard_dirs (per-shard
            # run dirs); no driver consumer reads these three merged keys.
            "report": None,
            "obs_xlsx": None,
            "lifecycle_series": None,
            # Patched by write_merged_artifacts when a merged index is written.
            "trajectory_index": None,
            "vm_monitor_dir": vm_dirs[0] if vm_dirs else None,
        },
        "error": _merged_error(present, artifacts, result),
        # --- shard provenance (additive; not part of the schema-2 contract) ---
        "shard_count": plan.count,
        "shard_start_skew_sec": span["shard_start_skew_sec"],
        "shard_return_codes": result.returncodes,
        "shard_run_summaries": artifacts.summary_paths,
        "shard_dirs": [str(spec.directory) for spec in plan.specs],
    }


def _promote_legacy_entry(entry: dict) -> list[dict]:
    """Old index.json shape (no ``runs[]``): the entry IS its single run."""
    return [{k: entry.get(k) for k in _RUN_SCOPED_KEYS}]


def _none_last(value) -> tuple[int, float]:
    """None-safe ascending sort key (mirrors the kernel's _sort_key intent)."""
    return (1, 0.0) if value is None else (0, float(value))


def _p50(values: list[float]) -> float:
    """The kernel's p50 (utils.calc_percentiles): upper median, NOT the mean of
    the middle two for even n -- statistics.median would diverge at k=2."""
    ordered = sorted(values)
    return ordered[min(len(ordered) // 2, len(ordered) - 1)]


def _aggregate_entry(merged: dict) -> None:
    """Recompute one merged entry's aggregates from its runs[], kernel rules.

    Mirrors ``aggregate_trajectories`` + ``_index_row`` exactly: counts are
    sums; ``success_rate`` is the mean of per-run rates; headline
    ``elapsed_sec`` is the success-run median, falling back to the all-run
    median + ``all_failed`` when nothing succeeded; ``n_steps_max`` is the max
    across runs; entry-level ``time_breakdown_sec`` is the per-segment median
    over the same use-set (successful runs, else all); error types are
    first-seen across the runs. Runs are sorted by ``(sandbox_index,
    round_id)`` like the kernel's index rows.
    """
    runs = sorted(merged["runs"], key=lambda r: (_none_last(r.get("sandbox_index")), _none_last(r.get("round_id"))))
    merged["runs"] = runs
    success = [r for r in runs if r.get("success_rate") is not None and r["success_rate"] >= 1.0]
    merged["n_runs"] = len(runs)
    merged["n_success"] = len(success)
    merged["n_failed"] = len(runs) - len(success)
    merged["all_failed"] = bool(runs) and not success
    rates = [r["success_rate"] for r in runs if r.get("success_rate") is not None]
    merged["success_rate"] = round(sum(rates) / len(rates), 6) if rates else None
    use_set = success or runs
    elapsed = [r["elapsed_sec"] for r in use_set if r.get("elapsed_sec") is not None]
    merged["elapsed_sec"] = round(_p50(elapsed), 6) if elapsed else None
    merged["n_steps_max"] = max((r.get("n_steps") or 0 for r in runs), default=0)
    seg_keys = sorted({k for r in use_set for k in (r.get("time_breakdown_sec") or {})})
    merged["time_breakdown_sec"] = {
        k: round(_p50([(r.get("time_breakdown_sec") or {}).get(k, 0.0) for r in use_set]), 6) for k in seg_keys
    }
    merged["create_error_type"] = next((r["create_error_type"] for r in runs if r.get("create_error_type")), None)
    merged["kill_error_type"] = next((r["kill_error_type"] for r in runs if r.get("kill_error_type")), None)
    merged["file"] = None


def merge_trajectory_indexes(catalogs: list[tuple[int, dict]]) -> dict:
    """Merge per-shard catalogs into one trial-level ``index.json`` shape.

    Runs are grouped by ``trajectory_id`` (every shard replays the same pool,
    so the same trajectory appears in several shards) and each run's
    ``sandbox_index`` is re-keyed by the shard's slot offset -- the run
    identity ``(trajectory_id, sandbox_index, round_id)`` stays unique across
    the merged file, exactly as in a single-process k*N trial. Entry-level
    aggregates are recomputed from the merged runs with the kernel's own rules
    (:func:`_aggregate_entry`); only true per-trajectory scalars
    (``action_series`` and friends) are inherited first-non-null.
    """
    by_tid: dict[str, dict] = {}
    for offset, catalog in catalogs:
        for entry in catalog.get("trajectories", []):
            tid = str(entry.get("trajectory_id"))
            merged = by_tid.setdefault(tid, {"trajectory_id": entry.get("trajectory_id"), "runs": []})
            for key, value in entry.items():
                if key in ("runs", "trajectory_id") or key in _RUN_SCOPED_KEYS or key in _AGGREGATE_KEYS:
                    continue
                if merged.get(key) is None and value is not None:
                    merged[key] = value  # first non-null scalar wins (action_series, ...)
            runs = entry.get("runs") or _promote_legacy_entry(entry)
            for run in runs:
                run = dict(run)
                if run.get("sandbox_index") is not None:
                    run["sandbox_index"] = run["sandbox_index"] + offset
                merged["runs"].append(run)
    for merged in by_tid.values():
        _aggregate_entry(merged)
    return {
        "n_trajectories": len(by_tid),
        "trajectories": sorted(by_tid.values(), key=lambda e: str(e["trajectory_id"])),
        "merged_from_shards": len(catalogs),
    }


def write_merged_artifacts(
    trial_dir: Path,
    prefix: str,
    summary: dict,
    index: dict | None,
) -> Path:
    """Write ``merged/{prefix}_run_summary.json`` (+ index); return summary path.

    The merged pair sits at a driver-owned fixed path (not a kernel timestamped
    run dir) so ``--reuse`` and ``run_summary_path`` consumers have one stable
    location; ``_trajectory_rows`` finds the index via the same parent-relative
    ``trajectories/index.json`` convention as a kernel run dir.
    """
    merged_dir = trial_dir / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    index_path = None
    if index is not None:
        (merged_dir / "trajectories").mkdir(parents=True, exist_ok=True)
        index_path = merged_dir / "trajectories" / "index.json"
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        summary.setdefault("paths", {})["trajectory_index"] = str(index_path)
    summary_path = merged_dir / f"{prefix}_run_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary_path
