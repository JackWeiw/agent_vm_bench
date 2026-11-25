"""Unit tests for the AenvProvider (no live E2B/AENV SDK needed).

AenvProvider subclasses E2BProvider and reuses the E2B SandboxManager handle
table. pause/resume are exercised by injecting a fake manager whose
sandbox_states carries a fake SDK handle. The firecracker CPU pin
(aenv.pin_cpus, fail-closed) is exercised through the real manager with a
fake e2b Sandbox and the MMDS resolver / thread binder monkeypatched.
"""
from __future__ import annotations

import logging
import threading

import pytest

import bench_core.task_runner.replay  # noqa: F401 -- self-registers workflow_type="replay"
from bench_core.config import KernelConfig
from bench_core.task_runner.replay_config import ReplayConfig
from env_provider import LifecycleCapable, ProviderConfigError, SandboxInstance
from env_provider.aenv import AenvProvider, _parse_pin_cpus
from env_provider.e2b import E2BProvider
from env_provider.e2b.config import Config
from env_provider.e2b.schemas import SandboxState


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


# ------------------------------------------------------------------ fc cpu pin
class _PinSandboxCls:
    """Stand-in for ``e2b.Sandbox`` (static create/list/connect)."""

    def __init__(self):
        self.created = []  # (template, timeout, envs, metadata, sbx)

    def create(self, template, timeout=86400, envs=None, metadata=None):  # noqa: ARG002
        sid = f"sbx-{len(self.created) + 1}"
        sbx = _PinSbx(sid)
        self.created.append((template, timeout, envs, metadata, sbx))
        return sbx

    def list(self):
        class _Paginator:
            has_next = False

            def next_items(self):
                return []

        return _Paginator()

    def connect(self, sid):
        return _PinSbx(sid)


class _PinSbx:
    """Fake SDK handle: uname-probe output, kill flag, sandbox_id."""

    def __init__(self, sid):
        self.sandbox_id = sid
        self.killed = False

        class _Commands:
            def run(self, cmd, user="root", timeout=60, **kw):  # noqa: ARG002
                class _R:
                    exit_code = 0
                    stdout = "Linux sbx 5.4.0 #1 x86_64 GNU/Linux"
                    stderr = ""

                return _R()

        self.commands = _Commands()

    def kill(self):
        self.killed = True


def _make_pin_provider(
    monkeypatch,
    *,
    pin_cpus="100-101",
    total_count=1,
    replay_mode="exec_only",
    ccl_bind=False,
):
    """Build an AenvProvider with a fake e2b SDK over the real manager.

    ``replay_mode`` defaults to exec_only (pin applies); ``"lifecycle"`` skips
    the pin (each resume respawns the firecracker under a new pid).
    """
    fake = _PinSandboxCls()
    monkeypatch.setattr("env_provider.e2b.manager.Sandbox", fake)
    wf = ReplayConfig(replay_mode=replay_mode) if replay_mode is not None else None
    cfg = KernelConfig(workflow_type="replay", total_count=total_count, workflow_config=wf)
    provider = AenvProvider(cfg, Config(pin_cpus=pin_cpus, ccl_bind=ccl_bind), threading.Event())
    return provider, fake


def _mock_pin(monkeypatch, *, pid=999, verified=True):
    """Replace the MMDS resolver + thread binder with call recorders."""
    resolved: list[str] = []
    binds: list[tuple[int, set[int]]] = []

    class _Resolver:
        def resolve_firecracker_pid(self, sandbox_id, attempts=0, timeout_sec=0.0):  # noqa: ARG002
            resolved.append(sandbox_id)
            return pid

    def _bind(proc_pid, cpus):
        binds.append((proc_pid, cpus))
        return (verified, "bound=100-101 verified=2/2 threads")

    monkeypatch.setattr("env_provider.aenv.RESOLVER", _Resolver())
    monkeypatch.setattr("env_provider.aenv.bind_process_all_threads", _bind)
    return resolved, binds


# ----------------------------------------------------------- _parse_pin_cpus
def test_parse_pin_cpus_accepts_ranges_and_sparse():
    assert _parse_pin_cpus("100-101") == {100, 101}
    assert _parse_pin_cpus(" 100 , 103 ") == {100, 103}
    assert _parse_pin_cpus("100-102,104") == {100, 101, 102, 104}


def test_parse_pin_cpus_rejects_malformed():
    with pytest.raises(ValueError, match="bad token"):
        _parse_pin_cpus("100-101xxS")
    with pytest.raises(ValueError, match="bad range"):
        _parse_pin_cpus("5-3")


def test_parse_pin_cpus_rejects_absurd_span():
    # A YAML typo must fail the pre-check instead of materialising the range.
    with pytest.raises(ValueError, match="wider than"):
        _parse_pin_cpus("0-99999999999")


def test_parse_pin_cpus_rejects_empty():
    with pytest.raises(ValueError, match="no CPUs parsed"):
        _parse_pin_cpus(",, ")


