"""Contract tests for the host-agnostic provider interface."""
import pytest

from bench_core.provider import (
    CommandResult,
    CreationMetrics,
    EnvironmentProvider,
    SandboxInstance,
)


def test_creation_metrics_defaults():
    m = CreationMetrics()
    assert m.start_time == 0.0
    assert m.success is False
    assert m.error == ""


def test_command_result_defaults():
    r = CommandResult()
    assert r.exit_code == 0
    assert r.stdout == ""
    assert r.stderr == ""


def test_sandbox_instance_required_fields():
    inst = SandboxInstance(id="sbx-1", index=0)
    assert inst.id == "sbx-1"
    assert inst.index == 0
    assert inst.numa_node is None
    assert inst.ready is False
    assert inst.is_alive is True
    assert inst.warmup_done is False
    assert isinstance(inst.creation_metrics, CreationMetrics)


def test_environment_provider_is_abstract():
    # Cannot instantiate the ABC directly.
    with pytest.raises(TypeError):
        EnvironmentProvider()  # type: ignore[abstract]


def test_default_hooks_are_noops():
    """A concrete provider inherits no-op defaults for optional hooks."""

    class _Minimal(EnvironmentProvider):
        name = "minimal"

        def create_all(self):
            return {}

        def detect_existing(self):
            return {}

        def check_alive(self, inst):
            return True

        def cleanup_all(self):
            pass

        def exec(self, inst, command, *, timeout=None, cwd=None, env=None):
            return CommandResult()

    p = _Minimal()
    assert p.detect_from_ids("any") is None
    assert p.prepare_env() is None
    assert p.prepare(SandboxInstance(id="x", index=0)) is None
    assert p.save_ids({}, "any") is None
