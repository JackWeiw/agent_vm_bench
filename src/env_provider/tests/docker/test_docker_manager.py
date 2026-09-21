"""Seam tests for the real docker :class:`SandboxManager`.

The provider/adapter tests (``test_docker_provider.py``) mock the manager itself,
so the real manager's SDK wiring -- ``_create_single`` (stale-name removal +
containers.run kwargs), ``_list_existing`` (prefix filter), ``_attach``
(listed-container-is-the-handle), ``_exec_probe`` (bytes decode), ``_kill_one``
(container.remove) -- has no coverage. These tests inject a fake Docker client
(via ``docker.from_env``) and drive the real base lifecycle (create_all /
detect_existing / cleanup_all) through it, locking the seam contract.

Requires the docker SDK (an optional extra); the module skips via
``importorskip`` when it is absent.
"""
from __future__ import annotations

from threading import Event

import pytest

docker = pytest.importorskip("docker")
from docker.errors import NotFound  # noqa: E402

from bench_core.config import KernelConfig  # noqa: E402
from env_provider.docker.config import Config  # noqa: E402
from env_provider.docker.manager import SandboxManager  # noqa: E402


# ------------------------------------------------------------------ docker fakes
class _ExecResult:
    """Stand-in for docker SDK's ``ExecResult`` (exit_code + output)."""

    def __init__(self, exit_code: int = 0, output: bytes | str = b""):
        self.exit_code = exit_code
        self.output = output


class _FakeContainer:
    def __init__(self, name: str):
        self.name = name
        self.status = "running"
        self.removed = False
        self.reload_called = False
        self.exec_calls: list[str] = []
        self._exec_result = _ExecResult(0, b"0.0.0.0:18789 0.0.0.0:*\n")

    def reload(self):
        self.reload_called = True

    def remove(self, force: bool = False):  # noqa: ARG002
        self.removed = True

    def exec_run(self, cmd, user="root", demux=False):  # noqa: ARG002
        self.exec_calls.append(cmd)
        return self._exec_result


class _FakeContainers:
    def __init__(self):
        self._existing: dict[str, _FakeContainer] = {}  # pre-existing for get()
        self._running: list[_FakeContainer] = []  # returned by list()
        self.run_calls: list[dict] = []

    def get(self, name: str):
        if name in self._existing:
            return self._existing[name]
        raise NotFound(f"no such container: {name}")

    def run(self, image=None, name=None, detach=False, remove=False, cpu_quota=None, mem_limit=None):
        c = _FakeContainer(name)
        self.run_calls.append({"image": image, "name": name, "cpu_quota": cpu_quota, "mem_limit": mem_limit})
        self._running.append(c)
        return c

    def list(self, all=False):  # noqa: A002
        return list(self._running)


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()


# --------------------------------------------------------------------- helpers
def _client(monkeypatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr("env_provider.docker.manager.docker.from_env", lambda: client)
    return client


def _kc(**kw) -> KernelConfig:
    kw.setdefault("workflow_type", "browser")
    return KernelConfig(total_count=kw.pop("total_count", 2), **kw)


# --------------------------------------------------------------------- _create_single
class TestCreateSingle:
    def test_no_existing_runs_with_resource_limits(self, monkeypatch):
        client = _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)
        result = mgr._create_single(state)
        assert result["success"] is True
        assert state.docker_container is not None
        assert state.creation_metrics.status.value == "created"
        call = client.containers.run_calls[0]
        assert call["image"] == Config().docker_image
        assert call["name"] == "oc-bench-1"
        assert call["cpu_quota"] == 200000  # int(2.0 * 100000)
        assert call["mem_limit"] == "2g"

    def test_stale_same_name_removed_before_create(self, monkeypatch):
        client = _client(monkeypatch)
        stale = _FakeContainer("oc-bench-1")
        client.containers._existing = {"oc-bench-1": stale}
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        mgr._create_single(mgr._new_state(1))
        assert stale.removed is True

    def test_create_exception_returns_failure(self, monkeypatch):
        client = _client(monkeypatch)
        client.containers.run = lambda **kw: (_ for _ in ()).throw(RuntimeError("run failed"))
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        result = mgr._create_single(mgr._new_state(1))
        assert result["success"] is False
        assert "run failed" in result["error"]

    def test_create_single_with_template_uses_custom_image(self, monkeypatch):
        client = _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)
        result = mgr._create_single(state, template="custom-image")
        assert result["success"] is True
        call = client.containers.run_calls[0]
        assert call["image"] == "custom-image"
        assert result["template"] == "custom-image"

    def test_create_single_without_template_uses_default(self, monkeypatch):
        client = _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)
        result = mgr._create_single(state)
        assert result["success"] is True
        call = client.containers.run_calls[0]
        assert call["image"] == Config().docker_image
        assert result["template"] == Config().docker_image

    def test_create_single_exception_returns_template_in_failure(self, monkeypatch):
        client = _client(monkeypatch)
        client.containers.run = lambda **kw: (_ for _ in ()).throw(RuntimeError("run failed"))
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)
        result = mgr._create_single(state, template="custom-image")
        assert result["success"] is False
        assert result["template"] == "custom-image"


