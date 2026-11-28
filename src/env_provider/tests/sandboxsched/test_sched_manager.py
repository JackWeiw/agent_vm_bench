"""Seam tests for the sandboxsched :class:`SandboxManager`.

The provider/adapter tests mock the manager itself; these tests inject a
controllable fake OpenSandbox SDK (async ``Sandbox`` / ``SandboxManager``)
into the manager module and drive the real base lifecycle (create_all /
detect_existing / detect_from_file / cleanup_all) through it, locking the SDK
seam contract: ``Sandbox.create(image, *, extensions, skip_health_check)``
-> handle with ``.id`` / ``.get_endpoint(port)`` / ``.kill()``, the paginator
flatten in ``_list_existing``, ``Sandbox.connect`` for attach. Command
execution rides the vendored envd client (E2B-mode sandboxes speak envd's
Connect RPC, not the SDK's execd REST contract) -- these tests stub the
manager's ``_envd_client_for`` with a fake client. All fake SDK methods are
coroutines -- the manager's submit() drives them on its bridge loop exactly
like the real SDK.

The opensandbox SDK is an optional extra; the manager module imports the async
classes under a try/except that falls back to an inline mock. These tests
replace those bindings with controllable fakes, so no SDK install is needed.
"""

from __future__ import annotations

import asyncio
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
from env_provider.sandboxsched.envd_client import EnvdError, EnvdTruncatedError
from env_provider.sandboxsched.manager import (
    METADATA_MANAGED_BY,
    METADATA_SANDBOX_GROUP,
    TIMEOUT_EXIT_CODE,
    TRUNCATED_EXIT_CODE,
    SandboxManager,
)
from env_provider.sandboxsched.schemas import SandboxState


# -------------------------------------------------------------------- SDK fakes
class _RunResult:
    """Stand-in for envd_client.RunResult (exec via the envd data plane)."""

    def __init__(self, exit_code=0, stdout=b"", stderr=b""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandboxSync:
    """A single OpenSandbox Sandbox handle (created or connected)."""

    def __init__(self, sid: str, *, ready: bool = True, metadata: dict | None = None):
        self.id = sid
        self.ready = ready
        self.metadata = metadata
        self.killed = False
        self.paused = False
        self.closed = False

    async def kill(self):
        self.killed = True

    async def pause(self):
        self.paused = True

    async def close(self):  # noqa: ARG002
        self.closed = True

    async def get_endpoint(self, port):  # noqa: ARG002
        class _Endpoint:
            endpoint = "http://cni-ip:49983"

        return _Endpoint()


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
        self._ready = ready  # noqa: ARG002  (envd readiness is stubbed separately now)

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
        ready_timeout=None,  # noqa: ARG002
    ):  # noqa: ARG004
        self._next_id += 1
        sid = f"sbx-{self._next_id:04d}"
        sbx = _FakeSandboxSync(sid, ready=self._ready, metadata=dict(metadata or {}))
        self.created.append((image, extensions, sbx))
        return sbx

    async def connect(self, sandbox_id, connection_config=None, skip_health_check=False, connect_timeout=None):  # noqa: ARG002
        self.connected.append(sandbox_id)
        return _FakeSandboxSync(sandbox_id, ready=self._ready)

    async def resume(self, sandbox_id, connection_config=None, skip_health_check=False, resume_timeout=None):  # noqa: ARG002
        self.resumed.append(sandbox_id)
        return _FakeSandboxSync(sandbox_id, ready=self._ready)


