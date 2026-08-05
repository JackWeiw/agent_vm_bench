# Multi-NUMA Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow E2B sandbox creation to spread sandboxes across multiple NUMA nodes in round-robin order (e.g. `numa_bind: [2, 3]` with `total_count: 20` → 10 on node 2, 10 on node 3), defaulting to node 2.

**Architecture:** Add a normalizer that converts `numa_bind` (`int | list[int] | null`) to a canonical `Optional[List[int]]` at config load time. Add a `numa_node_for_index` helper that round-robins a sandbox's 0-based index across the node list. Change `SandboxManager._create_single` to look up the per-sandbox node by `sandbox_id - 1` instead of using one fixed value. Existing single-int configs normalize to a single-element list and behave identically.

**Tech Stack:** Python 3, dataclasses, PyYAML, pytest, unittest.mock.

**Spec:** [docs/superpowers/specs/2026-08-05-multi-numa-binding-design.md](../specs/2026-08-05-multi-numa-binding-design.md)

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `e2b_bench/config.py` | Config dataclass, YAML/CLI loading, NUMA normalization + distribution helpers | Modify |
| `e2b_bench/sandbox_manager.py` | Sandbox lifecycle; `_create_single` applies per-sandbox NUMA binding | Modify |
| `e2b_bench/tests/test_config.py` | Config + NUMA helper unit tests | Modify |
| `e2b_bench/tests/test_sandbox_manager.py` | NUMA binding creation tests | Modify |
| `config/e2b/bench.yaml` | Documented multi-node example | Modify (comment only) |

**Branch:** Create a new branch off `main` before Task 1. Do not commit unrelated files (the working tree has unrelated modified/untracked files — only stage the files listed above plus this plan and the spec).

**All code comments must be in English.**

---

### Task 1: Add NUMA normalization and distribution helpers

**Files:**
- Modify: `e2b_bench/config.py` (add helpers near the top, after the imports at line 12, before the first `@dataclass` at line 22)
- Test: `e2b_bench/tests/test_config.py` (add a new test class at the end of the file)

- [ ] **Step 1: Write the failing tests**

Append to `e2b_bench/tests/test_config.py`:

```python
from e2b_bench.config import _normalize_numa_bind, numa_node_for_index


class TestNormalizeNumaBind:
    """Tests for _normalize_numa_bind helper"""

    def test_none_returns_none(self):
        """None input returns None (no binding)"""
        assert _normalize_numa_bind(None) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None"""
        assert _normalize_numa_bind("") is None

    def test_empty_list_returns_none(self):
        """Empty list returns None"""
        assert _normalize_numa_bind([]) is None

    def test_single_int_returns_singleton_list(self):
        """A single int N returns [N]"""
        assert _normalize_numa_bind(2) == [2]

    def test_list_returns_list_unchanged(self):
        """A list of ints is returned as-is"""
        assert _normalize_numa_bind([2, 3]) == [2, 3]

    def test_list_dedup_preserves_order(self):
        """Duplicate nodes are removed, preserving first-seen order"""
        assert _normalize_numa_bind([2, 2, 3]) == [2, 3]

    def test_list_drops_non_positive(self):
        """Non-positive node IDs are dropped"""
        assert _normalize_numa_bind([2, 0, 3, -1]) == [2, 3]

    def test_list_all_non_positive_returns_none(self):
        """All-non-positive list returns None (no binding)"""
        assert _normalize_numa_bind([0, -1]) is None


class TestNumaNodeForIndex:
    """Tests for numa_node_for_index round-robin helper"""

    def test_none_nodes_returns_none(self):
        """None nodes returns None (no binding)"""
        assert numa_node_for_index(0, None) is None

    def test_empty_nodes_returns_none(self):
        """Empty node list returns None"""
        assert numa_node_for_index(0, []) is None

    def test_round_robin_two_nodes(self):
        """Round-robin across two nodes: 0->2, 1->3, 2->2, 3->3"""
        nodes = [2, 3]
        assert numa_node_for_index(0, nodes) == 2
        assert numa_node_for_index(1, nodes) == 3
        assert numa_node_for_index(2, nodes) == 2
        assert numa_node_for_index(3, nodes) == 3

    def test_single_node_always_returns_that_node(self):
        """A single-node list always returns that node"""
        nodes = [5]
        assert numa_node_for_index(0, nodes) == 5
        assert numa_node_for_index(99, nodes) == 5

    def test_20_sandboxes_two_nodes_even_split(self):
        """20 sandboxes on [2,3]: 10 on node 2, 10 on node 3"""
        nodes = [2, 3]
        node_counts = {2: 0, 3: 0}
        for i in range(20):
            node_counts[numa_node_for_index(i, nodes)] += 1
        assert node_counts == {2: 10, 3: 10}

    def test_21_sandboxes_two_nodes_remainder_to_first(self):
        """21 sandboxes on [2,3]: 11 on node 2, 10 on node 3"""
        nodes = [2, 3]
        node_counts = {2: 0, 3: 0}
        for i in range(21):
            node_counts[numa_node_for_index(i, nodes)] += 1
        assert node_counts == {2: 11, 3: 10}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest e2b_bench/tests/test_config.py::TestNormalizeNumaBind e2b_bench/tests/test_config.py::TestNumaNodeForIndex -v`
