"""Trace-derived PDF/XLSX document benchmark runners.

The scene recipe is a ``scene-key-operations-v2`` JSON file.  Every key
operation contains the original read/write/exec tool calls, including complete
helper source code.  This module replays the selected success path against a
fresh copy of a case seed and records operation IDs as step-level metrics.
"""

import json
import math
import posixpath
import random
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .helpers import wait_for_port_ready
from .schemas import SandboxState, get_step_order


class SceneRecipeError(ValueError):
    """Raised when a trusted key-operations file is invalid."""


class DocumentTaskTimeout(TimeoutError):
    """Raised when the complete document task exceeds its scene deadline."""


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT_OPERATIONS_FILES = {
    "pdf": Path("dockerfile_build/document/assets/operations/pdf_key_operations.json"),
    "xlsx": Path("dockerfile_build/document/assets/operations/xlsx_key_operations.json"),
}
DOCUMENT_MAX_REPAIR_ATTEMPTS = {"pdf": 0, "xlsx": 1}


def get_document_operations_path(case_kind: str) -> Path:
    """Return the repository-relative, non-configurable recipe path."""
    try:
        return REPO_ROOT / DOCUMENT_OPERATIONS_FILES[case_kind]
    except KeyError:
        raise SceneRecipeError("document case_kind must be 'pdf' or 'xlsx'") from None


def load_scene_recipe(expected_case_kind: str) -> Dict[str, Any]:
    """Load and validate a trace-derived scene recipe."""
    recipe_path = get_document_operations_path(expected_case_kind)

    with recipe_path.open(encoding="utf-8") as handle:
        recipe = json.load(handle)

    if recipe.get("schema_version") != "scene-key-operations-v2":
        raise SceneRecipeError(f"unsupported recipe schema in {recipe_path}")
    if recipe.get("case_kind") != expected_case_kind:
        raise SceneRecipeError(f"recipe case_kind={recipe.get('case_kind')!r}, expected {expected_case_kind!r}")

    operations = recipe.get("key_operations")
    if not isinstance(operations, list) or not operations:
        raise SceneRecipeError("recipe key_operations must be a non-empty list")
    if recipe.get("operation_count") != len(operations):
        raise SceneRecipeError("recipe operation_count does not match key_operations")

    operation_ids = []
    for operation in operations:
        operation_id = operation.get("operation_id")
        calls = operation.get("source_tool_calls")
        if not operation_id or operation_id in operation_ids:
            raise SceneRecipeError(f"missing or duplicate operation_id: {operation_id!r}")
        if not isinstance(calls, list) or not calls:
            raise SceneRecipeError(f"operation {operation_id} has no source tool calls")
        for source_call in calls:
            call = source_call.get("tool_call", {})
            if call.get("function_name") not in {"read", "write", "exec"}:
                raise SceneRecipeError(f"operation {operation_id} contains unsupported tool call")
            if not isinstance(call.get("arguments"), dict):
                raise SceneRecipeError(f"operation {operation_id} has invalid tool arguments")
        operation_ids.append(operation_id)

    success_path = recipe.get("workflow", {}).get("success_path")
    if not isinstance(success_path, list) or not success_path:
        raise SceneRecipeError("recipe workflow.success_path must be a non-empty list")
    missing = [operation_id for operation_id in success_path if operation_id not in operation_ids]
    if missing:
        raise SceneRecipeError(f"success_path references unknown operations: {missing}")
    conditional_path = recipe.get("workflow", {}).get("conditional_path", {})
    conditional_steps = conditional_path.get("steps", [])
    if not isinstance(conditional_steps, list):
        raise SceneRecipeError("recipe workflow.conditional_path.steps must be a list")
    missing = [operation_id for operation_id in conditional_steps if operation_id not in operation_ids]
    if missing:
        raise SceneRecipeError(f"conditional_path references unknown operations: {missing}")
    expected_count = 14 if expected_case_kind == "pdf" else 12
    expected_ids = set(get_step_order("document", expected_case_kind))
    if set(operation_ids) != expected_ids:
        raise SceneRecipeError(
            f"recipe operation IDs are incomplete: expected {sorted(expected_ids)}, got {sorted(operation_ids)}"
        )
    referenced_ids = set(success_path) | set(conditional_steps)
    if referenced_ids != expected_ids:
        raise SceneRecipeError("recipe workflow paths do not cover every key operation")
    return recipe


