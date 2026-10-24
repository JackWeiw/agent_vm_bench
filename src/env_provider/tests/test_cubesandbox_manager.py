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

import threading
import time
from threading import Event

import pytest

from bench_core.config import KernelConfig
from env_provider.cubesandbox.config import Config
from env_provider.cubesandbox.manager import CubesandboxManager, _call_bounded


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
        assert sorted(fake.connected) == ["cube-a", "cube-b"]  # connected (pool order nondeterministic)
        # Ready checker is never built (no readiness probe on teardown).
        assert mgr._ready is None

    def test_empty_list_kills_none(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeSandboxCls())
        fake.list_items = []
        mgr = CubesandboxManager(_kc(), Config(), Event())
        assert mgr.cleanup_existing() == 0
        assert fake.connected == []

    def test_blocking_connect_is_bounded(self, monkeypatch):
        # Degraded control plane: connect blackholes -> _call_bounded raises at
        # request_timeout, the item counts as an error, cleanup_existing returns
        # (base serial loop: N x request_timeout; here bounded by the pool deadline).
        fake = _FakeSandboxCls()
        release = threading.Event()
        fake.connect = lambda sid, *a, **k: release.wait(30)
        fake.list_items = [{"sandboxID": "cube-x"}]
        _patch(monkeypatch, fake)
        cfg = Config(request_timeout=0.2, cleanup_concurrency=2)
        mgr = CubesandboxManager(_kc(), cfg, Event())
        try:
            start = time.monotonic()
            assert mgr.cleanup_existing() == 0
            assert time.monotonic() - start < 10
        finally:
            release.set()


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


class TestCleanupAllConcurrent:
    """Cube overrides the base serial loop with a bounded worker pool."""

    def test_blocking_kill_does_not_hang_cleanup(self, monkeypatch):
        """A blackholed kill (the ratio-04 hang: DELETE with no reply) must not
        stall teardown -- the overall deadline abandons it and cleanup_all
        returns, so the report and subsequent trials proceed."""
        _patch(monkeypatch, _FakeSandboxCls())
        cfg = Config(template="cube-tpl", request_timeout=0.2, cleanup_concurrency=2)
        mgr = CubesandboxManager(_kc(total_count=2), cfg, Event())
        mgr.create_all()
        release = Event()
        for s in mgr._states.values():
            s.cube_sandbox.kill = lambda: release.wait(30)
        start = time.monotonic()
        try:
            mgr.cleanup_all()
        finally:
            release.set()  # free the abandoned workers so the test run exits
        # Deadline is (2/2 + 1) * 0.2 = 0.4s; allow generous scheduling slack.
        assert time.monotonic() - start < 10

    def test_concurrency_is_bounded(self, monkeypatch):
        """Parallelism never exceeds cleanup_concurrency (the request-storm cap
        that keeps cubemaster's accept backlog from overflowing)."""
        _patch(monkeypatch, _FakeSandboxCls())
        cfg = Config(template="cube-tpl", cleanup_concurrency=2)
        mgr = CubesandboxManager(_kc(total_count=6), cfg, Event())
        mgr.create_all()
        lock = threading.Lock()
        live = peak = 0

        def slow_kill():
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1

        for s in mgr._states.values():
            s.cube_sandbox.kill = slow_kill
        mgr.cleanup_all()
        assert peak <= 2
        # Every kill still ran and the bookkeeping flags are set (shared
        # _kill_tracked helper -- same transitions as the base serial loop).
        assert all(not s.is_alive and s.stopped_by_cleanup for s in mgr._states.values())


