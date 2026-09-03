"""Unit tests for the AenvProvider (no live E2B/AENV SDK needed).

AenvProvider subclasses E2BProvider and reuses the E2B SandboxManager handle
table. pause/resume are exercised by injecting a fake manager whose
sandbox_states carries a fake SDK handle.
"""
from __future__ import annotations

import threading

import pytest

from env_provider import LifecycleCapable, SandboxInstance
from env_provider.aenv import AenvProvider
from env_provider.e2b import E2BProvider
from env_provider.e2b.config import Config
from env_provider.e2b.schemas import SandboxState
from bench_core.config import KernelConfig


class _FakeSbx:
    """Fake SDK handle: records beta_pause and is swapped by connect()."""

    def __init__(self):
        self.pause_calls = 0

    def beta_pause(self):
        self.pause_calls += 1


def _make_provider(sbx):
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    provider = AenvProvider(cfg, Config(), threading.Event())

    class _FakeManager:
        sandbox_states = {0: SandboxState(sandbox_id=1, sandbox_obj=sbx)}

    provider._manager = _FakeManager()
    return provider


def test_aenv_is_lifecycle_capable():
    provider = _make_provider(_FakeSbx())
    assert isinstance(provider, LifecycleCapable)


def test_aenv_default_replay_mode_is_lifecycle():
    assert AenvProvider.default_replay_mode == "lifecycle"


def test_aenv_name():
    assert AenvProvider.name == "aenv"


def test_pause_calls_beta_pause():
    sbx = _FakeSbx()
    provider = _make_provider(sbx)
    inst = SandboxInstance(id="sbx-1", index=0)
    provider.pause(inst)
    assert sbx.pause_calls == 1


def test_resume_calls_connect_and_swaps_handle():
    sbx = _FakeSbx()
    provider = _make_provider(sbx)
    inst = SandboxInstance(id="sbx-1", index=0)

    new_handle = _FakeSbx()
    # Monkeypatch the aenv module's Sandbox.connect to return our sentinel.
    import env_provider.aenv as aenv_mod

    original = aenv_mod.Sandbox.connect
    captured = {}

    def fake_connect(sandbox_id):
        captured["sandbox_id"] = sandbox_id
        return new_handle

    aenv_mod.Sandbox.connect = staticmethod(fake_connect)
    try:
        provider.resume(inst)
    finally:
        aenv_mod.Sandbox.connect = original

    state = provider.manager.sandbox_states[0]
    assert state.sandbox_obj is new_handle
    assert captured["sandbox_id"] == "sbx-1"


def test_pause_raises_on_missing_handle():
    provider = _make_provider(_FakeSbx())

    class _EmptyManager:
        sandbox_states = {}

    provider._manager = _EmptyManager()
    inst = SandboxInstance(id="x", index=99)
    with pytest.raises(RuntimeError, match="index 99"):
        provider.pause(inst)


def test_resume_raises_on_missing_handle():
    provider = _make_provider(_FakeSbx())

    class _EmptyManager:
        sandbox_states = {}

    provider._manager = _EmptyManager()
    inst = SandboxInstance(id="x", index=99)
    with pytest.raises(RuntimeError, match="index 99"):
        provider.resume(inst)


def test_e2b_is_not_lifecycle_capable():
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    e2b = E2BProvider(cfg, Config(), threading.Event())
    assert not isinstance(e2b, LifecycleCapable)


def test_e2b_default_replay_mode_is_exec_only():
    assert E2BProvider.default_replay_mode == "exec_only"


def test_build_provider_reads_aenv_block_not_e2b():
    """build_provider must read the `aenv:` block, not `e2b:` (BLOCKER regression)."""
    from env_provider.aenv import build_provider

    kernel_cfg = KernelConfig(workflow_type="replay", total_count=1)
    raw = {
        "e2b": {
            "template": "e2b-tpl",
            "env": {"E2B_API_URL": "http://e2b-host:3000"},
        },
        "aenv": {
            "template": "aenv-tpl",
            "sandbox_ids_file": "aenv-ids.txt",
            "env": {"E2B_API_URL": "http://127.0.0.1:8000"},
        },
    }
    provider = build_provider(kernel_cfg, raw)

    # The AenvProvider must have consumed the aenv block, not e2b.
    assert provider._config.template == "aenv-tpl"
    assert provider._config.e2b_api_url == "http://127.0.0.1:8000"
    assert provider._config.sandbox_ids_file == "aenv-ids.txt"
