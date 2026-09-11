---
rfc: 0001
title: Abstract EnvironmentProvider layer, decouple benchmark kernel from sandbox impl (e2b/docker -> kata/gvisor/agentenv)
status: Active
author: "@JackWeiw"
shepherd: ""
areas: [e2b, docker, vm_monitor]
created: 2026-08-12
updated: 2026-08-12
---

# Abstract EnvironmentProvider layer, decouple benchmark kernel from sandbox impl

> Vision-level RFC: records the refactor direction and interface-contract sketch, **not for immediate implementation**. Open an implementation plan once finalized.

## Summary

Extract an `EnvironmentProvider` (ABC/Protocol) + a unified `SandboxInstance` state, and lift `e2b_bench/bench.py` + `batch_scheduler` + task runners into a host-agnostic "benchmark kernel" that takes a provider by injection. `e2b_bench/` becomes the e2b provider impl, `docker_bench/` the docker provider, and kata/gvisor/agentenv each get their own.

## Motivation

`agent_vm_bench` currently has two parallel benchmark packages: `e2b_bench/` (8482 lines, full-featured: browser/coding/document workflows, round_robin, batch_scheduler, metrics_extractor, report_aggregator) and `docker_bench/` (2101 lines, a stripped browser-only copy). Their structure mirrors one-to-one (`bench.py / config.py / task_runner.py / stats_collector.py / utils.py / schemas.py + manager`).

The stronger signal: the two managers are already **two implementations of the same interface**, just not formalized as a contract:

| Responsibility | `e2b_bench/sandbox_manager.SandboxManager` | `docker_bench/container_manager.ContainerManager` |
|---|---|---|
| Create | `create_all()` | `create_all()` |
| Detect existing | `detect_existing()` | `detect_existing()` |
| Batched create | `_create_batched/_create_concurrent/_create_single` | same |
| Port readiness | `_check_ports()` | `_check_ports()` |
| Liveness | `check_alive(state)` | `check_alive(state)` |
| Cleanup | `kill_all()` | `remove_all()` |
| State object | `SandboxState` | `ContainerState` |

Differences are only naming (`kill_all` vs `remove_all`) and the state type name. Adding kata / gvisor / agentenv means a third, fourth, fifth parallel copy.

**Goals**

- Formalize the existing-but-implicit shared manager interface as a contract.
- Stop parallel-copy proliferation when adding kata / gvisor / agentenv providers.
- Keep the benchmark kernel host-agnostic (depends only on `EnvironmentProvider` + `SandboxInstance`).
- Reuse `vm_monitor`'s `VMMonitorBase` (qemu/firecracker) — a provider exposes its VMM type and monitoring plugs in, no reinvention.

**Non-goals**

- Immediate implementation — this RFC is the direction anchor only.
- Restructuring config layering (handled by #76).
- Changing existing e2b/docker benchmark behavior.

## Detailed design

Extract an `EnvironmentProvider` (ABC/Protocol) + a unified `SandboxInstance` state, and lift `e2b_bench/bench.py` + `batch_scheduler` + task runners into a host-agnostic benchmark kernel that takes a provider by injection. `e2b_bench/` becomes the e2b provider impl, `docker_bench/` the docker provider, and kata/gvisor/agentenv each get their own.

`vm_monitor` already has the `VMMonitorBase` abstraction (qemu/firecracker) — a provider exposes its VMM type and monitoring plugs in, no reinvention.

### Interface-contract sketch (pseudo-code, not final)

```python
class SandboxInstance:  # unifies SandboxState / ContainerState
    id: str
    index: int
    numa_node: int | None
    ready: bool
    # ...

class EnvironmentProvider(ABC):
    name: str                            # "e2b" / "docker" / "kata" / ...
    @abstractmethod
    def create_all(self) -> dict[int, SandboxInstance]: ...
    @abstractmethod
    def detect_existing(self) -> dict[int, SandboxInstance]: ...
    @abstractmethod
    def check_alive(self, inst: SandboxInstance) -> bool: ...
    @abstractmethod
    def cleanup_all(self) -> None: ...   # unify kill_all / remove_all naming
    @property
    def vmm_type(self) -> str: ...       # plugs into vm_monitor.VMMonitorBase
    # exec / command-run, port probing, etc. added as needed
```

The benchmark kernel depends only on `EnvironmentProvider` and `SandboxInstance`, never importing e2b/docker-specific types.

### Scope boundary (important)

e2b-specific details **must not leak into the benchmark kernel**:

- NUMA binding (`_normalize_numa_bind` / `numa_node_for_index`)
- `smap_tool` memory migration
- sandbox IDs persistence (`detect_from_file` / `sandbox_ids_file`)
- E2B SDK env vars (`setup_e2b_env`)

These stay inside the provider impl or its dedicated config section.

## Drawbacks

- Largest refactor in the project; most work, phased over multiple independently-mergeable steps.
- Risk of over-abstraction if the contract is locked before real third/fourth providers exist.
- Interim cost: maintaining the contract while e2b/docker still carry provider-specific divergence.

## Alternatives considered

- **A. Provider ABC + lift the benchmark kernel (recommended, phased)**: cleanest, most work. First lock in the contract (step C below), then incrementally peel bench-kernel code off e2b specifics, each step independently mergeable.
- **B. Inheritance base class** (`SandboxManagerBase -> E2B/Docker` subclasses): smaller change, but inheritance couples more than composition, and duck-typing already works — limited payoff.
- **C. Minimal plugin registry**: define a Protocol + register providers only, leave the bench kernel untouched for now. Lowest risk, but keeps the kernel's duplication debt.

Recommend **A**, but phased: lock the contract first (the C step), then incrementally lift the kernel.

## Prior art / references

- `vm_monitor/VMMonitorBase` — the in-repo precedent (qemu/firecracker providers behind a base class).
- #76 — refactor(e2b): split config.py, eliminate field duplication (independent precursor cleanup; config layering ties in here).

## Unresolved questions

- **agentenv's shape**: listed as a to-be-implemented provider; its concrete runtime form (local process / lightweight sandbox / ...) TBD.
- Kernel workflow coverage: do docker/kata/gvisor run browser only, or also reuse coding/document workflows?
- Config layering: core config + `e2b_env` / `docker_env` / ... sections, and how it ties into #76 (config.py dedup).

## Future possibilities

- kata / gvisor / agentenv as first-class providers behind the same contract.
- Once finalized, open an implementation-plan issue / PR; this RFC is only the direction anchor.