# --------------------------------------------------------------------- session timeout pin
class TestPinSessionTimeout:
    def test_mounts_adapter_once_and_noop_without_session(self):
        pytest.importorskip("requests")
        from env_provider.cubesandbox.manager import _TimeoutAdapter, pin_session_timeout

        class _Sess:
            def __init__(self):
                self.mounted: dict = {}

            def mount(self, prefix, adapter):
                self.mounted[prefix] = adapter

        class _Sbx:
            def __init__(self):
                self._session = _Sess()

        sbx = _Sbx()
        pin_session_timeout(sbx, 7.0)
        assert isinstance(sbx._session.mounted["http://"], _TimeoutAdapter)
        assert isinstance(sbx._session.mounted["https://"], _TimeoutAdapter)
        first = sbx._session.mounted["http://"]
        pin_session_timeout(sbx, 99.0)
        assert sbx._session.mounted["http://"] is first  # idempotent

        pin_session_timeout(object(), 7.0)  # no _session -> warn-once no-op

    def test_adapter_fills_missing_timeout_only(self, monkeypatch):
        pytest.importorskip("requests")
        from requests.adapters import HTTPAdapter

        from env_provider.cubesandbox.manager import _TimeoutAdapter

        captured: dict = {}
        monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: captured.update(kw))
        adapter = _TimeoutAdapter(5.0)
        adapter.send("req")
        assert captured["timeout"] == 5.0
        adapter.send("req", timeout=2.0)
        assert captured["timeout"] == 2.0  # explicit timeout wins
        adapter.send("req", timeout=None)
        assert captured["timeout"] == 5.0  # requests' default None -> bounded

    def test_create_and_attach_pin_the_session(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            "env_provider.cubesandbox.manager.pin_session_timeout",
            lambda sbx, t: calls.append((sbx, t)),
        )
        fake = _patch(monkeypatch, _FakeSandboxCls())
        mgr = CubesandboxManager(_kc(total_count=1), Config(template="tpl", request_timeout=11.0), Event())
        state = mgr._new_state(1)
        mgr._create_single(state)
        assert calls == [(state.cube_sandbox, 11.0)]
        fake.list_items = [{"sandboxID": "cube-a"}]
        handle = mgr._attach({"sandboxID": "cube-a"})
        assert calls[-1] == (handle, 11.0)

    def test_version_mismatch_warns_once(self, monkeypatch, caplog):
        # The pin rides SDK-internal `_session` layout verified against
        # _TESTED_SDK_VERSION; upstream drift must be visible in the run log.
        import logging
        import sys

        import env_provider.cubesandbox.manager as mgr_mod

        monkeypatch.setattr(mgr_mod, "_guard_warned", set())
        monkeypatch.setitem(sys.modules, "cubesandbox", type("_M", (), {"__version__": "9.9.9"})())

        class _Sbx:
            def __init__(self):
                self._session = type("_Sess", (), {"mount": staticmethod(lambda p, a: None)})()

        with caplog.at_level(logging.WARNING, logger=mgr_mod.__name__):
            mgr_mod.pin_session_timeout(_Sbx(), 1.0)
            mgr_mod.pin_session_timeout(_Sbx(), 1.0)
        version_warnings = [r for r in caplog.records if "pin-tested" in r.getMessage()]
        assert len(version_warnings) == 1  # warned, exactly once

    def test_no_session_warns_once(self, monkeypatch, caplog):
        # A skipped pin (SDK renamed/removed `_session`) silently unbounds every
        # control-plane call -- must warn once, not per handle (1320 at oversub).
        import logging

        import env_provider.cubesandbox.manager as mgr_mod

        monkeypatch.setattr(mgr_mod, "_guard_warned", set())
        with caplog.at_level(logging.WARNING, logger=mgr_mod.__name__):
            mgr_mod.pin_session_timeout(object(), 1.0)
            mgr_mod.pin_session_timeout(object(), 1.0)
        layout_warnings = [r for r in caplog.records if "no _session" in r.getMessage()]
        assert len(layout_warnings) == 1


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


# --------------------------------------------------------------------- _call_bounded
class TestCallBounded:
    def test_returns_result(self):
        assert _call_bounded(lambda: 42, 5.0) == 42

    def test_reraises_error_on_caller_thread(self):
        with pytest.raises(RuntimeError, match="boom"):
            _call_bounded(lambda: (_ for _ in ()).throw(RuntimeError("boom")), 5.0)

    def test_blocking_call_times_out_bounded(self):
        release = threading.Event()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError, match="did not return"):
                _call_bounded(release.wait, 0.2)
            assert time.monotonic() - start < 5.0  # abandoned at the deadline, daemon thread left behind
        finally:
            release.set()

    def test_create_single_blocking_sdk_fails_fast(self, monkeypatch):
        # Sandbox.create rides a throwaway session (un-pinnable by
        # pin_session_timeout); a blackholed create must return a failure dict
        # at request_timeout instead of wedging the caller forever.
        fake = _FakeSandboxCls()
        release = threading.Event()
        fake.create = lambda *a, **k: release.wait(30)
        _patch(monkeypatch, fake)
        cfg = Config(template="t", request_timeout=0.2)
        mgr = CubesandboxManager(_kc(total_count=1), cfg, Event())
        state = mgr._new_state(1)
        try:
            result = mgr._create_single(state)
        finally:
            release.set()
        assert result["success"] is False
        assert "did not return" in result["error"]
