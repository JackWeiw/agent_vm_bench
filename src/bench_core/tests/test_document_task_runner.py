"""Tests for the document workflow runners (host-agnostic).

A small synthetic ``scene-key-operations-v2`` recipe stands in for the real 27KB
trace recipe so the unit tests stay fast and focused on the exec-only port
(notably the ``sbx.files.write`` -> base64-heredoc-via-exec conversion). Runners
are driven through a :class:`FakeProvider`; thread bodies are invoked directly.
"""
from __future__ import annotations

import base64
import json
import threading

from bench_core.config import KernelConfig
from bench_core.document_task_runner import (
    DocumentOperationExecutor,
    DocumentRoundRunner,
    DocumentTaskRunner,
    DocumentWarmupRunner,
    SceneRecipeError,
    _build_write_command,
    load_scene_recipe,
    preflight_document,
)
from bench_core.provider import CommandResult
from bench_core.schemas import BenchSandbox
from bench_core.tests.fake_provider import FakeProvider

XLSX_PHASES = [
    "XLSX-P01-inspect_prepare",
    "XLSX-P02-build",
    "XLSX-P03-process_publish",
    "XLSX-P04-verify_deliver",
]


def _make_recipe(path, *, case_kind="xlsx", phase_ids=None, success_path=None, extra=None):
    """Write a minimal valid scene-key-operations-v2 recipe; return the path."""
    phase_ids = list(phase_ids or XLSX_PHASES)
    success_path = list(success_path or phase_ids)
    phases = []
    for pid in phase_ids:
        phases.append(
            {
                "operation_id": pid,
                "source_tool_calls": [
                    {"tool_call": {"function_name": "exec", "arguments": {"command": f"echo {pid}"}}}
                ],
            }
        )
    recipe = {
        "schema_version": "scene-key-operations-v2",
        "case_kind": case_kind,
        "operation_count": len(phases),
        "key_operations": phases,
        "workflow": {"success_path": success_path},
    }
    if extra:
        recipe.update(extra)
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return path


def _ready_sandbox(index: int = 0) -> BenchSandbox:
    return BenchSandbox(id=f"fake-{index}", index=index, ready=True, is_alive=True)


def _config(recipe_path) -> KernelConfig:
    return KernelConfig(
        workflow_type="document",
        document_case_kind="xlsx",
        document_recipe_path=str(recipe_path),
        document_interval_min=0,
        document_interval_max=0,
    )


class _FailOnProvider(FakeProvider):
    """FakeProvider that fails any command containing one of ``fail_substrs``."""

    def __init__(self, *fail_substrs):
        super().__init__()
        self._fail_substrs = fail_substrs

    def exec(self, inst, command, **kw):  # type: ignore[override]
        if any(s in command for s in self._fail_substrs):
            return CommandResult(1, "", "boom")
        return super().exec(inst, command, **kw)


class _RecordingProvider(FakeProvider):
    """FakeProvider that records every exec'd command (in order)."""

    def __init__(self):
        super().__init__()
        self.commands: list[str] = []

    def exec(self, inst, command, **kw):  # type: ignore[override]
        self.commands.append(command)
        return super().exec(inst, command, **kw)


class TestBuildWriteCommand:
    def test_encodes_content_as_base64_heredoc(self):
        content = "print('hello')\n"
        cmd = _build_write_command("/tmp/out/foo.py", content)

        assert base64.b64encode(content.encode()).decode() in cmd
        # path is shell-quoted (no special chars here -> unchanged).
        assert "/tmp/out/foo.py" in cmd
        assert "<<'PYEOF'" in cmd
        assert "base64.b64decode" in cmd

    def test_path_with_spaces_is_quoted(self):
        cmd = _build_write_command("/tmp/my dir/x.py", "x")
        # shlex.quote wraps spaces in single quotes.
        assert "'/tmp/my dir/x.py'" in cmd


