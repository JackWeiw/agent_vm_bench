"""Host-agnostic benchmark kernel.

The kernel drives any EnvironmentProvider (e2b, docker, ...) through a single
exec() primitive. Provider implementations live in their own packages
(e2b_bench, docker_bench); the contract they all speak lives in ``env_provider``
(kernel and providers both depend on it, neither owns it). The re-exports below
are a convenience alias so ``from bench_core import EnvironmentProvider`` keeps
working.
"""

from env_provider import CommandResult, CreationMetrics, EnvironmentProvider, SandboxInstance

__all__ = ["EnvironmentProvider", "SandboxInstance", "CommandResult", "CreationMetrics"]
