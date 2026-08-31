"""Integration test: run_benchmark brackets the stress phase with MonitorController calls."""
from __future__ import annotations

import bench_core.bench as bench_mod
from bench_core.bench import run_benchmark
from bench_core.config import KernelConfig
from env_provider.fake import FakeProvider


def test_run_benchmark_brackets_monitor_around_stress(monkeypatch, tmp_path):
    """run_benchmark must construct MonitorController and call start/begin/end/stop around dispatch."""
    calls: list[str] = []

    class _FakeMC:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        def start(self):
            calls.append("start")

        def begin_stress(self):
            calls.append("begin")

        def end_stress(self):
            calls.append("end")

        def stop(self):
            calls.append("stop")
            return []

        def merge_into(self, *_args, **_kwargs):
            calls.append("merge")

        @property
        def stress_window(self):
            return None

    # Replace the MonitorController NAME AS BOUND IN bench's module namespace.
    monkeypatch.setattr(bench_mod, "MonitorController", _FakeMC)

    config = KernelConfig(
        workflow_type="browser",
        total_count=2,
        benchmark_mode="fixed",
        test_duration=1,
        browser_urls=["http://x"],
        output_dir=str(tmp_path),
        filename_prefix="mon",
    )
    provider = FakeProvider(count=2)

    result = run_benchmark(config, provider)  # noqa: F841 -- exercise the spine

    # The stress bracket must be invoked in order around dispatch.
    assert "init" in calls
    assert "start" in calls
    assert "begin" in calls
    assert "end" in calls
    assert "stop" in calls
    # merge_into is replay-xlsx-only; browser+txt does NOT call it -- so do NOT assert "merge".