class _FakeManagerSync:
    def __init__(self):
        self.list_pages: list[_PagedInfos] = []
        self.list_filters: list = []  # SandboxFilter seen per list call
        self.killed: list[str] = []
        self.closed = False
        # Control-plane state model for the state-gating tests: ``states``
        # holds the current state per sandbox id; ``transitions`` serves a
        # sequence of states per get_sandbox_info call (last one sticks) to
        # simulate the server's 202-then-transition async contract.
        self.states: dict[str, str] = {}
        self.transitions: dict[str, list[str]] = {}

    def _info(self, sandbox_id: str):
        seq = self.transitions.get(sandbox_id)
        if seq:
            state = seq.pop(0)
            if not seq:
                self.transitions.pop(sandbox_id)
                self.states[sandbox_id] = state
        else:
            state = self.states.get(sandbox_id, "Running")
        return type("Info", (), {"status": type("S", (), {"state": state})()})()

    async def get_sandbox_info(self, sandbox_id):
        return self._info(sandbox_id)

    async def list_sandbox_infos(self, flt):
        self.list_filters.append(flt)
        if not self.list_pages:
            return _PagedInfos([], has_next_page=False)
        return self.list_pages.pop(0)

    async def kill_sandbox(self, sandbox_id):
        self.killed.append(sandbox_id)

    async def close(self):  # noqa: ARG002
        self.closed = True


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
    return Config(sandbox_group="sg-1u1g", template="python:3.11", sandbox_ttl=300)


@pytest.fixture
def patched_sdk(monkeypatch):
    """Replace the SDK bindings in the manager module with controllable fakes."""
    fake_sandbox = _FakeSandboxSyncCls()
    fake_manager_inner = _FakeManagerSync()
    fake_manager = _FakeManagerCls(fake_manager_inner)
    monkeypatch.setattr(mgr_mod, "Sandbox", fake_sandbox)
    monkeypatch.setattr(mgr_mod, "SdkSandboxManager", fake_manager)
    # backend-state waits poll at the module constant; speed the tests up
    monkeypatch.setattr(mgr_mod, "_STATE_POLL_INTERVAL", 0.01)
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

    def test_create_stamps_ownership_metadata(self, kernel_config, sched_config, patched_sdk):
        # cleanup/detect scope their listing by this tag: without it,
        # bench-core --cleanup on a shared OpenSandbox server would kill every
        # RUNNING/PAUSED sandbox on the server, other users' fleets included
        fake_sandbox, _ = patched_sdk
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.create_all()
        for _, _, sbx in fake_sandbox.created:
            assert sbx.metadata[METADATA_MANAGED_BY] == "bench-core"
            assert sbx.metadata[METADATA_SANDBOX_GROUP] == "sg-1u1g"

    def test_create_caller_metadata_survives_but_cannot_override_tag(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, _ = patched_sdk
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        # the ephemeral path forwards caller metadata (trajectory_id) through
        # _create_single -- it must land on the sandbox AND not clobber the tag
        fake_sandbox.created.clear()
        mgr._create_single(state, metadata={"trajectory_id": "t-1"})
        sbx = fake_sandbox.created[0][2]
        assert sbx.metadata["trajectory_id"] == "t-1"
        assert sbx.metadata[METADATA_MANAGED_BY] == "bench-core"

    def test_cleanup_existing_lists_only_own_fleet(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("p-1")])]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.cleanup_existing()
        # the list query itself carries the ownership filter -- the server
        # returns only this config's sandboxes, so nothing else is killable
        assert len(fake_manager.list_filters) == 1
        assert fake_manager.list_filters[0].metadata == {
            METADATA_MANAGED_BY: "bench-core",
            METADATA_SANDBOX_GROUP: "sg-1u1g",
        }

    def test_detect_existing_lists_only_own_fleet(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("x")])]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.detect_existing()
        assert fake_manager.list_filters[0].metadata == {
            METADATA_MANAGED_BY: "bench-core",
            METADATA_SANDBOX_GROUP: "sg-1u1g",
        }


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

    def test_detect_from_file_lists_unfiltered(self, kernel_config, sched_config, patched_sdk, tmp_path):
        # the file names explicit ids the user owns -- matching by id is the
        # ownership claim, so the listing must NOT scope to the metadata tag
        _, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("keep-1")])]
        ids_file = tmp_path / "ids.txt"
        ids_file.write_text("keep-1\n")
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.detect_from_file(str(ids_file))
        assert fake_manager.list_filters[0].metadata is None

    def test_detect_from_file_recovers_templates(self, kernel_config, sched_config, patched_sdk, tmp_path):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("keep-1")])]
        ids_file = tmp_path / "ids.txt"
        ids_file.write_text("keep-1\tubuntu-22-04-custom\nother-9\n")
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.detect_from_file(str(ids_file))
        assert len(states) == 1
        # the file's "<id>\t<template>" binding survives the create->detect
        # boundary so template-affinity routing keeps working after a restart
        assert mgr._slot_templates[1] == "ubuntu-22-04-custom"

    def test_list_states_counts_by_status(self, kernel_config, sched_config, patched_sdk):
        _, fake_manager = patched_sdk
        fake_manager.list_pages = [
            _PagedInfos([_SandboxInfo("a"), _SandboxInfo("b", state="Paused"), _SandboxInfo("c")])
        ]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        # SDK info carries status.state; the _state_of override normalizes it
        assert mgr.list_states() == {"running": 2, "paused": 1}


