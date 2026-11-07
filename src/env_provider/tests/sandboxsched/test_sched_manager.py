"""Seam tests for the sandboxsched :class:`SandboxManager`.

The provider/adapter tests mock the manager itself; these tests inject a
controllable fake OpenSandbox SDK (async ``Sandbox`` / ``SandboxManager``)
into the manager module and drive the real base lifecycle (create_all /
detect_existing / detect_from_file / cleanup_all) through it, locking the SDK
seam contract: ``Sandbox.create(image, *, extensions, skip_health_check)``
-> handle with ``.id`` / ``.commands.run(cmd, opts=)`` / ``.kill()``, the
paginator flatten in ``_list_existing``, ``Sandbox.connect`` for attach, and
``_execution_to_result`` (Execution.text/logs.stderr/exit_code -> tuple, None
exit_code -> 124). All fake SDK methods are coroutines -- the manager's
submit() drives them on its bridge loop exactly like the real SDK.

The opensandbox SDK is an optional extra; the manager module imports the async
classes under a try/except that falls back to an inline mock. These tests
replace those bindings with controllable fakes, so no SDK install is needed.
"""

from __future__ import annotations

from datetime import timedelta
from threading import Event

import pytest

from bench_core.config import KernelConfig
from bench_core.workflow_registry import ensure_workflow_registered

# The workflow registry self-registers at task_runner module import; trigger
# the browser workflow's registration so KernelConfig.validate() passes when
# these tests run in isolation (mirrors the e2b manager test's implicit import
# chain when the full suite runs).
ensure_workflow_registered("coding")
from env_provider.sandboxsched import manager as mgr_mod
from env_provider.sandboxsched.config import Config
from env_provider.sandboxsched.manager import SandboxManager, _execution_to_result


# -------------------------------------------------------------------- SDK fakes
class _OutputMessage:
    def __init__(self, text: str, is_error: bool = False):
        self.text = text
        self.is_error = is_error


class _ExecutionLogs:
    def __init__(self, stdout=(), stderr=()):
        self.stdout = [_OutputMessage(t) for t in stdout]
        self.stderr = [_OutputMessage(t, is_error=True) for t in stderr]


class _ExecutionError:
    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value


class _Execution:
    """Fake opensandbox Execution (commands.run return value)."""

    def __init__(self, *, exit_code=None, stdout=(), stderr=(), error=None, result_text=()):
        self.exit_code = exit_code
        self.logs = _ExecutionLogs(stdout=stdout, stderr=stderr)
        self.error = error
        self._result_text = result_text

    @property
    def text(self) -> str:
        chunks = [m.text.rstrip("\n") for m in self.logs.stdout]
        chunks.extend(t.rstrip("\n") for t in self._result_text if t)
        return "\n".join(chunks)


class _RunResult(_Execution):
    """Convenience: a normal result with an integer exit code + stdout text."""

    def __init__(self, exit_code: int = 0, stdout_text: str = "", stderr_text: str = ""):
        stdout = [stdout_text] if stdout_text else []
        stderr = [stderr_text] if stderr_text else []
        super().__init__(exit_code=exit_code, stdout=stdout, stderr=stderr)


class _FakeCommands:
    def __init__(self, *, ready: bool = True):
        self._ready = ready
        self.calls: list[str] = []

    async def run(self, cmd, *, opts=None, handlers=None):  # noqa: ARG002
        self.calls.append(cmd)
        if self._ready:
            return _RunResult(0, "Linux sandbox 6.6 aarch64\n", "")
        return _RunResult(1, "", "")


class _FakeSandboxSync:
    """A single OpenSandbox SandboxSync handle (created or connected)."""

    def __init__(self, sid: str, *, ready: bool = True):
        self.id = sid
        self.commands = _FakeCommands(ready=ready)
        self.killed = False
        self.paused = False

    async def kill(self):
        self.killed = True

    async def pause(self):
        self.paused = True

    async def close(self):  # noqa: ARG002
        pass


class _Pagination:
    def __init__(self, has_next_page: bool):
        self.has_next_page = has_next_page


class _PagedInfos:
    def __init__(self, infos, has_next_page=False):
        self.sandbox_infos = infos
        self.pagination = _Pagination(has_next_page)


