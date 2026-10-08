"""Seam tests for the real cube :class:`CubesandboxManager`.

The provider/adapter tests (``test_cubesandbox_provider.py``) mock the manager
itself, so the real manager's SDK wiring -- ``_create_single`` (Sandbox.create
kwargs, envs=None), ``_list_existing`` (flat list of dicts), ``_attach``
(Sandbox.connect), ``_exec_probe`` (commands.run -> tuple), ``_kill_one``
(sb.kill), ``check_alive`` (get_info) -- has no coverage. These tests inject a
controllable fake ``Sandbox`` class into the manager module and drive the real
base lifecycle (create_all / detect_existing / detect_from_file / cleanup_all /
cleanup_existing) through it, locking the seam contract.

The cubesandbox SDK is an optional extra; the manager module imports ``Sandbox``
under a try/except that falls back to an inline mock. These tests replace that
binding with a controllable fake, so no SDK install is needed.
"""
from __future__ import annotations

from threading import Event

import pytest

from bench_core.config import KernelConfig
from env_provider.cubesandbox.config import Config
from env_provider.cubesandbox.manager import CubesandboxManager


# ------------------------------------------------------------------ cube fakes
class _RunResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeCommands:
    """cube Sandbox.commands stand-in. Returns a listening port line for any
    probe when ``ready``; records every run call."""

    def __init__(self, *, ready: bool = True):
        self._ready = ready
        self.calls: list[str] = []

    def run(self, cmd, *, user="root", timeout=None, **kwargs):  # noqa: ARG002
        self.calls.append(cmd)
        if self._ready:
            return _RunResult(0, "0.0.0.0:18789 0.0.0.0:*\n", "")
        return _RunResult(1, "", "")


class _FakeSandbox:
    """A single cube Sandbox handle (created or connected)."""

    def __init__(self, sid: str):
        self.sandbox_id = sid
        self.commands = _FakeCommands()
        self.killed = False
        self.info_calls = 0

    def kill(self):
        self.killed = True

    def get_info(self):
        self.info_calls += 1
        return {"state": "running", "sandboxID": self.sandbox_id}


class _FakeSandboxCls:
    """Stand-in for the ``cubesandbox.Sandbox`` class (static create/list/connect).

    CubeSandbox's ``Sandbox.list()`` returns a **flat list of dicts** (not a
    paginator); each dict carries ``sandboxID``. ``create`` is keyword-only
    after ``template`` (``timeout``/``envs``/``metadata``).
    """

    def __init__(self):
        self.created: list = []  # (template, timeout, envs, metadata, sandbox)
        self.list_items: list[dict] = []
        self.connected: list[str] = []

    def create(self, template, *, timeout=86400, envs=None, metadata=None, **kwargs):  # noqa: ARG003
        sbx = _FakeSandbox(f"cube-{len(self.created) + 1}")
        self.created.append((template, timeout, envs, metadata, sbx))
        return sbx

    def list(self, *args, **kwargs):  # noqa: ARG004
        return list(self.list_items)

    def connect(self, sandbox_id, *args, **kwargs):  # noqa: ARG004
        sbx = _FakeSandbox(sandbox_id)
        self.connected.append(sandbox_id)
        return sbx


# --------------------------------------------------------------------- helpers
def _kc(**kw) -> KernelConfig:
    kw.setdefault("workflow_type", "browser")
    return KernelConfig(total_count=kw.pop("total_count", 2), **kw)


def _patch(monkeypatch, fake: _FakeSandboxCls) -> _FakeSandboxCls:
    monkeypatch.setattr("env_provider.cubesandbox.manager.Sandbox", fake)
    return fake


# --------------------------------------------------------------------- _create_single
class TestCreateSingle:
    def test_sets_handle_metrics_and_envs_none(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        cfg = Config(template="cube-tpl")
        mgr = CubesandboxManager(_kc(total_count=1), cfg, Event())

        s1 = mgr._new_state(1)
        r1 = mgr._create_single(s1)
        assert r1["success"] is True
        assert s1.cube_sandbox is not None
        assert s1.creation_metrics.status.value == "created"
        assert s1.creation_metrics.create_elapsed >= 0.0
        template, timeout, envs, metadata, _ = fake.created[0]
        assert template == "cube-tpl"
        assert envs is None  # v1 passes no envs (no FC_BIND NUMA guess)
        assert metadata is None  # default create forwards no metadata

    def test_create_forwards_metadata(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=1), Config(template="cube-tpl"), Event())
        s1 = mgr._new_state(1)
        mgr._create_single(s1, metadata={"trajectory": "t1"})
        _, _, _, metadata, _ = fake.created[0]
        assert metadata == {"trajectory": "t1"}

    def test_create_exception_returns_failure(self, monkeypatch):
        fake = _FakeSandboxCls()
        fake.create = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("create failed"))
        _patch(monkeypatch, fake)
        mgr = CubesandboxManager(_kc(total_count=1), Config(), Event())
        result = mgr._create_single(mgr._new_state(1))
        assert result["success"] is False
        assert "create failed" in result["error"]


