"""Tests for the SandboxSched provider adapter (:class:`SandboxSchedProvider`).

Mocks the :class:`SandboxManager` so the adapter's translation + exec routing +
lifecycle/ephemeral/snapshot delegation is tested without the SDK. Mirrors
``test_e2b_provider.py``'s approach: the manager is the seam, the adapter maps
SDK types -> kernel contract.
"""

from __future__ import annotations

from datetime import timedelta
from threading import Event
from unittest.mock import MagicMock

import pytest

from bench_core.config import KernelConfig
from bench_core.workflow_registry import ensure_workflow_registered

ensure_workflow_registered("coding")
from env_provider import SandboxInstance, SandboxStatus
from env_provider._base import BackendCreationMetrics, BackendSandboxStatus
from env_provider.sandboxsched import SandboxSchedProvider, build_provider
from env_provider.sandboxsched.config import Config
from env_provider.sandboxsched.schemas import SandboxState


# -------------------------------------------------------------------- fixtures
def _kernel_config():
    cfg = KernelConfig()
    cfg.total_count = 2
    cfg.workflow_type = "coding"
    return cfg


def _state(index: int, *, ready: bool = True, sbx_id: str = "sbx-1") -> SandboxState:
    cm = BackendCreationMetrics()
    cm.status = BackendSandboxStatus.PORT_READY if ready else BackendSandboxStatus.FAILED
    cm.create_elapsed = 1.0
    cm.total_elapsed = 1.5
    cm.port_wait_elapsed = 0.5
    cm.port_ready_time = 100.0
    cm.submit_time = 99.0
    handle = MagicMock()
    handle.id = sbx_id
    return SandboxState(sandbox_id=index, sandbox_obj=handle, creation_metrics=cm, is_alive=ready)


@pytest.fixture
def provider():
    p = SandboxSchedProvider(_kernel_config(), Config(sandbox_group="sg-1"), Event())
    p._manager = MagicMock()
    return p


# -------------------------------------------------------------------- translation
class TestTranslate:
    def test_to_instance_maps_ready(self, provider):
        provider._manager._slot_templates = {1: "python:3.11"}
        inst = provider._to_instance(_state(1, ready=True, sbx_id="abc"))
        assert inst.id == "abc"
        assert inst.index == 1
        assert inst.ready is True
        assert inst.is_alive is True
        assert inst.numa_node is None  # sandboxsched has no client-side NUMA
        assert inst.template == "python:3.11"
        assert inst.creation_metrics.status == SandboxStatus.READY

    def test_to_instance_maps_failed(self, provider):
        provider._manager._slot_templates = {}
        inst = provider._to_instance(_state(2, ready=False))
        assert inst.ready is False
        assert inst.creation_metrics.status == SandboxStatus.FAILED

    def test_no_handle_yields_empty_id(self, provider):
        provider._manager._slot_templates = {}
        st = _state(1)
        st.sandbox_obj = None
        inst = provider._to_instance(st)
        assert inst.id == ""


# -------------------------------------------------------------------- lifecycle delegation
class TestLifecycle:
    def test_pause_delegates_to_manager(self, provider):
        st = _state(1)
        provider._manager.sandbox_states = {1: st}
        inst = SandboxInstance(id="x", index=1, ready=True)
        provider.pause(inst)
        provider._manager.pause_handle.assert_called_once_with(st)

    def test_pause_missing_state_raises(self, provider):
        provider._manager.sandbox_states = {}
        with pytest.raises(RuntimeError, match="No sandboxsched handle"):
            provider.pause(SandboxInstance(id="x", index=1))

    def test_resume_swaps_handle(self, provider):
        st = _state(1)
        provider._manager.sandbox_states = {1: st}
        new_handle = MagicMock(id="x")
        provider._manager.resume_handle.return_value = new_handle
        provider.resume(SandboxInstance(id="x", index=1))
        assert st.sandbox_obj is new_handle
        provider._manager.resume_handle.assert_called_once_with("x")


# -------------------------------------------------------------------- ephemeral
class TestEphemeral:
    def test_create_one_success(self, provider):
        provider._manager._new_state.return_value = _state(1)
        provider._manager._create_single.return_value = {"success": True, "create_elapsed": 1.0, "template": "t"}
        provider._manager._handle_of.return_value = MagicMock()
        provider._manager._ready_checker.return_value.check.return_value = {
            "success": True,
            "wait_elapsed": 0.5,
            "error": "",
        }
        provider._manager._apply_ready = MagicMock()
        provider._manager._slot_templates = {}
        inst = provider.create_one(1, template="t")
        assert inst.index == 1
        provider._manager._create_single.assert_called_once()

    def test_create_one_failure_raises(self, provider):
        provider._manager._new_state.return_value = _state(1)
        provider._manager._create_single.return_value = {
            "success": False,
            "create_elapsed": 0.0,
            "error": "boom",
            "template": "t",
        }
        provider._manager._slot_templates = {}
        with pytest.raises(RuntimeError, match="create_one"):
            provider.create_one(1)

    def test_kill_one_marks_torn_down(self, provider):
        st = _state(1)
        provider._manager.sandbox_states = {1: st}
        provider._manager._handle_of.return_value = MagicMock()
        inst = SandboxInstance(id="x", index=1, ready=True, is_alive=True)
        provider.kill_one(inst)
        assert st.torn_down is True
        assert inst.is_alive is False

    def test_kill_one_missing_state_noop(self, provider):
        provider._manager.sandbox_states = {}
        # no raise
        provider.kill_one(SandboxInstance(id="x", index=99))


# -------------------------------------------------------------------- snapshot
class TestSnapshot:
    def test_snapshot_returns_dict_on_success(self, provider):
        st = _state(1)
        provider._manager.sandbox_states = {1: st}
        snap = MagicMock()
        snap.id = "snap-1"
        snap.status.state = "Ready"
        provider._manager.snapshot_handle.return_value = snap
        result = provider.snapshot_sizes(SandboxInstance(id="x", index=1, ready=True))
        assert result == {"snapshot_id": "snap-1", "snapshot_state": "Ready"}
        provider._manager.snapshot_handle.assert_called_once_with(st)

    def test_snapshot_none_on_exception(self, provider):
        st = _state(1)
        provider._manager.sandbox_states = {1: st}
        provider._manager.snapshot_handle.side_effect = RuntimeError("nope")
        assert provider.snapshot_sizes(SandboxInstance(id="x", index=1)) is None


# -------------------------------------------------------------------- exec
class TestExec:
    def test_exec_missing_state_raises(self, provider):
        provider._manager.sandbox_states = {}
        with pytest.raises(RuntimeError, match="No sandboxsched handle"):
            provider.exec(SandboxInstance(id="x", index=1), "echo hi")

    def test_exec_none_handle_raises(self, provider):
        st = _state(1)
        st.sandbox_obj = None
        provider._manager.sandbox_states = {1: st}
        with pytest.raises(RuntimeError, match="No sandboxsched handle"):
            provider.exec(SandboxInstance(id="x", index=1), "echo hi")


# -------------------------------------------------------------------- build_provider
class TestBuildProvider:
    def test_build_from_raw(self):
        raw = {"sandboxsched": {"sandbox_group": "sg-1", "template": "python:3.11"}}
        p = build_provider(_kernel_config(), raw)
        assert isinstance(p, SandboxSchedProvider)
        assert p._config.sandbox_group == "sg-1"

    def test_build_empty_raw(self):
        p = build_provider(_kernel_config(), {})
        assert isinstance(p, SandboxSchedProvider)
        assert p._config.sandbox_group == ""