class _SandboxInfo:
    def __init__(self, sid: str, state: str = "Running"):
        self.id = sid
        self.status = type("Status", (), {"state": state})()


class _FakeSandboxSyncCls:
    """Stand-in for the async ``opensandbox.Sandbox`` class (coroutine methods)."""

    def __init__(self):
        self.created: list[tuple] = []  # (image, extensions, sandbox)
        self.connected: list[str] = []
        self.resumed: list[str] = []
        self._next_id = 0
        self._ready = True

    def set_ready(self, ready: bool) -> None:
        self._ready = ready

    async def create(
        self,
        image=None,
        *,
        snapshot_id=None,
        timeout=timedelta(minutes=10),  # noqa: ARG002
        env=None,
        metadata=None,
        resource=None,
        extensions=None,  # noqa: ARG002
        connection_config=None,
        skip_health_check=False,
    ):  # noqa: ARG002
        self._next_id += 1
        sid = f"sbx-{self._next_id:04d}"
        sbx = _FakeSandboxSync(sid, ready=self._ready)
        self.created.append((image, extensions, sbx))
        return sbx

    async def connect(self, sandbox_id, connection_config=None, skip_health_check=False):  # noqa: ARG002
        self.connected.append(sandbox_id)
        return _FakeSandboxSync(sandbox_id, ready=self._ready)

    async def resume(self, sandbox_id, connection_config=None, skip_health_check=False):  # noqa: ARG002
        self.resumed.append(sandbox_id)
        return _FakeSandboxSync(sandbox_id, ready=self._ready)


class _FakeManagerSync:
    def __init__(self):
        self.list_pages: list[_PagedInfos] = []
        self.killed: list[str] = []

    async def list_sandbox_infos(self, flt):  # noqa: ARG002
        if not self.list_pages:
            return _PagedInfos([], has_next_page=False)
        return self.list_pages.pop(0)

    async def kill_sandbox(self, sandbox_id):
        self.killed.append(sandbox_id)

    async def close(self):  # noqa: ARG002
        pass


class _FakeManagerCls:
    def __init__(self, inner: _FakeManagerSync):
        self._inner = inner

    async def create(self, connection_config=None):  # noqa: ARG002
        return self._inner


# -------------------------------------------------------------------- fixtures
@pytest.fixture
def kernel_config():
    cfg = KernelConfig()
    cfg.total_count = 3
    cfg.create_batch_size = 0
    # coding/replay probe = uname -a until non-empty stdout; the fake commands
    # run returns a uname-shaped string, so the ready-check passes immediately
    # (browser would poll ports for READY_MAX_WAIT=300s -> test hang).
    cfg.workflow_type = "coding"
    return cfg


@pytest.fixture
def sched_config():
    return Config(sandbox_group="sg-1u1g", template="python:3.11", create_timeout=300)


@pytest.fixture
def patched_sdk(monkeypatch):
    """Replace the SDK bindings in the manager module with controllable fakes."""
    fake_sandbox = _FakeSandboxSyncCls()
    fake_manager_inner = _FakeManagerSync()
    fake_manager = _FakeManagerCls(fake_manager_inner)
    monkeypatch.setattr(mgr_mod, "Sandbox", fake_sandbox)
    monkeypatch.setattr(mgr_mod, "SdkSandboxManager", fake_manager)
    monkeypatch.setattr(
        mgr_mod,
        "ConnectionConfig",
        lambda **kw: type(
            "Cfg",
            (),
            {"api_key": kw.get("api_key"), "domain": kw.get("domain"), "protocol": kw.get("protocol", "http")},
        )(),
    )

    # Stub the ready checker so create_all's ready probe is instant. The real
    # ReadyChecker polls uname for up to READY_MAX_WAIT=300s; a fake that returns
    # empty/exit!=0 stdout (``set_ready(False)``) would hang there. Patching the
    # base class's lazy builder means every SandboxManager returns the stub
    # without each test having to wire it.
    class _StubReady:
        def check(self, handle, workflow_type, label):  # noqa: ARG002
            return {"success": True, "wait_elapsed": 0.0, "error": ""}

    from env_provider._base import BaseSandboxManager

    monkeypatch.setattr(BaseSandboxManager, "_ready_checker", lambda self: _StubReady())
    return fake_sandbox, fake_manager_inner


