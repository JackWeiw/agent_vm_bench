"""Golden config-compatibility test (RFC 0002 Phase 0).

Loads every shipped ``config/common/*.yaml``, builds ``KernelConfig.from_raw``, and
asserts the resolved per-workflow field values are bit-for-bit what they are today.
This pins current behavior so Phase 2's per-workflow config-field moves onto typed
``WorkflowConfigBase`` views cannot silently shift a resolved value: same YAML in,
identical resolved config out.

P2-2 moved the per-workflow fields off ``KernelConfig`` onto the typed views, so the
golden dict splits each entry into ``shared`` (read off ``cfg``) and ``view`` (read
off ``cfg.workflow_config``). ``test_moved_fields_are_gone_from_kernel_config`` pins
that the moved names no longer surface on ``KernelConfig`` (zombie-field guard).
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from bench_core.config import KernelConfig
from bench_core.task_runner.browser import BrowserConfig
from bench_core.task_runner.coding import CodingConfig
from bench_core.task_runner.document import DocumentConfig
from bench_core.task_runner.replay import ReplayConfig
from bench_core.workflow_registry import WorkflowConfigError

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "common"


def _load(name: str) -> KernelConfig:
    raw = yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
    return KernelConfig.from_raw(raw)


# Each entry: ``shared`` = resolved kernel fields (read off ``cfg``); ``view`` =
# resolved per-workflow fields (read off ``cfg.workflow_config``). ``warmup_only``
# stays shared (D3); the warmup URLs/loops/delay moved onto BrowserConfig.
EXPECTED = {
    "browser.yaml": dict(
        shared=dict(
            workflow_type="browser",
            total_count=100,
            create_batch_size=20,
            create_batch_interval=3,
            task_batch_size=10,
            task_batch_interval=5,
            warmup_only=False,
            test_duration=160,
            benchmark_mode="round_robin",
            round_size=5,
            round_count=5,
            round_interval=5,
            output_dir="results/browser",
            filename_prefix="browser_bench",
            report_format="txt",
        ),
        view=dict(
            browser_urls=["http://192.168.110.10:8080/Hubble_Space_Telescope.html"],
            browser_timeout=200,
            browser_interval_min=5,
            browser_interval_max=15,
            warmup_loops=1,
            warmup_delay=5,
        ),
    ),
    "docker.yaml": dict(
        shared=dict(
            workflow_type="browser",
            total_count=10,
            create_batch_size=5,
            create_batch_interval=10,
            test_duration=160,
            benchmark_mode="fixed",
            round_size=5,
            output_dir="results/docker",
            filename_prefix="docker_bench",
            report_format="txt",
        ),
        view=dict(
            browser_urls=["http://192.168.110.10:8080/Weibo.html"],
            browser_timeout=200,
            browser_interval_min=5,
            browser_interval_max=15,
            # No warmup_urls block -> defaults (empty list, loops=2, delay=10).
            warmup_urls=[],
            warmup_loops=2,
            warmup_delay=10,
        ),
    ),
    "coding-ts.yaml": dict(
        shared=dict(
            workflow_type="coding",
            total_count=10,
            benchmark_mode="round_robin",
            round_count=20,
            round_interval=3,
            output_dir="results/coding/ts",
            filename_prefix="coding_ts_bench",
        ),
        view=dict(
            coding_language="ts",
            coding_project_dir="/opt/coding-bench",
            coding_verify_cmd="npx tsx /tmp/bench_verify.mjs",
            coding_verify_timeout=120,
            coding_verify_repeat=3,
            coding_skip_verify=False,
            coding_interval_min=2.0,
            coding_interval_max=10.0,
        ),
    ),
    "coding-go.yaml": dict(
        shared=dict(
            workflow_type="coding",
            total_count=10,
            output_dir="results/coding/go",
            filename_prefix="coding_go_bench",
        ),
        view=dict(
            coding_language="go",
            coding_verify_cmd="go run /tmp/bench_verify.go",
            coding_verify_repeat=1,
            coding_interval_min=2.0,
            coding_interval_max=10.0,
        ),
    ),
    "coding-python.yaml": dict(
        shared=dict(
            workflow_type="coding",
            total_count=10,
            output_dir="results/coding/python",
            filename_prefix="coding_python_bench",
        ),
        view=dict(
            coding_language="python",
            coding_verify_cmd="python3 /tmp/bench_verify.py",
            coding_verify_repeat=1,
        ),
    ),
    "replay.yaml": dict(
        shared=dict(
            workflow_type="replay",
            total_count=384,
            benchmark_mode="round_robin",
            round_size=384,
            round_count=1,
            round_interval=0,
            output_dir="results/replay",
            filename_prefix="replay_bench",
            report_format="both",
        ),
        view=dict(
            replay_trajectory_dir="trajectories/swe-bench",
            replay_trajectory_glob="*.replay.json",
            replay_workdir="/",
            replay_action_timeout=300,
            replay_delay_scale=1.0,
            replay_stop_on_error=False,
            replay_mode="lifecycle",
            replay_running_concurrency=384,
            replay_ready_probe=True,  # lifecycle keeps the probe
            replay_lifecycle_retries=2,
            replay_pause_duration_sec=0.0,
        ),
    ),
    "replay-exec-only.yaml": dict(
        shared=dict(
            workflow_type="replay",
            total_count=384,
            output_dir="results/replay-exec-only",
            filename_prefix="replay_exec_only_bench",
            report_format="both",
        ),
        view=dict(
            replay_mode="exec_only",
            replay_action_timeout=300,
            replay_running_concurrency=384,
            replay_control_plane_qps=1000.0,
            replay_control_plane_inflight_cap=1024,
            # ReplayConfig.__post_init__ force-disables the ready probe in exec_only.
            replay_ready_probe=False,
            replay_lifecycle_retries=2,
        ),
    ),
    "replay-trajectory.yaml": dict(
        shared=dict(
            workflow_type="replay",
            total_count=384,
            output_dir="results/replay-trajectory",
            filename_prefix="replay_trajectory_bench",
            report_format="both",
        ),
        view=dict(
            replay_mode="trajectory",
            replay_action_timeout=300,
            replay_running_concurrency=384,
            replay_control_plane_qps=1000.0,
            replay_control_plane_inflight_cap=1024,
            replay_launch_interval_sec=0.5,
            replay_ready_probe=True,
        ),
    ),
}


_VIEW_BY_WORKFLOW = {
    "browser": BrowserConfig,
    "coding": CodingConfig,
    "document": DocumentConfig,
    "replay": ReplayConfig,
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_config_resolves_to_golden_values(name: str):
    cfg = _load(name)
    assert isinstance(cfg.workflow_config, _VIEW_BY_WORKFLOW[cfg.workflow_type])
    for field_name, expected in EXPECTED[name]["shared"].items():
        actual = getattr(cfg, field_name)
        assert actual == expected, f"{name}: {field_name} = {actual!r}, expected {expected!r}"
    for field_name, expected in EXPECTED[name]["view"].items():
        actual = getattr(cfg.workflow_config, field_name)
        assert actual == expected, f"{name}: view.{field_name} = {actual!r}, expected {expected!r}"


@pytest.mark.parametrize("name", ["coding-ts.yaml", "coding-go.yaml", "coding-python.yaml"])
def test_coding_source_files_resolved_by_post_init(name: str):
    """coding_source_files omitted -> CodingConfig.__post_init__ fills the canonical pairs."""
    cfg = _load(name)
    assert isinstance(cfg.workflow_config, CodingConfig)
    assert cfg.workflow_config.coding_source_files is not None
    assert len(cfg.workflow_config.coding_source_files) > 0


# P2-2: the per-workflow fields moved off KernelConfig onto the typed views. A
# forgotten flat def (zombie) surfaces here as a leftover attribute on cfg.
_MOVED_FLAT_FIELDS = (
    "browser_urls",
    "browser_timeout",
    "browser_interval_min",
    "browser_interval_max",
    "warmup_urls",
    "warmup_loops",
    "warmup_delay",
    "coding_project_dir",
    "coding_language",
    "coding_source_files",
    "coding_verify_cmd",
    "coding_verify_timeout",
    "coding_skip_verify",
    "coding_verify_repeat",
    "coding_interval_min",
    "coding_interval_max",
    "document_case_kind",
    "document_recipe_path",
    "document_operation_timeout",
    "document_recalc_timeout",
    "document_task_timeout",
    "document_interval_min",
    "document_interval_max",
    "document_seed_dir",
    "document_workspace_dir",
    "replay_trajectory_dir",
    "replay_trajectory_glob",
    "replay_template_manifest",
    "replay_workdir",
    "replay_env",
    "replay_action_timeout",
    "replay_delay_scale",
    "replay_stop_on_error",
    "replay_mode",
    "replay_running_concurrency",
    "replay_control_plane_qps",
    "replay_control_plane_inflight_cap",
    "replay_ready_probe",
    "replay_lifecycle_retries",
    "replay_launch_interval_sec",
    "replay_pause_duration_sec",
)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_moved_fields_are_gone_from_kernel_config(name: str):
    """P2-2: the per-workflow fields moved off KernelConfig onto the views.

    A zombie flat def (forgotten deletion) surfaces here as a leftover attribute.
    ``warmup_only`` stays shared (D3), so it is the one name asserted present.
    """
    cfg = _load(name)
    for field_name in _MOVED_FLAT_FIELDS:
        assert not hasattr(cfg, field_name), f"{name}: KernelConfig still carries {field_name}"
    assert hasattr(cfg, "warmup_only")


def test_document_config_from_inline_dict():
    """D4: no shipped document.yaml, so DocumentConfig is covered by an inline dict."""
    cfg = KernelConfig.from_raw(
        {
            "workflow_type": "document",
            "document": {"case_kind": "pdf", "operation_timeout": 1200, "recalc_timeout": 700},
        }
    )
    assert isinstance(cfg.workflow_config, DocumentConfig)
    assert cfg.workflow_config.document_case_kind == "pdf"
    assert cfg.workflow_config.document_operation_timeout == 1200
    assert cfg.workflow_config.document_recalc_timeout == 700
    assert cfg.workflow_config.document_task_timeout == 1800  # default


def test_illegal_workflow_type_raises():
    """Unknown workflow_type -> friendly WorkflowConfigError (from_raw registry miss)."""
    raw = {"workflow_type": "nonexistent", "sandbox": {"total_count": 1}}
    with pytest.raises(WorkflowConfigError, match="Unsupported workflow_type"):
        KernelConfig.from_raw(raw)


def test_replay_invalid_qps_raises():
    """Invalid per-workflow value raises WorkflowConfigError (ReplayConfig.validate fires
    at from_raw; P2-2 moved the range checks off the deleted flat __post_init__)."""
    raw = {"workflow_type": "replay", "sandbox": {"total_count": 10}, "replay": {"control_plane_qps": 0}}
    with pytest.raises(WorkflowConfigError, match="replay_control_plane_qps"):
        KernelConfig.from_raw(raw)


def test_replay_config_validate_cross_section():
    """The sole cross-section check (running_concurrency <= total_count) lives on the view."""
    view = ReplayConfig(replay_running_concurrency=50)
    cfg = KernelConfig(total_count=10)  # direct construction -> workflow_config stays None
    with pytest.raises(WorkflowConfigError, match="must be <= total_count"):
        view.validate(cfg)


def test_cli_workflow_type_override_selects_right_view():
    """D7: --workflow-type applies to raw before from_raw so the right config_cls view is built."""
    from bench_core.bench import load_config

    cfg, _ = load_config(CONFIG_DIR / "browser.yaml", workflow_type_override="coding")
    assert cfg.workflow_type == "coding"
    assert isinstance(cfg.workflow_config, CodingConfig)
