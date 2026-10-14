"""CCL CPU-affinity binding tests for AenvProvider (no live firecracker/Linux).

Covers three layers:
1. Pure helpers: ``get_ccl_cpu_affinity`` topology math, ``read_kernel_cpu_affinity``
   /proc/status parsing.
2. ``bind_ccl_affinity`` best-effort guard contract (skip paths + verified bind),
   and the resolver's setdefault cache (hit short-circuits, miss scans).
3. ``AenvProvider`` integration: ``create_all`` binds each sandbox iff ``ccl_bind``
   is on AND the replay mode is ``exec_only`` (driven through the real
   SandboxManager + a fake e2b Sandbox, mirroring test_aenv_ephemeral).
   ``create_one`` (trajectory) and ``resume`` (lifecycle) never bind -- their
   firecracker respawns per resume, so the pid is unstable.
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading

import pytest

import bench_core.task_runner.replay  # noqa: F401 -- registers the replay workflow
from bench_core.config import KernelConfig
from bench_core.task_runner.replay_config import ReplayConfig
from env_provider.aenv import AenvProvider
from env_provider.aenv import _ccl as ccl_mod
from env_provider.aenv._ccl import (
    LocalFirecrackerPidResolver,
    RESOLVER,
    bind_ccl_affinity,
    get_ccl_cpu_affinity,
    read_kernel_cpu_affinity,
    reset_ccl_cache,
)
from env_provider.e2b.config import Config


# ----------------------------------------------------------------------- helpers
class _FakeSandboxCls:
    """Stand-in for ``e2b.Sandbox`` (static create/connect) -- one id per call."""

    def __init__(self):
        self.created = []

    def create(self, template, timeout=86400, envs=None, metadata=None):  # noqa: ARG002
        sid = f"sbx-{len(self.created) + 1}"
        sbx = _FakeSbx(sid)
        self.created.append((template, timeout, envs, metadata, sbx))
        return sbx

    def list(self):
        class _Paginator:
            has_next = False

            def next_items(self):
                return []

        return _Paginator()

    def connect(self, sid):
        return _FakeSbx(sid)


class _FakeSbx:
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


def _make_provider(monkeypatch, *, ccl_bind=False, total_count=1, replay_mode="exec_only"):
    """Build an AenvProvider with a fake e2b SDK + an attached ReplayConfig.

    ``replay_mode`` defaults to ``exec_only`` (the only mode where CCL binds);
    pass ``"lifecycle"`` / ``"trajectory"`` / ``None`` to exercise the skip path.
    """
    monkeypatch.setattr("env_provider.e2b.manager.Sandbox", _FakeSandboxCls())
    wf = ReplayConfig(replay_mode=replay_mode) if replay_mode is not None else None
    cfg = KernelConfig(workflow_type="replay", total_count=total_count, workflow_config=wf)
    return AenvProvider(cfg, Config(ccl_bind=ccl_bind), threading.Event())


def _record_binds(monkeypatch):
    calls: list[tuple[str, int]] = []

    def _rec(sandbox_id, sandbox_index, config):  # noqa: ARG001
        calls.append((sandbox_id, sandbox_index))

    monkeypatch.setattr("env_provider.aenv.bind_ccl_affinity", _rec)
    return calls


# ----------------------------------------------------------- get_ccl_cpu_affinity
def test_get_ccl_cpu_affinity_default_topology():
    # 384 vCPU / 16 per CCL = 24 CCLs.
    assert get_ccl_cpu_affinity(0, 24, 16) == list(range(0, 16))
    assert get_ccl_cpu_affinity(23, 24, 16) == list(range(368, 384))


def test_get_ccl_cpu_affinity_wraps_round_robin():
    # index 24 wraps back to CCL 0 (24 % 24 == 0).
    assert get_ccl_cpu_affinity(24, 24, 16) == list(range(0, 16))


def test_get_ccl_cpu_affinity_custom_topology():
    # 2 CCLs of 4 vCPUs each: idx 0 -> [0..3], idx 1 -> [4..7], idx 2 wraps.
    assert get_ccl_cpu_affinity(0, 2, 4) == [0, 1, 2, 3]
    assert get_ccl_cpu_affinity(1, 2, 4) == [4, 5, 6, 7]
    assert get_ccl_cpu_affinity(2, 2, 4) == [0, 1, 2, 3]


# ------------------------------------------------------- read_kernel_cpu_affinity
def test_read_kernel_cpu_affinity_parses_cpulist(monkeypatch):
    def fake(self, *a, **k):
        return "Name:\tfc\nCpus_allowed_list:\t0-15\nState:\tR\n"

    monkeypatch.setattr(pathlib.Path, "read_text", fake)
    assert read_kernel_cpu_affinity(999) == "0-15"


def test_read_kernel_cpu_affinity_missing_pid(monkeypatch):
    def fake(self, *a, **k):
        raise FileNotFoundError("no such pid")

    monkeypatch.setattr(pathlib.Path, "read_text", fake)
    assert read_kernel_cpu_affinity(999) is None


def test_read_kernel_cpu_affinity_no_field(monkeypatch):
    def fake(self, *a, **k):
        return "Name:\tfoo\nState:\tR\n"

    monkeypatch.setattr(pathlib.Path, "read_text", fake)
    assert read_kernel_cpu_affinity(999) is None


# ------------------------------------------------------------- bind_ccl_affinity
def test_bind_ccl_skips_without_sched_setaffinity(monkeypatch):
    # On a non-Linux host (no os.sched_setaffinity) the bind is a warned skip.
    monkeypatch.delattr(os, "sched_setaffinity", raising=False)
    assert bind_ccl_affinity("sbx-1", 0, Config()) is False


def test_bind_ccl_skips_without_sandbox_id(monkeypatch):
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, mask: None, raising=False)
    assert bind_ccl_affinity("", 0, Config()) is False


def test_bind_ccl_skips_when_pid_unresolved(monkeypatch):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, mask: None, raising=False)
    reset_ccl_cache()
    # No firecrackers on the host -> scan finds nothing -> resolve returns None.
    monkeypatch.setattr(LocalFirecrackerPidResolver, "_scan_pids", lambda self, t: (0, 0))
    assert bind_ccl_affinity("sbx-1", 0, Config()) is False


def test_bind_ccl_success_verified(monkeypatch):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    reset_ccl_cache()
    RESOLVER._pid_map["sbx-1"] = 999
    # Cached pid -> cache hit, no scan, no liveness check.

    set_calls: list[tuple[int, set[int]]] = []
    monkeypatch.setattr(os, "sched_setaffinity", lambda pid, mask: set_calls.append((pid, mask)), raising=False)
    # Kernel reports the requested mask back -> verified.
    monkeypatch.setattr(ccl_mod, "read_kernel_cpu_affinity", lambda pid: "0-15")

    assert bind_ccl_affinity("sbx-1", 0, Config()) is True
    assert set_calls == [(999, set(range(0, 16)))]


def test_bind_ccl_setaffinity_failure_is_false(monkeypatch):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    reset_ccl_cache()
    RESOLVER._pid_map["sbx-1"] = 999

    def _fail(pid, mask):
        raise OSError(1, "operation not permitted")

    monkeypatch.setattr(os, "sched_setaffinity", _fail, raising=False)
    assert bind_ccl_affinity("sbx-1", 0, Config()) is False


# ------------------------------------------------------------- resolver cache
def test_resolver_cache_hit_short_circuits(monkeypatch):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    reset_ccl_cache()
    RESOLVER._pid_map["sbx-1"] = 999
    scanned = {"count": 0}
    monkeypatch.setattr(
        LocalFirecrackerPidResolver, "_scan_pids", lambda self, t: scanned.__setitem__("count", 1) or (0, 0)
    )
    assert RESOLVER.resolve_firecracker_pid("sbx-1", attempts=1, timeout_sec=0.1) == 999
    assert scanned["count"] == 0  # no scan on a cache hit


def test_resolver_cache_miss_scans(monkeypatch):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    reset_ccl_cache()

    def _scan(self, t):
        self._pid_map["sbx-1"] = 1000  # scan populates the cache
        return (1, 1)

    monkeypatch.setattr(LocalFirecrackerPidResolver, "_scan_pids", _scan)
    assert RESOLVER.resolve_firecracker_pid("sbx-1", attempts=1, timeout_sec=0.1) == 1000


# ----------------------------------------------------- AenvProvider integration
def test_create_all_binds_each_sandbox_in_exec_only(monkeypatch):
    provider = _make_provider(monkeypatch, ccl_bind=True, total_count=3)
    calls = _record_binds(monkeypatch)
    provider.create_all()
    # One bind per created sandbox; indices 0..2, ids sbx-1..sbx-3.
    assert {idx for _, idx in calls} == {0, 1, 2}
    assert {sid for sid, _ in calls} == {"sbx-1", "sbx-2", "sbx-3"}


def test_create_all_skips_ccl_in_lifecycle(monkeypatch, caplog):
    provider = _make_provider(monkeypatch, ccl_bind=True, total_count=3, replay_mode="lifecycle")
    calls = _record_binds(monkeypatch)
    with caplog.at_level(logging.WARNING):
        provider.create_all()
    assert calls == []  # lifecycle respawns the firecracker -> no bind
    assert any("exec_only" in r.message for r in caplog.records)


def test_create_all_skips_ccl_when_mode_unknown(monkeypatch, caplog):
    # No workflow_config -> replay_mode is None -> skip (backward compat).
    provider = _make_provider(monkeypatch, ccl_bind=True, total_count=1, replay_mode=None)
    calls = _record_binds(monkeypatch)
    with caplog.at_level(logging.WARNING):
        provider.create_all()
    assert calls == []
    assert any("exec_only" in r.message for r in caplog.records)


def test_create_all_skips_ccl_when_disabled(monkeypatch):
    provider = _make_provider(monkeypatch, ccl_bind=False, total_count=3)
    calls = _record_binds(monkeypatch)
    provider.create_all()
    assert calls == []


def test_create_one_never_binds(monkeypatch):
    # create_one is trajectory-only (pause/resume -> pid unstable); CCL is
    # exec_only-only, so create_one never binds even with ccl_bind + exec_only.
    provider = _make_provider(monkeypatch, ccl_bind=True)
    calls = _record_binds(monkeypatch)
    provider.create_one(1, template="swb-a")
    assert calls == []


def test_resume_never_binds(monkeypatch):
    provider = _make_provider(monkeypatch, ccl_bind=True)
    calls = _record_binds(monkeypatch)
    inst = provider.create_one(1)  # create_one never binds
    assert calls == []

    # resume swaps the handle (Sandbox.connect) and does NOT re-bind -- a
    # resumed firecracker respawned under a new pid; re-resolving would block
    # the stress path. CCL is exec_only-only.
    import env_provider.aenv as aenv_mod

    monkeypatch.setattr(aenv_mod.Sandbox, "connect", staticmethod(lambda sid: _FakeSbx(sid)))
    provider.resume(inst)
    assert calls == []


# ----------------------------------------------------------------- config wiring
def test_config_from_raw_reads_ccl_fields():
    cfg = Config.from_raw(
        {
            "aenv": {
                "ccl_bind": True,
                "ccl_total_count": 8,
                "ccl_vcpus_per_ccl": 32,
            }
        },
        block="aenv",
    )
    assert cfg.ccl_bind is True
    assert cfg.ccl_total_count == 8
    assert cfg.ccl_vcpus_per_ccl == 32


def test_config_ccl_defaults_off():
    cfg = Config.from_raw({"aenv": {}}, block="aenv")
    assert cfg.ccl_bind is False
    assert cfg.ccl_total_count == 24
    assert cfg.ccl_vcpus_per_ccl == 16
