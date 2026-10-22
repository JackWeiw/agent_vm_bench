"""CCL CPU-affinity binding tests for AenvProvider (no live firecracker/Linux).

Covers four layers:
1. Pure helpers: cpulist parse/format round-trips, ``get_ccl_cpu_affinity``
   fallback arithmetic.
2. Sysfs topology detection against fake ``/sys`` trees (zen4-style SMT L3
   layout, Kunpeng-style small-CL layout, NUMA, socket) and the
   ``select_ccl_domains`` granularity / auto cascade / arithmetic fallback.
3. ``bind_process_all_threads`` (all-thread one-shot binding: main first,
   task sweep, per-tid verification) + ``bind_ccl_affinity`` best-effort guard
   contract, and the resolver's setdefault cache (hit short-circuits, miss
   scans).
4. ``AenvProvider`` integration: ``create_all`` binds each sandbox iff ``ccl_bind``
   is on AND the replay mode is ``exec_only`` / ``exec_ephemeral`` (driven
   through the real SandboxManager + a fake e2b Sandbox, mirroring
   test_aenv_ephemeral). ``create_one`` binds in ``exec_ephemeral`` (the
   ephemeral create path; pid stable for the round) but skips in
   ``trajectory``/``lifecycle`` (firecracker respawns per resume). ``resume``
   never re-binds (the resumed firecracker's pid is stale).
"""
from __future__ import annotations

import logging
import os
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
    bind_process_all_threads,
    detect_l3_domains,
    detect_numa_domains,
    detect_socket_domains,
    format_cpu_list,
    get_ccl_cpu_affinity,
    parse_cpu_list,
    reset_ccl_cache,
    select_ccl_domains,
)
from env_provider.e2b.config import Config


@pytest.fixture(autouse=True)
def _fresh_ccl_caches():
    """Every test starts with empty pid + topology caches."""
    reset_ccl_cache()
    yield
    reset_ccl_cache()


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

    ``replay_mode`` defaults to ``exec_only`` (one of the two modes where CCL
    binds); pass ``"lifecycle"`` / ``"trajectory"`` / ``None`` to exercise the
    skip path, or ``"exec_ephemeral"`` for the create_one bind path.
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


# zen4/zen5-style SMT layout: Linux numbers physical cores first, siblings
# after -- CCD 0 = "0-7,192-199" (NOT the contiguous "0-15" the legacy
# arithmetic assumed, which spans two CCDs and no siblings).
ZEN4_L3 = ["0-7,192-199", "8-15,200-207"]
# Kunpeng-style: small CLs (4 CPUs, no SMT, contiguous) inside 2 NUMA nodes.
KUNPENG_L3 = [f"{i * 4}-{i * 4 + 3}" for i in range(16)]
KUNPENG_NUMA = ["0-31", "32-63"]


def _mk_sysfs(tmp_path, *, l3=(), numa=(), sockets=None):
    """Build a fake sysfs tree; returns its root. ``l3``: one cpulist string
    per domain (written to every member cpu's index3/shared_cpu_list, as the
    kernel does). ``sockets``: {cpu_id: package_id}."""
    cpu_root = tmp_path / "devices" / "system" / "cpu"
    cpu_root.mkdir(parents=True, exist_ok=True)
    for domain in l3:
        for cpu in parse_cpu_list(domain):
            idx3 = cpu_root / f"cpu{cpu}" / "cache" / "index3"
            idx3.mkdir(parents=True, exist_ok=True)
            (idx3 / "shared_cpu_list").write_text(domain + "\n", encoding="utf-8")
    for cpu, pkg in (sockets or {}).items():
        topo = cpu_root / f"cpu{cpu}" / "topology"
        topo.mkdir(parents=True, exist_ok=True)
        (topo / "physical_package_id").write_text(f"{pkg}\n", encoding="utf-8")
    for node, cpulist in enumerate(numa):
        node_dir = tmp_path / "devices" / "system" / "node" / f"node{node}"
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "cpulist").write_text(cpulist + "\n", encoding="utf-8")
    return tmp_path