# ------------------------------------------------- create_all: pin orchestration
def test_create_all_aborts_before_create_on_bad_pin(monkeypatch):
    # Regression: the pin format used to be validated AFTER the sandboxes
    # existed, leaking orphans. The strict pre-check aborts the run with no
    # sandbox created at all (the manager is never even built). It raises a
    # typed config error so the CLI exits non-zero instead of reporting a run
    # that created nothing as success.
    provider, fake = _make_pin_provider(monkeypatch, pin_cpus="100-101xxS")
    with pytest.raises(ProviderConfigError, match="invalid aenv pin_cpus"):
        provider.create_all()
    assert provider._manager is None
    assert fake.created == []


def test_create_all_pins_each_sandbox(monkeypatch):
    provider, fake = _make_pin_provider(monkeypatch, total_count=2)
    resolved, binds = _mock_pin(monkeypatch)
    instances = provider.create_all()
    # The resolver received each sandbox_id; the binder received (pid, cpus).
    assert resolved == ["sbx-1", "sbx-2"]
    assert binds == [(999, {100, 101}), (999, {100, 101})]
    for inst in instances.values():
        assert inst.ready is True
        assert inst.is_alive is True
    for entry in fake.created:
        assert entry[4].killed is False


def test_pin_unverified_kills_and_fails_closed(monkeypatch, caplog):
    provider, fake = _make_pin_provider(monkeypatch)
    _mock_pin(monkeypatch, verified=False)
    with caplog.at_level(logging.ERROR):
        instances = provider.create_all()
    inst = instances[1]
    assert inst.ready is False  # save_ids records only ready instances
    assert inst.is_alive is False
    assert "firecracker cpu pin failed" in (inst.creation_metrics.error or "")
    assert any("PIN FAILED" in r.message for r in caplog.records)
    # Fail-closed: the just-created sandbox is killed, not leaked as an orphan.
    assert fake.created[0][4].killed is True
    state = provider._manager.sandbox_states[1]
    assert state.torn_down is True  # cleanup_all must not re-kill it


def test_pin_unresolved_pid_fails_closed(monkeypatch):
    provider, fake = _make_pin_provider(monkeypatch)
    resolved, binds = _mock_pin(monkeypatch, pid=None)
    instances = provider.create_all()
    assert resolved == ["sbx-1"]
    assert binds == []  # no pid -> no bind attempted
    inst = instances[1]
    assert inst.ready is False
    assert "could not resolve firecracker pid" in (inst.creation_metrics.error or "")
    assert fake.created[0][4].killed is True


def test_pin_skipped_in_lifecycle_mode(monkeypatch, caplog):
    # lifecycle resume respawns the firecracker under a new pid, losing the
    # pin; skip it with a warning instead of pinning (and then lying).
    provider, fake = _make_pin_provider(monkeypatch, replay_mode="lifecycle")
    resolved, binds = _mock_pin(monkeypatch)
    with caplog.at_level(logging.WARNING):
        instances = provider.create_all()
    assert resolved == []
    assert binds == []
    assert instances[1].ready is True  # sandbox stays usable, just unpinned
    assert any("skipping pin_cpus" in r.message for r in caplog.records)
    assert fake.created[0][4].killed is False


def test_ccl_bind_skipped_when_pin_cpus_set(monkeypatch, caplog):
    # The CCL bind would overwrite the pin_cpus cpuset; pin_cpus wins and the
    # ccl pass is skipped with a warning.
    ccl_calls: list[tuple[str, int]] = []

    def _rec(sandbox_id, sandbox_index, config):  # noqa: ARG001
        ccl_calls.append((sandbox_id, sandbox_index))

    monkeypatch.setattr("env_provider.aenv.bind_ccl_affinity", _rec)
    provider, _fake = _make_pin_provider(monkeypatch, ccl_bind=True)
    resolved, _binds = _mock_pin(monkeypatch)
    with caplog.at_level(logging.WARNING):
        provider.create_all()
    assert ccl_calls == []
    assert resolved == ["sbx-1"]  # the pin itself still ran
    assert any("honoring pin_cpus" in r.message for r in caplog.records)


# ------------------------------------------------- create_one (exec_ephemeral)
def test_create_one_pins_and_returns_ready_instance(monkeypatch):
    provider, _fake = _make_pin_provider(monkeypatch, replay_mode="exec_ephemeral")
    resolved, binds = _mock_pin(monkeypatch)
    inst = provider.create_one(1)
    assert resolved == ["sbx-1"]
    assert binds == [(999, {100, 101})]
    assert inst.ready is True
    assert inst.is_alive is True


def test_create_one_aborts_on_bad_pin_before_create(monkeypatch):
    provider, fake = _make_pin_provider(monkeypatch, replay_mode="exec_ephemeral", pin_cpus="100-101xxS")
    with pytest.raises(ProviderConfigError, match="aborted"):
        provider.create_one(1)
    assert provider._manager is None
    assert fake.created == []


def test_create_one_pin_failure_fails_closed(monkeypatch):
    provider, fake = _make_pin_provider(monkeypatch, replay_mode="exec_ephemeral")
    _mock_pin(monkeypatch, verified=False)
    with pytest.raises(RuntimeError, match="firecracker cpu pin failed"):
        provider.create_one(1)
    assert fake.created[0][4].killed is True
    state = provider._manager.sandbox_states[1]
    assert state.torn_down is True
