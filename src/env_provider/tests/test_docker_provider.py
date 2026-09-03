"""Tests for the Docker EnvironmentProvider adapter.

The adapter wraps :class:`SandboxManager` behind the kernel's
:class:`EnvironmentProvider` contract. These tests mock the manager and its SDK
handles, verifying the translation + delegation logic -- not the Docker SDK
itself.
"""
from __future__ import annotations

import time
from threading import Event
from unittest.mock import Mock

import pytest

from bench_core.config import KernelConfig
from env_provider import CommandResult, SandboxInstance, SandboxStatus
from env_provider.docker.config import Config
from env_provider.docker import DockerProvider
from env_provider.docker.schemas import ContainerState
from env_provider.docker.schemas import ContainerStatus as DockerStatus


class _ExecResult:
    """Stand-in for docker SDK's ``ExecResult`` (exit_code + output tuple)."""

    def __init__(self, exit_code: int = 0, output: tuple = (b"out", b"err")):
        self.exit_code = exit_code
        self.output = output


def _make_state(
    container_id: int,
    *,
    status: DockerStatus,
    container_name: str = "oc-bench-1",
    is_alive: bool = True,
    browser_started: bool = False,
) -> ContainerState:
    """Build a docker ContainerState with a mock SDK handle for the adapter."""
    state = ContainerState(container_id=container_id, container_name=container_name)
    state.creation_metrics.status = status
    state.creation_metrics.submit_time = 1000.0
    state.creation_metrics.port_ready_time = 1002.3
    state.creation_metrics.create_elapsed = 1.5
    state.creation_metrics.port_wait_elapsed = 0.8
    state.creation_metrics.total_elapsed = 2.3
    state.creation_metrics.error_msg = ""
    state.creation_metrics.port_check_error = ""
    state.is_alive = is_alive
    state.browser_started = browser_started

    container = Mock()
    container.exec_run = Mock(return_value=_ExecResult(0, (b"ok", b"")))
    state.docker_container = container
    return state


def _provider_with(
    states: dict[int, ContainerState],
    *,
    config: Config | None = None,
    kernel_config: KernelConfig | None = None,
) -> tuple[DockerProvider, Mock]:
    """Build a DockerProvider over a mock manager holding the given states.

    The provider constructs its SandboxManager lazily; tests inject a mock by
    setting ``_manager`` so no SDK client is ever built.
    """
    from bench_core.config import KernelConfig

    cfg = config if config is not None else Config()
    kcfg = kernel_config if kernel_config is not None else KernelConfig()
    provider = DockerProvider(kcfg, cfg, Event())
    manager = Mock()
    manager.container_states = dict(states)
    manager.create_all.return_value = manager.container_states
    manager.detect_existing.return_value = manager.container_states
    manager.check_alive.return_value = True
    provider._manager = manager  # inject; bypasses lazy ContainerManager construction
    return provider, manager


