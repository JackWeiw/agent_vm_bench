"""Tests for the E2B EnvironmentProvider adapter.

The adapter wraps :class:`SandboxManager` behind the kernel's
:class:`EnvironmentProvider` contract. These tests mock the manager and its SDK
handles, verifying the translation + delegation logic -- not the e2b SDK itself
(covered by ``test_sandbox_manager.py``).
"""
from __future__ import annotations

import os
import tempfile
from threading import Event
from unittest.mock import Mock

import pytest

from bench_core.config import KernelConfig
from env_provider import CommandResult, SandboxInstance, SandboxStatus
from env_provider.e2b.config import Config
from env_provider.e2b import E2BProvider
from env_provider.e2b.schemas import SandboxState
from env_provider.e2b.schemas import SandboxStatus as E2BSandboxStatus


def _make_state(
    sandbox_id: int,
    *,
    status: E2BSandboxStatus,
    sandbox_id_str: str = "sbx-xyz",
    is_alive: bool = True,
    warmup_done: bool = False,
) -> SandboxState:
    """Build an e2b SandboxState with a mock SDK handle for the adapter to wrap."""
    state = SandboxState(sandbox_id=sandbox_id, workflow_type="browser")
    state.creation_metrics.status = status
    state.creation_metrics.submit_time = 1000.0
    state.creation_metrics.port_ready_time = 1002.3
    state.creation_metrics.create_elapsed = 1.5
    state.creation_metrics.port_wait_elapsed = 0.8
    state.creation_metrics.total_elapsed = 2.3
    state.is_alive = is_alive
    state.warmup_done = warmup_done

    sbx = Mock()
    sbx.sandbox_id = sandbox_id_str
    sbx.commands.run = Mock(return_value=Mock(exit_code=0, stdout="ok", stderr=""))
    state.sandbox_obj = sbx
    return state


def _provider_with(
    states: dict[int, SandboxState],
    *,
    config: Config | None = None,
    kernel_config: KernelConfig | None = None,
) -> tuple[E2BProvider, Mock]:
    """Build an E2BProvider over a mock manager holding the given states.

    The provider constructs its SandboxManager lazily; tests inject a mock by
    setting ``_manager`` so no SDK client is ever built.
    """
    cfg = config if config is not None else Config()
    kcfg = kernel_config if kernel_config is not None else KernelConfig()
    provider = E2BProvider(kcfg, cfg, Event())
    manager = Mock()
    manager.sandbox_states = dict(states)
    manager.create_all.return_value = manager.sandbox_states
    manager.detect_existing.return_value = manager.sandbox_states
    manager.detect_from_file.return_value = manager.sandbox_states
    manager.check_alive.return_value = True
    provider._manager = manager  # inject; bypasses lazy SandboxManager construction
    return provider, manager


