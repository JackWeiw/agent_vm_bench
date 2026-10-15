"""Trajectory replay payload: parsing + action classification (host-agnostic).

Loads recorded SWE-bench / SWE-smith agent trajectories (ordered shell +
``str_replace_editor`` actions with per-step ``delay_time``) into frozen,
shareable data structures. The runners in :mod:`bench_core.task_runner.replay`
consume them; this module owns no SDK and no timing.

A trajectory is a frozen, immutable record safe for concurrent read by many
sandbox threads (the pool is shared read-only; each runner keeps its own
cursor). Mirrors :mod:`bench_core.payload.coding_payload`'s positioning as the
host-agnostic payload module for the replay workflow.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bench_core.config import KernelConfig

# Action-type taxonomy (drives metrics bucketing). Terminal actions are
# recognized for truncation but never executed.
_TERMINAL_ACTIONS = {"submit", "finish", "done"}


@dataclass(slots=True, frozen=True)
class ReplayStep:
    """One parsed trajectory action.

    ``action_type`` is parsed from the action prefix by :func:`classify_action`
    and drives the per-bucket metrics in :class:`bench_core.schemas.ReplayMetrics`.
    """

    index: int
    action: str
    delay_time_sec: float
    action_type: str


@dataclass(slots=True, frozen=True)
class Trajectory:
    """One loaded trajectory.

    ``template`` is the concrete backend template this trajectory runs against,
    attached from the side manifest by :func:`load_pool` (None when no manifest
    or no entry -> provider uses its default template). ``environment`` is a
    reserved passthrough field, not consulted for template resolution. ``steps``
    excludes the terminal action (``submit``/``finish``/``done``).
    """

    path: Path
    instance_id: str
    environment: str
    steps: tuple[ReplayStep, ...]
    template: str | None = None


def classify_action(action: str) -> str:
    """Classify a recorded action string into a metrics bucket.

    - ``str_replace_editor``/``submit``/``finish``/``done``: matched by exact
      leading token (these are tool names, not shell commands).
    - leading ``bash ``: a bash invocation (bucketed separately from shell so
      the report can distinguish bare shell from explicit bash -lc).
    - anything else: ``"shell"`` (the common case: find/grep/git/python3 ...).
    """
    action = action.strip()
    if not action:
        return "shell"
    first = action.split()[0]
    if first == "str_replace_editor":
        return "str_replace_editor"
    if first in _TERMINAL_ACTIONS:
        return first
    if first == "bash":
        return "bash"
    return "shell"


def load_trajectory(path: Path) -> Trajectory:
    """Load one trajectory JSON, truncating at the first terminal action.

    The terminal action (``submit``/``finish``/``done``) marks "agent
    submitted"; it is **not executed** and **its ``delay_time`` is discarded**
    — only steps preceding it are kept. If no terminal action is present (a
    failed / interrupted trajectory), all steps are kept and no error is
    raised.

    ``instance_id`` falls back to the filename stem when absent.
    """
    path = Path(path)
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)

    environment = raw.get("environment", "main")
    instance_id = raw.get("instance_id") or path.stem
    raw_steps = raw.get("trajectory") or []

    steps: list[ReplayStep] = []
    for idx, entry in enumerate(raw_steps):
        action = entry.get("action") or ""
        action_type = classify_action(action)
        if action_type in _TERMINAL_ACTIONS:
            break  # truncate; terminal action + its delay_time discarded
        steps.append(
            ReplayStep(
                index=idx,
                action=action,
                delay_time_sec=float(entry.get("delay_time", 0.0) or 0.0),
                action_type=action_type,
            )
        )

    return Trajectory(path=path, instance_id=instance_id, environment=environment, steps=tuple(steps))


logger = logging.getLogger(__name__)

_TRAJECTORY_SUFFIXES = (".replay.json", ".json", ".traj")

# Module-level pool cache: (dir, glob, manifest_path) -> tuple[Trajectory, ...].
# Frozen tuples are safe for concurrent read by many runner threads; the pool is
# shared read-only across all sandboxes. Each runner keeps its own cursor.
_POOL_CACHE: dict[tuple[str, str, str], tuple[Trajectory, ...]] = {}


def find_trajectories(directory: Path, glob: str = "*.replay.json") -> list[Path]:
    """List trajectory files under ``directory`` matching ``glob``.

    Sorted by path for deterministic pool ordering across runs. The glob
    selects filenames; the suffix whitelist (``.replay.json`` / ``.json`` /
    ``.traj``) filters out unrelated JSON so a ``*`` glob still stays scoped.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    matches = [p for p in directory.glob(glob) if p.is_file() and p.name.endswith(_TRAJECTORY_SUFFIXES)]
    return sorted(matches)


