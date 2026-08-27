"""End-to-end tests for the host-agnostic benchmark spine.

Drives :func:`run_benchmark` with a :class:`FakeProvider` (no e2b/docker needed)
across the four exit shapes: round-robin benchmark, fixed benchmark, create-only,
and warmup-only. Plus the no-ready-sandboxes early exit.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bench_core.bench import run_benchmark
from bench_core.config import KernelConfig
from env_provider import CreationMetrics, SandboxStatus
from env_provider.fake import FakeProvider

REPLAY_FIXTURES = Path(__file__).parent / "fixtures" / "replay"


class _NoReadyProvider(FakeProvider):
    """A FakeProvider whose instances never reach ready state."""

    def create_all(self):
        instances = super().create_all()
        for inst in instances.values():
            inst.ready = False
            inst.is_alive = False
            inst.creation_metrics = CreationMetrics(status=SandboxStatus.FAILED)
        return instances


class TestRunBenchmarkRoundRobin:
    def test_round_robin_completes_and_writes_report(self, tmp_path):
        config = KernelConfig(
            workflow_type="browser",
            total_count=3,
            benchmark_mode="round_robin",
            round_count=1,
            round_size=2,
            round_interval=0,
            test_duration=60,
            browser_urls=["http://x"],
            output_dir=str(tmp_path),
            filename_prefix="rr",
        )
        provider = FakeProvider(count=3)

        result = run_benchmark(config, provider)

        assert result["filepath"]
        assert os.path.isfile(result["filepath"])
        assert "Performance Report" in result["report"]
        assert "Browser Task Statistics" in result["report"]
        # Sandboxes were created (not detected) -> cleaned up at the end.
        assert provider.cleanup_called is True
        assert provider.prepare_env_calls == 1


class TestRunBenchmarkFixed:
    def test_fixed_mode_completes(self, tmp_path):
        config = KernelConfig(
            workflow_type="browser",
            total_count=2,
            benchmark_mode="fixed",
            test_duration=1,
            browser_urls=["http://x"],
            output_dir=str(tmp_path),
            filename_prefix="fixed",
        )
        provider = FakeProvider(count=2)

        result = run_benchmark(config, provider)

        assert os.path.isfile(result["filepath"])
        assert "Performance Report" in result["report"]
        assert provider.cleanup_called is True


class TestRunBenchmarkCreateOnly:
    def test_create_only_emits_timing_report_and_leaves_running(self, tmp_path):
        config = KernelConfig(
            workflow_type="browser",
            total_count=2,
            create_only=True,
            output_dir=str(tmp_path),
        )
        provider = FakeProvider(count=2)

        result = run_benchmark(config, provider)

        assert result["filepath"] is None
        assert "Creation Timing Report" in result["report"]
        # IDs persisted (provider hook fired once); sandboxes left running.
        assert provider.save_ids_calls == 1
        assert provider.cleanup_called is False


class TestRunBenchmarkWarmupOnly:
    def test_warmup_only_returns_early_and_persists_ids(self, tmp_path):
        config = KernelConfig(
            workflow_type="browser",
            total_count=2,
            warmup_only=True,
            warmup_urls=["http://x"],
            warmup_delay=0,
            warmup_loops=1,
            output_dir=str(tmp_path),
        )
        provider = FakeProvider(count=2)

        result = run_benchmark(config, provider)

        assert result["filepath"] is None
        assert "Warmup-only" in result["report"]
        assert provider.save_ids_calls == 1
        assert provider.cleanup_called is False


class TestRunBenchmarkCleanup:
    def test_cleanup_only_lists_kills_and_exits(self, tmp_path):
        # Simulate sandboxes left running by a prior --create-only / --detect run.
        config = KernelConfig(
            workflow_type="browser",
            total_count=2,
            cleanup_only=True,
            output_dir=str(tmp_path),
        )
        provider = FakeProvider(count=2)
        provider.create_all()  # populate the instances a prior run left running

        result = run_benchmark(config, provider)

        assert result["filepath"] is None
        assert "tore down 2" in result["report"]
        assert provider.prepare_env_calls == 1
        # The default cleanup_existing detect+kill path fired cleanup_all.
        assert provider.cleanup_called is True
        assert all(not inst.is_alive for inst in provider._instances.values())


class TestRunBenchmarkNoReady:
    def test_no_ready_sandboxes_returns_empty(self, tmp_path):
        config = KernelConfig(
            workflow_type="browser",
            total_count=2,
            output_dir=str(tmp_path),
        )
        provider = _NoReadyProvider(count=2)

        result = run_benchmark(config, provider)

        assert result == {}
        # Never reached the cleanup phase.
        assert provider.cleanup_called is False


def test_build_arg_parser_includes_replay_choice():
    from bench_core.bench import build_arg_parser

    args = build_arg_parser().parse_args(["--workflow-type", "replay"])
    assert args.workflow_type == "replay"


def test_load_config_replay_yaml():
    from bench_core.bench import load_config

    config, raw = load_config("config/common/replay.yaml")
    assert config.workflow_type == "replay"
    assert config.replay_trajectory_dir == "trajectories/swe-bench"
    assert config.replay_mode is None  # sentinel; resolved to provider default in run_benchmark


class TestRunBenchmarkReplayRoundRobin:
    def test_replay_round_robin_completes_and_writes_report(self, tmp_path):
        config = KernelConfig(
            workflow_type="replay",
            total_count=2,
            benchmark_mode="round_robin",
            round_count=1,
            round_size=2,
            round_interval=0,
            test_duration=60,
            replay_trajectory_dir=str(REPLAY_FIXTURES),
            replay_mode="exec_only",
            replay_delay_scale=0.0,
            output_dir=str(tmp_path),
            filename_prefix="rr_replay",
        )
        provider = FakeProvider(count=2)

        result = run_benchmark(config, provider)

        assert result["filepath"]
        assert os.path.isfile(result["filepath"])
        assert "Performance Report" in result["report"]
        assert "Replay Task Statistics" in result["report"]
        assert provider.cleanup_called is True
        assert provider.prepare_env_calls == 1


class TestRunBenchmarkReplayFixed:
    def test_replay_fixed_mode_completes(self, tmp_path):
        config = KernelConfig(
            workflow_type="replay",
            total_count=2,
            benchmark_mode="fixed",
            test_duration=1,
            replay_trajectory_dir=str(REPLAY_FIXTURES),
            replay_mode="exec_only",
            replay_delay_scale=0.0,
            output_dir=str(tmp_path),
            filename_prefix="fixed_replay",
        )
        provider = FakeProvider(count=2)

        result = run_benchmark(config, provider)

        assert os.path.isfile(result["filepath"])
        assert "Performance Report" in result["report"]
        assert "Replay Task Statistics" in result["report"]
        assert provider.cleanup_called is True


class _LifecycleCapableFake(FakeProvider):
    """FakeProvider that satisfies LifecycleCapable (pause/resume no-ops)."""

    def pause(self, inst):
        return None

    def resume(self, inst):
        return None


class TestRunBenchmarkReplayLifecycleStartup:
    def test_lifecycle_on_non_capable_provider_raises(self, tmp_path):
        config = KernelConfig(
            workflow_type="replay",
            total_count=1,
            benchmark_mode="fixed",
            test_duration=1,
            replay_trajectory_dir=str(REPLAY_FIXTURES),
            replay_mode="lifecycle",
            replay_delay_scale=0.0,
            output_dir=str(tmp_path),
            filename_prefix="lc_fail",
        )
        provider = FakeProvider(count=1)  # NOT LifecycleCapable
        with pytest.raises(ValueError, match="LifecycleCapable"):
            run_benchmark(config, provider)

    def test_lifecycle_on_capable_fake_resolves_and_runs(self, tmp_path):
        config = KernelConfig(
            workflow_type="replay",
            total_count=1,
            benchmark_mode="fixed",
            test_duration=1,
            replay_trajectory_dir=str(REPLAY_FIXTURES),
            replay_mode=None,  # sentinel -> resolve to provider default
            replay_delay_scale=0.0,
            output_dir=str(tmp_path),
            filename_prefix="lc_resolve",
        )
        provider = _LifecycleCapableFake(count=1)
        # default_replay_mode is exec_only (inherited) -> resolves to exec_only,
        # so lifecycle validation passes and the run completes.
        result = run_benchmark(config, provider)
        assert "Replay Task Statistics" in result["report"]
        assert config.replay_mode == "exec_only"  # resolved from provider default


def test_build_arg_parser_includes_aenv_provider():
    from bench_core.bench import build_arg_parser

    args = build_arg_parser().parse_args(["--provider", "aenv"])
    assert args.provider == "aenv"


def test_load_config_replay_yaml_has_aenv_block():
    from bench_core.bench import load_config

    config, raw = load_config("config/common/replay.yaml")
    assert "aenv" in raw
    assert raw["aenv"]["env"]["E2B_API_URL"] == "http://127.0.0.1:8000"
    # replay.mode is absent in the shared YAML -> provider default applies at
    # runtime (exec_only for e2b/docker/fake; lifecycle for aenv).
    assert "mode" not in raw["replay"]
