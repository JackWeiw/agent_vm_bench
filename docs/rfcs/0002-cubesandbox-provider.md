---
rfc: 0002
title: Add cubesandbox provider (Cloud Hypervisor microVM, lifecycle-capable)
status: Draft
author: "@JackWeiw"
shepherd: ""
areas: [e2b, vm_monitor]
created: 2026-09-03
updated: 2026-09-03
---

# Add cubesandbox provider (Cloud Hypervisor microVM, lifecycle-capable)

## Summary

Add a new `cubesandbox` provider under `src/env_provider/` — the second lifecycle-capable backend alongside `aenv` — driven by the native `cubesandbox` Python SDK (a Cloud Hypervisor / KVM microVM service by Tencent Cloud). It implements all three replay Protocols (`LifecycleCapable` pause/resume with memory snapshot, `EphemeralCapable` create_one/kill_one, `SnapshotSizeCapable`), so it runs every replay mode (`exec_only`/`lifecycle`/`trajectory`) like `aenv`, but on a different VMM. Host-level `vm_monitor` support is deferred to a follow-on (`cube-hypervisor` monitor type); the contract and kernel are untouched except two one-line registrations.

## Motivation

CubeSandbox is a high-performance microVM sandbox service built on a Cloud Hypervisor fork (RustVMM/KVM — the same family as Firecracker). It offers **native pause/resume that preserves a full memory snapshot**, plus snapshot/rollback/clone. That is exactly the lifecycle surface the replay `lifecycle` mode (memory-reuse oversubscription benchmark) requires, on a *different* VMM than aenv/e2b (Firecracker) — giving the bench a second, comparable lifecycle backend instead of another exec-only one.

Adding a provider is an architecture-level change — a new backend family, a new optional SDK dependency, a new config block, and a possible monitor extension — so it warrants RFC review per `docs/rfcs/README.md` before code lands.

**Goals**

- A first-class `cubesandbox` provider exposing all three replay Protocols → all replay modes work with `--provider cubesandbox`.
- Reuse the existing `BaseSandboxManager` + `ReadyChecker` + config-layering patterns; **no kernel/contract changes**.
- Use the native SDK (not E2B-SDK reuse) so the full lifecycle surface (pause/resume/snapshot/rollback/clone) is directly available.
- Keep the SDK optional (lazy import), consistent with e2b/docker.

**Non-goals**

- Host-level `vm_monitor` (`cube-hypervisor` monitor class) in v1 — documented follow-on.
- Real VM-id parsing from the cloud-hypervisor cmdline (the firecracker monitor has the same open TODO).
- NUMA binding (CubeSandbox resources are template-level; `numa_node` stays `None`).
- Changing the contract (`src/env_provider/__init__.py`), the kernel (`bench_core/`), `BaseSandboxManager`, or `ReadyChecker`.

## Detailed design

### Architecture

New package `src/env_provider/cubesandbox/` (mirrors `e2b/`/`docker/`):

- `__init__.py` — `CubesandboxProvider(EnvironmentProvider)` + `build_provider()`.
- `config.py` — `Config.from_raw(raw, block="cubesandbox")` + `setup_cube_env()`.
- `manager.py` — `CubesandboxManager(BaseSandboxManager)`; supplies the SDK seams.
- `schemas.py` — `CubeSandboxState`; re-exports `BackendSandboxStatus`/`BackendCreationMetrics`.
- `_snapshot.py` — snapshot-dir scan for `snapshot_sizes`.

Provider class attrs: `name="cubesandbox"`, `default_replay_mode="lifecycle"`, `vmm_type=None` (v1; flip to the `cube-hypervisor` monitor type when the follow-on lands). Implements `LifecycleCapable`, `EphemeralCapable`, `SnapshotSizeCapable`.

### SDK → contract mapping

| contract | cubesandbox SDK | notes |
|---|---|---|
| `create_all` | `Sandbox.create(template, timeout, envs, metadata, lifecycle=…)` | via `BaseSandboxManager` concurrent/batched skeleton |
| `detect_existing` | `Sandbox.list()` → `Sandbox.connect(id)` | list → attach → ready-check |
| `detect_from_ids` | `Sandbox.connect(id)` per ID | mirrors e2b `detect_from_file` |
| `save_ids` | write IDs file | mirrors e2b |
| `check_alive` | `sb.get_info()`; `SandboxNotFoundError` → dead | 404 = deleted |
| `cleanup_all` / `cleanup_existing` | `sb.kill()` | `_set_killed_on_cleanup=False` (keep original creation status) |
| `exec` | `sb.commands.run(cmd, timeout, cwd, envs, user="root")` → `CommandResult` | timeout → exit 124; `SandboxNotFoundError` → raise |
| `prepare_env` | `setup_cube_env()` exports `CUBE_*` env vars | |
| `prepare` | no-op | template-baked backend (like e2b) — see Unresolved questions |

