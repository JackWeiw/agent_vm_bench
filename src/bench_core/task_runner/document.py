"""Document workflow task runners (host-agnostic port of e2b_bench.document_task_runner).

The scene recipe is a ``scene-key-operations-v2`` JSON file. Every phase contains
the original read/write/exec tool calls, including complete helper source code.
This module replays one fixed success path against a fresh copy of a case seed
and records phase IDs as step-level metrics.

The host-agnostic port replaces every ``sbx.commands.run(...)`` with
``provider.exec(state, cmd, timeout=...)`` and -- critically -- replaces
``sbx.files.write(path, content)`` (the e2b file-upload API, which bypasses the
exec contract) with a base64 heredoc written through ``provider.exec``. That
keeps the runner on the single exec primitive: a provider that can run commands
can serve the document workflow with no file-upload API of its own.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import posixpath
import random
import shlex
import threading
import time
from pathlib import Path
from typing import Any

from bench_core.config import KernelConfig
from env_provider import EnvironmentProvider
from bench_core.schemas import BenchSandbox, get_step_order

logger = logging.getLogger(__name__)


class SceneRecipeError(ValueError):
    """Raised when a trusted key-operations file is invalid."""


class DocumentTaskTimeout(TimeoutError):
    """Raised when the complete document task exceeds its scene deadline."""


# Recipe JSONs live under the repo root (dockerfile_build/...). Resolve the repo
# root by walking up from this module to the dir holding pyproject.toml -- this
# works under src-layout (src/bench_core/x.py -> ... -> repo root with
# pyproject.toml). The framework is run from a repo checkout, never pip-installed
# from PyPI, so the marker is always present.
_RECIPE_FILES = {
    "pdf": Path("dockerfile_build/document/assets/operations/pdf_key_operations.json"),
    "xlsx": Path("dockerfile_build/document/assets/operations/xlsx_key_operations.json"),
}


def _find_repo_root() -> Path:
    """Walk up from this module to the repo root (the dir holding pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    # Fallback for src-layout: src/bench_core/x.py -> parents[2] = repo root.
    return here.parents[2] if len(here.parents) > 2 else here.parent


def _default_recipe_path(case_kind: str) -> Path:
    try:
        return _find_repo_root() / _RECIPE_FILES[case_kind]
    except KeyError:
        raise SceneRecipeError("document case_kind must be 'pdf' or 'xlsx'") from None


def load_scene_recipe(expected_case_kind: str, recipe_path: Path | None = None) -> dict[str, Any]:
    """Load and validate a trace-derived scene recipe."""
    path = recipe_path if recipe_path is not None else _default_recipe_path(expected_case_kind)

    with path.open(encoding="utf-8") as handle:
        recipe = json.load(handle)

    if recipe.get("schema_version") != "scene-key-operations-v2":
        raise SceneRecipeError(f"unsupported recipe schema in {path}")
    if recipe.get("case_kind") != expected_case_kind:
        raise SceneRecipeError(f"recipe case_kind={recipe.get('case_kind')!r}, expected {expected_case_kind!r}")

    phases = recipe.get("key_operations")
    if not isinstance(phases, list) or not phases:
        raise SceneRecipeError("recipe key_operations must be a non-empty list")
    if recipe.get("operation_count") != len(phases):
        raise SceneRecipeError("recipe operation_count does not match key_operations")

    phase_ids: list[str] = []
    for phase in phases:
        phase_id = phase.get("operation_id")
        calls = phase.get("source_tool_calls")
        if not phase_id or phase_id in phase_ids:
            raise SceneRecipeError(f"missing or duplicate phase ID: {phase_id!r}")
        if not isinstance(calls, list) or not calls:
            raise SceneRecipeError(f"phase {phase_id} has no source tool calls")
        for source_call in calls:
            call = source_call.get("tool_call", {})
            if call.get("function_name") not in {"read", "write", "exec"}:
                raise SceneRecipeError(f"phase {phase_id} contains unsupported tool call")
            if not isinstance(call.get("arguments"), dict):
                raise SceneRecipeError(f"phase {phase_id} has invalid tool arguments")
        phase_ids.append(phase_id)

    success_path = recipe.get("workflow", {}).get("success_path")
    if not isinstance(success_path, list) or not success_path:
        raise SceneRecipeError("recipe workflow.success_path must be a non-empty list")
    missing = [phase_id for phase_id in success_path if phase_id not in phase_ids]
    if missing:
        raise SceneRecipeError(f"success_path references unknown phases: {missing}")
    expected_order = get_step_order("document", expected_case_kind)
    if phase_ids != expected_order:
        raise SceneRecipeError(f"recipe phase order is invalid: expected {expected_order}, got {phase_ids}")
    if success_path != expected_order:
        raise SceneRecipeError("recipe workflow.success_path must contain every phase in canonical order")
    if "conditional_path" in recipe.get("workflow", {}):
        raise SceneRecipeError("document recipes must use a single fixed success path")
    return recipe


def preflight_document(config: KernelConfig) -> dict[str, Any]:
    """Validate Document config and its fixed recipe before any sandbox access."""
    config.validate()
    if config.workflow_type != "document":
        raise ValueError("document preflight requires workflow_type='document'")
    recipe_path = Path(config.document_recipe_path) if config.document_recipe_path else None
    return load_scene_recipe(config.document_case_kind, recipe_path)


def _build_write_command(path: str, content: str) -> str:
    """Build a heredoc command that writes ``content`` to ``path`` via exec.

    Replaces ``sbx.files.write(path, content)`` (the e2b file-upload API), which
    bypassed the exec-only contract. Content is base64-carried so quotes,
    backticks, ``$``, backslashes, and newlines in helper source code are all
    inert -- the same approach ``_build_edit_command`` uses for coding edits.
    Decoded by an inline python3 heredoc (present in the ubuntu base image).
    """
    b64 = base64.b64encode(content.encode()).decode()
    quoted_path = shlex.quote(path)
    return (
        f"python3 - {b64} {quoted_path} <<'PYEOF'\n"
        "import base64, sys\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write(base64.b64decode(sys.argv[1]).decode())\n"
        "PYEOF"
    )


class DocumentOperationExecutor:
    """Execute one complete document task against a sandbox via exec only."""

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        provider: EnvironmentProvider,
        stop_event: threading.Event | None = None,
    ):
        self.state = state
        self.config = config
        self.provider = provider
        config.validate()
        recipe_path = Path(config.document_recipe_path) if config.document_recipe_path else None
        self.recipe = load_scene_recipe(config.document_case_kind, recipe_path)
        self.phases = {item["operation_id"]: item for item in self.recipe["key_operations"]}
        # The scheduler stop event prevents a *new* complete task from starting.
        # It must not interrupt a recipe that has already begun.
        self.deadline: float | None = None

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

    def prepare_workspace(self) -> tuple[bool, str]:
        """Restore the exact trace workspace from the immutable image seed."""
        self._check_cancelled()
        seed = shlex.quote(self.config.document_seed_dir)
        workspace = shlex.quote(self.config.document_workspace_dir)
        parent = shlex.quote(posixpath.dirname(self.config.document_workspace_dir))
        command = (
            f"test -d {seed}/input && mkdir -p {parent} && "
            f"rm -rf {workspace} && cp -a {seed} {workspace} && mkdir -p {workspace}/output"
        )
        result = self.provider.exec(self.state, command, timeout=self._command_timeout(120))
        if result.exit_code != 0:
            return False, self._result_error("workspace reset", result)
        return True, ""

    def execute(self) -> tuple[bool, float, dict[str, float], bool, str]:
        """Run the recipe's single fixed phase path."""
        started = time.perf_counter()
        self.deadline = time.monotonic() + self.config.document_task_timeout
        step_times: dict[str, float] = {}
        timed_out = False

        try:
            prepared, error = self.prepare_workspace()
            if not prepared:
                return False, time.perf_counter() - started, step_times, False, error

            for phase_id in self.recipe["workflow"]["success_path"]:
                self._check_cancelled()
                ok, detail = self._execute_phase(phase_id, step_times)
                if not ok:
                    return False, time.perf_counter() - started, step_times, False, detail

            verified, detail = self._validate_business_result()
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

    def _execute_phase(self, phase_id: str, step_times: dict[str, float]) -> tuple[bool, str]:
        started = time.perf_counter()
        try:
            self._check_cancelled()
            phase = self.phases[phase_id]
            for source_call in phase["source_tool_calls"]:
                self._check_cancelled()
                call = source_call["tool_call"]
                ok, detail = self._execute_tool_call(call["function_name"], call["arguments"])
                if not ok:
                    return False, f"{phase_id}: {detail}"
            return True, ""
        finally:
            step_times[phase_id] = step_times.get(phase_id, 0.0) + (time.perf_counter() - started)

    def _execute_tool_call(self, function_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        if function_name == "read":
            path = shlex.quote(arguments["path"])
            result = self.provider.exec(
                self.state,
                f"test -f {path} && head -c 65536 {path} >/dev/null",
                timeout=self._command_timeout(30),
            )
        elif function_name == "write":
            path = arguments["path"]
            parent = shlex.quote(posixpath.dirname(path))
            created = self.provider.exec(self.state, f"mkdir -p {parent}", timeout=self._command_timeout(30))
            if created.exit_code != 0:
                return False, self._result_error("create write directory", created)
            # files.write (e2b upload API) -> base64 heredoc through exec, so the
            # runner stays on the single exec primitive.
            result = self.provider.exec(
                self.state,
                _build_write_command(path, arguments["content"]),
                timeout=self._command_timeout(int(self.config.document_operation_timeout)),
            )
            self._check_cancelled()
            if result.exit_code != 0:
                return False, self._result_error("write", result)
            return True, ""
        elif function_name == "exec":
            # Per-call timeouts are retained as source-trace metadata only. A
            # single benchmark timeout prevents old 10/60 second trace values
            # from killing valid work on constrained sandboxes.
            timeout = self._command_timeout(int(self.config.document_operation_timeout))
            command = arguments["command"].replace(
                "__DOCUMENT_RECALC_TIMEOUT__", str(self.config.document_recalc_timeout)
            )
            result = self.provider.exec(self.state, command, timeout=timeout)
        else:  # guarded by recipe validation
            return False, f"unsupported tool call: {function_name}"

        if result.exit_code != 0:
            return False, self._result_error(function_name, result)
        self._check_cancelled()
        return True, ""

    def _validate_business_result(self) -> tuple[bool, str]:
        report = posixpath.join(self.config.document_workspace_dir, "output", "business_verification.json")
        report_q = shlex.quote(report)
        command = (
            'python3 -c "import json,sys; d=json.load(open(sys.argv[1])); '
            "sys.exit(0 if d.get('status') == 'success' and not d.get('failures') else 1)\" "
            f"{report_q}"
        )
        result = self.provider.exec(self.state, command, timeout=self._command_timeout(30))
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

    def __init__(self, state: BenchSandbox, config: KernelConfig, provider: EnvironmentProvider):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.provider = provider

    def run(self) -> None:
        # Gate on readiness. The provider's create_all runs the readiness check
        # before returning, so a non-ready instance never reaches warmup.
        if not self.state.ready:
            self.state.document_metrics.last_error = "sandbox did not reach runtime-ready state"
            self.state.warmup_done = True
            return
        try:
            executor = DocumentOperationExecutor(self.state, self.config, self.provider)
            ok, detail = executor.prepare_workspace()
            if not ok:
                self.state.document_metrics.last_error = detail
                logger.error(f"[Sandbox{self.state.index}] Document warmup failed: {detail}")
            else:
                logger.info(
                    f"[Sandbox{self.state.index}] "
                    f"{self.config.document_case_kind.upper()} document warmup completed"
                )
        except Exception as exc:
            self.state.document_metrics.last_error = str(exc)
            logger.error(f"[Sandbox{self.state.index}] Document warmup exception: {exc}")
        finally:
            self.state.warmup_done = True


class DocumentTaskRunner(threading.Thread):
    """Continuously execute one fresh trace-derived document task per cycle."""

    def __init__(
        self,
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        provider: EnvironmentProvider,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.provider = provider
        self.executor = DocumentOperationExecutor(state, config, provider)
        self.consecutive_errors = 0

    def run(self) -> None:
        if not self.state.ready:
            logger.warning(
                f"[Sandbox{self.state.index}] Cannot start tasks: {self.state.creation_metrics.status.value}"
            )
            return
        while not self.stop_event.is_set() and self.state.is_alive:
            success, latency, step_times, timed_out, detail = self.executor.execute()
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
        state: BenchSandbox,
        config: KernelConfig,
        stop_event: threading.Event,
        round_id: int,
        provider: EnvironmentProvider,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.round_id = round_id
        self.provider = provider

    def run(self) -> None:
        if not self.state.ready or not self.state.is_alive:
            logger.info(f"[Sandbox{self.state.index}] Not ready/alive for document round")
            return
        executor = DocumentOperationExecutor(self.state, self.config, self.provider)
        success, latency, step_times, timed_out, detail = executor.execute()
        self.state.document_metrics.add(latency, success and not timed_out, timed_out, step_times)
        self.state.update_last_task_time(time.time())
        self.state.document_metrics.last_error = "" if success else detail
        if timed_out:
            self.state.is_alive = False
        outcome = "completed" if success else f"failed: {detail[:160]}"
        logger.info(
            f"[Sandbox{self.state.index}] {self.config.document_case_kind.upper()} "
            f"round {self.round_id} {outcome} ({latency:.2f}s)"
        )
