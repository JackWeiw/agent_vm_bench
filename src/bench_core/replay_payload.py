"""Trajectory replay payload: parsing + action classification (host-agnostic).

Loads recorded SWE-bench / SWE-smith agent trajectories (ordered shell +
``str_replace_editor`` actions with per-step ``delay_time``) into frozen,
shareable data structures. The runners in :mod:`bench_core.task_runner.replay`
consume them; this module owns no SDK and no timing.

A trajectory is a frozen, immutable record safe for concurrent read by many
sandbox threads (the pool is shared read-only; each runner keeps its own
cursor). Mirrors :mod:`bench_core.coding_payload`'s positioning as the
host-agnostic payload module for the replay workflow.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
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

    When ``config.replay_template_manifest`` is set, each trajectory's
    ``template`` is attached from the manifest (keyed by path relative to
    ``replay_trajectory_dir``); a missing entry leaves ``template=None`` with a
    WARNING. A missing/unreadable manifest file is a hard error (an explicit
    manifest the user asked for must be readable). Returns a cached frozen tuple
    so repeat calls from different runner threads share one immutable object.
    """
    directory = config.replay_trajectory_dir
    glob = config.replay_trajectory_glob
    manifest_path = config.replay_template_manifest
    cache_key = (str(directory), glob, str(manifest_path or ""))
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

        template: str | None = None
        if manifest is not None:
            rel = os.path.relpath(path, directory).replace("\\", "/")
            raw_template = manifest.get(rel)
            if raw_template is not None and not isinstance(raw_template, str):
                logger.warning(
                    f"[replay] manifest entry for {rel} is not a string ({type(raw_template).__name__}); ignoring"
                )
                raw_template = None
            template = raw_template
            if template is None and rel not in manifest:
                logger.warning(f"[replay] trajectory {rel} has no manifest entry; falling back to default template")
            traj = dataclasses.replace(traj, template=template)
        pool.append(traj)

    cached = tuple(pool)
    _POOL_CACHE[cache_key] = cached
    return cached


def reset_pool_cache() -> None:
    """Test hook: clear the module-level pool cache between tests."""
    _POOL_CACHE.clear()
