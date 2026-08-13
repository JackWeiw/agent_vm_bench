"""Docker backend configuration (backend-only fields).

Host-agnostic stress params (total_count, create_batch, task_batch,
benchmark_*, browser, test, warmup, report, workflow_type) live on
:class:`bench_core.config.KernelConfig` and are read from the shared sections
of the unified YAML by :meth:`KernelConfig.from_raw`. This Config holds only
the docker-specific knobs -- image, prefix, resources, port check -- read from
the ``docker:`` block of that YAML.

Legacy ``docker_bench`` keeps its own ``docker_bench/config.py`` (with the
old-schema load_from_yaml / merge_with_args / from_args CLI flow); this module
is the src kernel's backend config and is consumed only by
:func:`env_provider.docker.build_provider`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Config:
    """Docker backend configuration (backend-specific fields only)."""

    # Image / naming
    docker_image: str = "ubuntu-openclaw-chromium:arm64"
    container_prefix: str = "oc-bench"  # Container name prefix (oc-bench-1, ...)

    # Per-container resources
    cpu_limit: float = 2.0  # --cpus
    memory_limit: str = "2g"  # -m
    create_timeout: int = 300  # Container creation timeout (seconds)

    # Port readiness check (openclaw-gateway + llama-server)
    required_ports: list[int] = field(default_factory=lambda: [18789, 11436])
    port_check_max_wait: int = 300  # Max wait for ports (seconds)
    port_check_interval: int = 5  # Port poll interval (seconds)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Config:
        """Build from the unified YAML's ``docker:`` block.

        ``port_check`` nests under ``docker:`` in the unified schema (it is
        docker-specific: e2b has no port probe). Missing keys fall back to the
        dataclass defaults, so a minimal ``docker:`` block still loads.
        """
        docker = raw.get("docker") or {}
        port_check = docker.get("port_check") or {}
        return cls(
            docker_image=docker.get("image", "ubuntu-openclaw-chromium:arm64"),
            container_prefix=docker.get("container_prefix", "oc-bench"),
            cpu_limit=docker.get("cpu_limit", 2.0),
            memory_limit=docker.get("memory_limit", "2g"),
            create_timeout=docker.get("create_timeout", 300),
            required_ports=port_check.get("ports", [18789, 11436]),
            port_check_max_wait=port_check.get("max_wait", 300),
            port_check_interval=port_check.get("interval", 5),
        )