def _fake_sched(monkeypatch, tids_fn, default_mask=None):
    """Monkeypatch os.sched_setaffinity/getaffinity + _task_tids.

    ``tids_fn(pid)`` lists the process's threads; ``default_mask`` is what an
    *unbound* tid reports from getaffinity (defaults to 64 free CPUs). Returns
    the live mask dict {tid: set[int]} -- only bound tids appear in it.
    """
    masks: dict[int, set[int]] = {}
    free = set(range(64)) if default_mask is None else default_mask

    def _set(tid, cpus):
        masks[tid] = set(cpus)

    def _get(tid):
        return masks.get(tid, free)

    monkeypatch.setattr(os, "sched_setaffinity", _set, raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", _get, raising=False)
    monkeypatch.setattr(ccl_mod, "_task_tids", tids_fn)
    return masks


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


# ------------------------------------------------------- cpulist parse / format
def test_parse_cpu_list():
    assert parse_cpu_list("0-7,192-199") == set(range(8)) | set(range(192, 200))
    assert parse_cpu_list("3") == {3}
    assert parse_cpu_list("") == set()
    assert parse_cpu_list("0-1,junk,5") == {0, 1, 5}  # garbage parts skipped


def test_format_cpu_list_roundtrip():
    for text in ("0-7,192-199", "0-15", "3", "0-3,8-11,20"):
        assert format_cpu_list(parse_cpu_list(text)) == text
    assert format_cpu_list(set()) == ""


# ------------------------------------------------------------ sysfs detection
def test_detect_l3_domains_zen4_dedupes_and_sorts(tmp_path):
    root = _mk_sysfs(tmp_path, l3=ZEN4_L3)
    assert detect_l3_domains(root) == [
        set(range(8)) | set(range(192, 200)),
        set(range(8, 16)) | set(range(200, 208)),
    ]


def test_detect_l3_domains_missing_sysfs(tmp_path):
    assert detect_l3_domains(tmp_path) == []


def test_detect_numa_domains(tmp_path):
    root = _mk_sysfs(tmp_path, numa=KUNPENG_NUMA)
    assert detect_numa_domains(root) == [set(range(32)), set(range(32, 64))]


def test_detect_socket_domains(tmp_path):
    root = _mk_sysfs(tmp_path, sockets={0: 0, 1: 0, 2: 1, 3: 1})
    assert detect_socket_domains(root) == [{0, 1}, {2, 3}]


# ---------------------------------------------------------- select_ccl_domains
def test_select_auto_prefers_l3_on_zen4(tmp_path):
    root = _mk_sysfs(tmp_path, l3=ZEN4_L3, numa=["0-15,192-207", "8-15,200-207"])
    domains, source = select_ccl_domains(Config(), root)
    # auto always prefers L3 when sysfs exposes it (no capacity-based downgrade).
    assert source == "sysfs-l3(auto)"
    assert domains[0] == parse_cpu_list("0-7,192-199")


def test_select_auto_binds_kunpeng_at_cl_granularity(tmp_path):
    # Kunpeng exposes its CLs via index3 -> auto binds at CL granularity even
    # though NUMA nodes exist (no capacity-based NUMA downgrade).
    root = _mk_sysfs(tmp_path, l3=KUNPENG_L3, numa=KUNPENG_NUMA)
    domains, source = select_ccl_domains(Config(), root)
    assert source == "sysfs-l3(auto)"
    assert len(domains) == 16
    assert domains[0] == {0, 1, 2, 3}


def test_select_auto_numa_only_when_no_l3(tmp_path):
    root = _mk_sysfs(tmp_path, numa=KUNPENG_NUMA)
    domains, source = select_ccl_domains(Config(), root)
    assert source == "sysfs-numa(auto: no l3 domains)"
    assert domains == [set(range(32)), set(range(32, 64))]


def test_select_granularity_config_skips_detection(tmp_path):
    root = _mk_sysfs(tmp_path, l3=ZEN4_L3)
    domains, source = select_ccl_domains(Config(ccl_granularity="config"), root)
    assert source == "config-arithmetic"
    assert len(domains) == 24
    assert domains[0] == set(range(16))


def test_select_granularity_l3_forced(tmp_path):
    root = _mk_sysfs(tmp_path, l3=KUNPENG_L3, numa=KUNPENG_NUMA)
    domains, source = select_ccl_domains(Config(ccl_granularity="l3"), root)
    assert source == "sysfs-l3"
    assert len(domains) == 16  # no NUMA fallback when explicitly forced


def test_select_explicit_granularity_without_sysfs_falls_back(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        domains, source = select_ccl_domains(Config(ccl_granularity="l3"), tmp_path)
    assert source == "config-arithmetic"
    assert len(domains) == 24
    assert any("falling back to config arithmetic" in r.message for r in caplog.records)


def test_select_unknown_granularity_warns_and_autos(tmp_path, caplog):
    root = _mk_sysfs(tmp_path, l3=ZEN4_L3)
    with caplog.at_level(logging.WARNING):
        _, source = select_ccl_domains(Config(ccl_granularity="ccd"), root)
    assert source == "sysfs-l3(auto)"
    assert any("Unknown ccl_granularity" in r.message for r in caplog.records)


def test_select_caches_until_reset(tmp_path):
    root = _mk_sysfs(tmp_path, l3=ZEN4_L3)
    first = select_ccl_domains(Config(), root)
    # A different config/root is ignored while the cache is warm...
    assert select_ccl_domains(Config(ccl_granularity="config"), tmp_path) == first
    reset_ccl_cache()
    _, source = select_ccl_domains(Config(ccl_granularity="config"), tmp_path)
    assert source == "config-arithmetic"


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


def test_bind_ccl_success_verified_all_threads(monkeypatch, tmp_path):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    monkeypatch.setattr(ccl_mod, "_SYSFS_ROOT", tmp_path)  # empty -> arithmetic fallback
    RESOLVER._pid_map["sbx-1"] = 999
    # Cached pid -> cache hit, no scan, no liveness check.
    masks = _fake_sched(monkeypatch, lambda pid: [999, 1000, 1001])

    assert bind_ccl_affinity("sbx-1", 0, Config()) is True
    # Domain 0 of the fallback arithmetic = cpus 0-15, applied to EVERY thread
    # (main 999 + vCPU tids 1000/1001), not just the main one.
    assert masks == {tid: set(range(16)) for tid in (999, 1000, 1001)}


def test_bind_ccl_uses_detected_l3_domain_not_arithmetic(monkeypatch, tmp_path):
    # Regression: on an SMT host the arithmetic block for index 1 would be
    # cpus 16-31; the detected CCD 1 is "8-15,200-207".
    root = _mk_sysfs(tmp_path, l3=ZEN4_L3)
    monkeypatch.setattr(ccl_mod, "_SYSFS_ROOT", root)
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    RESOLVER._pid_map["sbx-2"] = 999
    masks = _fake_sched(monkeypatch, lambda pid: [999, 1000])

    assert bind_ccl_affinity("sbx-2", 1, Config()) is True
    expected = set(range(8, 16)) | set(range(200, 208))
    assert masks == {999: expected, 1000: expected}


def test_bind_ccl_setaffinity_failure_is_false(monkeypatch, tmp_path):
    monkeypatch.setattr(ccl_mod, "CCL_PID_RESOLVE_ATTEMPTS", 1)
    monkeypatch.setattr(ccl_mod, "_SYSFS_ROOT", tmp_path)
    RESOLVER._pid_map["sbx-1"] = 999

    def _fail(pid, mask):
        raise OSError(1, "operation not permitted")

    monkeypatch.setattr(os, "sched_setaffinity", _fail, raising=False)
    assert bind_ccl_affinity("sbx-1", 0, Config()) is False


# ------------------------------------------------- bind_process_all_threads
def test_bind_all_threads_covers_every_tid(monkeypatch):
    masks = _fake_sched(monkeypatch, lambda pid: [999, 1000, 1001])
    ok, detail = bind_process_all_threads(999, set(range(8)))
    assert ok
    assert "threads=3" in detail
    assert masks == {tid: set(range(8)) for tid in (999, 1000, 1001)}


def test_bind_second_pass_covers_late_thread(monkeypatch):
    # A thread that appears during the bind (vCPU spawn race) is caught by the
    # convergence re-list -- no polling needed.
    calls = {"n": 0}

    def tids(pid):
        calls["n"] += 1
        return [999] if calls["n"] == 1 else [999, 1002]

    masks = _fake_sched(monkeypatch, tids)
    ok, _ = bind_process_all_threads(999, set(range(8)))
    assert ok
    assert masks.get(1002) == set(range(8))


def test_bind_verify_fails_when_a_tid_escapes(monkeypatch):
    # Regression for the old verified=True false-positive: a vCPU thread whose
    # setaffinity silently did not stick MUST fail the per-tid verification.
    masks = _fake_sched(monkeypatch, lambda pid: [999, 1000])

    def _set(tid, cpus):
        if tid != 1000:  # 1000 escapes the bind
            masks[tid] = set(cpus)

    monkeypatch.setattr(os, "sched_setaffinity", _set, raising=False)
    ok, detail = bind_process_all_threads(999, set(range(8)))
    assert not ok
    assert "mismatched_tids=[1000]" in detail


def test_bind_main_setaffinity_failure(monkeypatch):
    def _raise(tid, cpus):
        raise OSError(1, "operation not permitted")

    monkeypatch.setattr(os, "sched_setaffinity", _raise, raising=False)
    ok, detail = bind_process_all_threads(999, set(range(8)))
    assert not ok
    assert "failed" in detail


def test_bind_verify_false_when_process_dies_mid_bind(monkeypatch):
    # A firecracker that exits between the bind and the verify must not report
    # verified=True: every getaffinity raises, so zero threads were checked.
    _fake_sched(monkeypatch, lambda pid: [999])

    def _raise_get(tid):
        raise OSError(3, "no such process")

    monkeypatch.setattr(os, "sched_getaffinity", _raise_get, raising=False)
    ok, detail = bind_process_all_threads(999, set(range(8)))
    assert not ok
    assert "no_live_thread_to_verify" in detail


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


def test_create_one_binds_in_exec_ephemeral(monkeypatch):
    # exec_ephemeral: create_one IS the create path and the firecracker pid is
    # stable for the round (no pause/resume) -> bind right after a successful
    # create, reusing create_all's _bind_ccl (best-effort).
    provider = _make_provider(monkeypatch, ccl_bind=True, replay_mode="exec_ephemeral")
    calls = _record_binds(monkeypatch)
    provider.create_one(1, template="swb-a")
    assert len(calls) == 1
    # e2b sandbox_id string + 0-based CCL index (sandbox_id 1 -> index 0).
    assert calls[0] == ("sbx-1", 0)


def test_create_one_skips_in_trajectory(monkeypatch, caplog):
    # trajectory respawns the firecracker per resume -> pid unstable -> skip.
    provider = _make_provider(monkeypatch, ccl_bind=True, replay_mode="trajectory")
    calls = _record_binds(monkeypatch)
    with caplog.at_level(logging.WARNING):
        provider.create_one(1, template="swb-a")
    assert calls == []
    assert any("exec_only" in r.message for r in caplog.records)


def test_create_one_bind_failure_is_best_effort(monkeypatch):
    # A bind that raises must not unwind a successful create (best-effort):
    # _bind_ccl wraps it in try/except -> WARNING, the sandbox stays usable.
    provider = _make_provider(monkeypatch, ccl_bind=True, replay_mode="exec_ephemeral")

    def _raise(sandbox_id, sandbox_index, config):  # noqa: ARG001
        raise RuntimeError("bind blew up")

    monkeypatch.setattr("env_provider.aenv.bind_ccl_affinity", _raise)
    inst = provider.create_one(1, template="swb-a")  # must NOT raise
    assert inst.is_alive is True


def test_resume_never_binds(monkeypatch):
    # lifecycle: create_one skips the bind (firecracker respawns per resume),
    # so the create path here leaves calls empty before we even call resume.
    provider = _make_provider(monkeypatch, ccl_bind=True, replay_mode="lifecycle")
    calls = _record_binds(monkeypatch)
    inst = provider.create_one(1)
    assert calls == []

    # resume swaps the handle (Sandbox.connect) and does NOT re-bind -- a
    # resumed firecracker respawned under a new pid; re-resolving would block
    # the stress path. CCL binds at create time only (exec_only's create_all /
    # exec_ephemeral's create_one), never on resume.
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
                "ccl_granularity": "NUMA",
                "ccl_total_count": 8,
                "ccl_vcpus_per_ccl": 32,
            }
        },
        block="aenv",
    )
    assert cfg.ccl_bind is True
    assert cfg.ccl_granularity == "numa"  # normalized to lower-case
    assert cfg.ccl_total_count == 8
    assert cfg.ccl_vcpus_per_ccl == 32


def test_config_ccl_defaults_off():
    cfg = Config.from_raw({"aenv": {}}, block="aenv")
    assert cfg.ccl_bind is False
    assert cfg.ccl_granularity == "auto"
    assert cfg.ccl_total_count == 24
    assert cfg.ccl_vcpus_per_ccl == 16


def test_config_ccl_granularity_null_becomes_auto():
    # An explicit YAML null (uncommented `ccl_granularity:` with no value) must
    # default to auto, not stringify to the bogus "none".
    cfg = Config.from_raw({"aenv": {"ccl_granularity": None}}, block="aenv")
    assert cfg.ccl_granularity == "auto"