class TestLoadSceneRecipe:
    def test_loads_valid_recipe(self, tmp_path):
        path = _make_recipe(tmp_path / "recipe.json")
        recipe = load_scene_recipe("xlsx", path)
        assert recipe["case_kind"] == "xlsx"
        assert [p["operation_id"] for p in recipe["key_operations"]] == XLSX_PHASES

    def test_rejects_bad_schema(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json", extra={"schema_version": "other"})
        try:
            load_scene_recipe("xlsx", path)
        except SceneRecipeError as e:
            assert "unsupported recipe schema" in str(e)
        else:
            raise AssertionError("expected SceneRecipeError")

    def test_rejects_case_kind_mismatch(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json", case_kind="pdf")
        try:
            load_scene_recipe("xlsx", path)
        except SceneRecipeError as e:
            assert "expected 'xlsx'" in str(e)
        else:
            raise AssertionError("expected SceneRecipeError")

    def test_rejects_wrong_phase_order(self, tmp_path):
        swapped = [XLSX_PHASES[1], XLSX_PHASES[0], *XLSX_PHASES[2:]]
        path = _make_recipe(tmp_path / "r.json", phase_ids=swapped)
        try:
            load_scene_recipe("xlsx", path)
        except SceneRecipeError as e:
            assert "phase order is invalid" in str(e)
        else:
            raise AssertionError("expected SceneRecipeError")

    def test_rejects_conditional_path(self, tmp_path):
        path = _make_recipe(
            tmp_path / "r.json", extra={"workflow": {"success_path": XLSX_PHASES, "conditional_path": []}}
        )
        try:
            load_scene_recipe("xlsx", path)
        except SceneRecipeError as e:
            assert "single fixed success path" in str(e)
        else:
            raise AssertionError("expected SceneRecipeError")


class TestPreflightDocument:
    def test_returns_recipe_for_document_workflow(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        recipe = preflight_document(config)
        assert recipe["case_kind"] == "xlsx"

    def test_rejects_non_document_workflow(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        config.workflow_type = "browser"
        try:
            preflight_document(config)
        except ValueError as e:
            assert "workflow_type='document'" in str(e)
        else:
            raise AssertionError("expected ValueError")


class TestDocumentOperationExecutor:
    def test_execute_happy_path_records_all_phases(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        provider = FakeProvider()
        state = _ready_sandbox()
        executor = DocumentOperationExecutor(state, config, provider)

        success, latency, step_times, timed_out, detail = executor.execute()

        assert success is True
        assert timed_out is False
        assert detail == ""
        assert set(step_times) == set(XLSX_PHASES)
        assert latency > 0.0

    def test_workspace_prep_failure_short_circuits(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        # Fail the workspace reset (test -d {seed}/input ...).
        provider = _FailOnProvider("test -d")
        state = _ready_sandbox()
        executor = DocumentOperationExecutor(state, config, provider)

        success, _latency, step_times, _timed_out, detail = executor.execute()

        assert success is False
        assert "workspace reset failed" in detail
        # No phase ran (prep failed before any phase).
        assert step_times == {}

    def test_write_tool_call_uses_exec_not_files_api(self, tmp_path):
        # A recipe with a write call whose content has shell-special chars --
        # proving the base64 heredoc survives them and goes through exec (not a
        # file-upload API the provider doesn't expose).
        recipe_path = tmp_path / "r.json"
        content = "x = '$HOME' && `backtick`\n"
        recipe = {
            "schema_version": "scene-key-operations-v2",
            "case_kind": "xlsx",
            "operation_count": 4,
            "key_operations": [
                {
                    "operation_id": "XLSX-P01-inspect_prepare",
                    "source_tool_calls": [
                        {
                            "tool_call": {
                                "function_name": "write",
                                "arguments": {"path": "out/x.py", "content": content},
                            }
                        }
                    ],
                },
                {
                    "operation_id": "XLSX-P02-build",
                    "source_tool_calls": [
                        {"tool_call": {"function_name": "exec", "arguments": {"command": "echo build"}}}
                    ],
                },
                {
                    "operation_id": "XLSX-P03-process_publish",
                    "source_tool_calls": [
                        {"tool_call": {"function_name": "exec", "arguments": {"command": "echo pub"}}}
                    ],
                },
                {
                    "operation_id": "XLSX-P04-verify_deliver",
                    "source_tool_calls": [
                        {"tool_call": {"function_name": "exec", "arguments": {"command": "echo verify"}}}
                    ],
                },
            ],
            "workflow": {"success_path": XLSX_PHASES},
        }
        recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
        config = _config(recipe_path)
        provider = _RecordingProvider()
        state = _ready_sandbox()
        executor = DocumentOperationExecutor(state, config, provider)

        success, _latency, _step_times, _timed_out, _detail = executor.execute()

        assert success is True
        # The write was issued through exec as a base64 heredoc: the content's
        # base64 and the PYEOF marker both appear in some recorded command.
        b64 = base64.b64encode(content.encode()).decode()
        write_cmds = [c for c in provider.commands if b64 in c and "<<'PYEOF'" in c]
        assert len(write_cmds) == 1


class TestDocumentWarmupRunner:
    def test_prepare_workspace_and_marks_done(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        provider = FakeProvider()
        state = _ready_sandbox()
        DocumentWarmupRunner(state, config, provider).run()
        assert state.warmup_done is True
        assert state.document_metrics.last_error == ""

    def test_skips_when_not_ready(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        DocumentWarmupRunner(state, config, FakeProvider()).run()
        assert state.warmup_done is True
        assert "runtime-ready" in state.document_metrics.last_error


class TestDocumentTaskRunner:
    def test_three_consecutive_failures_mark_offline(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        provider = _FailOnProvider("test -d")  # workspace prep always fails
        state = _ready_sandbox()
        runner = DocumentTaskRunner(state, config, threading.Event(), provider)

        runner.run()  # loop runs until 3 failures -> is_alive False -> break

        metrics = state.document_metrics
        assert metrics.total_tasks == 3
        assert metrics.failed_count == 3
        assert state.is_alive is False

    def test_stop_event_prevents_loop(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        state = _ready_sandbox()
        stop = threading.Event()
        stop.set()
        runner = DocumentTaskRunner(state, config, stop, FakeProvider())
        runner.run()
        assert state.document_metrics.total_tasks == 0


class TestDocumentRoundRunner:
    def test_round_records_all_phases(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        provider = FakeProvider()
        state = _ready_sandbox()
        runner = DocumentRoundRunner(state, config, threading.Event(), round_id=0, provider=provider)

        runner.run()

        metrics = state.document_metrics
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        assert set(metrics.get_step_times_copy()) == set(XLSX_PHASES)
        assert state.get_last_task_time() > 0.0

    def test_skips_when_not_ready(self, tmp_path):
        path = _make_recipe(tmp_path / "r.json")
        config = _config(path)
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        runner = DocumentRoundRunner(state, config, threading.Event(), 0, FakeProvider())
        runner.run()
        assert state.document_metrics.total_tasks == 0
