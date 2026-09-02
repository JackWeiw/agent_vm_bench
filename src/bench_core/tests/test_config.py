"""KernelConfig tests (host-agnostic core fields only)."""
import pytest

from bench_core.config import KernelConfig


def test_defaults():
    c = KernelConfig()
    assert c.total_count == 100
    assert c.detect_existing is False
    assert c.create_only is False
    assert c.benchmark_mode == "fixed"
    assert c.workflow_type == "browser"
    assert c.test_duration == 600


def test_benchmark_count():
    c = KernelConfig(total_count=100, benchmark_percent=0.5)
    assert c.benchmark_count == 50


def test_benchmark_count_floor_is_one():
    c = KernelConfig(total_count=1, benchmark_percent=0.0)
    assert c.benchmark_count == 1


def test_validation_rejects_bad_workflow():
    with pytest.raises(ValueError):
        KernelConfig(workflow_type="bogus").validate()


def test_validation_rejects_bad_round_size():
    with pytest.raises(ValueError):
        KernelConfig(round_size=0).validate()


def test_create_batch_count_concurrent_when_unset():
    assert KernelConfig().create_batch_count == 1


def test_create_batch_count_rounds_up():
    c = KernelConfig(total_count=10, create_batch_size=3)
    assert c.create_batch_count == 4


# --- KernelConfig.from_raw: the shared loader (unified YAML schema) ---
# Mirrors the nested->flat mapping the e2b/docker Configs used to each carry.
# This is what load_config calls, so the kernel actually reads create_batch /
# test / browser / sandbox sections instead of running on defaults.


def _unified_raw(**overrides):
    """A raw dict in the unified schema shape (backend block omitted)."""
    raw = {
        "workflow_type": "browser",
        "sandbox": {"total_count": 100, "detect_existing": False, "create_only": False},
        "create_batch": {"size": 20, "interval": 3},
        "task_batch": {"size": 10, "interval": 5},
        "browser": {
            "urls": ["http://x/page.html"],
            "task_timeout": 200,
            "interval_min": 5,
            "interval_max": 15,
            "warmup_urls": ["http://x/w.html"],
            "warmup_loops": 1,
            "warmup_delay": 5,
            "warmup_only": False,
        },
        "test": {
            "duration": 160,
            "stats_interval": 10,
            "benchmark_percent": 1.0,
            "benchmark_mode": "round_robin",
            "round_size": 5,
            "round_count": 5,
            "round_interval": 5,
        },
        "report": {"output_dir": "results/common", "filename_prefix": "bench"},
    }
    raw.update(overrides)
    return raw


def test_from_raw_reads_nested_sections():
    c = KernelConfig.from_raw(_unified_raw())

    # sandbox -> shared control
    assert c.total_count == 100
    assert c.detect_existing is False
    assert c.create_only is False
    # create_batch / task_batch
    assert c.create_batch_size == 20
    assert c.create_batch_interval == 3
    assert c.task_batch_size == 10
    assert c.task_batch_interval == 5
    # browser (incl warmup)
    assert c.browser_urls == ["http://x/page.html"]
    assert c.browser_timeout == 200
    assert c.warmup_urls == ["http://x/w.html"]
    assert c.warmup_loops == 1
    # test (incl round-robin)
    assert c.test_duration == 160
    assert c.stats_interval == 10
    assert c.benchmark_mode == "round_robin"
    assert c.round_size == 5
    assert c.round_count == 5
    assert c.round_interval == 5
    # report
    assert c.output_dir == "results/common"
    assert c.filename_prefix == "bench"
    assert c.workflow_type == "browser"


def test_from_raw_missing_sections_use_defaults():
    # An empty dict (no shared sections) must not raise; every field falls back
    # to its KernelConfig default, so a backend-only YAML still loads.
    c = KernelConfig.from_raw({})

    assert c.total_count == 100  # default
    assert c.create_batch_size is None
    assert c.test_duration == 600
    assert c.benchmark_mode == "fixed"
    assert c.workflow_type == "browser"


def test_from_raw_partial_sections_fill_gaps():
    # Partial sections merge with defaults (only the keys present are applied).
    raw = {"sandbox": {"total_count": 7}, "test": {"duration": 30}}

    c = KernelConfig.from_raw(raw)

    assert c.total_count == 7
    assert c.test_duration == 30
    assert c.detect_existing is False  # default (key absent from sandbox)
    assert c.benchmark_mode == "fixed"  # default (key absent from test)