# -------------------------------------------------------------------- exec probe (envd)
class _StubEnvd:
    """Fake envd client: records commands, returns canned results."""

    def __init__(self, *, healthy: bool = True, stdout: bytes = b"Linux sandbox 6.6 aarch64\n"):
        self.healthy = healthy
        self.stdout = stdout
        self.commands: list[str] = []

    def run(self, cmd, *, envs=None, cwd=None, timeout=None):  # noqa: ARG002
        self.commands.append(cmd)
        return _RunResult(0, self.stdout, b"")

    def health(self, timeout=None):  # noqa: ARG002
        return self.healthy


class TestExecProbe:
    def test_exec_probe_routes_through_envd(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        handle = mgr._handle_of(next(iter(states.values())))
        envd = _StubEnvd()
        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: envd)
        code, out, _ = mgr._exec_probe(handle, "uname -a", timeout=10)
        assert code == 0
        assert "Linux sandbox" in out
        assert envd.commands == ["uname -a"]

    def test_check_alive_true_on_healthy_envd(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _StubEnvd(healthy=True))
        assert mgr.check_alive(state) is True

    def test_check_alive_false_on_unhealthy_envd(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _StubEnvd(healthy=False))
        assert mgr.check_alive(state) is False

    def test_resume_and_kill_invalidate_envd_urls(self, kernel_config, sched_config, patched_sdk):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        sid = state.sandbox_obj.id
        # a stale endpoint URL (e.g. resolved before a pause) must not survive:
        # the endpoint may change across pause/resume
        mgr._envd_urls[sid] = "http://stale:49983"
        handle = mgr.resume_handle(sid)
        assert sid not in mgr._envd_urls
        # the next envd client build re-resolves through get_endpoint
        mgr._envd_client_for(handle)
        assert mgr._envd_urls[sid] == "http://cni-ip:49983"
        # kill drops the entry again
        mgr._kill_one(state)
        assert sid not in mgr._envd_urls