# --------------------------------------------------------------------- _list_existing
class TestListExisting:
    def test_returns_flat_list_of_dicts(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        fake.list_items = [{"sandboxID": "a"}, {"sandboxID": "b"}, {"sandboxID": "c"}]
        mgr = CubesandboxManager(_kc(), Config(), Event())
        listed = mgr._list_existing()
        assert len(listed) == 3
        # A flat list (NOT a paginator): a second call lists fresh again.
        assert len(mgr._list_existing()) == 3

    def test_external_id_reads_sandboxID_key(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(), Config(), Event())
        assert mgr._external_id({"sandboxID": "cube-x"}) == "cube-x"

    def test_empty_list(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(), Config(), Event())
        assert mgr._list_existing() == []


# --------------------------------------------------------------------- _exec_probe
class TestExecProbe:
    def test_translates_run_result_to_tuple(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(), Config(), Event())
        sbx = _FakeSandbox("x")
        exit_code, stdout, stderr = mgr._exec_probe(sbx, "uname -a", 10)
        assert exit_code == 0
        assert "0.0.0.0" in stdout
        assert stderr == ""


# --------------------------------------------------------------------- create_all
class TestCreateAll:
    def test_browser_create_marks_port_ready(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=3, workflow_type="browser"), Config(template="cube-tpl"), Event())
        states = mgr.create_all()
        assert sorted(states) == [1, 2, 3]
        for s in states.values():
            assert s.creation_metrics.status.value == "port_ready"
            assert s.cube_sandbox is not None
            assert s.creation_metrics.port_wait_elapsed >= 0.0
        assert len(fake.created) == 3

    def test_slot_templates_stamped_from_create_result(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=2), Config(template="cube-tpl"), Event())
        mgr.create_all()
        assert mgr._slot_templates[1] == "cube-tpl"
        assert mgr._slot_templates[2] == "cube-tpl"
        assert len(fake.created) == 2


# --------------------------------------------------------------------- detect
class TestDetectExisting:
    def test_attaches_via_connect_and_marks_ready(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        fake.list_items = [{"sandboxID": "cube-a", "state": "running"}, {"sandboxID": "cube-b", "state": "running"}]
        mgr = CubesandboxManager(_kc(workflow_type="browser"), Config(), Event())
        states = mgr.detect_existing()
        assert sorted(states) == [1, 2]
        assert fake.connected == ["cube-a", "cube-b"]
        for s in states.values():
            assert s.creation_metrics.status.value == "port_ready"

    def test_empty_list_returns_empty(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(), Config(), Event())
        assert mgr.detect_existing() == {}


class TestDetectFromFile:
    def test_matches_ids_and_attaches_only_matched(self, monkeypatch, tmp_path):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        fake.list_items = [
            {"sandboxID": "cube-a"},
            {"sandboxID": "cube-b"},
            {"sandboxID": "cube-c"},
        ]
        ids = tmp_path / "ids.txt"
        ids.write_text("cube-a\ncube-c\n")
        mgr = CubesandboxManager(_kc(), Config(), Event())
        states = mgr.detect_from_file(str(ids))
        assert sorted(states) == [1, 2]
        assert fake.connected == ["cube-a", "cube-c"]  # cube-b skipped

    def test_missing_file_raises(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(), Config(), Event())
        with pytest.raises(FileNotFoundError):
            mgr.detect_from_file("/no/such/ids.txt")


# --------------------------------------------------------------------- cleanup_existing
class TestCleanupExisting:
    def test_lists_connects_kills_each_without_ready_check(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        fake.list_items = [{"sandboxID": "cube-a"}, {"sandboxID": "cube-b"}]
        mgr = CubesandboxManager(_kc(), Config(), Event())

        killed = mgr.cleanup_existing()

        assert killed == 2
        assert fake.connected == ["cube-a", "cube-b"]  # connected, not just listed
        # Ready checker is never built (no readiness probe on teardown).
        assert mgr._ready is None

    def test_empty_list_kills_none(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        fake.list_items = []
        mgr = CubesandboxManager(_kc(), Config(), Event())
        assert mgr.cleanup_existing() == 0
        assert fake.connected == []


# --------------------------------------------------------------------- cleanup_all
class TestCleanupAll:
    def test_kills_each_handle_and_marks_not_alive(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=3), Config(template="cube-tpl"), Event())
        mgr.create_all()
        mgr.cleanup_all()
        for s in mgr._states.values():
            assert s.cube_sandbox.killed is True
            assert s.is_alive is False
            # cube keeps the original creation status (no KILLED overwrite, like e2b)
            assert s.creation_metrics.status.value == "port_ready"


# --------------------------------------------------------------------- check_alive
class TestCheckAlive:
    def test_ready_handle_is_alive(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=1), Config(template="cube-tpl"), Event())
        state = mgr._new_state(1)
        mgr._create_single(state)
        assert mgr.check_alive(state) is True
        assert state.cube_sandbox.info_calls == 1  # get_info was called

    def test_dead_handle_returns_false(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=1), Config(template="cube-tpl"), Event())
        state = mgr._new_state(1)
        mgr._create_single(state)
        # Force get_info to raise -> check_alive swallows -> False
        state.cube_sandbox.get_info = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        assert mgr.check_alive(state) is False

    def test_missing_handle_returns_false(self, monkeypatch):
        _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)  # cube_sandbox stays None
        assert mgr.check_alive(state) is False
