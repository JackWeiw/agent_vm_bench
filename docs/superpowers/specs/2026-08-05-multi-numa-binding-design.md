# Multi-NUMA Binding for E2B Sandbox Creation — Design

**Date:** 2026-08-05
**Status:** Approved (pending implementation)
**Branch:** to be created off `main`

## Problem

E2B sandbox creation binds each sandbox to a single NUMA node via the
`numa_bind` config field (`Optional[int]`, default `2`), passed to the E2B
`Sandbox.create()` API as the `FC_BIND` environment variable. When the total
sandbox count is large (e.g. 20), all sandboxes land on one NUMA node, which
does not exercise multi-node memory placement during benchmarks.

We need to spread sandboxes across multiple NUMA nodes. Given `numa_bind: [2, 3]`
and `total_count: 20`, the framework should create 10 sandboxes bound to node 2
and 10 bound to node 3. The default remains node 2.

## Goals

- Accept a list of NUMA nodes in `numa_bind` (e.g. `[2, 3]`), in addition to
  the existing single-int and `null` forms.
- Distribute sandboxes across the listed nodes in round-robin order by sandbox
  index, so counts divide evenly when divisible.
- Keep full backward compatibility: every existing config (`numa_bind: 2`,
  `numa_bind: null`) behaves exactly as before.
- No change to sandbox ID persistence or the detect-from-file flow.
- All code comments in English.

## Non-Goals

- No new CLI flag for `numa_bind`. It stays YAML-only, matching current
  behavior.
- No per-NUMA grouping in the sandbox IDs file. `Sandbox.list()` does not
  expose the NUMA node a running sandbox was bound to, so reconstructing
  placement on detect is not reliable. Detect reconnects by ID regardless of
  node.
- No change to the `vm_monitor_numa` field — that is a separate monitoring
  concept, not sandbox creation.
- No change to the E2B template's internal binding logic (lives outside this
  repo; it consumes the `FC_BIND` env var as today).

## Decisions

| Question | Decision |
|----------|----------|
| Remainder distribution (count not divisible by node count) | Round-robin by index — earlier nodes get the extra. 21 on `[2,3]` → 11 on node 2, 10 on node 3. |
| Config representation | Reuse `numa_bind`; accept `int`, `list[int]`, or `null`. Backward compatible. |
| ID file format | No change. One ID per line, as today. |

## Design

### Config schema

**Field** (`config.py`):

```python
# NUMA binding for sandbox creation.
# Accepts an int (single node), a list of ints (round-robin across nodes),
# or null (no binding). Defaults to node 2.
numa_bind: Optional[Union[int, List[int]]] = 2
```

Accepted YAML forms:

```yaml
numa_bind: 2          # single node (current behavior)
numa_bind: [2, 3]     # round-robin across nodes 2 and 3
numa_bind: null       # no binding
# omitted            # defaults to node 2
```

### Normalization

A module-level helper normalizes any input into a canonical
`Optional[List[int]]` so downstream code only ever handles the list-or-None
shape:

```python
def _normalize_numa_bind(value) -> Optional[List[int]]:
    """Normalize numa_bind input to a canonical list of node IDs or None.

    - None / empty / empty string -> None (no binding)
    - int N                       -> [N]
    - list of ints                -> dedup preserving order, drop non-positive
    """
```

Rules:

- `None`, `""`, `[]` → `None`
- `int N` (N > 0) → `[N]`
- `[N, M, ...]` → dedup preserving first-seen order, drop non-positive entries
- Invalid types raise `TypeError`; non-positive values are dropped with a
  warning.

`_from_dict()` runs the normalizer on the YAML value before constructing the
dataclass, so `config.numa_bind` is always `Optional[List[int]]` after load.
`from_args()` sets `numa_bind=[2]` (the normalized default).

### Distribution helper

```python
def numa_node_for_index(index: int, nodes: Optional[List[int]]) -> Optional[int]:
    """Return the NUMA node for a sandbox at the given 0-based index.

    Round-robin across `nodes`. Returns None when `nodes` is None (no binding).
    """
    if not nodes:
        return None
    return nodes[index % len(nodes)]
```

Examples with `nodes = [2, 3]`:

- index 0 → 2
- index 1 → 3
- index 2 → 2
- ...
- 20 sandboxes (indices 0–19): 10 on node 2, 10 on node 3.
- 21 sandboxes (indices 0–20): 11 on node 2, 10 on node 3 (round-robin
  remainder to the earlier node).

### Sandbox creation change

`sandbox_manager.py::_create_single()` currently sets a single fixed
`FC_BIND`:

```python
envs = {}
if self.config.numa_bind is not None:
    envs["FC_BIND"] = str(self.config.numa_bind)
```

Replace with a per-sandbox lookup by sandbox index (`sandbox_id - 1`):