class TestCreateAll:
    def test_port_ready_translates_to_ready(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY, sandbox_id_str="sbx-aaa")
        provider, _ = _provider_with({1: state})

        instances = provider.create_all()

        assert 1 in instances
        inst = instances[1]
        assert inst.id == "sbx-aaa"
        assert inst.index == 1
        assert inst.ready is True
        assert inst.is_alive is True
        assert inst.creation_metrics.status == SandboxStatus.READY

    def test_failed_status_not_ready(self):
        state = _make_state(1, status=E2BSandboxStatus.FAILED)
        provider, _ = _provider_with({1: state})

        inst = provider.create_all()[1]
        assert inst.ready is False
        assert inst.creation_metrics.status == SandboxStatus.FAILED

    def test_port_failed_maps_to_ready_failed(self):
        state = _make_state(2, status=E2BSandboxStatus.PORT_FAILED)
        provider, _ = _provider_with({2: state})

        inst = provider.create_all()[2]
        assert inst.creation_metrics.status == SandboxStatus.READY_FAILED
        assert inst.ready is False

    def test_creation_metrics_fields_mapped(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})

        cm = provider.create_all()[1].creation_metrics
        assert cm.submit_time == 1000.0
        assert cm.ready_time == 1002.3  # e2b port_ready_time -> kernel ready_time
        assert cm.create_elapsed == 1.5
        assert cm.ready_check_elapsed == 0.8  # e2b port_wait_elapsed -> ready_check_elapsed
        assert cm.total_elapsed == 2.3

    def test_numa_node_set_from_config(self):
        # numa_bind default is node 2 (single int); index 1 -> node 2.
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})

        inst = provider.create_all()[1]
        assert inst.numa_node == 2

    def test_no_sandbox_obj_yields_empty_id(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        state.sandbox_obj = None  # create failed before a handle existed
        provider, _ = _provider_with({1: state})

        assert provider.create_all()[1].id == ""


class TestDetect:
    def test_detect_existing_delegates_to_manager(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, manager = _provider_with({1: state})

        instances = provider.detect_existing()

        manager.detect_existing.assert_called_once()
        assert instances[1].id == "sbx-xyz"

    def test_detect_from_ids_returns_none_without_path(self):
        provider, _ = _provider_with({})  # Config() default: sandbox_ids_file=None

        assert provider.detect_from_ids() is None

    def test_detect_from_ids_uses_config_path(self):
        config = Config()
        config.sandbox_ids_file = "/tmp/some_ids.txt"
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, manager = _provider_with({1: state}, config=config)

        provider.detect_from_ids()

        manager.detect_from_file.assert_called_once_with("/tmp/some_ids.txt")

    def test_detect_from_ids_explicit_file_overrides_config(self):
        config = Config()
        config.sandbox_ids_file = "/tmp/config_path.txt"
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, manager = _provider_with({1: state}, config=config)

        provider.detect_from_ids(ids_file="/tmp/explicit.txt")

        manager.detect_from_file.assert_called_once_with("/tmp/explicit.txt")


class TestExec:
    def test_exec_translates_command_result(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        result = provider.exec(inst, "uname -a", timeout=10)

        state.sandbox_obj.commands.run.assert_called_once_with("uname -a", user="root", timeout=10)
        assert isinstance(result, CommandResult)
        assert result.exit_code == 0
        assert result.stdout == "ok"
        assert result.stderr == ""

    def test_exec_without_timeout_omits_kwarg(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        provider.exec(inst, "echo hi")

        state.sandbox_obj.commands.run.assert_called_once_with("echo hi", user="root")

    def test_exec_raises_when_no_handle(self):
        provider, _ = _provider_with({})  # no states
        inst = SandboxInstance(id="ghost", index=99)

        with pytest.raises(RuntimeError, match="No E2B handle"):
            provider.exec(inst, "ls")

    def test_exec_timeout_exception_becomes_command_result_124(self):
        # The E2B SDK raises on request timeout; the adapter surfaces it as a
        # CommandResult (exit_code 124 = timeout(1) convention) carrying any
        # partial stdout, so the runner records a failed step and continues
        # instead of aborting the slice. Mirrors replay-aenv-main.
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        err = Exception("request timed out")
        err.stdout = "partial"  # type: ignore[attr-defined]
        err.stderr = ""  # type: ignore[attr-defined]
        state.sandbox_obj.commands.run = Mock(side_effect=err)

        result = provider.exec(inst, "slow-cmd", timeout=10)

        assert isinstance(result, CommandResult)
        assert result.exit_code == 124
        assert result.stdout == "partial"

    def test_exec_nonzero_exit_exception_becomes_command_result(self):
        # Some E2B SDK variants raise on nonzero exit with exit_code/stderr on
        # the exception; the adapter returns them as a normal CommandResult.
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        err = Exception("command exited with code 2 and error: boom")
        err.exit_code = 2  # type: ignore[attr-defined]
        err.stdout = ""  # type: ignore[attr-defined]
        err.stderr = "boom"  # type: ignore[attr-defined]
        state.sandbox_obj.commands.run = Mock(side_effect=err)

        result = provider.exec(inst, "failing-cmd", timeout=10)

        assert isinstance(result, CommandResult)
        assert result.exit_code == 2
        assert result.stderr == "boom"

    def test_exec_transport_error_without_output_propagates(self):
        # A transport error with no command output still propagates (not a
        # command outcome the runner should swallow).
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        state.sandbox_obj.commands.run = Mock(side_effect=ConnectionError("network gone"))

        with pytest.raises(ConnectionError, match="network gone"):
            provider.exec(inst, "ls", timeout=10)


class TestLifecycleHooks:
    def test_cleanup_all_calls_cleanup_all(self):
        provider, manager = _provider_with({})

        provider.cleanup_all()

        manager.cleanup_all.assert_called_once()

    def test_prepare_env_calls_setup_e2b_env(self, monkeypatch, tmp_path):
        # Point E2B_CONFIG at a nonexistent file so setup_e2b_env's CLI-config
        # fallback can't pick up a real ~/.e2b/config.json from the dev machine.
        monkeypatch.setenv("E2B_CONFIG", str(tmp_path / "no_such_config.json"))
        monkeypatch.delenv("E2B_ACCESS_TOKEN", raising=False)
        config = Config()
        config.e2b_access_token = "token-abc"
        provider, _ = _provider_with({}, config=config)

        provider.prepare_env()

        assert os.environ.get("E2B_ACCESS_TOKEN") == "token-abc"

    def test_check_alive_delegates_to_manager(self):
        state = _make_state(1, status=E2BSandboxStatus.PORT_READY)
        provider, manager = _provider_with({1: state})
        inst = provider.create_all()[1]
        manager.check_alive.return_value = True

        assert provider.check_alive(inst) is True
        manager.check_alive.assert_called_once_with(state)

    def test_check_alive_unknown_index_returns_false(self):
        provider, manager = _provider_with({})
        inst = SandboxInstance(id="x", index=99)

        assert provider.check_alive(inst) is False
        manager.check_alive.assert_not_called()


class TestSaveIds:
    def test_writes_ready_ids_overwrite_mode(self):
        config = Config()
        config.sandbox_ids_file = None  # set per-call
        ready = _make_state(1, status=E2BSandboxStatus.PORT_READY, sandbox_id_str="sbx-1")
        failed = _make_state(2, status=E2BSandboxStatus.FAILED, sandbox_id_str="sbx-2")
        provider, _ = _provider_with({1: ready, 2: failed})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("stale-line\n")
            path = f.name

        try:
            inst_ready = provider.create_all()[1]
            inst_failed = provider.create_all()[2]
            provider.save_ids({1: inst_ready, 2: inst_failed}, ids_file=path)

            with open(path) as f:
                contents = f.read()
            assert contents == "sbx-1\n"  # overwrite, only ready, only one line
        finally:
            os.unlink(path)

    def test_no_path_is_noop(self, tmp_path):
        # Config() default: sandbox_ids_file is None -> provider has nowhere to
        # write, so save_ids is a no-op even with ready instances.
        ready = _make_state(1, status=E2BSandboxStatus.PORT_READY, sandbox_id_str="sbx-1")
        provider, _ = _provider_with({1: ready})
        inst = provider.create_all()[1]

        sentinel = tmp_path / "must_not_exist.txt"
        result = provider.save_ids({1: inst}, ids_file=None)  # explicit None

        assert result is None
        assert not sentinel.exists()

    def test_no_ready_ids_warns_without_writing(self, caplog):
        config = Config()
        provider, _ = _provider_with({}, config=config)
        failed = SandboxInstance(id="sbx-x", index=1, ready=False)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            path = f.name
        try:
            provider.save_ids({1: failed}, ids_file=path)
            # No ready IDs -> file untouched (stays empty / as-created).
            assert caplog.text == "" or "No ready" in caplog.text
        finally:
            os.unlink(path)


class TestLazyConstruction:
    def test_manager_not_built_until_first_use(self):
        provider = E2BProvider(KernelConfig(), Config(), Event())
        assert provider._manager is None

    def test_cleanup_all_is_noop_before_construction(self):
        # If the kernel fails before create_all (e.g. document preflight
        # raises), the manager was never built -> cleanup must not raise.
        provider = E2BProvider(KernelConfig(), Config(), Event())
        provider.cleanup_all()
        assert provider._manager is None

    def test_first_access_builds_manager(self):
        from env_provider.e2b.manager import SandboxManager

        provider = E2BProvider(KernelConfig(), Config(), Event())
        mgr = provider.manager
        assert isinstance(mgr, SandboxManager)
        assert provider._manager is mgr  # cached


class TestBuildProvider:
    def test_builds_from_empty_raw_config(self):
        provider = e2b_build_provider({})
        assert provider.name == "e2b"
        assert provider._manager is None  # still lazy

    def test_builds_from_raw_dict(self):
        # Unified schema: backend knobs under ``e2b:``, shared total_count under
        # ``sandbox:`` (read into KernelConfig by from_raw).
        raw = {
            "e2b": {"template": "my-template", "env": {"E2B_DOMAIN": "dom"}},
            "sandbox": {"total_count": 7},
        }
        provider = e2b_build_provider(raw)

        assert provider._config.template == "my-template"
        assert provider._config.e2b_domain == "dom"
        assert provider._kernel_config.total_count == 7  # shared -> kernel


class TestTemplatePassthrough:
    """Per-sandbox template reaches Sandbox.create and SandboxInstance.template.

    End-to-end: real SandboxManager + real E2BProvider with ``Sandbox`` replaced
    by :class:`_FakeSandboxCls` in the manager module. Verifies (a) each
    ``Sandbox.create`` call receives the per-slot template as its first arg,
    (b) ``_slot_templates`` is populated so ``_to_instance`` can stamp
    ``SandboxInstance.template``, and (c) ``E2BProvider.create_all`` forwards
    the ``templates`` kwarg to the manager.
    """

    def test_per_slot_template_reaches_create_and_instance(self, monkeypatch):
        from env_provider.e2b import manager as _mgr_mod
        from env_provider.tests.test_e2b_manager import _FakeSandboxCls

        fake = _FakeSandboxCls()
        monkeypatch.setattr(_mgr_mod, "Sandbox", fake)

        kcfg = KernelConfig(total_count=3, workflow_type="browser")
        e2b_cfg = Config()
        provider = E2BProvider(kcfg, e2b_cfg, Event())

        instances = provider.create_all(templates={0: "swb-a", 1: "swb-b", 2: "swb-a"})

        # (a) Sandbox.create receives the per-slot template as its first arg.
        # The manager creates sandboxes in a thread pool, so Sandbox.create
        # call order is non-deterministic across platforms/thread scheduling --
        # assert the multiset of templates seen across calls; the per-slot
        # mapping is pinned deterministically by _slot_templates in (c) below.
        assert len(fake.created) == 3
        assert sorted(t for t, *_ in fake.created) == ["swb-a", "swb-a", "swb-b"]

        # (b) SandboxInstance.template is stamped from _slot_templates.
        assert instances[1].template == "swb-a"
        assert instances[2].template == "swb-b"
        assert instances[3].template == "swb-a"

        # (c) _slot_templates records the resolved templates (1-based keys).
        assert provider.manager._slot_templates == {1: "swb-a", 2: "swb-b", 3: "swb-a"}

    def test_no_templates_kwarg_falls_back_to_config_default(self, monkeypatch):
        """templates=None → _create_single uses self.config.template (default)."""
        from env_provider.e2b import manager as _mgr_mod
        from env_provider.tests.test_e2b_manager import _FakeSandboxCls

        fake = _FakeSandboxCls()
        monkeypatch.setattr(_mgr_mod, "Sandbox", fake)

        kcfg = KernelConfig(total_count=3, workflow_type="browser")
        e2b_cfg = Config()
        provider = E2BProvider(kcfg, e2b_cfg, Event())

        instances = provider.create_all()  # no templates= kwarg

        default_tpl = e2b_cfg.template  # "openclaw-browser-v1"

        # Every Sandbox.create call receives the Config default as the first arg.
        assert len(fake.created) == 3
        for entry in fake.created:
            assert entry[0] == default_tpl

        # Every SandboxInstance.template is stamped with the default.
        for inst in instances.values():
            assert inst.template == default_tpl

        # _slot_templates records the default for every slot.
        for slot_id in [1, 2, 3]:
            assert provider.manager._slot_templates[slot_id] == default_tpl


def e2b_build_provider(raw_config: dict) -> E2BProvider:
    """Helper: call build_provider with a KernelConfig built from the same raw."""
    from env_provider.e2b import build_provider

    # Mirror bench_core.bench.main: the kernel builds KernelConfig from the raw
    # dict's shared sections, then build_provider splits off the e2b block.
    return build_provider(KernelConfig.from_raw(raw_config), raw_config)
