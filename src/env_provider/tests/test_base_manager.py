"""Tests for the :class:`BaseSandboxManager` lifecycle template.

The e2b/docker provider tests mock the manager, so the real lifecycle skeleton
(create_all / _create_batched / detect_existing / cleanup_all + the result→status
mapping driven by ReadyChecker) has no direct coverage. A toy subclass supplies
SDK seams; these tests lock the skeleton's behavior so a backend migration
(e2b/docker inheriting the base) can rely on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event

from env_provider._base import (
    BackendCreationMetrics,
    BackendSandboxStatus,
    BaseSandboxManager,
)


@dataclass
class FakeState:
    sandbox_id: int
    sandbox_obj: object | None = None  # the handle attr name matches _handle_attr
    batch_id: int = -1
    creation_metrics: BackendCreationMetrics = field(default_factory=BackendCreationMetrics)
    is_alive: bool = True
    stopped_by_cleanup: bool = False


class FakeListed:
    def __init__(self, sid: str):
        self.sandbox_id = sid


class FakeManager(BaseSandboxManager):
    """Toy backend: every probe succeeds; create/attach/kill are recorded."""

    _handle_attr = "sandbox_obj"
    _noun = "Sandbox"
    _set_killed_on_cleanup = False

    def __init__(self, kernel_config, stop_event, *, create_fails=(), ready_fails=()):
        super().__init__(kernel_config, stop_event)
        self.created: list[int] = []
        self.killed: list[int] = []
        self.seen_templates: list[tuple[int, str | None]] = []
        self._create_fails = set(create_fails)
        self._ready_fails = set(ready_fails)

    def _new_state(self, index, *, batch_id=-1, external_id=""):
        return FakeState(sandbox_id=index, batch_id=batch_id)

    def _create_single(self, state, *, template=None):
        self.created.append(state.sandbox_id)
        self.seen_templates.append((state.sandbox_id, template))
        if state.sandbox_id in self._create_fails:
            state.creation_metrics.create_ready_time = 0.0
            return {"success": False, "create_elapsed": 0.0, "error": "boom", "template": template or "default"}
        state.sandbox_obj = object()  # a handle
        state.creation_metrics.status = BackendSandboxStatus.CREATED
        return {"success": True, "create_elapsed": 1.0, "error": "", "template": template or "default"}

    def _list_existing(self):
        return [FakeListed("sbx-a"), FakeListed("sbx-b")]

    def _external_id(self, listed):
        return listed.sandbox_id

    def _attach(self, listed):
        return object()

    def _kill_one(self, state):
        self.killed.append(state.sandbox_id)

    def _exec_probe(self, handle, cmd, timeout):
        return (0, "ok\n", "")

    def _ready_config(self):
        return (2, 1, [18789, 11436])

    # override ready to simulate failures without sleeping
    def _ready_checker(self):
        checker = super()._ready_checker()

        def fake_check(handle, workflow_type, label):
            idx = int(label.replace("Sandbox", ""))
            if idx in self._ready_fails:
                return {"success": False, "wait_elapsed": 0.5, "error": "timeout"}
            return {"success": True, "wait_elapsed": 0.5, "error": ""}

        checker.check = fake_check  # type: ignore[method-assign]
        return checker


def _kernel_config(total_count=4, create_batch_size=None, create_batch_interval=0):
    from bench_core.config import KernelConfig

    return KernelConfig(
        total_count=total_count,
        create_batch_size=create_batch_size,
        create_batch_interval=create_batch_interval,
        workflow_type="coding",
    )


# ---------------------------------------------------------------------- create
class TestCreateAll:
    def test_concurrent_create_marks_ready(self):
        mgr = FakeManager(_kernel_config(total_count=3), Event())
        states = mgr.create_all()

        assert sorted(states) == [1, 2, 3]
        assert all(s.creation_metrics.status == BackendSandboxStatus.PORT_READY for s in states.values())
        assert states[1].sandbox_obj is not None
        assert sorted(mgr.created) == [1, 2, 3]  # order is non-deterministic (concurrent)

    def test_batched_create_runs_in_batches(self):
        mgr = FakeManager(
            _kernel_config(total_count=4, create_batch_size=2, create_batch_interval=0),
            Event(),
        )
        states = mgr.create_all()

        assert sorted(states) == [1, 2, 3, 4]
        assert all(s.batch_id in (0, 1) for s in states.values())
        assert states[1].batch_id == 0 and states[3].batch_id == 1

    def test_create_fail_marks_failed(self):
        mgr = FakeManager(_kernel_config(total_count=3), Event(), create_fails={2})
        states = mgr.create_all()

        assert states[2].creation_metrics.status == BackendSandboxStatus.FAILED
        assert states[2].creation_metrics.error_msg == "boom"
        assert states[1].creation_metrics.status == BackendSandboxStatus.PORT_READY

    def test_ready_fail_marks_port_failed(self):
        mgr = FakeManager(_kernel_config(total_count=3), Event(), ready_fails={3})
        states = mgr.create_all()

        assert states[3].creation_metrics.status == BackendSandboxStatus.PORT_FAILED
        assert "timeout" in states[3].creation_metrics.port_check_error
        assert states[1].creation_metrics.status == BackendSandboxStatus.PORT_READY

    def test_stop_event_aborts_batched_creation(self):
        stop = Event()
        stop.set()
        mgr = FakeManager(
            _kernel_config(total_count=4, create_batch_size=2, create_batch_interval=0),
            stop,
        )
        states = mgr.create_all()

        assert states == {}  # aborted before any batch
        assert mgr.created == []


# ----------------------------------------------------------------------- detect
class TestDetectExisting:
    def test_detect_attaches_and_marks_ready(self):
        mgr = FakeManager(_kernel_config(), Event())
        states = mgr.detect_existing()

        assert sorted(states) == [1, 2]
        assert states[1].sandbox_obj is not None
        assert all(s.creation_metrics.status == BackendSandboxStatus.PORT_READY for s in states.values())
        assert states[1].creation_metrics.port_wait_elapsed == 0.5

    def test_detect_empty_list_returns_empty(self):
        mgr = FakeManager(_kernel_config(), Event())
        mgr._list_existing = lambda: []  # type: ignore[method-assign]
        assert mgr.detect_existing() == {}


# --------------------------------------------------------------------- cleanup
class TestCleanupExisting:
    def test_lists_attaches_kills_each_without_ready_check(self):
        # --cleanup lists fresh, attaches each, kills each -- WITHOUT the
        # readiness probe (we are tearing down, not running tasks), so a dead
        # sandbox or a service-down browser container can't stall teardown.
        mgr = FakeManager(_kernel_config(), Event())
        mgr._list_existing = lambda: [FakeListed("sbx-a"), FakeListed("sbx-b")]  # type: ignore[assignment]

        killed = mgr.cleanup_existing()

        assert killed == 2
        # attach + kill each (FakeManager records attaches via the FakeListed-
        # derived id; killed list records the sandbox_id the state carried).
        assert mgr.killed == [1, 2]
        # The ready checker is never built (no readiness probe on cleanup).
        assert mgr._ready is None

    def test_empty_list_kills_none(self):
        mgr = FakeManager(_kernel_config(), Event())
        mgr._list_existing = lambda: []  # type: ignore[assignment]
        assert mgr.cleanup_existing() == 0
        assert mgr.killed == []

    def test_list_failure_returns_zero(self):
        mgr = FakeManager(_kernel_config(), Event())

        def boom():
            raise RuntimeError("list API down")

        mgr._list_existing = boom  # type: ignore[assignment]
        assert mgr.cleanup_existing() == 0
        assert mgr.killed == []

    def test_one_kill_error_does_not_abort_rest(self):
        mgr = FakeManager(_kernel_config(), Event())
        mgr._list_existing = lambda: [FakeListed("a"), FakeListed("b"), FakeListed("c")]  # type: ignore[assignment]
        # Make the second attach fail; the others must still be killed.
        original_attach = mgr._attach

        calls = {"n": 0}

        def flaky_attach(listed):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("attach failed")
            return original_attach(listed)

        mgr._attach = flaky_attach  # type: ignore[assignment]
        killed = mgr.cleanup_existing()
        assert killed == 2  # first + third; second skipped
        assert sorted(mgr.killed) == [1, 3]


# --------------------------------------------------------------------- cleanup
class TestCleanupAll:
    def test_kill_all_marks_stopped_by_cleanup(self):
        mgr = FakeManager(_kernel_config(total_count=3), Event())
        mgr.create_all()
        mgr.cleanup_all()

        assert mgr.killed == [1, 2, 3]
        assert all(s.is_alive is False for s in mgr._states.values())
        assert all(s.stopped_by_cleanup is True for s in mgr._states.values())

    def test_no_handle_skipped(self):
        mgr = FakeManager(_kernel_config(total_count=2), Event(), create_fails={1, 2})
        mgr.create_all()  # both failed -> no handle
        mgr.cleanup_all()
        assert mgr.killed == []

    def test_killed_status_flag_respected(self):
        class KillSetsStatus(FakeManager):
            _set_killed_on_cleanup = True

        mgr = KillSetsStatus(_kernel_config(total_count=2), Event())
        mgr.create_all()
        mgr.cleanup_all()

        assert all(s.creation_metrics.status == BackendSandboxStatus.KILLED for s in mgr._states.values())

    def test_already_offline_not_marked_stopped(self):
        mgr = FakeManager(_kernel_config(total_count=2), Event())
        mgr.create_all()
        mgr._states[1].is_alive = False  # already offline before cleanup
        mgr.cleanup_all()
        assert mgr._states[1].stopped_by_cleanup is False
        assert mgr._states[2].stopped_by_cleanup is True


# --------------------------------------------------------------- templates
class TestCreateAllTemplates:
    def test_create_all_threads_templates_to_create_single(self):
        # _FakeManager records (index, template) seen by _create_single.
        mgr = FakeManager(_kernel_config(total_count=3), Event())
        mgr.create_all(templates={0: "swb-a", 1: "swb-b", 2: "swb-a"})
        seen = {i: t for i, t in mgr.seen_templates}
        assert seen[1] == "swb-a"  # slot 0 -> sandbox_id 1
        assert seen[2] == "swb-b"
        assert seen[3] == "swb-a"
        # resolved templates recorded for _to_instance:
        assert mgr._slot_templates == {1: "swb-a", 2: "swb-b", 3: "swb-a"}

    def test_create_all_none_templates_passes_none_per_slot(self):
        mgr = FakeManager(_kernel_config(total_count=2), Event())
        mgr.create_all()  # no templates
        assert all(t is None for _, t in mgr.seen_templates)

    def test_create_all_records_template_on_failure(self):
        # Failed creates still record the resolved template in _slot_templates,
        # so downstream code can inspect the per-slot mapping regardless of outcome.
        mgr = FakeManager(_kernel_config(total_count=2), Event(), create_fails={2})
        mgr.create_all(templates={0: "swb-a", 1: "swb-b"})
        assert mgr._slot_templates[1] == "swb-a"
        assert mgr._slot_templates[2] == "swb-b"  # failure path still records
        assert mgr._states[2].creation_metrics.status == BackendSandboxStatus.FAILED

    def test_create_batched_threads_templates(self):
        cfg = _kernel_config(total_count=4, create_batch_size=2, create_batch_interval=0)
        mgr = FakeManager(cfg, Event())
        mgr.create_all(templates={0: "t0", 1: "t1", 2: "t2", 3: "t3"})
        seen = {i: t for i, t in mgr.seen_templates}
        assert seen == {1: "t0", 2: "t1", 3: "t2", 4: "t3"}
        assert mgr._slot_templates == {1: "t0", 2: "t1", 3: "t2", 4: "t3"}