```python
nodes = self.config.numa_bind  # Optional[List[int]], normalized
numa_node = numa_node_for_index(state.sandbox_id - 1, nodes)
envs = {}
if numa_node is not None:
    envs["FC_BIND"] = str(numa_node)
```

Everything else in `_create_single` (timeout, `Sandbox.create` call,
handle preservation) is unchanged. `_create_batched`,
`_create_batch_concurrent`, and `_create_concurrent` are unchanged — they
already iterate sandbox indices 1..N and call `_create_single`, so the
round-robin falls out naturally.

### Creation banner

`create_all()` / `_create_batched()` / `_create_concurrent()` print a banner.
Extend the banner to show the NUMA distribution when `numa_bind` is a list:

```
Concurrent Sandbox Creation
  Total: 20 sandboxes (full concurrent)
  NUMA nodes: [2, 3] (round-robin, 10 on node 2, 10 on node 3)
```

For a single node or None, the existing one-line summary stays.

### Batch scheduler

`batch_scheduler.py::GroupRunner._get_group_config()` already copies the whole
config (`**{k: v for k, v in self.config.__dict__.items()}`), so each group
inherits `numa_bind` and distributes its own `total_count` independently. No
change needed.

### CLI / config flow

- `from_args()`: change hardcoded `numa_bind=2` → `numa_bind=[2]` (normalized
  list, equivalent behavior).
- `merge_with_args()`: unchanged — it already does
  `numa_bind=yaml_config.numa_bind`, and the YAML value is normalized by
  `_from_dict`.
- No new CLI flag.

### Config YAMLs

Leave `numa_bind: 2` as-is in `bench.yaml`, `batch_template.yaml`,
`coding_bench.yaml`, `coding_go_bench.yaml`. Add a commented multi-node
example to `bench.yaml` only, as documentation:

```yaml
# NUMA node to bind sandbox (null/omit = no binding)
# Single node: numa_bind: 2
# Multi node (round-robin): numa_bind: [2, 3]
numa_bind: 2
```

No behavior change to any existing config.

## Testing

### Existing tests (must keep passing — backward compat)

- `test_sandbox_manager.py::TestNumaBinding` — single-int and null cases.
- `test_config.py::TestConfigNumaBind` — single-int, null, YAML load, merge,
  `from_args` default.

The single-int behavior is preserved because `numa_bind: 2` normalizes to
`[2]`, and `numa_node_for_index(i, [2])` always returns `2` — equivalent to
the old `FC_BIND = "2"` for every sandbox.

### New tests

**`test_config.py::TestConfigMultiNumaBind`**:

- YAML `numa_bind: [2, 3]` → normalized `[2, 3]`.
- YAML `numa_bind: 2` → `[2]` (backward compat).
- YAML `numa_bind: null` → `None`.
- YAML `numa_bind: [2, 2, 3]` → dedup to `[2, 3]`.
- YAML `numa_bind: [2, 0, 3]` → drop non-positive → `[2, 3]`.
- `from_args()` default → `[2]`.
- `numa_node_for_index` unit tests: index 0/1/2 on `[2,3]` → 2/3/2; `None`
  nodes → `None`; empty list → `None`.

**`test_sandbox_manager.py::TestMultiNumaBinding`**:

- 20 sandboxes on `numa_bind=[2, 3]` → assert odd sandbox IDs get
  `FC_BIND="2"`, even IDs get `FC_BIND="3"` (mock `Sandbox.create` to capture
  `envs`).
- 21 sandboxes on `[2, 3]` → 11 on node 2, 10 on node 3.
- Single-node `[2]` → every sandbox gets `FC_BIND="2"` (equivalent to old
  `numa_bind=2`).
- `numa_bind=None` → `envs` is `None` / empty for every sandbox.

## Files Touched (Commit Scope)

- `e2b_bench/config.py` — field type, `_normalize_numa_bind`,
  `numa_node_for_index`, `_from_dict`, `from_args`, validation.
- `e2b_bench/sandbox_manager.py` — `_create_single` NUMA lookup, banner print.
- `e2b_bench/tests/test_config.py` — `TestConfigMultiNumaBind` + helper unit
  tests.
- `e2b_bench/tests/test_sandbox_manager.py` — `TestMultiNumaBinding`.
- `config/e2b/bench.yaml` — commented multi-node example only (no behavior
  change).

No unrelated code. New branch off `main`. Pre-commit run before commit. No
Claude attribution in the commit message.

## Out of Scope / Future

- CLI flag for `numa_bind` (can be added later if needed).
- Per-NUMA ID persistence / detect-time reconstruction (not reliable without
  E2B exposing the node).
- Binding via `numactl` (the E2B template consumes `FC_BIND`; no host-side
  `numactl` is involved in sandbox creation).