def load_pool(config: KernelConfig) -> tuple[Trajectory, ...]:
    """Load + cache the shared trajectory pool from ``config``.

    With no ``replay_template_manifest``, every trajectory keeps
    ``template=None`` (legacy single-template: the provider uses its block-level
    ``template`` default). When a manifest IS set it is the authoritative run
    set -- only trajectories it maps to a concrete template string enter the
    pool; entries absent from the manifest (or mapped to a null, empty, or
    non-string value) are skipped with a WARNING rather than falling back to the provider
    default, which may be a wrong image for the trajectory's repo or a shipped
    placeholder that 400s on the backend. A missing/unreadable manifest file is
    a hard error. Returns a cached frozen tuple so repeat calls from different
    runner threads share one immutable object.
    """
    from bench_core.task_runner.replay import ReplayConfig

    rcfg = config.workflow_config
    assert isinstance(rcfg, ReplayConfig), "load_pool requires a ReplayConfig view"
    directory = rcfg.replay_trajectory_dir
    glob = rcfg.replay_trajectory_glob
    manifest_path = rcfg.replay_template_manifest
    limit = rcfg.replay_trajectory_limit
    rc = rcfg.replay_running_concurrency
    # ``rc`` (the oversub baseline fleet size N) is in the key because the
    # linearity clamp below may reduce ``limit`` to ``rc // T`` -- two configs
    # with the same trajectory_limit but different rc clamp differently, so they
    # must not share a cache slot.
    cache_key = (str(directory), glob, str(manifest_path or ""), str(limit or ""), str(rc or ""))
    if cache_key in _POOL_CACHE:
        return _POOL_CACHE[cache_key]

    manifest: dict[str, str] | None = None
    if manifest_path:
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"replay.template_manifest not readable at {manifest_path}: {type(exc).__name__}: {exc}"
            ) from exc

    pool: list[Trajectory] = []
    for path in find_trajectories(Path(directory), glob):
        try:
            traj = load_trajectory(path)
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning(f"[replay] skipping unparseable trajectory {path.name}: {type(exc).__name__}: {exc}")
            continue
        if not traj.steps:
            logger.warning(f"[replay] skipping trajectory with no executable steps: {path.name}")
            continue

        if manifest is None:
            # Legacy single-template path: every trajectory keeps template=None
            # and the provider uses its block-level `template` default.
            pool.append(traj)
            continue
        rel = os.path.relpath(path, directory).replace("\\", "/")
        raw_template = manifest.get(rel)
        if raw_template is not None and not isinstance(raw_template, str):
            logger.warning(
                f"[replay] manifest entry for {rel} is not a string ({type(raw_template).__name__}); skipping"
            )
            continue
        if not raw_template:
            logger.warning(f"[replay] trajectory {rel} has no manifest entry; skipping")
            continue
        pool.append(dataclasses.replace(traj, template=raw_template))

    if limit is not None:
        # Oversub linearity clamp: the oversub driver sets trajectory_limit=N
        # (mirroring the single-template ``trajectories[:N]`` strategy). For a
        # MULTI-template pool (T>1) N is too large -- the balanced ratio-1 fleet
        # gives each template only N/T sandboxes, so capping to N lets the
        # RUNNING issue set grow with ratio until k>=T (only N/T of N issues run
        # at ratio 1, then 2N/T, ...), drifting the linearity baseline. Clamp to
        # the ratio-1 fleet share (running_concurrency // T) so count_t <=
        # fleet_t(ratio 1) and the issue set stays fixed across ratios (per-issue
        # execution count then scales linearly with k). Single-template (T=1):
        # rc//T == N, no-op. rc unset (non-oversub direct run): honor the explicit
        # limit as-is (no clamp).
        if rc is not None and rc > 0:
            t_count = len({t.template for t in pool})
            if t_count > 1:
                fleet_share = max(rc // t_count, 1)
                if fleet_share < limit:
                    logger.info(
                        f"[replay] trajectory_limit={limit} clamped to ratio-1 fleet share "
                        f"{fleet_share} (running_concurrency={rc} // T={t_count}); keeps "
                        f"the issue set fixed across oversub ratios"
                    )
                    limit = fleet_share
        # Then cap each template's sub-pool to the first ``limit`` trajectories
        # (natural sort order preserved) -- per-template, NOT a global first-N. A
        # global first-N can land entirely inside one template and starve the rest
        # in a multi-template manifest (orphan skips at replay time). Capping per
        # template keeps every template covered; combined with the clamp above
        # (limit = N//T) the issue set per template stays fixed across ratios.
        kept: list[Trajectory] = []
        kept_n: dict[str | None, int] = {}
        total_n: dict[str | None, int] = {}
        for traj in pool:
            total_n[traj.template] = total_n.get(traj.template, 0) + 1
            if kept_n.get(traj.template, 0) < limit:
                kept.append(traj)
                kept_n[traj.template] = kept_n.get(traj.template, 0) + 1
        if len(kept) < len(pool):
            parts = [f"{t!r} {total_n[t]}->{kept_n[t]}" for t in total_n if total_n[t] > kept_n[t]]
            logger.info(
                f"[replay] trajectory_limit={limit}: capping per template {len(pool)} -> {len(kept)} "
                f"({'; '.join(parts)})"
            )
        pool = kept

    cached = tuple(pool)
    _POOL_CACHE[cache_key] = cached
    return cached


# ---------------------------------------------------------------------------
# Fleet-assignment helpers: balanced template distribution + within-group rank.
#
# Both ``template_fleet`` (what ``_replay_template_map`` feeds create_all) and
# ``within_template_rank`` (what the runner uses to pick a trajectory) MUST
# derive the distinct-template order from here -- the single source of truth --
# so a sandbox's fleet template and its within-group rank agree. Both consume
# the SAME cached ``load_pool`` tuple (one immutable object per config), so the
# order can never drift between create_all and the runner.
# ---------------------------------------------------------------------------


def _template_sort_key(name: str | None) -> tuple[bool, str]:
    """Sort None before any named template without crashing on mixed pools.

    Python 3 forbids ``None < "str"`` (TypeError), so a bare ``sorted({None,
    "swb"})`` crashes if a pool ever held both. In practice a pool is either
    all-``None`` (no manifest -> provider block default) or all-named (manifest
    set) -- never mixed -- but this key makes ``_distinct_templates`` total.
    """
    return (name is not None, name or "")


@lru_cache(maxsize=8)
def _distinct_templates(pool: tuple[Trajectory, ...]) -> list[str | None]:
    """Sorted distinct template names in ``pool`` (deterministic; None first).

    Cached on the immutable ``load_pool`` tuple (few distinct inputs per run),
    so the runner hot path never rebuilds the set per sandbox.
    """
    return sorted({t.template for t in pool}, key=_template_sort_key)


def template_fleet(pool: tuple[Trajectory, ...], total_count: int) -> dict[int, str | None]:
    """Balanced per-sandbox template map: sandbox ``k`` -> ``distinct[k % T]``.

    Round-robin over the *distinct templates* (NOT over pool entries), so each
    template gets ``~total_count/T`` sandboxes at every ratio regardless of pool
    order, length, or per-template trajectory counts -> the fleet mix is
    ratio-stable (the precondition for oversub linearity). When
    ``total_count % T != 0`` the remainder always lands on the earlier distinct
    templates -- a fixed, deterministic bias (locked by tests). Empty pool ->
    ``{}``.
    """
    distinct = _distinct_templates(pool)
    if not distinct:
        return {}
    T = len(distinct)
    return {k: distinct[k % T] for k in range(total_count)}


def within_template_rank(pool: tuple[Trajectory, ...], k: int) -> int:
    """Sandbox ``k``'s 0-based rank within its template group under :func:`template_fleet`.

    Under the balanced fleet ``distinct[k % T]`` all sandboxes of one template
    share ``k % T`` and are ordered 0, 1, 2, ... by increasing ``k`` -- so the
    within-group rank is exactly ``k // T``. The runner uses this (NOT the
    global sandbox index) to pick ``my_pool[(rank + round_id) % len(my_pool)]``,
    giving stride-1 within-template coverage -- which kills the gcd/T stride
    bug where ``gcd(T, count_t) > 1`` left some of a template's trajectories
    unreachable per round.

    Only valid when ``pool`` is the SAME tuple :func:`template_fleet` consumed
    (both must come from ``load_pool``); a stale distinct order would silently
    mis-route. Returns 0 for an empty pool (callers guard ``if not pool``
    before reaching here, so the divide-by-zero is unreachable in practice).
    """
    T = len(_distinct_templates(pool))
    return k // T if T else 0


def template_at(pool: tuple[Trajectory, ...], k: int) -> str | None:
    """Sandbox ``k``'s template under :func:`template_fleet` -- ``distinct[k % T]``.

    exec_ephemeral skips ``create_all`` (no fleet dict to read a template from),
    so each worker derives its template from its shell index via this helper.
    Mods on ``T`` (the distinct-template count) directly -- indexing-agnostic,
    so it is correct for the 1-based shell indices bench.py builds
    (``range(1, total_count+1)``) as well as the 0-based fleet dict, and never
    mis-routes the last sandbox the way ``template_fleet(total_count).get(k)``
    does when ``T`` does not divide ``total_count``. Shares the cached
    :func:`_distinct_templates` source of truth so its order is byte-identical
    to :func:`template_fleet`'s. Empty pool -> ``None``.
    """
    distinct = _distinct_templates(pool)
    return distinct[k % len(distinct)] if distinct else None


def reset_pool_cache() -> None:
    """Test hook: clear the module-level pool cache between tests."""
    _POOL_CACHE.clear()
    _distinct_templates.cache_clear()