def preflight_document(config: Config) -> Dict[str, Any]:
    """Validate Document config and its fixed recipe before any Sandbox access."""
    config.validate()
    if config.workflow_type != "document":
        raise ValueError("document preflight requires workflow_type='document'")
    return load_scene_recipe(config.document_case_kind)


class DocumentOperationExecutor:
    """Execute one complete document task in an E2B sandbox."""

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: Optional[threading.Event] = None,
    ):
        self.state = state
        self.config = config
        config.validate()
        self.recipe = load_scene_recipe(config.document_case_kind)
        self.operations = {item["operation_id"]: item for item in self.recipe["key_operations"]}
        # The scheduler stop event prevents a *new* complete task from starting.
        # It must not interrupt a recipe that has already begun.
        self.deadline: Optional[float] = None

    def _check_cancelled(self) -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise DocumentTaskTimeout(f"document task exceeded {self.config.document_task_timeout} seconds")

    def _command_timeout(self, maximum: int) -> int:
        self._check_cancelled()
        if self.deadline is None:
            return maximum
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self._check_cancelled()
        return max(1, min(maximum, math.ceil(remaining)))

    def prepare_workspace(self, sbx) -> Tuple[bool, str]:
        """Restore the exact trace workspace from the immutable image seed."""
        self._check_cancelled()
        seed = shlex.quote(self.config.document_seed_dir)
        workspace = shlex.quote(self.config.document_workspace_dir)
        parent = shlex.quote(posixpath.dirname(self.config.document_workspace_dir))
        command = (
            f"test -d {seed}/input && mkdir -p {parent} && "
            f"rm -rf {workspace} && cp -a {seed} {workspace} && mkdir -p {workspace}/output"
        )
        result = sbx.commands.run(command, timeout=self._command_timeout(120), user="root")
        if result.exit_code != 0:
            return False, self._result_error("workspace reset", result)
        return True, ""

    def execute(self, sbx) -> Tuple[bool, float, Dict[str, float], bool, str]:
        """Run the recipe's main path and, for XLSX, one trace-derived repair."""
        started = time.perf_counter()
        self.deadline = time.monotonic() + self.config.document_task_timeout
        step_times: Dict[str, float] = {}
        timed_out = False

        try:
            prepared, error = self.prepare_workspace(sbx)
            if not prepared:
                return False, time.perf_counter() - started, step_times, False, error

            for operation_id in self.recipe["workflow"]["success_path"]:
                self._check_cancelled()
                ok, detail = self._execute_operation(sbx, operation_id, step_times)
                if not ok:
                    if self._can_repair(operation_id):
                        repaired, repair_error = self._execute_xlsx_repair(sbx, step_times)
                        if repaired:
                            return self._finish_after_repair(sbx, started, step_times)
                        detail = f"{detail}; repair failed: {repair_error}"
                    return False, time.perf_counter() - started, step_times, False, detail

            verified, detail = self._validate_business_result(sbx)
            return verified, time.perf_counter() - started, step_times, False, detail
        except Exception as exc:
            message = str(exc)
            timed_out = (
                isinstance(exc, DocumentTaskTimeout)
                or "timed out" in message.lower()
                or "context deadline exceeded" in message.lower()
            )
            return False, time.perf_counter() - started, step_times, timed_out, message
        finally:
            self.deadline = None

    def _execute_operation(self, sbx, operation_id: str, step_times: Dict[str, float]) -> Tuple[bool, str]:
        started = time.perf_counter()
        try:
            self._check_cancelled()
            operation = self.operations[operation_id]
            for source_call in operation["source_tool_calls"]:
                self._check_cancelled()
                call = source_call["tool_call"]
                ok, detail = self._execute_tool_call(sbx, call["function_name"], call["arguments"])
                if not ok:
                    return False, f"{operation_id}: {detail}"
            return True, ""
        finally:
            step_times[operation_id] = step_times.get(operation_id, 0.0) + (time.perf_counter() - started)

    def _execute_tool_call(self, sbx, function_name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        if function_name == "read":
            path = shlex.quote(arguments["path"])
            result = sbx.commands.run(
                f"test -f {path} && head -c 65536 {path} >/dev/null",
                timeout=self._command_timeout(30),
                user="root",
            )
        elif function_name == "write":
            path = arguments["path"]
            parent = shlex.quote(posixpath.dirname(path))
            created = sbx.commands.run(f"mkdir -p {parent}", timeout=self._command_timeout(30), user="root")
            if created.exit_code != 0:
                return False, self._result_error("create write directory", created)
            sbx.files.write(
                path,
                arguments["content"],
                user="root",
                request_timeout=self._command_timeout(int(self.config.document_operation_timeout)),
            )
            self._check_cancelled()
            return True, ""
        elif function_name == "exec":
            # Per-call timeouts are retained as source-trace metadata only.  A
            # single benchmark timeout prevents old 10/60 second trace values
            # from killing valid work on constrained sandboxes.
            timeout = self._command_timeout(int(self.config.document_operation_timeout))
            command = arguments["command"].replace(
                "__DOCUMENT_RECALC_TIMEOUT__", str(self.config.document_recalc_timeout)
            )
            result = sbx.commands.run(command, timeout=timeout, user="root")
        else:  # guarded by recipe validation
            return False, f"unsupported tool call: {function_name}"

        if result.exit_code != 0:
            return False, self._result_error(function_name, result)
        self._check_cancelled()
        return True, ""

    def _can_repair(self, operation_id: str) -> bool:
        return (
            self.config.document_case_kind == "xlsx"
            and DOCUMENT_MAX_REPAIR_ATTEMPTS[self.config.document_case_kind] > 0
            and operation_id in {"XLSX-K07-validate_workbook", "XLSX-K10-verify_business_rules"}
            and "XLSX-K08-repair_workbook" in self.operations
        )

    def _execute_xlsx_repair(self, sbx, step_times: Dict[str, float]) -> Tuple[bool, str]:
        # K08 contains the actual repair helper plus execution/recalc/check calls
        # from the source trajectory; it is intentionally treated atomically.
        return self._execute_operation(sbx, "XLSX-K08-repair_workbook", step_times)

    def _finish_after_repair(
        self, sbx, started: float, step_times: Dict[str, float]
    ) -> Tuple[bool, float, Dict[str, float], bool, str]:
        # Re-publish the stable CSVs after repair before verifier/summary/final
        # validation.  K09 is harmless to repeat when K10 was the trigger.
        for operation_id in (
            "XLSX-K09-export_summary_csvs",
            "XLSX-K10-verify_business_rules",
            "XLSX-K11-generate_summary",
            "XLSX-K12-check_deliverables",
        ):
            self._check_cancelled()
            ok, detail = self._execute_operation(sbx, operation_id, step_times)
            if not ok:
                return False, time.perf_counter() - started, step_times, False, detail
        verified, detail = self._validate_business_result(sbx)
        return verified, time.perf_counter() - started, step_times, False, detail

    def _validate_business_result(self, sbx) -> Tuple[bool, str]:
        report = posixpath.join(self.config.document_workspace_dir, "output", "business_verification.json")
        report_q = shlex.quote(report)
        command = (
            'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); '
            "sys.exit(0 if d.get('status') == 'success' and not d.get('failures') else 1)\" "
            f"{report_q}"
        )
        result = sbx.commands.run(command, timeout=self._command_timeout(30), user="root")
        if result.exit_code != 0:
            return False, self._result_error("business verification", result)
        return True, ""

    @staticmethod
    def _result_error(label: str, result) -> str:
        stderr = (getattr(result, "stderr", "") or "").strip()[:300]
        stdout = (getattr(result, "stdout", "") or "").strip()[:300]
        detail = stderr or stdout or "no command output"
        return f"{label} failed (exit_code={result.exit_code}): {detail}"


class DocumentWarmupRunner(threading.Thread):
    """Validate image assets and prepare an initial clean document workspace."""

    def __init__(self, state: SandboxState, config: Config):
        super().__init__(daemon=True)
        self.state = state
        self.config = config

    def run(self) -> None:
        if not wait_for_port_ready(self.state):
            self.state.warmup_error = "sandbox did not reach runtime-ready state"
            self.state.warmup_done = True
            return
        sbx = self.state.sandbox_obj
        if not sbx:
            self.state.warmup_error = "sandbox object is unavailable"
            self.state.warmup_done = True
            return
        try:
            executor = DocumentOperationExecutor(self.state, self.config)
            ok, detail = executor.prepare_workspace(sbx)
            if not ok:
                self.state.warmup_error = detail
                self.state.document_metrics.last_error = detail
                print(f"[Sandbox{self.state.sandbox_id}] Document warmup failed: {detail}")
            else:
                self.state.warmup_error = ""
                print(
                    f"[Sandbox{self.state.sandbox_id}] "
                    f"{self.config.document_case_kind.upper()} document warmup completed"
                )
        except Exception as exc:
            self.state.warmup_error = str(exc)
            self.state.document_metrics.last_error = str(exc)
            print(f"[Sandbox{self.state.sandbox_id}] Document warmup exception: {exc}")
        finally:
            self.state.warmup_done = True


class DocumentTaskRunner(threading.Thread):
    """Continuously execute one fresh trace-derived document task per cycle."""

    def __init__(self, state: SandboxState, config: Config, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.executor = DocumentOperationExecutor(state, config)
        self.consecutive_errors = 0

    def run(self) -> None:
        if not wait_for_port_ready(self.state, self.stop_event):
            return
        while not self.stop_event.is_set() and self.state.is_alive:
            success, latency, step_times, timed_out, detail = self.executor.execute(self.state.sandbox_obj)
            self.state.document_metrics.add(latency, success and not timed_out, timed_out, step_times)
            self.state.update_last_task_time(time.time())
            self.state.document_metrics.last_error = "" if success else detail
            self.consecutive_errors = 0 if success else self.consecutive_errors + 1
            if self.consecutive_errors >= 3:
                self.state.is_alive = False
                break
            self.stop_event.wait(random.uniform(self.config.document_interval_min, self.config.document_interval_max))


class DocumentRoundRunner(threading.Thread):
    """Execute exactly one complete PDF or XLSX task in round-robin mode."""

    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
        round_id: int,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.round_id = round_id

    def run(self) -> None:
        if self.stop_event.is_set() or not self.state.sandbox_obj:
            return
        executor = DocumentOperationExecutor(self.state, self.config)
        success, latency, step_times, timed_out, detail = executor.execute(self.state.sandbox_obj)
        self.state.document_metrics.add(latency, success and not timed_out, timed_out, step_times)
        self.state.update_last_task_time(time.time())
        self.state.document_metrics.last_error = "" if success else detail
        if timed_out:
            self.state.is_alive = False
        outcome = "completed" if success else f"failed: {detail[:160]}"
        print(
            f"[Sandbox{self.state.sandbox_id}] {self.config.document_case_kind.upper()} "
            f"round {self.round_id} {outcome} ({latency:.2f}s)"
        )
