"""Phase 3: XlsxReportRenderer produces the expected sheets/columns."""
from __future__ import annotations

import pytest

openpyxl = pytest.importorskip("openpyxl", reason="openpyxl is a core dep")
from openpyxl import load_workbook  # noqa: E402

from bench_core.config import KernelConfig  # noqa: E402
from bench_core.observability import ReplayObservability  # noqa: E402
from bench_core.obs_xlsx import XlsxReportRenderer  # noqa: E402
from bench_core.schemas import BenchSandbox, ReplayMetrics  # noqa: E402
from env_provider import SandboxInstance  # noqa: E402


def _seeded_observability(*, replay_mode="trajectory", with_retry=True) -> tuple[ReplayObservability, KernelConfig]:
    state = BenchSandbox.from_instance(SandboxInstance(id="x", index=0), "replay")
    m = ReplayMetrics()
    for i in range(3):
        m.add(
            latency=0.1 + i * 0.05,
            success=True,
            action_type="shell",
            resume_sec=0.05,
            pause_sec=0.05,
            slice_total_sec=1.0,
            running_slot_held_sec=0.8,
            interaction_total_sec=1.0,
            create_sec=1.0 + i * 0.5,
            kill_sec=0.5 + i * 0.1,
        )
    if with_retry:
        m.record_retry_event("retry_queued", operation="resume", time_lost_sec=0.05)
        m.append_retries_per_slice(1)
    state.replay_metrics = m
    cfg = KernelConfig(
        workflow_type="replay",
        replay_mode=replay_mode,
        total_count=2,
        replay_running_concurrency=1,
        test_duration=300,
    )
    obs = ReplayObservability(
        cfg,
        {0: state},
        admission_snapshot={
            "running": 1,
            "total": 2,
            "qps": 100.0,
            "running_slots": {
                "maximum": 1,
                "active": 0,
                "peak_active": 1,
                "granted": 3,
                "average_queue_wait_sec": 0.01,
                "waiting": 0,
            },
            "qps_limiter": {
                "qps": 100.0,
                "inflight_cap": 4,
                "in_flight": 0,
                "dispatched": 9,
                "average_wait_sec": 0.001,
                "max_wait_sec": 0.01,
                "dispatched_by_operation": {
                    "resume": 3,
                    "pause": 3,
                    "cleanup": 0,
                    "create": 3,
                    "command": 0,
                },
                "waiting": 0,
                "waiting_by_operation": {
                    "resume": 0,
                    "pause": 0,
                    "cleanup": 0,
                    "create": 0,
                    "command": 0,
                },
            },
        },
        wall_sec=10.0,
    )
    return obs, cfg


class TestXlsxReportRenderer:
    def test_produces_all_expected_sheets(self, tmp_path):
        obs, _ = _seeded_observability()
        path = tmp_path / "obs.xlsx"
        XlsxReportRenderer(obs).render(str(path))
        wb = load_workbook(str(path))
        names = wb.sheetnames
        for sheet in (
            "Overview",
            "Per-step timings",
            "Lifecycle overhead",
            "Admission & QPS",
            "Throughput & overcommit",
            "Trajectory summary",
            "Retry impact",
        ):
            assert sheet in names, f"missing sheet {sheet}; got {names}"

    def test_admission_sheet_has_per_op_columns(self, tmp_path):
        obs, _ = _seeded_observability()
        path = tmp_path / "obs.xlsx"
        XlsxReportRenderer(obs).render(str(path))
        wb = load_workbook(str(path))
        ws = wb["Admission & QPS"]
        headers = [c.value for c in ws[1]]
        assert "operation" in headers
        assert "dispatched" in headers

    def test_throughput_sheet_has_metrics(self, tmp_path):
        obs, _ = _seeded_observability()
        path = tmp_path / "obs.xlsx"
        XlsxReportRenderer(obs).render(str(path))
        wb = load_workbook(str(path))
        ws = wb["Throughput & overcommit"]
        rows = [[c.value for c in r] for r in ws.iter_rows()]
        # has steps_per_sec / overcommit_ratio rows
        joined = " ".join(str(v) for row in rows for v in row)
        assert "steps_per_sec" in joined
        assert "overcommit_ratio" in joined

    def test_retry_impact_sheet_present_when_retries_exist(self, tmp_path):
        obs, _ = _seeded_observability(with_retry=True)
        path = tmp_path / "obs.xlsx"
        XlsxReportRenderer(obs).render(str(path))
        wb = load_workbook(str(path))
        ws = wb["Retry impact"]
        rows = [[c.value for c in r] for r in ws.iter_rows()]
        joined = " ".join(str(v) for row in rows for v in row)
        assert "retry_count" in joined

    def test_trajectory_sheet_omitted_content_when_not_trajectory(self, tmp_path):
        # lifecycle mode: Trajectory summary sheet exists but is empty/flagged
        obs, _ = _seeded_observability(replay_mode="lifecycle")
        path = tmp_path / "obs.xlsx"
        XlsxReportRenderer(obs).render(str(path))
        wb = load_workbook(str(path))
        assert "Trajectory summary" in wb.sheetnames
        ws = wb["Trajectory summary"]
        # no create_sec data in lifecycle mode -> header only
        assert ws.max_row == 1
