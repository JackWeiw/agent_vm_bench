"""Incremental coverage for the PDF/XLSX document workflow."""

import threading
from unittest.mock import Mock, patch

import pytest

from e2b_bench.bench import run_benchmark
from e2b_bench.config import Config
from e2b_bench.document_task_runner import (
    DOCUMENT_MAX_REPAIR_ATTEMPTS,
    DocumentOperationExecutor,
    DocumentTaskRunner,
    SceneRecipeError,
    get_document_operations_path,
    load_scene_recipe,
)
from e2b_bench.metrics_extractor import MetricsExtractor
from e2b_bench.report_aggregator import ReportAggregator
from e2b_bench.schemas import (
    BrowserMetrics,
    CodingMetrics,
    DocumentMetrics,
    SandboxState,
    SandboxStatus,
    get_step_order,
)
from e2b_bench.stats_collector import StatsCollector
from e2b_bench.task_runner import BrowserTaskRunner, TaskManager


class Result:
    exit_code = 0
    stdout = ""
    stderr = ""


class FakeSandbox:
    def __init__(self):
        self.commands = Mock()
        self.commands.run.return_value = Result()
        self.files = Mock()


def document_config(case_kind="pdf"):
    if case_kind == "pdf":
        return Config(
            workflow_type="document",
            document_case_kind="pdf",
            document_operation_timeout=300,
            document_recalc_timeout=180,
            document_task_timeout=900,
        )
    return Config(workflow_type="document", document_case_kind="xlsx")


@pytest.mark.parametrize("case_kind,count", [("pdf", 14), ("xlsx", 12)])
def test_fixed_recipes_load_and_validate(case_kind, count):
    recipe = load_scene_recipe(case_kind)
    assert recipe["operation_count"] == count
    assert get_document_operations_path(case_kind).is_file()


def test_invalid_case_kind_is_rejected():
    with pytest.raises(SceneRecipeError):
        load_scene_recipe("docx")


def test_repair_policy_is_fixed_by_case_kind():
    expected = {"pdf": 0, "xlsx": 1}
    assert expected == DOCUMENT_MAX_REPAIR_ATTEMPTS


def test_workspace_restore_has_no_image_recipe_or_sha_check():
    executor = DocumentOperationExecutor(SandboxState(1, workflow_type="document"), document_config())
    sandbox = FakeSandbox()
    ok, _detail = executor.prepare_workspace(sandbox)
    command = sandbox.commands.run.call_args.args[0]
    assert ok
    assert "cp -a /opt/document-bench/pdf" in command
    assert "manifest.json" not in command
    assert "sha256" not in command.lower()


def test_pdf_complete_path_and_set_stop_event_does_not_interrupt_active_task():
    stop_event = threading.Event()
    stop_event.set()
    executor = DocumentOperationExecutor(SandboxState(1, workflow_type="document"), document_config(), stop_event)
    success, _latency, steps, timed_out, detail = executor.execute(FakeSandbox())
    assert success and not timed_out and not detail
    assert list(steps) == get_step_order("document", "pdf")


def test_xlsx_failure_uses_single_fixed_repair_path():
    executor = DocumentOperationExecutor(SandboxState(1, workflow_type="document"), document_config("xlsx"))
    calls = []

    def operation(_sandbox, operation_id, step_times):
        calls.append(operation_id)
        step_times[operation_id] = 0.01
        if operation_id == "XLSX-K07-validate_workbook" and calls.count(operation_id) == 1:
            return False, "expected trigger"
        return True, ""

    with patch.object(executor, "prepare_workspace", return_value=(True, "")), patch.object(
        executor, "_execute_operation", side_effect=operation
    ), patch.object(executor, "_validate_business_result", return_value=(True, "")):
        success, _latency, _steps, timed_out, _detail = executor.execute(FakeSandbox())
    assert success and not timed_out
    assert "XLSX-K08-repair_workbook" in calls
    assert calls[-4:] == [
        "XLSX-K09-export_summary_csvs",
        "XLSX-K10-verify_business_rules",
        "XLSX-K11-generate_summary",
        "XLSX-K12-check_deliverables",
    ]


def test_complete_task_timeout_is_recorded():
    config = document_config()
    config.document_operation_timeout = 1
    config.document_task_timeout = 2
    executor = DocumentOperationExecutor(SandboxState(1, workflow_type="document"), config)
    with patch("e2b_bench.document_task_runner.time.monotonic", side_effect=[0.0, 3.0]):
        success, _latency, _steps, timed_out, detail = executor.execute(FakeSandbox())
    assert not success and timed_out
    assert "exceeded" in detail


