"""EphemeralCapable (trajectory mode) tests for AenvProvider (no live SDK).

Mirrors the test_e2b_manager pattern: inject a controllable fake ``Sandbox``
class into ``env_provider.e2b.manager`` and drive the real AenvProvider +
SandboxManager machinery through create_one/kill_one. No recording fake --
the real manager's _new_state / _create_single / _ready_checker / _apply_ready
/ _to_instance path is exercised faithfully.
"""
from __future__ import annotations

import threading

import pytest

from bench_core.config import KernelConfig
from env_provider import EphemeralCapable, SandboxInstance
from env_provider.aenv import AenvProvider
from env_provider.e2b.config import Config


class _RunResult:
    def __init__(self, exit_code=0, stdout="ok", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeCommands:
    """e2b Sandbox.commands: returns a responsive uname for the replay probe."""

    def run(self, cmd, user="root", timeout=60, **kwargs):  # noqa: ARG002
        return _RunResult(0, "Linux sbx 5.4.0 #1 x86_64 GNU/Linux", "")


class _FakeSandbox:
    """A single e2b Sandbox handle (created or connected)."""

    def __init__(self, sid):
        self.sandbox_id = sid
        self.commands = _FakeCommands()
        self.killed = False

    def kill(self):
        self.killed = True


class _FakeSandboxCls:
    """Stand-in for the ``e2b.Sandbox`` class (static create/list/connect)."""

    def __init__(self):
        self.created = []  # (template, timeout, envs, metadata, sbx)

    def create(self, template, timeout=86400, envs=None, metadata=None):
        sbx = _FakeSandbox(f"sbx-{len(self.created) + 1}")
        self.created.append((template, timeout, envs, metadata, sbx))
        return sbx

    def list(self):
        class _Paginator:
            has_next = False

            def next_items(self):
                return []

        return _Paginator()

    def connect(self, sid):
        return _FakeSandbox(sid)


class _FailingSandboxCls:
    """Stand-in for e2b.Sandbox whose create() always raises."""

    def create(self, template, timeout=86400, envs=None, metadata=None):  # noqa: ARG002
        raise RuntimeError("simulated create failure")

    def list(self):
        class _Paginator:
            has_next = False

            def next_items(self):
                return []

        return _Paginator()

    def connect(self, sid):  # noqa: ARG002
        return _FakeSandbox(sid)


def _make_provider(monkeypatch, fake=None):
    fake = fake or _FakeSandboxCls()
    monkeypatch.setattr("env_provider.e2b.manager.Sandbox", fake)
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    return AenvProvider(cfg, Config(), threading.Event()), fake


def test_aenv_satisfies_ephemeral_capable(monkeypatch):
    provider, _ = _make_provider(monkeypatch)
    assert isinstance(provider, EphemeralCapable)


def test_create_one_accepts_template_and_populates_slot_templates(monkeypatch):
    """create_one with template parameter passes it to _create_single and populates _slot_templates."""
    provider, fake = _make_provider(monkeypatch)
    inst = provider.create_one(1, template="swb-a", metadata={"trajectory_id": "tr-7"})

    # Verify template was passed to SDK create call
    template_arg, _, _, _, _ = fake.created[0]
    assert template_arg == "swb-a"

    # Verify SandboxInstance.template is set (proves _slot_templates was populated)
    assert inst.template == "swb-a"


def test_create_one_without_template_uses_default(monkeypatch):
    """create_one without template parameter uses the default template from config."""
    provider, fake = _make_provider(monkeypatch)
    inst = provider.create_one(1, metadata={"trajectory_id": "tr-7"})

    # Verify template was passed to SDK create call (should be default from config)
    template_arg, _, _, _, _ = fake.created[0]
    assert template_arg == provider._config.template

    # Verify SandboxInstance.template reflects the default
    assert inst.template == provider._config.template


def test_create_one_delegates_and_probes_ready(monkeypatch):
    provider, fake = _make_provider(monkeypatch)
    inst = provider.create_one(1, metadata={"trajectory_id": "tr-7"})
    assert inst.index == 1
    assert inst.ready is True
    assert inst.is_alive is True
    # metadata forwarded to the SDK create call
    _, _, _, md, _ = fake.created[0]
    assert md == {"trajectory_id": "tr-7"}


def test_kill_one_calls_manager_kill(monkeypatch):
    provider, fake = _make_provider(monkeypatch)
    inst = provider.create_one(2)
    handle = provider.manager.sandbox_states[2].sandbox_obj
    provider.kill_one(inst)
    assert inst.is_alive is False
    assert handle.killed is True


def test_kill_one_unknown_index_is_noop(monkeypatch):
    provider, _ = _make_provider(monkeypatch)
    inst = SandboxInstance(id="x", index=999)
    provider.kill_one(inst)  # must not raise
    assert inst.is_alive is True  # untouched


def test_create_one_raises_on_failure(monkeypatch):
    monkeypatch.setattr("env_provider.e2b.manager.Sandbox", _FailingSandboxCls())
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    provider = AenvProvider(cfg, Config(), threading.Event())
    with pytest.raises(RuntimeError, match="simulated create failure"):
        provider.create_one(1)


def test_kill_one_safe_after_failed_create(monkeypatch):
    monkeypatch.setattr("env_provider.e2b.manager.Sandbox", _FailingSandboxCls())
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    provider = AenvProvider(cfg, Config(), threading.Event())
    with pytest.raises(RuntimeError):
        provider.create_one(1)
    # The failed-create state is registered in _states with sandbox_obj=None.
    # kill_one must NOT crash (runner finally-path safety).
    from env_provider import SandboxInstance

    inst = SandboxInstance(id="x", index=1)
    provider.kill_one(inst)  # must not raise
    assert inst.is_alive is False
