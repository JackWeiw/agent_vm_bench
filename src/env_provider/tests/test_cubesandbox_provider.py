"""Unit tests for the CubesandboxProvider (no live cubesandbox SDK needed).

CubesandboxProvider wraps a CubesandboxManager; exec/save_ids/_to_instance are
exercised by injecting a fake manager whose sandbox_states carries a fake SDK
handle. build_provider (the kernel smoke path) is driven directly against a
raw YAML dict.
"""
from __future__ import annotations

import threading

import pytest

from bench_core.config import KernelConfig
from env_provider import SandboxInstance
from env_provider.cubesandbox import CubesandboxProvider, build_provider
from env_provider.cubesandbox.config import Config
from env_provider.cubesandbox.schemas import CubeSandboxState


# --------------------------------------------------------------------- fakes
class _RunResult:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeCommands:
    def __init__(self, result=None, exc=None):
        self._result = result if result is not None else _RunResult(0, "ok", "")
        self._exc = exc

    def run(self, cmd, *, user="root", timeout=None, **kwargs):  # noqa: ARG002
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeHandle:
    """A cube Sandbox handle stand-in (created or connected)."""

    def __init__(self, commands, sandbox_id="cube-1"):
        self.commands = commands
        self.sandbox_id = sandbox_id


# Exception stand-ins whose class names carry "timeout" (mapped to exit 124) vs not.
class _ReadTimeout(Exception):
    pass


class _ConnectionError(Exception):  # noqa: N818 -- name intentional for the test
    pass


def _make_provider(handle):
    cfg = KernelConfig(workflow_type="replay", total_count=1)
    provider = CubesandboxProvider(cfg, Config(), threading.Event())

    class _FakeManager:
        sandbox_states = {1: CubeSandboxState(sandbox_id=1, cube_sandbox=handle)}
        _slot_templates = {1: "cube-tpl"}

    provider._manager = _FakeManager()
    return provider


# --------------------------------------------------------------------- identity
def test_cube_name():
    assert CubesandboxProvider.name == "cubesandbox"


def test_cube_default_replay_mode_is_exec_only():
    assert CubesandboxProvider.default_replay_mode == "exec_only"


def test_cube_vmm_type_is_none():
    assert CubesandboxProvider.vmm_type is None


def test_cube_is_not_lifecycle_capable_in_phase1():
    # Phase 1: exec-only baseline. LifecycleCapable is added in Phase 2.
    from env_provider import LifecycleCapable

    provider = _make_provider(_FakeHandle(_FakeCommands()))
    assert not isinstance(provider, LifecycleCapable)


def test_cube_is_not_ephemeral_capable_in_phase1():
    from env_provider import EphemeralCapable

    provider = _make_provider(_FakeHandle(_FakeCommands()))
    assert not isinstance(provider, EphemeralCapable)


# --------------------------------------------------------------------- build_provider
def test_build_provider_reads_cubesandbox_block():
    """build_provider must read the `cubesandbox:` block, not `e2b:`."""
    kernel_cfg = KernelConfig(workflow_type="replay", total_count=1)
    raw = {
        "e2b": {
            "template": "e2b-tpl",
            "env": {"E2B_API_URL": "http://e2b-host:3000"},
        },
        "cubesandbox": {
            "template": "cube-tpl",
            "timeout": 3600,
            "sandbox_ids_file": "cube-ids.txt",
            "env": {
                "CUBE_API_URL": "http://127.0.0.1:3000",
                "CUBE_API_KEY": "your_cube_api_key_here",
                "CUBE_TEMPLATE_ID": "fallback-tpl",
                "CUBE_SANDBOX_DOMAIN": "cube.app",
            },
        },
    }
    provider = build_provider(kernel_cfg, raw)

    # The CubesandboxProvider must have consumed the cubesandbox block, not e2b.
    assert provider._config.template == "cube-tpl"
    assert provider._config.timeout == 3600
    assert provider._config.sandbox_ids_file == "cube-ids.txt"
    assert provider._config.cube_api_url == "http://127.0.0.1:3000"
    assert provider._config.cube_template_id == "fallback-tpl"


def test_setup_cube_env_skips_placeholder_api_key(monkeypatch):
    # The example YAML ships your_cube_api_key_here; setup must NOT export it
    # (cube auth is optional; a placeholder would be sent as a real key).
    monkeypatch.delenv("CUBE_API_KEY", raising=False)
    cfg = Config(cube_api_key="your_cube_api_key_here", cube_api_url="http://127.0.0.1:3000")
    cfg.setup_cube_env()
    import os

    assert "CUBE_API_KEY" not in os.environ
    assert os.environ["CUBE_API_URL"] == "http://127.0.0.1:3000"