# --------------------------------------------------------------------- _list_existing
class TestListExisting:
    def test_filters_by_prefix(self, monkeypatch):
        client = _client(monkeypatch)
        keep = _FakeContainer("oc-bench-1")
        skip = _FakeContainer("other-9")
        client.containers._running = [keep, skip]
        mgr = SandboxManager(_kc(), Config(), Event())
        assert mgr._list_existing() == [keep]


# --------------------------------------------------------------------- _exec_probe
class TestExecProbe:
    def test_decodes_bytes_output(self, monkeypatch):
        _client(monkeypatch)
        mgr = SandboxManager(_kc(), Config(), Event())
        c = _FakeContainer("x")
        c._exec_result = _ExecResult(0, b"LISTENING\n")
        exit_code, stdout, stderr = mgr._exec_probe(c, "ss -tlnp", 10)
        assert exit_code == 0
        assert stdout == "LISTENING\n"
        assert stderr == ""


# --------------------------------------------------------------------- create_all
class TestCreateAll:
    def test_browser_create_marks_port_ready(self, monkeypatch):
        client = _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=3, workflow_type="browser"), Config(), Event())
        states = mgr.create_all()
        assert sorted(states) == [1, 2, 3]
        for s in states.values():
            assert s.creation_metrics.status.value == "port_ready"
            assert s.docker_container is not None
        assert len(client.containers.run_calls) == 3


# --------------------------------------------------------------------- detect
class TestDetectExisting:
    def test_attaches_listed_container_and_marks_ready(self, monkeypatch):
        client = _client(monkeypatch)
        c1 = _FakeContainer("oc-bench-1")
        c2 = _FakeContainer("oc-bench-2")
        client.containers._running = [c1, c2]
        mgr = SandboxManager(_kc(workflow_type="browser"), Config(), Event())
        states = mgr.detect_existing()
        assert sorted(states) == [1, 2]
        # _attach returns the listed container itself (docker has no connect)
        assert states[1].docker_container is c1
        assert states[2].docker_container is c2
        for s in states.values():
            assert s.creation_metrics.status.value == "port_ready"

    def test_empty_list_returns_empty(self, monkeypatch):
        _client(monkeypatch)
        mgr = SandboxManager(_kc(), Config(), Event())
        assert mgr.detect_existing() == {}


# --------------------------------------------------------------------- cleanup_existing
class TestCleanupExisting:
    def test_lists_removes_each_without_ready_check(self, monkeypatch):
        # --cleanup lists fresh, removes each -- WITHOUT the readiness probe,
        # so a service-down container can't stall on the 300s port wait.
        client = _client(monkeypatch)
        c1 = _FakeContainer("oc-bench-1")
        c2 = _FakeContainer("oc-bench-2")
        other = _FakeContainer("other-9")  # wrong prefix, filtered out
        client.containers._running = [c1, c2, other]
        mgr = SandboxManager(_kc(), Config(), Event())

        killed = mgr.cleanup_existing()

        assert killed == 2
        assert c1.removed is True
        assert c2.removed is True
        assert other.removed is False  # prefix filter holds on cleanup too
        assert mgr._ready is None  # no readiness probe on teardown

    def test_empty_list_kills_none(self, monkeypatch):
        _client(monkeypatch)
        mgr = SandboxManager(_kc(), Config(), Event())
        assert mgr.cleanup_existing() == 0


# --------------------------------------------------------------------- cleanup_all
class TestCleanupAll:
    def test_removes_each_container_and_marks_killed(self, monkeypatch):
        _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=3), Config(), Event())
        mgr.create_all()
        mgr.cleanup_all()
        for s in mgr._states.values():
            assert s.docker_container.removed is True
            assert s.is_alive is False
            assert s.creation_metrics.status.value == "killed"  # docker sets KILLED


# --------------------------------------------------------------------- check_alive
class TestCheckAlive:
    def test_running_container_is_alive(self, monkeypatch):
        _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)
        mgr._create_single(state)
        state.docker_container.status = "running"
        assert mgr.check_alive(state) is True
        assert state.docker_container.reload_called is True

    def test_exited_container_not_alive(self, monkeypatch):
        _client(monkeypatch)
        mgr = SandboxManager(_kc(total_count=1), Config(), Event())
        state = mgr._new_state(1)
        mgr._create_single(state)
        state.docker_container.status = "exited"
        assert mgr.check_alive(state) is False
