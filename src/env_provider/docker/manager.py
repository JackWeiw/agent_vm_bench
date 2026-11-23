"""Docker container manager (SDK seams over :class:`BaseSandboxManager`).

The workflow-agnostic create/detect/cleanup skeleton is inherited from
:class:`env_provider._base.BaseSandboxManager`; this module supplies only the
Docker SDK seams (containers.run / list / remove + the exec probe) and the
docker-specific methods the base can't own: ``check_alive`` (container.reload +
status), ``start_browser_backend`` (hot-start openclaw), and
``clear_browser_cache`` (wipe user-data). Readiness probing is delegated to
:class:`env_provider._ready.ReadyChecker` via the base -- so docker, which
previously only checked ports, now gains the same workflow-driven dispatch as
e2b (coding/document probes, not just ports).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Event
from typing import Any

try:
    import docker
    import docker.errors
except ImportError:  # pragma: no cover - SDK is an optional extra
    # The Docker SDK is an optional extra (only needed to actually talk to a
    # daemon). Keeping this module importable without it lets the provider
    # package and its tests load on SDK-free environments (e.g. CI), where the
    # real manager is never constructed -- tests inject a mock, and the one
    # SDK-backed construction test skips via ``pytest.importorskip("docker")``.
    docker = None  # type: ignore[assignment]

from env_provider._base import BackendSandboxStatus, BaseSandboxManager

from .config import Config
from .schemas import ContainerState

logger = logging.getLogger(__name__)

# Host topology for the pin pre-check; unreadable on non-Linux (check skipped).
_CPU_ONLINE_PATH = "/sys/devices/system/cpu/online"
_NODE_ONLINE_PATH = "/sys/devices/system/node/online"


def _parse_cpuset(value: str) -> set[int]:
    """Parse a docker cpuset list (``"0-3,8"``) into ``{0, 1, 2, 3, 8}``."""
    ids: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        first, dash, last = token.partition("-")
        start = int(first)
        end = int(last) if dash else start
        if start < 0 or end < start:
            raise ValueError(f"invalid range {token!r}")
        ids.update(range(start, end + 1))
    return ids


def _format_cpuset(ids: set[int]) -> str:
    """Render ``{0, 1, 2, 4}`` back as ``"0-2,4"`` for error messages."""
    ordered = sorted(ids)
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def _read_online_cpuset(path: str) -> set[int] | None:
    """Read a sysfs online list; ``None`` when sysfs is unavailable."""
    try:
        return _parse_cpuset(Path(path).read_text(encoding="ascii").strip())
    except OSError:
        return None


class SandboxManager(BaseSandboxManager):
    """Container lifecycle: SDK seams over the shared lifecycle template.

    Shared stress params (total_count, create_batch_*) are read from
    ``kernel_config``; backend knobs (image, prefix, resources) from
    ``docker_config``. Readiness is delegated to :class:`ReadyChecker` via the
    base (provider-transparent ``_ready_config`` -- no docker readiness knobs);
    docker now gets the full workflow-driven probe set (coding/document/browser),
    not just the port check it had before.
    """

    _handle_attr = "docker_container"
    _noun = "Container"
    _id_attr = "container_id"
    # Docker's original remove_all set status=KILLED on cleanup; preserve that.
    _set_killed_on_cleanup = True

    def __init__(self, kernel_config, docker_config: Config, stop_event: Event) -> None:
        super().__init__(kernel_config, stop_event)
        self.config = docker_config
        # Fail fast on a pin naming CPUs / NUMA nodes this host lacks.
        self._validate_pin()
        # Built here (not lazily) to match the original behavior: construction
        # itself validates the daemon is reachable. SDK-free environments never
        # reach here -- tests inject a mock, and the SDK-backed test skips via
        # ``pytest.importorskip("docker")``.
        self.docker_client = docker.from_env()

    def _validate_pin(self) -> None:
        """Reject malformed pins or CPUs / NUMA nodes absent from this host.

        Checked against ``cpu/online`` and ``node/online`` before any container
        is created; skipped when sysfs is unreadable (non-Linux dev box).
        """
        pin_cpus, pin_mems = self.config.pin_cpus, self.config.pin_mems
        if not pin_cpus and not pin_mems:
            return
        try:
            wanted_cpus = _parse_cpuset(pin_cpus) if pin_cpus else set()
            wanted_mems = _parse_cpuset(pin_mems) if pin_mems else set()
        except ValueError as exc:
            raise ValueError(
                f"invalid docker cpuset pin (pin_cpus={pin_cpus!r}, pin_mems={pin_mems!r}): {exc}"
            ) from None
        if pin_cpus and not wanted_cpus:
            raise ValueError(f"invalid docker pin_cpus {pin_cpus!r}: no CPUs parsed")
        if pin_mems and not wanted_mems:
            raise ValueError(f"invalid docker pin_mems {pin_mems!r}: no NUMA nodes parsed")

        online_cpus = _read_online_cpuset(_CPU_ONLINE_PATH)
        online_nodes = _read_online_cpuset(_NODE_ONLINE_PATH)
        if online_cpus is None or online_nodes is None:
            logger.debug("docker pin validation skipped: %s / %s not readable", _CPU_ONLINE_PATH, _NODE_ONLINE_PATH)
            return

        unknown_cpus = wanted_cpus - online_cpus
        if unknown_cpus:
            raise ValueError(
                f"docker pin_cpus {pin_cpus!r} names CPUs {_format_cpuset(unknown_cpus)} that are not "
                f"online on this host (online: {_format_cpuset(online_cpus)})"
            )
        unknown_mems = wanted_mems - online_nodes
        if unknown_mems:
            raise ValueError(
                f"docker pin_mems {pin_mems!r} names NUMA nodes {_format_cpuset(unknown_mems)} that are "
                f"not online on this host (online: {_format_cpuset(online_nodes)})"
            )

    # --------------------------------------------------------- subclass seams
    def _new_state(self, index: int, *, batch_id: int = -1, external_id: str = "") -> ContainerState:
        # On detect, external_id is the real container name; on create, generate
        # the prefixed name so the create path can remove a stale same-name box.
        name = external_id or f"{self.config.container_prefix}-{index}"
        return ContainerState(
            container_id=index,
            container_name=name,
            batch_id=batch_id,
        )

    def _create_single(self, state: ContainerState, *, template: str | None = None) -> dict:
        """Create one container; preserve the handle in ``state.docker_container``.

        Removes a stale same-name container first (handle 409 conflict), then
        runs the image with CPU/memory limits. The base runs the readiness
        probe after this returns success and maps the result onto
        ``creation_metrics``.
        """
        state.creation_metrics.status = BackendSandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        resolved_image = template or self.config.docker_image

        try:
            # Remove existing container with same name if exists (handle 409 conflict)
            try:
                existing = self.docker_client.containers.get(state.container_name)
                existing.remove(force=True)
                logger.info(f"[Container{state.container_id}] Removed existing container with same name")
            except docker.errors.NotFound:
                pass  # No existing container, proceed

            # Pin at create time so the first process is already constrained.
            pin_kwargs: dict[str, str] = {}
            if self.config.pin_cpus:
                pin_kwargs["cpuset_cpus"] = self.config.pin_cpus
            if self.config.pin_mems:
                pin_kwargs["cpuset_mems"] = self.config.pin_mems

            # Create container with resource limits
            container = self.docker_client.containers.run(
                image=resolved_image,
                name=state.container_name,
                detach=True,  # Run in background
                remove=False,  # Don't auto-remove
                cpu_quota=int(self.config.cpu_limit * 100000),  # CPU quota in microseconds
                mem_limit=self.config.memory_limit,
                **pin_kwargs,
            )

            if pin_kwargs:
                self._verify_pin(container, state)

            state.docker_container = container
            state.creation_metrics.create_ready_time = time.time()
            state.creation_metrics.create_elapsed = (
                state.creation_metrics.create_ready_time - state.creation_metrics.submit_time
            )
            state.creation_metrics.status = BackendSandboxStatus.CREATED
            return {
                "success": True,
                "create_elapsed": state.creation_metrics.create_elapsed,
                "error": "",
                "template": resolved_image,
            }
        except Exception as e:
            state.creation_metrics.create_ready_time = time.time()
            return {
                "success": False,
                "create_elapsed": 0.0,
                "error": str(e),
                "template": resolved_image,
            }

    def _verify_pin(self, container: Any, state: ContainerState) -> None:
        """Read the cpuset back; remove the container and fail on mismatch."""
        host_config = container.attrs.get("HostConfig") or {}
        checks = (
            ("cpus", self.config.pin_cpus, host_config.get("CpusetCpus") or ""),
            ("mems", self.config.pin_mems, host_config.get("CpusetMems") or ""),
        )
        for label, expected, actual in checks:
            if expected and actual != expected:
                container.remove(force=True)
                raise RuntimeError(f"cpuset {label} pin verify failed: expected {expected}, got {actual!r}")
        logger.info(
            f"[Container{state.container_id}] PINNED OK cpus={self.config.pin_cpus or '-'} "
            f"mems={self.config.pin_mems or '-'}"
        )

    def _list_existing(self) -> list:
        """List running containers matching the configured prefix."""
        all_containers = self.docker_client.containers.list(all=False)  # only running
        return [c for c in all_containers if c.name.startswith(self.config.container_prefix)]

    def _external_id(self, listed: Any) -> str:
        # The container name is docker's stable identifier (the numeric
        # container_id is the kernel index, not a docker id).
        return listed.name

    def _attach(self, listed: Any) -> Any:
        # The listed container object IS the SDK handle (docker has no connect step).
        return listed

    def _kill_one(self, state: ContainerState) -> None:
        state.docker_container.remove(force=True)

    def _exec_probe(self, handle: Any, cmd: str, timeout: int) -> tuple[int, str, str]:
        # docker's exec_run has no native timeout; the ReadyChecker passes one
        # but we honour the command only (probes are fast, bounded by max_wait).
        result = handle.exec_run(["/bin/sh", "-c", cmd], user="root")
        output = result.output
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else (output or "")
        return result.exit_code, text, ""

    def _create_header_extras(self) -> list[str]:
        return [
            f"Image: {self.config.docker_image}",
            f"Spec:  {self.config.cpu_limit}vCPU / {self.config.memory_limit}",
        ]

    # ------------------------------------------------- docker-specific methods
    def check_alive(self, state: ContainerState) -> bool:
        """Liveness via the Docker daemon (container.reload + status)."""
        container = state.docker_container
        if not container or not state.is_alive:
            return False
        try:
            container.reload()  # Refresh container status
            return container.status == "running"
        except Exception:
            return False

    def start_browser_backend(self, state: ContainerState) -> tuple[bool, str]:
        """Start OpenClaw browser backend (hot start).

        Execute: openclaw browser status && start
        """
        container = state.docker_container
        if not container:
            return False, "No container handle"

        try:
            cmd = "openclaw browser status && start || openclaw browser start"
            result = container.exec_run(["/bin/sh", "-c", cmd], user="root")
            output = (
                result.output.decode("utf-8", errors="ignore") if isinstance(result.output, bytes) else result.output
            )
            if result.exit_code == 0:
                state.browser_started = True
                return True, ""
            return False, f"exit_code={result.exit_code}, output={output[:200]}"
        except Exception as e:
            return False, str(e)

    def clear_browser_cache(self, state: ContainerState) -> bool:
        """Clear browser cache for a clean test.

        Execute: rm -rf /root/.openclaw/browser/openclaw/user-data
        """
        container = state.docker_container
        if not container:
            return False
        try:
            cmd = "rm -rf /root/.openclaw/browser/openclaw/user-data"
            result = container.exec_run(["/bin/sh", "-c", cmd], user="root")
            return result.exit_code == 0
        except Exception:
            return False

    # ----------------------------------------------------- adapter alias (state)
    @property
    def container_states(self) -> dict[int, ContainerState]:
        """Adapter-facing alias for the base state registry."""
        return self._states