def test_file_write_uses_remaining_document_deadline_as_request_timeout():
    executor = DocumentOperationExecutor(SandboxState(1, workflow_type="document"), document_config())
    executor.deadline = 110.0
    sandbox = FakeSandbox()

    with patch("e2b_bench.document_task_runner.time.monotonic", return_value=100.0):
        success, detail = executor._execute_tool_call(
            sandbox,
            "write",
            {"path": "/tmp/document/helper.py", "content": "print('ok')\n"},
        )

    assert success and not detail
    sandbox.files.write.assert_called_once_with(
        "/tmp/document/helper.py",
        "print('ok')\n",
        user="root",
        request_timeout=10,
    )


def test_task_manager_dispatch_and_shared_document_deadline():
    config = document_config()
    state = SandboxState(1, workflow_type="document")
    manager = TaskManager(config, {1: state}, threading.Event())
    assert isinstance(manager._create_task_runner(state), DocumentTaskRunner)

    config.document_operation_timeout = 0.001
    config.document_task_timeout = 0.002
    stuck = Mock()
    stuck.name = "stuck-document"
    stuck.is_alive.return_value = True
    manager.runners = [stuck]
    with pytest.raises(RuntimeError, match="did not finish"):
        manager.wait_all()


def test_metrics_dispatch_report_extraction_and_column_groups(tmp_path):
    for workflow, metrics_type in (
        ("browser", BrowserMetrics),
        ("coding", CodingMetrics),
        ("document", DocumentMetrics),
    ):
        assert isinstance(SandboxState(1, workflow_type=workflow).task_metrics, metrics_type)
    with pytest.raises(ValueError, match="Unsupported workflow_type"):
        _ = SandboxState(1, workflow_type="unknown").task_metrics

    config = document_config()
    state = SandboxState(1, workflow_type="document")
    state.creation_metrics.status = SandboxStatus.PORT_READY
    state.document_metrics.add(1.25, True, step_times={"PDF-K01-read_requirements": 0.25})
    report = StatsCollector(config, {1: state}).generate_report()
    report_file = tmp_path / "bench_report.txt"
    report_file.write_text(report, encoding="utf-8")
    metrics = MetricsExtractor().extract_document_metrics(str(report_file))
    assert metrics["Document_Case_Kind"] == "pdf"
    assert metrics["Document_Total_Tasks"] == 1
    assert metrics["Document_PDF-K01-read_requirements_Count"] == 1

    aggregator = ReportAggregator()
    assert aggregator._find_column_group("Document_Success_Rate") == "Document"
    assert aggregator._find_column_group("Document_PDF-K01-read_requirements_Avg_ms") == "Document_Steps"
    assert aggregator._find_column_group("Document_PDF-K01_Avg_ms") == "Document_Steps"
    assert aggregator._find_column_group("Coding_verify_Avg_ms") == "Coding_Steps"
    assert aggregator._find_column_group("Browser_snapshot_Avg_ms") == "Browser_Steps"


def test_legacy_document_step_ids_are_normalized_during_extraction(tmp_path):
    report_file = tmp_path / "legacy_document_report.txt"
    report_file.write_text(
        """[Document Task Statistics]
  Case Kind:     pdf
  Total Tasks:   1
  Success Rate:  100.0%

[Step-Level Timing (Document PDF Mode)]
Step     Count  Avg(ms)  P50(ms)  P95(ms)  P99(ms)
PDF-K01  1      250.0    250.0    250.0    250.0
================================================================================
""",
        encoding="utf-8",
    )
    metrics = MetricsExtractor().extract_document_metrics(str(report_file))
    assert metrics["Document_PDF-K01-read_requirements_Count"] == 1
    assert "Document_PDF-K01_Count" not in metrics


def test_unknown_task_runner_does_not_fall_back_to_browser():
    manager = TaskManager(Config(workflow_type="unknown"), {}, threading.Event())
    with pytest.raises(ValueError, match="Unsupported workflow_type"):
        manager._create_task_runner(SandboxState(1))


def test_document_preflight_runs_before_sandbox_manager_construction():
    with (
        patch(
            "e2b_bench.document_task_runner.preflight_document",
            side_effect=SceneRecipeError("invalid fixed recipe"),
        ),
        patch("e2b_bench.bench.SandboxManager") as manager,
        pytest.raises(SceneRecipeError, match="invalid fixed recipe"),
    ):
        run_benchmark(document_config())
    manager.assert_not_called()


def test_browser_task_runner_dispatch_remains_unchanged():
    state = SandboxState(1)
    browser = TaskManager(Config(), {1: state}, threading.Event())._create_task_runner(state)
    assert isinstance(browser, BrowserTaskRunner)