# -------------------------------------------------------------------- state gating
class TestStateGating:
    """The OpenSandbox API is asynchronous (202 + server-side transition) and an
    invalid transition fails server-side, so pause/resume gate on the backend
    state: resume skips the POST when the sandbox is already up, pause skips it
    when already Paused, and both wait for the transition to land."""

    def test_resume_skips_post_when_already_running(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.states["sbx-0001"] = "Running"
        mgr = SandboxManager(kernel_config, sched_config, Event())
        handle = mgr.resume_handle("sbx-0001")
        # resuming a Running sandbox is an invalid transition server-side:
        # the gate re-attaches with connect instead of POSTing
        assert fake_sandbox.resumed == []
        assert fake_sandbox.connected == ["sbx-0001"]
        assert handle.id == "sbx-0001"

    def test_resume_posts_when_paused_and_waits_for_running(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.states["sbx-0001"] = "Paused"
        # first get_sandbox_info (the gate) sees Paused; after the POST the
        # wait polls consume resuming -> running
        fake_manager.transitions["sbx-0001"] = ["paused", "resuming", "running"]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        handle = mgr.resume_handle("sbx-0001")
        assert fake_sandbox.resumed == ["sbx-0001"]
        assert handle.id == "sbx-0001"

    def test_pause_skips_post_when_already_paused(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.states["sbx-0001"] = "Paused"
        mgr = SandboxManager(kernel_config, sched_config, Event())
        handle = _FakeSandboxSync("sbx-0001")
        mgr.pause_handle(SandboxState(sandbox_id=1, sandbox_obj=handle))
        # pausing a Paused sandbox is an invalid transition server-side: no POST
        assert handle.paused is False

    def test_pause_posts_and_waits_for_paused(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.states["sbx-0001"] = "running"
        # first get_sandbox_info (the gate) sees running; after the POST the
        # wait polls consume pausing -> paused
        fake_manager.transitions["sbx-0001"] = ["running", "pausing", "paused"]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        handle = _FakeSandboxSync("sbx-0001")
        mgr.pause_handle(SandboxState(sandbox_id=1, sandbox_obj=handle))
        assert handle.paused is True  # POST happened
        # the transition sequence was consumed down to the sticky Paused state
        assert fake_manager.states["sbx-0001"] == "paused"


# -------------------------------------------------------------------- exec timeout / handle lifecycle
class _PartialTimeoutEnvd:
    """Fake envd client: streams partial output, then the read times out."""

    def run(self, cmd, *, envs=None, cwd=None, on_stdout=None, on_stderr=None, timeout=None):  # noqa: ARG002
        on_stdout(b"partial ")
        on_stdout(b"output")
        raise TimeoutError("read timed out")


class TestExecTimeoutMapping:
    """A command timeout that already produced output is exit-124 with the
    partials kept (the timeout(1) convention, mirroring e2b/cubesandbox), so
    the replay runner buckets the step and continues instead of stranding the
    sandbox mid-slice."""

    def test_timeout_with_partial_output_maps_to_124(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _PartialTimeoutEnvd())
        code, out, err = mgr.envd_exec(state, "long-cmd", timeout=5)
        assert code == 124
        assert out == "partial output"

    def test_transport_error_without_output_propagates(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))

        class _DeadEnvd:
            def run(self, cmd, **kwargs):  # noqa: ARG002
                raise EnvdError("HTTP 500 POST /process.Process/Start: boom")

        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _DeadEnvd())
        with pytest.raises(EnvdError):
            mgr.envd_exec(state, "cmd", timeout=5)

    def test_silent_timeout_with_zero_output_maps_to_124(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        # a hung command (blocked I/O, sleep) raises TimeoutError with NO output:
        # still bucketed timed_out, so the slice continues and the sandbox is
        # not stranded
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))

        class _HungEnvd:
            def run(self, cmd, *, envs=None, cwd=None, on_stdout=None, on_stderr=None, timeout=None):  # noqa: ARG002
                raise TimeoutError("read timed out")

        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _HungEnvd())
        code, out, err = mgr.envd_exec(state, "sleep 9999", timeout=5)
        assert code == 124
        assert out == "" and err == ""