def test_from_raw_workflow_type_legacy_top_level():
    # workflow_type may appear top-level OR as workflow.type (legacy); both resolve.
    assert KernelConfig.from_raw({"workflow_type": "coding"}).workflow_type == "coding"
    assert KernelConfig.from_raw({"workflow": {"type": "document"}}).workflow_type == "document"


def test_from_raw_ignores_backend_block():
    # Backend sections (e2b: / docker:) are passed through untouched; the kernel
    # never reads them, so from_raw must not choke on or copy them.
    raw = _unified_raw(e2b={"template": "t", "numa_bind": 2}, docker={"image": "img"})

    c = KernelConfig.from_raw(raw)

    assert not hasattr(c, "template")
    assert not hasattr(c, "docker_image")


def test_from_raw_replay_section():
    raw = {
        "workflow_type": "replay",
        "replay": {
            "trajectory_dir": "trajectories/swe",
            "trajectory_glob": "*.replay.json",
            "workdir": "/testbed",
            "env": {"PAGER": "cat"},
            "action_timeout": 120,
            "delay_scale": 0.5,
            "stop_on_error": True,
            "mode": "exec_only",
        },
    }
    cfg = KernelConfig.from_raw(raw)
    assert cfg.workflow_type == "replay"
    assert cfg.replay_trajectory_dir == "trajectories/swe"
    assert cfg.replay_trajectory_glob == "*.replay.json"
    assert cfg.replay_workdir == "/testbed"
    assert cfg.replay_env == {"PAGER": "cat"}
    assert cfg.replay_action_timeout == 120
    assert cfg.replay_delay_scale == 0.5
    assert cfg.replay_stop_on_error is True
    assert cfg.replay_mode == "exec_only"


def test_validate_accepts_replay():
    cfg = KernelConfig(workflow_type="replay")
    cfg.validate()  # must not raise


def test_replay_defaults():
    cfg = KernelConfig(workflow_type="replay")
    assert cfg.replay_trajectory_dir == "trajectories"
    assert cfg.replay_trajectory_glob == "*.replay.json"
    assert cfg.replay_workdir == "/"
    assert cfg.replay_env == {}
    assert cfg.replay_action_timeout == 300
    assert cfg.replay_delay_scale == 1.0
    assert cfg.replay_stop_on_error is False
    assert cfg.replay_mode is None


def test_validate_accepts_replay_lifecycle():
    cfg = KernelConfig(workflow_type="replay", replay_mode="lifecycle")
    cfg.validate()  # must not raise


def test_validate_accepts_replay_none_sentinel():
    cfg = KernelConfig(workflow_type="replay", replay_mode=None)
    cfg.validate()  # must not raise (None = pre-resolution sentinel)


def test_validate_rejects_bad_replay_mode():
    cfg = KernelConfig(workflow_type="replay", replay_mode="bogus")
    with pytest.raises(ValueError, match="replay_mode"):
        cfg.validate()


def test_from_raw_replay_mode_none_when_absent():
    raw = {"workflow_type": "replay", "replay": {"trajectory_dir": "t"}}
    cfg = KernelConfig.from_raw(raw)
    assert cfg.replay_mode is None


# --- replay lifecycle / trajectory knobs (P1 Task 2) ---


def test_config_defaults_trajectory_retries_and_pacing():
    cfg = KernelConfig()
    assert cfg.replay_lifecycle_retries == 2
    assert cfg.replay_launch_interval_sec == 0.0
    assert cfg.replay_pause_duration_sec == 0.0


def test_config_validates_trajectory_mode():
    cfg = KernelConfig(workflow_type="replay", replay_mode="trajectory")
    cfg.validate()  # must not raise


def test_config_rejects_negative_retries():
    with pytest.raises(ValueError):
        KernelConfig(replay_lifecycle_retries=-1)


def test_config_rejects_negative_launch_interval():
    with pytest.raises(ValueError):
        KernelConfig(replay_launch_interval_sec=-0.1)


def test_config_rejects_negative_pause_duration():
    with pytest.raises(ValueError):
        KernelConfig(replay_pause_duration_sec=-0.5)


def test_config_from_raw_reads_trajectory_knobs():
    raw = {"replay": {"mode": "trajectory", "lifecycle_retries": 5, "launch_interval_sec": 1.5}}
    cfg = KernelConfig.from_raw(raw)
    assert cfg.replay_mode == "trajectory"
    assert cfg.replay_lifecycle_retries == 5
    assert cfg.replay_launch_interval_sec == 1.5
