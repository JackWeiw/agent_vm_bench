"""Host-agnostic benchmark kernel.

The kernel drives any EnvironmentProvider (e2b, docker, ...) through a single
exec() primitive. Provider implementations live in their own packages
(e2b_bench, docker_bench); this package depends only on the contract.
"""

from .provider import CommandResult, CreationMetrics, EnvironmentProvider, SandboxInstance

__all__ = ["EnvironmentProvider", "SandboxInstance", "CommandResult", "CreationMetrics"]