class TestCreateAll:
    def test_port_ready_translates_to_ready(self):
        state = _make_state(1, status=DockerStatus.PORT_READY, container_name="oc-bench-7")
        provider, _ = _provider_with({1: state})

        instances = provider.create_all()

        assert 1 in instances
        inst = instances[1]
        assert inst.id == "oc-bench-7"  # container name is the stable id
        assert inst.index == 1  # numeric container_id is the kernel index
        assert inst.ready is True
        assert inst.is_alive is True
        assert inst.creation_metrics.status == SandboxStatus.READY

    def test_failed_status_not_ready(self):
        state = _make_state(1, status=DockerStatus.FAILED)
        provider, _ = _provider_with({1: state})

        inst = provider.create_all()[1]
        assert inst.ready is False
        assert inst.creation_metrics.status == SandboxStatus.FAILED

    def test_port_failed_maps_to_ready_failed(self):
        state = _make_state(2, status=DockerStatus.PORT_FAILED)
        provider, _ = _provider_with({2: state})

        inst = provider.create_all()[2]
        assert inst.creation_metrics.status == SandboxStatus.READY_FAILED
        assert inst.ready is False

    def test_killed_maps_through(self):
        state = _make_state(3, status=DockerStatus.KILLED, is_alive=False)
        provider, _ = _provider_with({3: state})

        inst = provider.create_all()[3]
        assert inst.creation_metrics.status == SandboxStatus.KILLED
        assert inst.is_alive is False

    def test_creation_metrics_fields_mapped(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        state.creation_metrics.error_msg = "boom"
        state.creation_metrics.port_check_error = "port-down"
        provider, _ = _provider_with({1: state})

        cm = provider.create_all()[1].creation_metrics
        assert cm.submit_time == 1000.0
        assert cm.ready_time == 1002.3  # docker port_ready_time -> kernel ready_time
        assert cm.create_elapsed == 1.5
        assert cm.ready_check_elapsed == 0.8  # docker port_wait_elapsed -> ready_check_elapsed
        assert cm.total_elapsed == 2.3
        assert cm.error == "boom"  # docker error_msg -> kernel error
        assert cm.ready_check_error == "port-down"  # port_check_error -> ready_check_error

    def test_numa_node_is_none(self):
        # docker does not apply NUMA binding; the kernel gets no numa hint.
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, _ = _provider_with({1: state})

        assert provider.create_all()[1].numa_node is None

    def test_warmup_done_reflects_browser_started(self):
        state = _make_state(1, status=DockerStatus.PORT_READY, browser_started=True)
        provider, _ = _provider_with({1: state})

        assert provider.create_all()[1].warmup_done is True

    def test_create_all_stamps_template_on_instance(self):
        """Regression test: _to_instance must read _slot_templates and stamp SandboxInstance.template."""
        state = _make_state(1, status=DockerStatus.PORT_READY, container_name="oc-bench-1")
        provider, manager = _provider_with({1: state})

        # Seed _slot_templates as the base manager's create_all would do
        manager._slot_templates = {1: "custom-image"}
        manager.create_all.return_value = {1: state}

        instances = provider.create_all(templates={0: "custom-image"})

        assert instances[1].template == "custom-image"

    def test_create_all_forwards_templates_to_manager(self):
        state = _make_state(1, status=DockerStatus.PORT_READY, container_name="oc-bench-1")
        provider, manager = _provider_with({1: state})
        templates = {1: "custom-image"}

        provider.create_all(templates=templates)

        manager.create_all.assert_called_once_with(templates=templates)

    def test_create_all_without_templates_is_legacy(self):
        state = _make_state(1, status=DockerStatus.PORT_READY, container_name="oc-bench-1")
        provider, manager = _provider_with({1: state})

        provider.create_all()

        manager.create_all.assert_called_once_with(templates=None)


class TestDetect:
    def test_detect_existing_delegates_to_manager(self):
        state = _make_state(1, status=DockerStatus.PORT_READY, container_name="oc-bench-1")
        provider, manager = _provider_with({1: state})

        instances = provider.detect_existing()

        manager.detect_existing.assert_called_once()
        assert instances[1].id == "oc-bench-1"

    def test_detect_from_ids_unsupported_returns_none(self):
        # docker detects by prefix, not persisted IDs; the default hook returns
        # None so the kernel falls back to detect_existing.
        provider, _ = _provider_with({})

        assert provider.detect_from_ids() is None


class TestExec:
    def test_exec_translates_command_result(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        result = provider.exec(inst, "uname -a", timeout=10)

        # timeout runs the worker; exec_run gets the raw command (no wrapping).
        state.docker_container.exec_run.assert_called_once_with("uname -a", user="root", demux=True)
        assert isinstance(result, CommandResult)
        assert result.exit_code == 0
        assert result.stdout == "ok"
        assert result.stderr == ""

    def test_exec_without_timeout_calls_directly(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        provider.exec(inst, "echo hi")

        state.docker_container.exec_run.assert_called_once_with("echo hi", user="root", demux=True)

    def test_exec_demux_none_output_yields_empty_streams(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        state.docker_container.exec_run = Mock(return_value=_ExecResult(0, None))
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        result = provider.exec(inst, "true")

        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code == 0

    def test_exec_decodes_bytes_output(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        state.docker_container.exec_run = Mock(return_value=_ExecResult(0, (b"snapshot\n[bodies]", b"warn")))
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        result = provider.exec(inst, "agent-browser snapshot -i")

        assert "snapshot" in result.stdout
        assert result.stderr == "warn"

    def test_exec_raises_when_no_handle(self):
        provider, _ = _provider_with({})  # no states
        inst = SandboxInstance(id="ghost", index=99)

        with pytest.raises(RuntimeError, match="No Docker handle"):
            provider.exec(inst, "ls")

    def test_exec_raises_when_container_handle_missing(self):
        # container_states has the index but docker_container is None (create
        # failed before a handle existed) -> still no route for exec.
        state = _make_state(1, status=DockerStatus.FAILED)
        state.docker_container = None
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        with pytest.raises(RuntimeError, match="No Docker handle"):
            provider.exec(inst, "ls")

    def test_exec_wraps_cwd(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        provider.exec(inst, "ls", cwd="/work")

        state.docker_container.exec_run.assert_called_once_with("cd /work && ls", user="root", demux=True)

    def test_exec_wraps_env(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        provider.exec(inst, "agent-browser snapshot -i", env={"http_proxy": "none"})

        state.docker_container.exec_run.assert_called_once_with(
            "http_proxy=none agent-browser snapshot -i", user="root", demux=True
        )

    def test_exec_wraps_cwd_and_env(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        provider.exec(inst, "ls", cwd="/work", env={"FOO": "bar baz"})

        # env applies to the command itself (not the cd); shlex quotes spaces.
        state.docker_container.exec_run.assert_called_once_with("cd /work && FOO='bar baz' ls", user="root", demux=True)

    def test_exec_timeout_raises_timeout_error(self):
        # A blocking exec_run that outlasts the timeout -> TimeoutError, so the
        # kernel's runners can classify the failure.
        state = _make_state(1, status=DockerStatus.PORT_READY)

        def _block(*_args, **_kwargs):
            time.sleep(0.2)
            return _ExecResult(0, (b"late", b""))

        state.docker_container.exec_run = Mock(side_effect=_block)
        provider, _ = _provider_with({1: state})
        inst = provider.create_all()[1]

        with pytest.raises(TimeoutError, match="timed out"):
            provider.exec(inst, "sleep 5", timeout=0.05)


class TestLifecycleHooks:
    def test_cleanup_all_calls_cleanup_all(self):
        provider, manager = _provider_with({})

        provider.cleanup_all()

        manager.cleanup_all.assert_called_once()

    def test_check_alive_delegates_to_manager(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
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

    def test_prepare_env_is_noop(self):
        provider, _ = _provider_with({})
        # No SDK env vars to set; the call must not raise and returns None.
        assert provider.prepare_env() is None

    def test_prepare_starts_backend_and_clears_cache(self):
        state = _make_state(1, status=DockerStatus.PORT_READY)
        provider, manager = _provider_with({1: state})
        inst = provider.create_all()[1]

        provider.prepare(inst)

        manager.clear_browser_cache.assert_called_once_with(state)
        manager.start_browser_backend.assert_called_once_with(state)

    def test_prepare_unknown_index_is_noop(self):
        provider, manager = _provider_with({})
        inst = SandboxInstance(id="x", index=99)

        provider.prepare(inst)

        manager.clear_browser_cache.assert_not_called()
        manager.start_browser_backend.assert_not_called()


class TestLazyConstruction:
    def test_manager_not_built_until_first_use(self):
        provider = DockerProvider(KernelConfig(), Config(), Event())
        assert provider._manager is None

    def test_cleanup_all_is_noop_before_construction(self):
        # If the kernel fails before create_all, the manager was never built ->
        # cleanup must not raise.
        provider = DockerProvider(KernelConfig(), Config(), Event())
        provider.cleanup_all()
        assert provider._manager is None

    def test_first_access_builds_manager(self):
        # The Docker SDK is an optional extra, so this real-construction test
        # skips where it is not installed (the mock-injection tests above still
        # run). docker's SandboxManager.__init__ pings the daemon
        # (docker.from_env eagerly fetches the server version); patch it so
        # construction is testable without a running daemon.
        pytest.importorskip("docker")
        from unittest.mock import patch

        from env_provider.docker.manager import SandboxManager

        provider = DockerProvider(KernelConfig(), Config(), Event())
        with patch("env_provider.docker.manager.docker.from_env") as from_env:
            from_env.return_value = Mock()
            mgr = provider.manager
        assert isinstance(mgr, SandboxManager)
        assert provider._manager is mgr  # cached


class TestBuildProvider:
    def test_builds_from_empty_raw_config(self):
        provider = docker_bench_build_provider({})
        assert provider.name == "docker"
        assert provider._manager is None  # still lazy

    def test_builds_from_raw_dict(self):
        # Unified schema: backend knobs under ``docker:``, shared total_count
        # under ``sandbox:`` (read into KernelConfig by from_raw).
        raw = {
            "docker": {"image": "my-img", "container_prefix": "x"},
            "sandbox": {"total_count": 4},
        }
        provider = docker_bench_build_provider(raw)

        assert provider._config.docker_image == "my-img"
        assert provider._config.container_prefix == "x"
        assert provider._kernel_config.total_count == 4  # shared -> kernel


def docker_bench_build_provider(raw_config: dict) -> DockerProvider:
    """Helper: call build_provider with a KernelConfig built from the same raw."""
    from env_provider.docker import build_provider

    # Mirror bench_core.bench.main: the kernel builds KernelConfig from the raw
    # dict's shared sections, then build_provider splits off the docker block.
    return build_provider(KernelConfig.from_raw(raw_config), raw_config)