Expected: FAIL with `ImportError: cannot import name '_normalize_numa_bind'` (or similar).

- [ ] **Step 3: Write minimal implementation**

Insert the following into `e2b_bench/config.py` after the imports (after line 12, the `import yaml` line; before line 22, the first `@dataclass`):

```python
def _normalize_numa_bind(value) -> Optional[List[int]]:
    """Normalize numa_bind input to a canonical list of node IDs or None.

    Accepts an int (single node), a list of ints, None, or an empty string.
    Returns None (no binding) for None / empty / all-non-positive input.
    Non-positive node IDs are dropped. Duplicate IDs are removed, preserving
    first-seen order.
    """
    # Treat empty string as "no binding" (defensive; YAML null is the norm)
    if value is None or value == "" or value == []:
        return None

    # Single int -> singleton list
    if isinstance(value, int) and not isinstance(value, bool):
        if value <= 0:
            return None
        return [value]

    # List of ints: dedup preserving order, drop non-positive
    if isinstance(value, list):
        seen = set()
        normalized: List[int] = []
        for item in value:
            # bool is a subclass of int; reject it explicitly
            if not isinstance(item, int) or isinstance(item, bool):
                raise TypeError(f"numa_bind list items must be ints, got {type(item).__name__}")
            if item <= 0:
                continue
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized if normalized else None

    raise TypeError(f"numa_bind must be int, list[int], or null, got {type(value).__name__}")


def numa_node_for_index(index: int, nodes: Optional[List[int]]) -> Optional[int]:
    """Return the NUMA node for a sandbox at the given 0-based index.

    Round-robins across `nodes`. Returns None when `nodes` is None or empty
    (no binding).
    """
    if not nodes:
        return None
    return nodes[index % len(nodes)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest e2b_bench/tests/test_config.py::TestNormalizeNumaBind e2b_bench/tests/test_config.py::TestNumaNodeForIndex -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add e2b_bench/config.py e2b_bench/tests/test_config.py
git commit -m "feat(e2b): add NUMA normalization and round-robin distribution helpers"
```

---

### Task 2: Wire the normalizer into Config loading and update existing config tests

**Files:**
- Modify: `e2b_bench/config.py:182` (field type), `e2b_bench/config.py:319` (`_from_dict`), `e2b_bench/config.py:396` (`merge_with_args`), `e2b_bench/config.py:496` (`from_args`)
- Test: `e2b_bench/tests/test_config.py` (update `TestConfigNumaBind` assertions)

- [ ] **Step 1: Update the field type and default**

In `e2b_bench/config.py`, find line 181-182:

```python
    # NUMA binding for sandbox creation (None = no binding, int = bind to specific NUMA node)
    numa_bind: Optional[int] = 2
```

Replace with:

```python
    # NUMA binding for sandbox creation.
    # Accepts an int (single node), a list of ints (round-robin across nodes),
    # or null (no binding). Defaults to node 2. Normalized to a list or None
    # at load time (see _normalize_numa_bind).
    numa_bind: Optional[Union[int, List[int]]] = 2
```

Ensure `Union` is imported. The existing import line is:

```python
from typing import Any, Dict, List, Optional
```

Change it to:

```python
from typing import Any, Dict, List, Optional, Union
```

- [ ] **Step 2: Normalize in `_from_dict`**

In `e2b_bench/config.py`, find line 319:

```python
            numa_bind=sandbox.get("numa_bind", 2),
```

Replace with:

```python
            numa_bind=_normalize_numa_bind(sandbox.get("numa_bind", 2)),
```

- [ ] **Step 3: Normalize in `from_args`**

In `e2b_bench/config.py`, find line 496:

```python
            numa_bind=2,  # Default to NUMA node 2 when using CLI args only
```

Replace with:

```python
            numa_bind=_normalize_numa_bind(2),  # Default to NUMA node 2 when using CLI args only
```

- [ ] **Step 4: Update `merge_with_args` to keep YAML value (already normalized)**

In `e2b_bench/config.py`, find line 396:

```python
            numa_bind=yaml_config.numa_bind,  # Use yaml config for numa_bind
```

This is already correct (YAML value is normalized by `_from_dict`). Leave the line as-is; the comment still holds. No change needed in this step.

- [ ] **Step 5: Update existing `TestConfigNumaBind` assertions**

The stored representation now normalizes single ints to singleton lists. Update the assertions in `e2b_bench/tests/test_config.py` within `class TestConfigNumaBind`:

Find (line ~1220-1223):

```python
    def test_default_numa_bind(self):
        """Default numa_bind is 2"""
        config = Config()
        assert config.numa_bind == 2
```

Replace the assertion with:

```python
        assert config.numa_bind == [2]
```

Find (line ~1225-1228):

```python
    def test_set_via_constructor(self):
        """Set numa_bind via constructor"""
        config = Config(numa_bind=3)
        assert config.numa_bind == 3
```

Note: the `Config` dataclass constructor does NOT normalize (only `_from_dict`/`from_args` do). To make this test exercise normalization, change it to pass an already-normalized value OR keep the raw value. The constructor stores raw values, so `Config(numa_bind=3)` stores `3` (int), not `[3]`. **Keep this test asserting the raw value** since the constructor is a raw passthrough. Leave it as `assert config.numa_bind == 3`.

Add a NEW test right after `test_set_via_constructor` to document the constructor's raw-passthrough nature:

```python
    def test_constructor_does_not_normalize(self):
        """Constructor stores raw value; normalization happens in _from_dict/from_args"""
        config = Config(numa_bind=[2, 3])
        assert config.numa_bind == [2, 3]
        # A raw int is NOT converted to a list by the constructor
        config_int = Config(numa_bind=5)
        assert config_int.numa_bind == 5
```

Find (line ~1230-1233):

```python
    def test_set_null_via_constructor(self):
        """Set numa_bind to None (disabled)"""
        config = Config(numa_bind=None)
        assert config.numa_bind is None
```

This is unchanged (constructor passthrough). Leave as-is.

Find (line ~1235-1248), `test_load_from_yaml`:

```python
        config = Config.load_from_yaml(temp_path)

        assert config.numa_bind == 5
```

Replace the assertion with:

```python
        assert config.numa_bind == [5]
```

- [ ] **Step 6: Write the failing multi-NUMA config tests**

Append a new class to `e2b_bench/tests/test_config.py`:

```python
class TestConfigMultiNumaBind:
    """Tests for multi-node numa_bind loading and normalization"""

    def test_load_list_from_yaml(self):
        """Load a list of NUMA nodes from YAML"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: [2, 3]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == [2, 3]

    def test_load_single_int_from_yaml_normalizes_to_list(self):
        """A single int in YAML normalizes to a singleton list"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: 5
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == [5]

    def test_load_null_from_yaml(self):
        """null numa_bind in YAML normalizes to None"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: null
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind is None

    def test_load_missing_numa_bind_defaults_to_node_2(self):
        """Missing numa_bind key defaults to [2]"""
        yaml_content = """
sandbox:
  template: custom-template
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == [2]

    def test_load_dedup_in_yaml(self):
        """Duplicate nodes in YAML are deduplicated"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: [2, 2, 3]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == [2, 3]

    def test_load_drops_non_positive_in_yaml(self):
        """Non-positive node IDs in YAML are dropped"""
        yaml_content = """
sandbox:
  template: custom-template
  numa_bind: [2, 0, 3, -1]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        config = Config.load_from_yaml(temp_path)
        os.unlink(temp_path)

        assert config.numa_bind == [2, 3]

    def test_from_args_defaults_to_singleton_list(self):
        """from_args (CLI-only) defaults numa_bind to [2]"""
        parser = build_arg_parser()
        args = parser.parse_args([])
        config = Config.from_args(args)
        assert config.numa_bind == [2]
```

