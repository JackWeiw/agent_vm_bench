"""Contract tests for the host-agnostic provider interface."""
import pytest

from bench_core.provider import (
    CommandResult,
    CreationMetrics,
    EnvironmentProvider,
    SandboxInstance,
    SandboxStatus,
)


def test_creation_metrics_defaults():
    m = CreationMetrics()
    assert m.create_elapsed == 0.0
    assert m.ready_check_elapsed == 0.0
    assert m.total_elapsed == 0.0
    assert m.status == SandboxStatus.PENDING
    assert m.error == ""
    assert m.ready_check_error == ""


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


from bench_core.tests.fake_provider import FakeProvider  # noqa: E402


def test_fake_provider_create_and_exec():
    p = FakeProvider(count=3)
    insts = p.create_all()
    assert len(insts) == 3
    assert all(i.ready for i in insts.values())
    res = p.exec(insts[0], "echo hi")
    assert res.exit_code == 0
    assert "hi" in res.stdout
    # cleanup tears every instance down and is observable.
    assert not p.cleanup_called
    p.cleanup_all()
    assert p.cleanup_called
    assert all(not i.is_alive for i in insts.values())
