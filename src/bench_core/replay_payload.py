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

import json
from dataclasses import dataclass
from pathlib import Path

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

    ``environment`` is a reserved passthrough field (P1 does not consume it;
    later multi-template / multi-environment matching will). ``steps`` excludes
    the terminal action (``submit``/``finish``/``done``), which marks "agent
    submitted" and is not executed.
    """

    path: Path
    instance_id: str
    environment: str
    steps: tuple[ReplayStep, ...]


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
