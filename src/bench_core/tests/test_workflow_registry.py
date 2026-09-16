"""Registry validation tests for the workflow plugin seam (RFC 0002 Phase 0).

Drives the registry via the 4 in-tree task_runner modules (importing each fires its
module-bottom ``register_workflow``), then asserts the contract holds. No live SDK.
"""
from __future__ import annotations

import threading

import pytest

from bench_core.schemas import TaskMetricsBase
from bench_core.workflow_registry import (
    RegistrationError,
    RunContext,
    TaskRunner,
    WorkflowConfigBase,
    WorkflowSpec,
    register_workflow,
    WORKFLOW_REGISTRY,
)


def _import_all_workflows() -> None:
    """Import the 4 task_runner modules so their module-bottom registration fires."""
    # Imported for the side effect of register_workflow() at module bottom.
    import bench_core.task_runner.browser  # noqa: F401
    import bench_core.task_runner.coding  # noqa: F401
    import bench_core.task_runner.document  # noqa: F401
    import bench_core.task_runner.replay  # noqa: F401


SHIPPED = ("browser", "coding", "document", "replay")


@pytest.fixture(scope="module", autouse=True)
def _ensure_registered():
    _import_all_workflows()


class _DummyMetrics(TaskMetricsBase):
    """Stand-in metrics class for registration-rejection tests."""


class _NotAMetrics:
    """Deliberately not a TaskMetricsBase subclass."""


def test_all_four_workflows_registered():
    for name in SHIPPED:
        assert name in WORKFLOW_REGISTRY, f"{name!r} missing from registry"


def test_each_spec_carries_valid_metadata():
    for name in SHIPPED:
        spec = WORKFLOW_REGISTRY[name]
        assert spec.name == name
        assert issubclass(spec.metrics_cls, TaskMetricsBase)
        assert isinstance(spec.step_order, tuple) and len(spec.step_order) > 0
        # config_cls / report_formatters are None in Phase 0 (land in Phases 2/3).
        assert spec.config_cls is None
        assert spec.report_formatters is None
        # Phase 0: runner fields are threading.Thread subclasses (tightened to
        # TaskRunner in Phase 1 once the 12 runners migrate).
        for attr in ("warmup_runner", "task_runner", "round_runner"):
            cls = getattr(spec, attr)
            assert issubclass(cls, threading.Thread)


def test_duplicate_registration_rejected():
    spec = _dummy_spec(name="browser")  # 'browser' is already registered
    with pytest.raises(RegistrationError):
        register_workflow(spec)


def test_force_overrides_duplicate():
    name = "__test_force__"
    try:
        register_workflow(_dummy_spec(name=name))
        assert name in WORKFLOW_REGISTRY
        # force=True replaces cleanly.
        register_workflow(_dummy_spec(name=name), force=True)
        assert name in WORKFLOW_REGISTRY
    finally:
        WORKFLOW_REGISTRY.pop(name, None)


def test_non_metrics_cls_rejected():
    spec = _dummy_spec(name="__test_bad_metrics__", metrics_cls=_NotAMetrics)
    with pytest.raises(TypeError):
        register_workflow(spec)
    assert "__test_bad_metrics__" not in WORKFLOW_REGISTRY


def test_non_thread_runner_rejected():
    class _NotAThread:
        pass

    spec = _dummy_spec(name="__test_bad_runner__", task_runner=_NotAThread)
    with pytest.raises(TypeError):
        register_workflow(spec)
    assert "__test_bad_runner__" not in WORKFLOW_REGISTRY


def test_non_workflow_spec_rejected():
    with pytest.raises(TypeError):
        register_workflow("not a spec")  # type: ignore[arg-type]


def test_dummy_spec_is_valid_baseline():
    """Sanity: _dummy_spec itself registers cleanly under a fresh name."""
    name = "__test_baseline__"
    try:
        register_workflow(_dummy_spec(name=name))
        assert WORKFLOW_REGISTRY[name].step_order == ("a", "b")
    finally:
        WORKFLOW_REGISTRY.pop(name, None)


def _dummy_spec(
    *,
    name: str,
    metrics_cls: type = _DummyMetrics,
    task_runner: type | None = None,
) -> WorkflowSpec:
    """Build a minimal valid spec for rejection/override tests."""

    class _T(threading.Thread):
        def run(self) -> None:  # pragma: no cover - never started
            ...

    return WorkflowSpec(
        name=name,
        warmup_runner=task_runner or _T,
        task_runner=task_runner or _T,
        round_runner=task_runner or _T,
        metrics_cls=metrics_cls,
        step_order=("a", "b"),
        config_section=name,
    )


def test_run_context_is_frozen():
    """frozen=True blocks field rebinding (mutable contents like stop_event stay mutable)."""
    import dataclasses

    # Minimal stand-ins — RunContext only needs the attributes referenced.
    class _State:
        index = 0

        class creation_metrics:
            class status:
                value = "ready"

        ready = True
        is_alive = True

    ctx = RunContext(
        state=_State(),  # type: ignore[arg-type]
        config=object(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        stop_event=threading.Event(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.config = object()  # type: ignore[misc]
    # ext is mutable in place (frozen blocks rebinding, not in-place mutation).
    ctx.ext["k"] = "v"
    assert ctx.ext == {"k": "v"}
