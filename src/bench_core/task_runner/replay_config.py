"""Typed view of the ``replay:`` YAML section for the replay workflow.

Split out of ``replay.py`` so the runner engine, config, and report formatter
each live in one focused module; ``replay.py`` re-exports ``ReplayConfig`` for
import-compat (``from bench_core.task_runner.replay import ReplayConfig``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bench_core.config import KernelConfig
from bench_core.workflow_registry import WorkflowConfigBase, WorkflowConfigError

logger = logging.getLogger(__name__)


@dataclass
class ReplayConfig(WorkflowConfigBase):
    """Typed view of the ``replay:`` YAML section.

    Range checks (``replay_running_concurrency`` / ``control_plane_*`` / lifecycle
    knobs) and the ``replay_mode`` allowed-set live in ``validate`` -- the sole view
    with real cross-section checks (``running_concurrency <= total_count`` reads
    ``kernel_config``). The ``exec_only -> ready_probe=False`` normalization lives
    in ``__post_init__``."""

    replay_trajectory_dir: str = "trajectories"
    replay_trajectory_glob: str = "*.replay.json"
    # Cap the loaded pool to the first N trajectories PER TEMPLATE (natural sort
    # order within each template). None = use the whole pool. Set by the oversub
    # driver to ``running_concurrency`` (N) so each template's issue set stays
    # fixed across ratios -- isolating the oversub variable for linearity
    # analysis (each issue's execution count then scales linearly with ratio k)
    # instead of letting the issue mix drift with ratio. Per-template (not a
    # global first-N) so a multi-template manifest keeps every template covered;
    # a global first-N could starve a template whose trajectories sort after
    # another's.
    replay_trajectory_limit: int | None = None
    # Per-sandbox trajectory count (M) for fixed exec_ephemeral: each of the N
    # running slots runs exactly M trajectories (total = M * concurrency), then
    # the run stops -- continuous (no barrier) + stop-when-done. Corresponds to
    # reference B's ``--total-multiplier`` (renamed for brevity; reference B is a
    # separate script). Ignored outside fixed+exec_ephemeral (the bound lives in
    # the fixed runner's exec_ephemeral branch; round_robin uses round_count).
    replay_multiplier: int | None = None
    replay_template_manifest: str | None = None
    replay_workdir: str = "/"
    replay_env: dict[str, str] = field(default_factory=dict)
    replay_action_timeout: int = 300
    replay_delay_scale: float = 1.0
    replay_stop_on_error: bool = False
    replay_mode: str | None = None
    replay_running_concurrency: int | None = None
    replay_control_plane_qps: float | None = None
    replay_control_plane_inflight_cap: int | None = None
    replay_ready_probe: bool = True
    replay_lifecycle_retries: int = 2
    replay_launch_interval_sec: float = 0.0
    replay_pause_duration_sec: float = 0.0

    def __post_init__(self) -> None:
        # exec_only / exec_ephemeral have no lifecycle calls (exec_ephemeral is a
        # bare create->exec->kill per trajectory, B's no-lifecycle model); the
        # ready probe is meaningless there. Fires for both from_raw and direct
        # construction so the invariant holds without depending on the parsing path.
        if self.replay_mode in ("exec_only", "exec_ephemeral"):
            self.replay_ready_probe = False

    @classmethod
    def migrate(cls, raw: dict) -> dict:
        """Forward-compat: no legacy ``replay:`` renames yet."""
        return raw

    @classmethod
    def from_raw(cls, raw: dict) -> ReplayConfig:
        r = raw or {}
        return cls(
            replay_trajectory_dir=r.get("trajectory_dir", "trajectories"),
            replay_trajectory_glob=r.get("trajectory_glob", "*.replay.json"),
            replay_trajectory_limit=r.get("trajectory_limit"),
            replay_multiplier=r.get("multiplier"),
            replay_template_manifest=r.get("template_manifest"),
            replay_workdir=r.get("workdir", "/"),
            replay_env=r.get("env", {}),
            replay_action_timeout=r.get("action_timeout", 300),
            replay_delay_scale=r.get("delay_scale", 1.0),
            replay_stop_on_error=r.get("stop_on_error", False),
            replay_mode=r.get("mode"),
            replay_running_concurrency=r.get("running_concurrency"),
            replay_control_plane_qps=r.get("control_plane_qps"),
            replay_control_plane_inflight_cap=r.get("control_plane_inflight_cap"),
            replay_ready_probe=r.get("ready_probe", True),
            replay_lifecycle_retries=r.get("lifecycle_retries", 2),
            replay_launch_interval_sec=r.get("launch_interval_sec", 0.0),
            replay_pause_duration_sec=r.get("pause_duration_sec", 0.0),
        )

    def validate(self, kernel_config: KernelConfig) -> None:
        """Range checks + the sole cross-section (running_concurrency <= total_count)."""
        if self.replay_running_concurrency is not None:
            if self.replay_running_concurrency < 1:
                raise WorkflowConfigError(
                    f"replay_running_concurrency must be >= 1, got {self.replay_running_concurrency}"
                )
            if self.replay_running_concurrency > kernel_config.total_count:
                raise WorkflowConfigError(
                    f"replay_running_concurrency ({self.replay_running_concurrency}) must be <= "
                    f"total_count ({kernel_config.total_count})"
                )
        if self.replay_trajectory_limit is not None and self.replay_trajectory_limit < 1:
            raise WorkflowConfigError(f"replay_trajectory_limit must be >= 1, got {self.replay_trajectory_limit}")
        if self.replay_multiplier is not None and self.replay_multiplier < 1:
            raise WorkflowConfigError(f"replay_multiplier must be >= 1, got {self.replay_multiplier}")
        # multiplier is the per-sandbox trajectory count for fixed+exec_ephemeral;
        # it's a no-op elsewhere (the bound lives in the fixed runner's
        # exec_ephemeral branch; round_robin uses round_count). Warn so a stray
        # value under another mode surfaces instead of being silently ignored.
        if self.replay_multiplier is not None and (
            self.replay_mode != "exec_ephemeral" or kernel_config.benchmark_mode != "fixed"
        ):
            logger.warning(
                "replay.multiplier=%d set but mode=%r / benchmark_mode=%r -- consulted only "
                "in fixed+exec_ephemeral; ignoring.",
                self.replay_multiplier,
                self.replay_mode,
                kernel_config.benchmark_mode,
            )
        if self.replay_control_plane_qps is not None and self.replay_control_plane_qps <= 0:
            raise WorkflowConfigError(f"replay_control_plane_qps must be > 0, got {self.replay_control_plane_qps}")
        if self.replay_control_plane_inflight_cap is not None and self.replay_control_plane_inflight_cap < 1:
            raise WorkflowConfigError("replay_control_plane_inflight_cap must be >= 1")
        if self.replay_lifecycle_retries < 0:
            raise WorkflowConfigError(f"replay_lifecycle_retries must be >= 0, got {self.replay_lifecycle_retries}")
        if self.replay_launch_interval_sec < 0:
            raise WorkflowConfigError(f"replay_launch_interval_sec must be >= 0, got {self.replay_launch_interval_sec}")
        if self.replay_pause_duration_sec < 0:
            raise WorkflowConfigError(f"replay_pause_duration_sec must be >= 0, got {self.replay_pause_duration_sec}")
        if self.replay_mode not in (None, "exec_only", "exec_ephemeral", "lifecycle", "trajectory"):
            raise WorkflowConfigError(
                f"replay_mode must be None, 'exec_only', 'exec_ephemeral', 'lifecycle', or 'trajectory', "
                f"got {self.replay_mode!r}"
            )