### Lifecycle / ephemeral / snapshot

- `pause(inst)` → `sb.pause(wait=True)` (preserves the memory snapshot).
- `resume(inst)` → `state.cube_sandbox = Sandbox.connect(inst.id)` (auto-resumes; fresh handle; mirrors aenv).
- `create_one(index, *, template, metadata)` → `Sandbox.create(template, metadata=metadata)` + ready-check. `metadata` is for operator visibility only, **not** an idempotency key (matches the contract's deferred G3).
- `kill_one(inst)` → `sb.kill()` if a handle is present; safe no-op otherwise (runner finally-path safety).
- `snapshot_sizes(inst)` → scan a configured local `snapshot_dir`; return a dict or `None` (silent skip). The SDK's `SnapshotInfo` carries only `snapshot_id`/`names` (no size) and exposes no runtime stats call, so size must come from the filesystem or be skipped. CubeSandbox is a remote service, so v1 defaults to `None` (graceful skip); real attribution is deployment-dependent.

### Idle timeout / auto_resume

Create with a long `timeout` (`create_timeout`, default `86400`) to avoid idle eviction during long bench runs. Do **not** enable platform `auto_resume` (keep `lifecycle={"on_timeout": "kill"}` default): lifecycle mode is driven by the kernel's explicit `resume()`/`pause()`; platform auto-wake would race with it. Note: `Sandbox.connect` / `detect_from_ids` auto-resumes a paused sandbox — acceptable for `--detect` (which expects running sandboxes), but a behavioral divergence from aenv/e2b (whose `connect` does not auto-resume); documented.

### Config layering

New `cubesandbox:` block in `config/common/*.yaml` (`replay.yaml`, `replay-trajectory.yaml`, `browser.yaml`, `coding-ts/go/python.yaml`):

```yaml
cubesandbox:
  template: "openclaw-coding-python-v1"
  create_timeout: 86400
  sandbox_ids_file: "sandboxs_replay_cube.txt"
  snapshot_dir: null            # local CubeCoW dir; null = skip snapshot_sizes
  env:
    CUBE_API_URL: "http://127.0.0.1:3000"
    CUBE_API_KEY: "your_cube_api_key_here"   # placeholder = unset → fall back to env
    CUBE_TEMPLATE_ID: ""
    CUBE_SANDBOX_DOMAIN: "cube.app"
```

`Config.from_raw(raw, block="cubesandbox")` reads `template` / `create_timeout` / `sandbox_ids_file` / `snapshot_dir` / `env.{CUBE_*}`. Credential placeholders (`your_cube_api_key_here`) are treated as unset → fall back to env (the SDK is env-only; there is no `~/.cubesandbox/config.json`, but `.env` + the config `env:` block are supported, consistent with e2b's placeholder fallback).

### Schemas / class attrs

`CubeSandboxState`: `cube_sandbox_id: str`, `cube_sandbox` (handle), `batch_id`, `workflow_type`, `creation_metrics`, `is_alive`, `stopped_by_cleanup`, `warmup_done`; re-export `BackendSandboxStatus as SandboxStatus`, `BackendCreationMetrics as CreationMetrics`.

Manager class attrs: `_handle_attr="cube_sandbox"`, `_noun="Sandbox"`, `_id_attr="cube_sandbox_id"`, `_set_killed_on_cleanup=False`; `_STATUS_MAP` mirrors e2b.

### Readiness

`_exec_probe(handle, cmd, timeout)` → `r = handle.commands.run(cmd, user="root", timeout=timeout); return r.exit_code, r.stdout, r.stderr` (a ~3-line closure mirroring e2b). Reuses the kernel constants `READY_MAX_WAIT` / `READY_INTERVAL` / `BROWSER_REQUIRED_PORTS`; **no per-backend YAML readiness knobs** (provider-transparent).

### Tests (SDK-free, two-tier)

- `test_cubesandbox_provider.py` — inject a `Mock()` manager via `provider._manager`; assert translate/exec/exception→`CommandResult`/`build_provider` reads the `cubesandbox:` block/`isinstance(LifecycleCapable, EphemeralCapable, SnapshotSizeCapable)`/`default_replay_mode == "lifecycle"`/`name`/`vmm_type is None`.
- `test_cubesandbox_manager.py` — inject a fake `Sandbox` class into the manager module; drive the real base lifecycle (create_all/detect_existing/detect_from_ids/cleanup_all) + pause/resume + create_one/kill_one.
- `test_cubesandbox_snapshot.py` — snapshot-dir scan.

### Registration touch points

1. `bench.py _build_provider`: add `elif name == "cubesandbox": from env_provider.cubesandbox import build_provider`.
2. `bench.py` CLI `--provider choices`: add `"cubesandbox"`.
3. `pyproject.toml`: add the `cubesandbox` SDK to `[project.optional-dependencies]`.
4. `config/common/*.yaml`: add `cubesandbox:` blocks.
5. Tests (above).
6. Docs: `CLAUDE.md` (architecture diagram, providers table, adding-a-provider section, scripts table), `docs/bench-core-usage-zh.md` §8.

The contract, kernel, `BaseSandboxManager`, and `ReadyChecker` are unchanged — this is the "one new submodule + two one-line edits" promise of RFC 0001 holding up against a fourth real provider.

## Drawbacks

- A new optional SDK dependency (`cubesandbox` on PyPI) and a new provider package to maintain.
- `snapshot_sizes` cannot read size from the SDK (no fields, no stats call); v1 silently skips unless a local snapshot dir is configured — the replay `snapshot_size` sheet will be empty for cubesandbox unless the deployment exposes the CubeCoW store locally.
- v1 carries no host-level metrics (no `cube-hypervisor` monitor yet), so cubesandbox runs are less directly comparable to e2b/aenv runs that ship vm_monitor sheets.
- Behavioral divergence: `Sandbox.connect` auto-resumes paused sandboxes, so `detect_from_ids` cannot preserve a paused state (aenv/e2b `connect` does not auto-resume). Documented, but a real difference.

## Alternatives considered

- **A. Standalone package + native `cubesandbox` SDK (recommended)** — the full lifecycle surface is directly available; cleanest fit for all three Protocols; consistent with the e2b/docker structure.
- **B. Subclass `E2BProvider`, reuse the E2B SDK pointed at CubeSandbox (mirror aenv)** — less code (reuses the e2b manager), but loses native `create_snapshot`/`rollback`/`clone`, relies on E2B `beta_pause` whose semantics may not match CubeSandbox's pause, and couples to e2b internals. `snapshot_sizes` still has to be self-implemented, so the reuse saving is nil for full scope.
- **C. Single-file `cubesandbox.py` (like `fake.py`)** — would reimplement concurrency/stop-event/ready-check plumbing, forgoing the `BaseSandboxManager` template; not worth it for a full lifecycle provider.

## Prior art / references

- RFC 0001 — the `EnvironmentProvider` abstract layer this provider plugs into.
- `src/env_provider/aenv/` — the existing lifecycle-capable provider template (`pause`/`resume`/`snapshot_sizes`/`create_one`/`kill_one`).
- `src/env_provider/e2b/` — the SDK-backed `BaseSandboxManager` pattern (create/detect/cleanup/exec/detect_from_file) this mirrors.
- `docs/superpowers/specs/2026-06-21-vm-monitor-refactor-design.md` — documents adding a new VMM type (the `cube-hypervisor` follow-on).
- CubeSandbox SDK: `sdk/python/cubesandbox/` (`Sandbox`, `Config`, `SnapshotInfo`, `SandboxState`, `CommandResult`); OpenAPI at `openapi.yml`.

## Unresolved questions

- Does the CubeSandbox browser workflow template bake the agent-browser backend (→ `prepare` no-op like e2b), or must the provider start it (like docker)? Assumed baked.
- Where does CubeSandbox persist snapshots on disk (CubeCoW store path), and is it locally accessible from the bench host for `snapshot_sizes`, or server-side only? Determines whether v1 `snapshot_sizes` can ever be non-`None`.
- What is the exact running process name of the VMM (`cube-hypervisor` per its `Cargo.toml`, or `cloud-hypervisor`)? Needed for the follow-on monitor's `get_process_names()`.
- Should `areas` gain a `cubesandbox`/`env_provider` value (this RFC uses `[e2b, vm_monitor]` as the closest existing labels)?

## Future possibilities

- A new `vm_monitor` type for the `cube-hypervisor` VMM process (Cloud Hypervisor fork) + registration + CLI choice; flip `vmm_type` to that type for host-level + per-microVM metrics during stress. (Exact type string / filename TBD — see Unresolved questions.)
- Real cloud-hypervisor cmdline/socket VM-id parsing (replaces the `clh-{pid}` fallback; the firecracker monitor has the same open TODO).
- `snapshot_sizes` real attribution when local FS access to the CubeCoW store is available.
- Clone/rollback benchmark scenarios leveraging `sb.clone()` / `sb.rollback()` — CubeSandbox-specific workflows beyond replay.