def test_setup_cube_env_exports_real_api_key(monkeypatch):
    monkeypatch.delenv("CUBE_API_KEY", raising=False)
    cfg = Config(cube_api_key="real-secret-key", cube_template_id="tpl-1")
    cfg.setup_cube_env()
    import os

    assert os.environ["CUBE_API_KEY"] == "real-secret-key"
    assert os.environ["CUBE_TEMPLATE_ID"] == "tpl-1"


# --------------------------------------------------------------------- exec
def test_exec_success_returns_command_result():
    handle = _FakeHandle(_FakeCommands(result=_RunResult(0, "hello", "")))
    provider = _make_provider(handle)
    inst = SandboxInstance(id="cube-1", index=1)
    result = provider.exec(inst, "echo hello", timeout=10)
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == ""


def test_exec_timeout_maps_to_124():
    handle = _FakeHandle(_FakeCommands(exc=_ReadTimeout("read timed out")))
    provider = _make_provider(handle)
    inst = SandboxInstance(id="cube-1", index=1)
    result = provider.exec(inst, "sleep 999", timeout=5)
    assert result.exit_code == 124
    assert "timed out" in result.stderr.lower()


def test_exec_transport_error_raises():
    handle = _FakeHandle(_FakeCommands(exc=_ConnectionError("connection refused")))
    provider = _make_provider(handle)
    inst = SandboxInstance(id="cube-1", index=1)
    with pytest.raises(_ConnectionError):
        provider.exec(inst, "echo hi")


def test_exec_raises_on_missing_handle():
    provider = _make_provider(_FakeHandle(_FakeCommands()))

    class _EmptyManager:
        sandbox_states = {}

    provider._manager = _EmptyManager()
    inst = SandboxInstance(id="x", index=99)
    with pytest.raises(RuntimeError, match="index 99"):
        provider.exec(inst, "echo hi")


def test_exec_forwards_cwd_and_envs():
    captured = {}

    class _CaptureCommands:
        def run(self, cmd, *, user="root", timeout=None, cwd=None, envs=None, **kwargs):  # noqa: ARG002
            captured["cwd"] = cwd
            captured["envs"] = envs
            captured["timeout"] = timeout
            return _RunResult(0, "", "")

    handle = _FakeHandle(_CaptureCommands(), sandbox_id="cube-1")
    provider = _make_provider(handle)
    inst = SandboxInstance(id="cube-1", index=1)
    provider.exec(inst, "ls", timeout=20, cwd="/tmp", env={"PATH": "/bin"})
    assert captured["cwd"] == "/tmp"
    assert captured["envs"] == {"PATH": "/bin"}
    assert captured["timeout"] == 20


# --------------------------------------------------------------------- save_ids
def test_save_ids_writes_ready_ids(tmp_path):
    handle = _FakeHandle(_FakeCommands())
    provider = _make_provider(handle)

    insts = {
        1: SandboxInstance(id="cube-1", index=1, ready=True),
        2: SandboxInstance(id="cube-2", index=2, ready=False),  # not ready -> skipped
    }
    path = tmp_path / "ids.txt"
    provider.save_ids(insts, ids_file=str(path))
    lines = path.read_text().splitlines()
    assert lines == ["cube-1"]


def test_save_ids_no_path_is_noop():
    provider = _make_provider(_FakeHandle(_FakeCommands()))
    # Config() has sandbox_ids_file=None -> no-op, no raise.
    provider.save_ids({1: SandboxInstance(id="x", index=1, ready=True)})


# --------------------------------------------------------------------- _to_instance
def test_to_instance_stamps_template_and_id():
    handle = _FakeHandle(_FakeCommands(), sandbox_id="cube-real-1")
    provider = _make_provider(handle)
    state = provider.manager.sandbox_states[1]
    inst = provider._to_instance(state)
    assert inst.id == "cube-real-1"
    assert inst.index == 1
    assert inst.template == "cube-tpl"  # from _slot_templates[1]
    assert inst.numa_node is None  # v1 has no NUMA


def test_to_instance_ready_status_maps_to_ready():
    from env_provider import SandboxStatus
    from env_provider._base import BackendSandboxStatus

    handle = _FakeHandle(_FakeCommands())
    provider = _make_provider(handle)
    state = provider.manager.sandbox_states[1]
    state.creation_metrics.status = BackendSandboxStatus.PORT_READY
    inst = provider._to_instance(state)
    assert inst.ready is True
    assert inst.creation_metrics.status == SandboxStatus.READY
