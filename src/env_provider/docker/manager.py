"""
Container Management Module

Responsible for Docker container creation, health check, batch control and termination
Uses Docker SDK for container lifecycle management
Supports port check (18789 openclaw-gateway + 11436 llama-server)
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event
from typing import Dict, Tuple

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

from .config import Config
from .schemas import ContainerState, ContainerStatus

logger = logging.getLogger(__name__)


class SandboxManager:
    """Container lifecycle management using Docker SDK.

    Shared stress params (total_count, create_batch_*) are read from
    ``kernel_config``; backend-specific knobs (image, prefix, resources,
    ports) from ``docker_config``. This split keeps a single source of truth
    per axis: the kernel owns the host-agnostic counts, the provider owns the
    docker runtime knobs.
    """

    def __init__(self, kernel_config, docker_config: Config, stop_event: Event):
        self.kernel_config = kernel_config
        self.config = docker_config
        self.stop_event = stop_event
        self.container_states: Dict[int, ContainerState] = {}
        self.docker_client = docker.from_env()

    def create_all(self) -> Dict[int, ContainerState]:
        """Batch create containers

        Strategy based on create_batch config:
        - With create_batch_size: batched creation to avoid resource spike
        - Without config: full concurrent creation for max performance test

        Returns: {container_id: ContainerState}
        """
        if self.kernel_config.create_batch_size and self.kernel_config.create_batch_size > 0:
            return self._create_batched()
        else:
            return self._create_concurrent()

    def detect_existing(self) -> Dict[int, ContainerState]:
        """Detect existing running containers

        Query Docker API for existing containers with matching prefix,
        check port readiness, and prepare for benchmark.

        Returns: {container_id: ContainerState}
        """
        logger.info(f"\n{'=' * 60}")
        logger.info("Detecting Existing Containers")
        logger.info(f"{'=' * 60}")

        # List containers matching the prefix
        try:
            all_containers = self.docker_client.containers.list(all=False)  # Only running
            matching_containers = [c for c in all_containers if c.name.startswith(self.config.container_prefix)]
            logger.info(
                f"  Found {len(matching_containers)} running containers with prefix '{self.config.container_prefix}'"
            )
        except Exception as e:
            logger.error(f"  Failed to list containers: {e}")
            return {}

        if not matching_containers:
            logger.info("  No existing containers found")
            return {}

        logger.info("  Processing all containers...")

        # Process each container
        for i, docker_container in enumerate(matching_containers):
            container_id = i + 1
            container_name = docker_container.name

            state = ContainerState(container_id=container_id, container_name=container_name)
            state.docker_container = docker_container
            self.container_states[container_id] = state

            logger.info(f"\n[Container{container_id}] {container_name}...")

            try:
                # Container is already running
                state.creation_metrics.status = ContainerStatus.CREATED
                logger.info(f"[Container{container_id}] Already running")

                # Check port readiness
                port_result = self._check_ports(state)
                if port_result["success"]:
                    state.creation_metrics.status = ContainerStatus.PORT_READY
                    state.creation_metrics.port_wait_elapsed = port_result["wait_elapsed"]
                    logger.info(f"[Container{container_id}] Ports ready in {port_result['wait_elapsed']:.1f}s")
                else:
                    state.creation_metrics.status = ContainerStatus.PORT_FAILED
                    state.creation_metrics.port_check_error = port_result["error"]
                    logger.warning(f"[Container{container_id}] Port check failed: {port_result['error'][:50]}")

            except Exception as e:
                state.creation_metrics.status = ContainerStatus.FAILED
                state.creation_metrics.error_msg = str(e)
                logger.error(f"[Container{container_id}] {str(e)[:80]}")

        return self.container_states

    def _create_batched(self) -> Dict[int, ContainerState]:
        """Batched container creation"""
        total = self.kernel_config.total_count
        batch_size = self.kernel_config.create_batch_size
        batch_count = self.kernel_config.create_batch_count

        logger.info(f"\n{'=' * 60}")
        logger.info("Batched Container Creation")
        logger.info(f"  Total: {total} containers")
        logger.info(f"  Image: {self.config.docker_image}")
        logger.info(f"  Spec:  {self.config.cpu_limit}vCPU / {self.config.memory_limit}")
        logger.info(f"  Batches: {batch_count} x {batch_size}")
        logger.info(f"  Interval: {self.kernel_config.create_batch_interval}s")
        logger.info(f"{'=' * 60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                logger.info("Stop event detected, aborting creation")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)

            logger.info(f"\n[Batch {batch_id}/{batch_count - 1}] Creating containers {start_idx + 1}-{end_idx}")

            # Concurrent creation of current batch
            batch_states = self._create_batch_concurrent(batch_id, start_idx, end_idx)
            self.container_states.update(batch_states)

            # Wait between batches (last batch no wait)
            if batch_id < batch_count - 1 and self.kernel_config.create_batch_interval:
                logger.info(f"Waiting {self.kernel_config.create_batch_interval}s before next batch...")
                time.sleep(self.kernel_config.create_batch_interval)

        return self.container_states

    def _create_batch_concurrent(self, batch_id: int, start: int, end: int) -> Dict[int, ContainerState]:
        """Concurrent creation of one batch"""
        states: Dict[int, ContainerState] = {}

        with ThreadPoolExecutor(max_workers=end - start) as executor:
            futures = {}

            for i in range(start, end):
                container_id = i + 1
                container_name = f"{self.config.container_prefix}-{container_id}"
                state = ContainerState(container_id=container_id, container_name=container_name, batch_id=batch_id)
                self.container_states[container_id] = state
                future = executor.submit(self._create_single, state)
                futures[future] = container_id

            for future in as_completed(futures):
                container_id = futures[future]
                state = self.container_states[container_id]

                try:
                    result = future.result()
                    if result["success"]:
                        # Container created, start port check
                        logger.info(
                            f"[Container{container_id}] Created in {result['create_elapsed']:.1f}s, checking ports..."
                        )

                        # Port check
                        port_result = self._check_ports(state)
                        if port_result["success"]:
                            state.creation_metrics.status = ContainerStatus.PORT_READY
                            state.creation_metrics.port_wait_elapsed = port_result["wait_elapsed"]
                            state.creation_metrics.total_elapsed = (
                                result["create_elapsed"] + port_result["wait_elapsed"]
                            )
                            logger.info(
                                f"[Container{container_id}] Ports ready in {port_result['wait_elapsed']:.1f}s, total {state.creation_metrics.total_elapsed:.1f}s"
                            )
                        else:
                            state.creation_metrics.status = ContainerStatus.PORT_FAILED
                            state.creation_metrics.port_check_error = port_result["error"]
                            logger.warning(f"[Container{container_id}] Port check failed: {port_result['error'][:50]}")
                    else:
                        state.creation_metrics.status = ContainerStatus.FAILED
                        state.creation_metrics.error_msg = result["error"]
                        logger.error(f"[Container{container_id}] Failed: {result['error'][:80]}")
                except Exception as e:
                    state.creation_metrics.status = ContainerStatus.FAILED
                    state.creation_metrics.error_msg = str(e)
                    logger.error(f"[Container{container_id}] Exception: {str(e)[:80]}")

        return {i + 1: self.container_states[i + 1] for i in range(start, end)}

    def _create_concurrent(self) -> Dict[int, ContainerState]:
        """Full concurrent creation of all containers"""
        total = self.kernel_config.total_count

        logger.info(f"\n{'=' * 60}")
        logger.info("Concurrent Container Creation")
        logger.info(f"  Total: {total} containers (full concurrent)")
        logger.info(f"  Image: {self.config.docker_image}")
        logger.info(f"  Spec:  {self.config.cpu_limit}vCPU / {self.config.memory_limit}")
        logger.info(f"{'=' * 60}")

        return self._create_batch_concurrent(batch_id=0, start=0, end=total)

    def _create_single(self, state: ContainerState) -> Dict[str, any]:
        """Create single container

        Key: Preserve docker container handle in state.docker_container
        Record time when container.create succeeds, no port waiting

        Returns: {'success': bool, 'create_elapsed': float, 'error': str}
        """
        state.creation_metrics.status = ContainerStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        try:
            # Remove existing container with same name if exists (handle 409 conflict)
            try:
                existing = self.docker_client.containers.get(state.container_name)
                existing.remove(force=True)
                logger.info(f"[Container{state.container_id}] Removed existing container with same name")
            except docker.errors.NotFound:
                pass  # No existing container, proceed

            # Create container with resource limits
            container = self.docker_client.containers.run(
                image=self.config.docker_image,
                name=state.container_name,
                detach=True,  # Run in background
                remove=False,  # Don't auto-remove
                cpu_quota=int(self.config.cpu_limit * 100000),  # CPU quota in microseconds
                mem_limit=self.config.memory_limit,
            )

            # Preserve container handle
            state.docker_container = container
            state.creation_metrics.create_ready_time = time.time()
            state.creation_metrics.create_elapsed = (
                state.creation_metrics.create_ready_time - state.creation_metrics.submit_time
            )
            state.creation_metrics.status = ContainerStatus.CREATED

            return {"success": True, "create_elapsed": state.creation_metrics.create_elapsed, "error": ""}
        except Exception as e:
            state.creation_metrics.create_ready_time = time.time()
            return {"success": False, "create_elapsed": 0.0, "error": str(e)}

    def _check_ports(self, state: ContainerState) -> Dict[str, any]:
        """Check if container ports are ready

        Check ports in self.config.required_ports (default: 18789 + 11436)

        Returns: {'success': bool, 'wait_elapsed': float, 'error': str}
        """
        container = state.docker_container
        if not container:
            return {"success": False, "wait_elapsed": 0.0, "error": "No container handle"}

        start_time = time.time()
        ready_ports = set()

        while time.time() - start_time < self.config.port_check_max_wait:
            if self.stop_event.is_set():
                return {"success": False, "wait_elapsed": time.time() - start_time, "error": "Stop event"}

            for port in self.config.required_ports:
                if port in ready_ports:
                    continue

                try:
                    # Execute port check command inside container
                    # Note: Use shell=True or sh -c to support pipe in exec_run
                    cmd = f"sh -c 'ss -tlnp 2>/dev/null | grep :{port}'"
                    result = container.exec_run(cmd, user="root")

                    output = (
                        result.output.decode("utf-8", errors="ignore")
                        if isinstance(result.output, bytes)
                        else result.output
                    )
                    exit_code = result.exit_code

                    # Check if port is listening (grep found the port)
                    if exit_code == 0 and len(output.strip()) > 0:
                        ready_ports.add(port)
                        logger.info(f"[Container{state.container_id}] Port {port} is listening")
                except Exception as e:
                    # exec_run error - continue checking
                    logger.warning(f"[Container{state.container_id}] Port {port} check exception: {str(e)[:50]}")
                    pass  # Continue checking other ports

            if len(ready_ports) == len(self.config.required_ports):
                wait_elapsed = time.time() - start_time
                state.creation_metrics.port_ready_time = time.time()
                return {"success": True, "wait_elapsed": wait_elapsed, "error": ""}

            time.sleep(self.config.port_check_interval)

        # Timeout, return missing ports info
        missing_ports = [p for p in self.config.required_ports if p not in ready_ports]
        wait_elapsed = time.time() - start_time
        return {"success": False, "wait_elapsed": wait_elapsed, "error": f"Timeout waiting for ports: {missing_ports}"}

    def check_alive(self, state: ContainerState) -> bool:
        """Check if container is alive"""
        container = state.docker_container
        if not container or not state.is_alive:
            return False
        try:
            container.reload()  # Refresh container status
            return container.status == "running"
        except Exception:
            return False

    def start_browser_backend(self, state: ContainerState) -> Tuple[bool, str]:
        """Start OpenClaw browser backend (hot start)

        Execute: openclaw browser status && start

        Returns: (success, error_msg)
        """
        container = state.docker_container
        if not container:
            return False, "No container handle"

        try:
            cmd = "openclaw browser status && start || openclaw browser start"
            result = container.exec_run(cmd, user="root")

            output = (
                result.output.decode("utf-8", errors="ignore") if isinstance(result.output, bytes) else result.output
            )

            if result.exit_code == 0:
                state.browser_started = True
                return True, ""
            else:
                return False, f"exit_code={result.exit_code}, output={output[:200]}"
        except Exception as e:
            return False, str(e)

    def clear_browser_cache(self, state: ContainerState) -> bool:
        """Clear browser cache for clean test

        Execute: rm -rf /root/.openclaw/browser/openclaw/user-data

        Returns: success
        """
        container = state.docker_container
        if not container:
            return False

        try:
            cmd = "rm -rf /root/.openclaw/browser/openclaw/user-data"
            result = container.exec_run(cmd, user="root")
            return result.exit_code == 0
        except Exception:
            return False

    def remove_all(self) -> None:
        """Remove all containers"""
        logger.info("\nRemoving all containers...")
        removed_count = 0
        for state in self.container_states.values():
            if state.docker_container:
                try:
                    state.docker_container.remove(force=True)
                    state.creation_metrics.status = ContainerStatus.KILLED
                    state.is_alive = False
                    removed_count += 1
                except Exception as e:
                    logger.warning(f"[Container{state.container_id}] Remove error: {str(e)[:50]}")
        logger.info(f"Removed {removed_count} containers")
