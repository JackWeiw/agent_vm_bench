"""Unit tests for the oversub benchmark driver's pure helpers.

The driver's pure helpers are module-level functions in bench_core.oversub,
so pytest imports them directly (no conftest path hack). The subprocess +
main() path is covered by the --dry-run integration test (Task 7).
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from bench_core.oversub import (
    build_trial_config,
    default_running_concurrency,
    parse_ratios,
)


def test_parse_ratios_basic():
    assert parse_ratios("1,2,3") == [1, 2, 3]


def test_parse_ratios_ignores_spaces_and_blanks():
    assert parse_ratios(" 1 , 2 ,3,") == [1, 2, 3]


def test_parse_ratios_rejects_non_positive():
    import pytest

    with pytest.raises(ValueError):
        parse_ratios("0,2")
    with pytest.raises(ValueError):
        parse_ratios("")


def _base_yaml() -> dict:
    return {
        "workflow_type": "replay",
        "sandbox": {"total_count": 384},
        "replay": {"running_concurrency": 384, "mode": "lifecycle"},
        "create_batch": {"size": 96, "interval": 1},
        "test": {"duration": 3600, "round_size": 384, "round_count": 1, "benchmark_mode": "round_robin"},
        "report": {"output_dir": "results/replay", "filename_prefix": "replay_bench", "format": "both"},
        "aenv": {"template": "t", "env": {"E2B_API_URL": "http://x"}},
    }


def test_default_running_concurrency_from_replay():
    assert default_running_concurrency(_base_yaml()) == 384


def test_default_running_concurrency_falls_back_to_total_count():
    base = _base_yaml()
    del base["replay"]["running_concurrency"]
    base["sandbox"]["total_count"] = 200
    assert default_running_concurrency(base) == 200


def test_build_trial_config_overrides_and_preserves_base():
    base = _base_yaml()
    cfg = build_trial_config(
        base,
        mode="exec_only",
        ratio=2,
        n=384,
        test_duration=600,
        trial_dir="out/t1",
        prefix="replay_bench",
    )
    # Overrides applied.
    assert cfg["sandbox"]["total_count"] == 768  # k*N = 2*384
    assert cfg["replay"]["running_concurrency"] == 384  # N stays fixed
    assert cfg["replay"]["mode"] == "exec_only"
    assert cfg["test"]["round_size"] == 768  # scale with total_count
    assert cfg["test"]["round_count"] == 0  # sustained until duration
    assert cfg["test"]["duration"] == 600
    assert cfg["report"]["output_dir"] == "out/t1"
    assert cfg["report"]["filename_prefix"] == "replay_bench"
    # Base fields preserved (backend block, create_batch pass through).
    assert cfg["aenv"]["template"] == "t"
    assert cfg["create_batch"] == {"size": 96, "interval": 1}
    # The base dict is not mutated (deep copy).
    assert base["sandbox"]["total_count"] == 384
    assert base["replay"]["mode"] == "lifecycle"


def test_build_trial_config_round_yaml_round_trips():
    """Generated trial.yaml must load back to the same overrides (real subprocess input)."""
    base = _base_yaml()
    cfg = build_trial_config(
        base,
        mode="lifecycle",
        ratio=3,
        n=384,
        test_duration=120,
        trial_dir="out/t2",
        prefix="rb",
    )
    text = yaml.safe_dump(cfg, sort_keys=False)
    reloaded = yaml.safe_load(text)
    assert reloaded["sandbox"]["total_count"] == 1152
    assert reloaded["test"]["round_size"] == 1152
    assert reloaded["test"]["round_count"] == 0
    assert reloaded["replay"]["running_concurrency"] == 384


def test_default_running_concurrency_rejects_bad():
    import pytest

    # absent -> 0 -> ValueError
    with pytest.raises(ValueError):
        default_running_concurrency({})
    # present-but-null total_count -> coerced to 0 -> ValueError (Fix 1)
    with pytest.raises(ValueError):
        default_running_concurrency({"sandbox": {"total_count": None}})
    # explicit 0 -> ValueError
    with pytest.raises(ValueError):
        default_running_concurrency({"sandbox": {"total_count": 0}})


from bench_core.oversub import compute_valid, parse_run_summary


def _summary(total=768, succeeded=768, failed=0, wall_sec=940.0, peak_active=380, maximum=384, mode="lifecycle"):
    return {
        "replay_mode": mode,
        "wall_sec": wall_sec,
        "throughput": {"total": total, "succeeded": succeeded, "failed": failed},
        "admission": {"maximum": maximum, "peak_active": peak_active} if peak_active is not None else None,
    }


def test_parse_run_summary_round_trips(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_summary()), encoding="utf-8")
    assert parse_run_summary(p)["throughput"]["total"] == 768


def test_compute_valid_happy_path():
    s = _summary()
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is True


def test_compute_valid_nonzero_return_code():
    s = _summary()
    assert compute_valid(s, return_code=2, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_no_work_done():
    s = _summary(total=0, succeeded=0)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_exited_early_wall_below_threshold():
    # wall_sec 100 < 0.9*1000 -> did not sustain the window -> invalid.
    s = _summary(wall_sec=100.0)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_peak_active_exceeds_n():
    s = _summary(peak_active=400)  # 400 > N=384 -> over-admitted -> invalid.
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False


def test_compute_valid_exec_only_drops_peak_active_clause():
    # exec_only: admission is null -> peak_active clause skipped; only wall+total+rc gate.
    s = _summary(peak_active=None, mode="exec_only", wall_sec=950.0, total=10)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is True


def test_compute_valid_failure_tolerance_allows_wrap_noise():
    # 10 failures / 768 total = ~1.3%; tolerance 0.05 -> valid; tolerance 0.0 -> invalid.
    s = _summary(total=768, succeeded=758, failed=10, wall_sec=950.0)
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.05) is True
    assert compute_valid(s, return_code=0, n=384, test_duration=1000, failure_tolerance=0.0) is False