# -------------------------------------------------------------------- _execution_to_result
class TestExecutionToResult:
    def test_integer_exit_code(self):
        ex = _RunResult(0, "hi\n")
        assert _execution_to_result(ex) == (0, "hi", "")

    def test_none_exit_code_maps_to_124(self):
        ex = _Execution(exit_code=None, stdout=["partial\n"])
        assert _execution_to_result(ex) == (124, "partial", "")

    def test_stderr_joined(self):
        ex = _RunResult(2, "out", "err line 1\nerr line 2\n")
        code, out, err = _execution_to_result(ex)
        assert code == 2
        assert out == "out"
        assert err == "err line 1\nerr line 2"

    def test_none_exit_code_fills_stderr_from_error(self):
        err = _ExecutionError("TimeoutError", "command timed out")
        ex = _Execution(exit_code=None, error=err)
        code, _, stderr = _execution_to_result(ex)
        assert code == 124
        assert "TimeoutError" in stderr


# -------------------------------------------------------------------- create / cleanup
class TestCreateAndCleanup:
    def test_create_all_creates_handles(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, _ = patched_sdk
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        assert len(states) == 3
        assert len(fake_sandbox.created) == 3
        # each state got a handle with an id
        for state in states.values():
            assert state.sandbox_obj is not None
            assert state.sandbox_obj.id.startswith("sbx-")
        # extensions carried the sandbox_group
        for image, extensions, _ in fake_sandbox.created:
            assert extensions == {"sandboxGroup": "sg-1u1g"}

    def test_missing_sandbox_group_fails_create(self, kernel_config, patched_sdk):
        cfg = Config(sandbox_group="", template="python:3.11")
        mgr = SandboxManager(kernel_config, cfg, Event())
        states = mgr.create_all()
        # all 3 failed, no SDK create calls
        assert len(mgr_mod.Sandbox.created) == 0
        for state in states.values():
            from env_provider._base import BackendSandboxStatus

            assert state.creation_metrics.status == BackendSandboxStatus.FAILED
            assert "sandbox_group is required" in state.creation_metrics.error_msg

    def test_cleanup_all_kills_each(self, kernel_config, sched_config, patched_sdk):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.create_all()
        mgr.cleanup_all()
        for state in mgr._states.values():
            assert state.sandbox_obj.killed is True
            assert state.torn_down is True


# -------------------------------------------------------------------- detect / list
class TestDetectAndList:
    def test_list_flattens_pages(self, kernel_config, sched_config, patched_sdk):
        _, fake_manager = patched_sdk
        fake_manager.list_pages = [
            _PagedInfos([_SandboxInfo("a"), _SandboxInfo("b")], has_next_page=True),
            _PagedInfos([_SandboxInfo("c")], has_next_page=False),
        ]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        listed = mgr._list_existing()
        assert [i.id for i in listed] == ["a", "b", "c"]

    def test_detect_existing_attaches(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("x"), _SandboxInfo("y")])]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.detect_existing()
        assert len(states) == 2
        assert fake_sandbox.connected == ["x", "y"]

    def test_detect_from_file_matches_ids(self, kernel_config, sched_config, patched_sdk, tmp_path):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("keep-1"), _SandboxInfo("skip-2")])]
        ids_file = tmp_path / "ids.txt"
        ids_file.write_text("keep-1\nskip-2-doesnt-match\n")
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.detect_from_file(str(ids_file))
        assert len(states) == 1
        assert fake_sandbox.connected == ["keep-1"]


# -------------------------------------------------------------------- exec probe
class TestExecProbe:
    def test_exec_probe_returns_tuple(self, kernel_config, sched_config, patched_sdk):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        handle = mgr._handle_of(next(iter(states.values())))
        code, out, _ = mgr._exec_probe(handle, "uname -a", timeout=10)
        assert code == 0
        assert "Linux sandbox" in out

    def test_check_alive_true_on_zero_exit(self, kernel_config, sched_config, patched_sdk):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        assert mgr.check_alive(state) is True

    def test_check_alive_false_when_not_ready(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, _ = patched_sdk
        fake_sandbox.set_ready(False)
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        assert mgr.check_alive(state) is False
