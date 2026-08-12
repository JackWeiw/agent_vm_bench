"""End-to-end tests for the host-agnostic benchmark spine.

Drives :func:`run_benchmark` with a :class:`FakeProvider` (no e2b/docker needed)
across the four exit shapes: round-robin benchmark, fixed benchmark, create-only,
and warmup-only. Plus the no-ready-sandboxes early exit.
"""
from __future__ import annotations

import os

from bench_core.bench import run_benchmark
from bench_core.config import KernelConfig
from env_provider import CreationMetrics, SandboxStatus
from env_provider.fake import FakeProvider


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