If `build_arg_parser` is not already imported in the test file, add to the imports at the top of `e2b_bench/tests/test_config.py`:

```python
from e2b_bench.config import Config
from e2b_bench.bench import build_arg_parser
```

(Adjust the import path if `build_arg_parser` lives elsewhere — verify with: `grep -n "def build_arg_parser" e2b_bench/bench.py`. It is defined in `e2b_bench/bench.py`.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest e2b_bench/tests/test_config.py -v`
Expected: PASS (all `TestConfigNumaBind`, `TestConfigMultiNumaBind`, `TestNormalizeNumaBind`, `TestNumaNodeForIndex` tests green).

- [ ] **Step 8: Commit**

```bash
git add e2b_bench/config.py e2b_bench/tests/test_config.py
git commit -m "feat(e2b): normalize numa_bind to list-of-nodes at config load time"
```

---

### Task 3: Apply per-sandbox NUMA binding in `_create_single`

**Files:**
- Modify: `e2b_bench/config.py` (add import in sandbox_manager), `e2b_bench/sandbox_manager.py:396-411` (`_create_single`)
- Test: `e2b_bench/tests/test_sandbox_manager.py` (add `TestMultiNumaBinding`)

- [ ] **Step 1: Write the failing multi-NUMA creation tests**

Append to `e2b_bench/tests/test_sandbox_manager.py`:

```python
class TestMultiNumaBinding:
    """Tests for multi-NUMA round-robin binding during sandbox creation"""

    def _create_single_and_capture_envs(self, config, sandbox_id):
        """Helper: run _create_single on a sandbox_id, return the envs passed to Sandbox.create"""
        manager = SandboxManager(config, Event())
        state = SandboxState(sandbox_id=sandbox_id)
        with patch("e2b_bench.sandbox_manager.Sandbox.create") as mock_create:
            mock_sandbox = Mock()
            mock_sandbox.sandbox_id = f"sbx_{sandbox_id}"
            mock_create.return_value = mock_sandbox
            manager._create_single(state)
            _, kwargs = mock_create.call_args
            return kwargs.get("envs")

    def test_two_nodes_round_robin_20_sandboxes(self):
        """20 sandboxes on [2,3]: odd IDs -> node 2, even IDs -> node 3"""
        config = Config(numa_bind=[2, 3])
        for sandbox_id in range(1, 21):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            expected_node = 2 if sandbox_id % 2 == 1 else 3
            assert envs == {"FC_BIND": str(expected_node)}, (
                f"sandbox_id={sandbox_id} expected FC_BIND={expected_node}, got {envs}"
            )

    def test_two_nodes_21_sandboxes_remainder_to_first(self):
        """21 sandboxes on [2,3]: 11 on node 2, 10 on node 3"""
        config = Config(numa_bind=[2, 3])
        node_counts = {2: 0, 3: 0}
        for sandbox_id in range(1, 22):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            # 0-based index = sandbox_id - 1; even index -> node 2, odd -> node 3
            index = sandbox_id - 1
            expected_node = 2 if index % 2 == 0 else 3
            assert envs == {"FC_BIND": str(expected_node)}
            node_counts[expected_node] += 1
        assert node_counts == {2: 11, 3: 10}

    def test_single_node_list_equivalent_to_int(self):
        """[2] produces FC_BIND=2 for every sandbox (same as old numa_bind=2)"""
        config = Config(numa_bind=[2])
        for sandbox_id in [1, 2, 5, 20]:
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            assert envs == {"FC_BIND": "2"}

    def test_three_nodes_round_robin(self):
        """[2, 3, 5] round-robins across three nodes"""
        config = Config(numa_bind=[2, 3, 5])
        nodes = [2, 3, 5]
        for sandbox_id in range(1, 10):
            envs = self._create_single_and_capture_envs(config, sandbox_id)
            expected_node = nodes[(sandbox_id - 1) % 3]
            assert envs == {"FC_BIND": str(expected_node)}

    def test_none_numa_bind_no_envs(self):
        """numa_bind=None produces envs=None for every sandbox"""
        config = Config(numa_bind=None)
        envs = self._create_single_and_capture_envs(config, sandbox_id=1)
        assert envs is None or envs == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest e2b_bench/tests/test_sandbox_manager.py::TestMultiNumaBinding -v`
Expected: FAIL — `_create_single` still uses the old fixed-value logic, so e.g. `sandbox_id=2` gets `FC_BIND=2` instead of `3`.

- [ ] **Step 3: Modify `_create_single` to look up the per-sandbox node**

In `e2b_bench/sandbox_manager.py`, find the import section at the top. The file imports from `.config`:

```python
from .config import Config
```

Change it to also import the helper:

```python
from .config import Config, numa_node_for_index
```

(Verify the exact import line with `grep -n "from .config import" e2b_bench/sandbox_manager.py` and adjust accordingly. If it uses `from e2b_bench.config import Config`, change that line instead.)

Then find the NUMA block in `_create_single` (lines ~407-411):

```python
        try:
            # Build envs dict with NUMA binding if configured
            envs = {}
            if self.config.numa_bind is not None:
                envs["FC_BIND"] = str(self.config.numa_bind)
```

Replace with:

```python
        try:
            # Build envs dict with NUMA binding if configured.
            # numa_bind is a normalized list of nodes (or None); round-robin
            # across them by sandbox index so sandboxes spread evenly.
            numa_node = numa_node_for_index(state.sandbox_id - 1, self.config.numa_bind)
            envs = {}
            if numa_node is not None:
                envs["FC_BIND"] = str(numa_node)
```

Leave the rest of `_create_single` (the `Sandbox.create` call, handle preservation, return dict) unchanged. The line a few lines down that passes `envs` to `Sandbox.create`:

```python
            sbx = Sandbox.create(self.config.template, timeout=self.config.create_timeout, envs=envs if envs else None)
```

already does the right thing (`envs if envs else None` → `None` when no binding). No change needed there.

- [ ] **Step 4: Run the multi-NUMA tests to verify they pass**

Run: `python -m pytest e2b_bench/tests/test_sandbox_manager.py::TestMultiNumaBinding -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the existing NUMA tests to verify backward compat**

Run: `python -m pytest e2b_bench/tests/test_sandbox_manager.py::TestNumaBinding -v`
Expected: PASS (4 tests). The existing tests pass `Config(numa_bind=2)` (constructor stores raw int `2`) and assert `envs == {"FC_BIND": "2"}`. **This requires the constructor value to flow through `numa_node_for_index`.**

⚠️ **Check:** `numa_node_for_index(0, 2)` — the second arg is an int `2`, not a list. The helper does `if not nodes: return None` then `nodes[index % len(nodes)]`. An int has no `len()` → `TypeError`. The existing `TestNumaBinding` tests will FAIL here because the constructor stores raw int `2`, not `[2]`.

**Fix option A (preferred):** Make `numa_node_for_index` tolerant of a raw int by normalizing defensively:

Update `numa_node_for_index` in `e2b_bench/config.py` to:

```python
def numa_node_for_index(index: int, nodes) -> Optional[int]:
    """Return the NUMA node for a sandbox at the given 0-based index.

    Round-robins across `nodes`. Accepts a list of ints, a single int (treated
    as a one-element list), or None (no binding). Returns None when `nodes`
    is None/empty.
    """
    if nodes is None or nodes == "" or nodes == []:
        return None
    # Tolerate a raw int (constructor passthrough) by wrapping it
    if isinstance(nodes, int) and not isinstance(nodes, bool):
        if nodes <= 0:
            return None
        return nodes
    if not nodes:
        return None
    return nodes[index % len(nodes)]
```

Also update the corresponding `TestNumaNodeForIndex::test_none_nodes_returns_none` and add a raw-int case. Add this test to `TestNumaNodeForIndex` in `e2b_bench/tests/test_config.py`:

```python
    def test_raw_int_returns_that_node(self):
        """A raw int (constructor passthrough) is treated as a one-element list"""
        assert numa_node_for_index(0, 2) == 2
        assert numa_node_for_index(5, 2) == 2
```

Re-run: `python -m pytest e2b_bench/tests/test_config.py::TestNumaNodeForIndex e2b_bench/tests/test_sandbox_manager.py::TestNumaBinding e2b_bench/tests/test_sandbox_manager.py::TestMultiNumaBinding -v`
Expected: PASS.

- [ ] **Step 6: Run the full e2b_bench test suite to verify nothing else broke**

Run: `python -m pytest e2b_bench/tests/ -v`
Expected: PASS (all tests). If any unrelated test fails, investigate — it should not be caused by these changes. Do not modify unrelated tests.

- [ ] **Step 7: Commit**

```bash
git add e2b_bench/config.py e2b_bench/sandbox_manager.py e2b_bench/tests/test_sandbox_manager.py e2b_bench/tests/test_config.py
git commit -m "feat(e2b): bind each sandbox to a NUMA node via round-robin across numa_bind list"
```

---

### Task 4: Document the multi-node form in `bench.yaml`

**Files:**
- Modify: `config/e2b/bench.yaml:26` (the `numa_bind` comment)

- [ ] **Step 1: Update the comment**

Find the line in `config/e2b/bench.yaml` (around line 26):

```yaml
  numa_bind: 2  # NUMA node to bind sandbox (null/omit = no binding)
```

Replace with:

```yaml
  # NUMA node(s) to bind sandboxes to.
  # Single node: numa_bind: 2
  # Multi node (round-robin across sandboxes): numa_bind: [2, 3]
  # null/omit: no binding
  numa_bind: 2
```

- [ ] **Step 2: Commit**

```bash
git add config/e2b/bench.yaml
git commit -m "docs(e2b): document multi-node numa_bind form in bench.yaml"
```

---

### Task 5: Pre-commit check and final verification

**Files:** None (verification only).

- [ ] **Step 1: Run pre-commit on all changed files**

Run: `pre-commit run --files e2b_bench/config.py e2b_bench/sandbox_manager.py e2b_bench/tests/test_config.py e2b_bench/tests/test_sandbox_manager.py config/e2b/bench.yaml`
Expected: All hooks pass. If a hook reformats code, re-stage and re-run until clean.

- [ ] **Step 2: Run the full e2b_bench test suite one final time**

Run: `python -m pytest e2b_bench/tests/ -v`
Expected: PASS (all tests).

- [ ] **Step 3: Verify no unrelated files are staged**

Run: `git status`
Expected: Only the files touched by this plan (plus the spec and plan docs) are staged/committed. The unrelated modified/untracked files present at session start (`dockerfile_build/build_e2b.py`, `vue_*_probe.sh`, other `docs/superpowers/...` files, `llm_replay/sessions/...`) must NOT be included.

- [ ] **Step 4: Verify commit messages have no Claude attribution**

Run: `git log --format="%H %s%n%b" -5`
Expected: No `Co-Authored-By`, no `Generated with Claude Code`, no mention of Claude in any commit message from this plan.

---

## Self-Review

**1. Spec coverage:**
- Config schema (int|list|null, default 2): Task 2 Step 1 ✅
- Normalization helper: Task 1 ✅
- Distribution helper `numa_node_for_index`: Task 1 ✅
- `_from_dict` normalization: Task 2 Step 2 ✅
- `from_args` normalization: Task 2 Step 3 ✅
- `merge_with_args` (no change needed): Task 2 Step 4 ✅
- `_create_single` per-sandbox lookup: Task 3 Step 3 ✅
- Round-robin remainder to earlier node (21 on [2,3] → 11/10): Task 1 (`test_21_sandboxes_two_nodes_remainder_to_first`) + Task 3 (`test_two_nodes_21_sandboxes_remainder_to_first`) ✅
- Single-node `[2]` equivalent to old behavior: Task 3 (`test_single_node_list_equivalent_to_int`) ✅
- `null` → no envs: Task 3 (`test_none_numa_bind_no_envs`) ✅
- Backward compat of existing tests: Task 2 Step 5 (updated assertions) + Task 3 Step 5 (raw-int tolerance) ✅
- Batch scheduler (no change, inherits): noted in spec; covered by the fact that `_get_group_config` copies `numa_bind` as-is and `_create_single` reads `self.config.numa_bind` ✅
- `bench.yaml` documented example: Task 4 ✅
- Pre-commit: Task 5 ✅
- No Claude attribution: Task 5 Step 4 ✅
- English comments: every code block above uses English comments ✅

**2. Placeholder scan:** No TBD/TODO/"add error handling" present. Every step has complete code. ✅

**3. Type consistency:** `_normalize_numa_bind` and `numa_node_for_index` are defined in Task 1 and referenced in Tasks 2 and 3 with matching signatures. The import added in Task 3 (`from .config import Config, numa_node_for_index`) matches the function name defined in Task 1. ✅

One issue found and resolved inline during planning: the constructor-passthrough raw-int case (Task 3 Step 5) required `numa_node_for_index` to tolerate a raw int — this is handled by making the helper defensive and adding a test for it. ✅