class TestTruncationMapping:
    """A truncated exec stream (killed process / the server's ~180s upstream
    timeout cutting the connection) is NOT a command timeout: it maps to its
    own exit code (125) instead of 124, so replay statistics can tell "the
    command ran too long" apart from "the output was cut"."""

    def test_truncation_with_partial_output_maps_to_125(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))

        class _CutEnvd:
            def run(self, cmd, *, envs=None, cwd=None, on_stdout=None, on_stderr=None, timeout=None):  # noqa: ARG002
                on_stdout(b"partial ")
                on_stdout(b"output")
                raise EnvdTruncatedError("connect stream ended without an end-of-stream frame: exec truncated")

        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _CutEnvd())
        code, out, _ = mgr.envd_exec(state, "killed-cmd", timeout=5)
        assert code == TRUNCATED_EXIT_CODE == 125
        assert code != TIMEOUT_EXIT_CODE
        assert out == "partial output"

    def test_truncation_with_zero_output_maps_to_125(self, kernel_config, sched_config, patched_sdk, monkeypatch):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))

        class _CutEnvd:
            def run(self, cmd, **kwargs):  # noqa: ARG002
                raise EnvdTruncatedError("connect stream truncated: partial frame header")

        monkeypatch.setattr(mgr, "_envd_client_for", lambda h: _CutEnvd())
        code, out, err = mgr.envd_exec(state, "killed-cmd", timeout=5)
        assert code == 125
        assert out == "" and err == ""


class TestHandleLifecycle:
    def test_kill_closes_the_handle(self, kernel_config, sched_config, patched_sdk):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        states = mgr.create_all()
        state = next(iter(states.values()))
        mgr._kill_one(state)
        # kill() does not release the handle's transport; close() must run
        assert state.sandbox_obj.closed is True

    def test_cleanup_existing_kills_paused_without_connect(self, kernel_config, sched_config, patched_sdk):
        fake_sandbox, fake_manager = patched_sdk
        fake_manager.list_pages = [_PagedInfos([_SandboxInfo("p-1"), _SandboxInfo("p-2")])]
        mgr = SandboxManager(kernel_config, sched_config, Event())
        killed = mgr.cleanup_existing()
        assert killed == 2
        # control-plane kill (needs no endpoint): works for a PAUSED fleet
        assert fake_manager.killed == ["p-1", "p-2"]
        # no attach: Sandbox.connect would stall ~ready_timeout per paused
        # sandbox and then fail, leaking the fleet
        assert fake_sandbox.connected == []

    def test_submit_bounds_hung_bridge_calls(self, kernel_config, patched_sdk, monkeypatch):
        monkeypatch.setattr(mgr_mod, "_SUBMIT_TIMEOUT_MARGIN", 0.2)
        cfg = Config(sandbox_group="sg-1", ready_timeout=0)  # submit bound = 0.2s
        mgr = SandboxManager(kernel_config, cfg, Event())

        async def _hang():
            await asyncio.sleep(30)

        with pytest.raises(TimeoutError):
            mgr.submit(_hang())

    def test_shutdown_closes_info_manager_and_stops_loop(self, kernel_config, sched_config, patched_sdk):
        _, fake_manager = patched_sdk
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.create_all()  # starts the bridge loop
        assert mgr._loop_thread is not None
        thread = mgr._loop_thread._thread
        mgr._backend_state("sbx-0001")  # builds the cached info manager
        mgr.shutdown()
        # the admin manager's transport is closed, the loop thread joined
        assert fake_manager.closed is True
        assert not thread.is_alive()
        assert mgr._loop_thread is None and mgr._info_manager is None
        # idempotent: a second call is a no-op
        mgr.shutdown()

    def test_shutdown_without_bridge_is_noop(self, kernel_config, sched_config):
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.shutdown()  # never talked to the SDK: nothing to tear down
        assert mgr._loop_thread is None

    def test_submit_after_shutdown_raises(self, kernel_config, sched_config):
        # a call landing on a torn-down manager is a caller bug: raise
        # instead of silently reviving a bridge loop nothing will ever stop
        mgr = SandboxManager(kernel_config, sched_config, Event())
        mgr.shutdown()
        coro = asyncio.sleep(0)
        coro.close()  # never submitted; closing avoids the never-awaited warning
        with pytest.raises(RuntimeError, match="shut down"):
            mgr.submit(coro)
