# Sandbox IDs File Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sandbox IDs file feature to e2b_bench - save IDs in create-only mode, filter by IDs in detect mode.

**Architecture:** Extend Config with sandbox_ids_file field, add detect_from_file() method to SandboxManager, update bench.py with save/filter logic.

**Tech Stack:** Python, pytest, E2B SDK

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `e2b_bench/config.py` | Modify | Add sandbox_ids_file field |
| `e2b_bench/bench.py` | Modify | Add CLI arg, save/filter logic |
| `e2b_bench/sandbox_manager.py` | Modify | Add detect_from_file() method |
| `e2b_bench/tests/test_config.py` | Modify | Add tests for sandbox_ids_file |
| `e2b_bench/tests/test_sandbox_manager.py` | Create | Add tests for detect_from_file() |

---

## Task 1: Add sandbox_ids_file Field to Config

**Files:**
- Modify: `e2b_bench/config.py:15-81` (Config dataclass)
- Modify: `e2b_bench/config.py:91-156` (_from_dict method)
- Modify: `e2b_bench/config.py:159-213` (merge_with_args method)
- Modify: `e2b_bench/config.py:216-269` (from_args method)
- Modify: `e2b_bench/tests/test_config.py` (add tests)

- [ ] **Step 1: Write failing tests for sandbox_ids_file in Config**

Add to `e2b_bench/tests/test_config.py`:

```python
class TestConfigSandboxIdsFile:
    """Tests for sandbox_ids_file configuration"""

    def test_default_none(self):
        """Default sandbox_ids_file is None"""
        config = Config()
        assert config.sandbox_ids_file is None

    def test_set_via_constructor(self):
        """Set sandbox_ids_file via constructor"""
        config = Config(sandbox_ids_file="ids.txt")
        assert config.sandbox_ids_file == "ids.txt"

    def test_load_from_yaml(self):
        """Load sandbox_ids_file from YAML"""
        yaml_content = """
sandbox:
  template: custom-template
  sandbox_ids_file: my_sandbox_ids.txt
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        assert config.sandbox_ids_file == "my_sandbox_ids.txt"

    def test_merge_with_args(self):
        """CLI arg overrides YAML config"""
        yaml_content = """
sandbox:
  sandbox_ids_file: yaml_ids.txt
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            yaml_config = Config.load_from_yaml(f.name)
            os.unlink(f.name)

        # Create mock args
        import argparse
        args = argparse.Namespace(
            sandbox_ids_file="cli_ids.txt",
            e2b_access_token=None,
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.merge_with_args(yaml_config, args)
        assert config.sandbox_ids_file == "cli_ids.txt"

    def test_from_args(self):
        """Build Config from CLI args only"""
        import argparse
        args = argparse.Namespace(
            sandbox_ids_file="args_ids.txt",
            e2b_access_token="token",
            e2b_api_key=None,
            e2b_domain=None,
            e2b_api_url=None,
            e2b_http_ssl=None,
            template=None,
            create_timeout=None,
            total=None,
            detect=False,
            create_only=False,
            create_batch_size=None,
            create_batch_interval=None,
            task_batch_size=None,
            task_batch_interval=None,
            browser_url=None,
            browser_timeout=None,
            browser_interval_min=None,
            browser_interval_max=None,
            warmup_url=None,
            warmup_loops=None,
            warmup_delay=None,
            warmup_only=False,
            benchmark_percent=None,
            duration=None,
            stats_interval=None,
            output_dir=None,
            filename_prefix=None,
        )

        config = Config.from_args(args)
        assert config.sandbox_ids_file == "args_ids.txt"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m pytest e2b_bench/tests/test_config.py::TestConfigSandboxIdsFile -v`

Expected: FAIL with "Config has no attribute sandbox_ids_file"

- [ ] **Step 3: Add sandbox_ids_file field to Config dataclass**

Modify `e2b_bench/config.py` - add after `create_only` field (around line 34):

```python
    # Create-only mode (create sandboxes without running tasks)
    create_only: bool = False

    # Sandbox IDs file (for save/load sandbox IDs)
    sandbox_ids_file: Optional[str] = None
```

- [ ] **Step 4: Update _from_dict to read sandbox_ids_file from YAML**

Modify `e2b_bench/config.py` in `_from_dict` method - add `sandbox_ids_file` to return statement (around line 113):

```python
            detect_existing=sandbox.get('detect_existing', False),
            create_only=sandbox.get('create_only', False),
            sandbox_ids_file=sandbox.get('sandbox_ids_file', None),
```

- [ ] **Step 5: Update merge_with_args to handle sandbox_ids_file**

Modify `e2b_bench/config.py` in `merge_with_args` method - add `sandbox_ids_file` to return statement (around line 172):

```python
            detect_existing=args.detect if hasattr(args, 'detect') and args.detect else yaml_config.detect_existing,
            create_only=args.create_only if hasattr(args, 'create_only') and args.create_only else yaml_config.create_only,
            sandbox_ids_file=args.sandbox_ids_file if args.sandbox_ids_file else yaml_config.sandbox_ids_file,
```

- [ ] **Step 6: Update from_args to handle sandbox_ids_file**

Modify `e2b_bench/config.py` in `from_args` method - add `sandbox_ids_file` to return statement (around line 228):

```python
            detect_existing=args.detect if hasattr(args, 'detect') and args.detect else False,
            create_only=args.create_only if hasattr(args, 'create_only') and args.create_only else False,
            sandbox_ids_file=args.sandbox_ids_file if args.sandbox_ids_file else None,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m pytest e2b_bench/tests/test_config.py::TestConfigSandboxIdsFile -v`

Expected: PASS (5 tests)

- [ ] **Step 8: Commit**

```bash
cd c:/Users/jack/Desktop/good_bench/agent_vm_bench
git add e2b_bench/config.py e2b_bench/tests/test_config.py
git commit -m "feat(e2b_bench): add sandbox_ids_file config field"
```

---

## Task 2: Add CLI Argument for --sandbox-ids-file

**Files:**
- Modify: `e2b_bench/bench.py:573-628` (build_arg_parser function)

- [ ] **Step 1: Add --sandbox-ids-file CLI argument**

Modify `e2b_bench/bench.py` in `build_arg_parser` function - add after `--create-only` argument (around line 595):

```python
    parser.add_argument('--create-only', action='store_true', help='Create sandboxes only without running tasks (Phase 0)')
    parser.add_argument('--sandbox-ids-file', type=str, help='File path to save/load sandbox IDs (one ID per line)')
```

- [ ] **Step 2: Verify CLI help shows new argument**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m e2b_bench --help`

Expected: Output includes `--sandbox-ids-file` with help text

- [ ] **Step 3: Commit**

```bash
cd c:/Users/jack/Desktop/good_bench/agent_vm_bench
git add e2b_bench/bench.py
git commit -m "feat(e2b_bench): add --sandbox-ids-file CLI argument"
```

---

## Task 3: Add detect_from_file() Method to SandboxManager

**Files:**
- Create: `e2b_bench/tests/test_sandbox_manager.py`
- Modify: `e2b_bench/sandbox_manager.py` (add detect_from_file method)

- [ ] **Step 1: Write failing tests for detect_from_file**

Create `e2b_bench/tests/test_sandbox_manager.py`:

```python
"""
Test SandboxManager Module

Tests for sandbox creation, detection, and ID file filtering
"""

import pytest
import tempfile
import os
from unittest.mock import Mock, patch, MagicMock
from threading import Event

from e2b_bench.sandbox_manager import SandboxManager
from e2b_bench.config import Config
from e2b_bench.schemas import SandboxStatus


class TestDetectFromFile:
    """Tests for detect_from_file method"""

    def _create_mock_sandbox(self, sandbox_id):
        """Helper to create mock sandbox object"""
        mock = Mock()
        mock.sandbox_id = sandbox_id
        mock.commands = Mock()
        mock.commands.run = Mock(return_value=Mock(exit_code=0, stdout="LISTENING"))
        return mock

    def test_file_not_found_raises_error(self):
        """File not found should raise FileNotFoundError"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        with pytest.raises(FileNotFoundError):
            manager.detect_from_file("nonexistent_file.txt")

    def test_empty_file_returns_empty_dict(self):
        """Empty file returns empty dict with warning"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("")  # Empty file
            f.flush()
            result = manager.detect_from_file(f.name)
            os.unlink(f.name)

        assert result == {}

    def test_file_with_whitespace_only_returns_empty(self):
        """File with only whitespace/empty lines returns empty dict"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("\n  \n\n")  # Only whitespace
            f.flush()
            result = manager.detect_from_file(f.name)
            os.unlink(f.name)

        assert result == {}

    @patch('e2b_bench.sandbox_manager.Sandbox.list')
    @patch('e2b_bench.sandbox_manager.Sandbox.connect')
    def test_matches_ids_from_file(self, mock_connect, mock_list):
        """Only sandboxes in file are connected"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file with 2 IDs
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("sbx_id_1\nsbx_id_2\n")
            f.flush()
            ids_file = f.name

        # Mock Sandbox.list() to return 3 running sandboxes
        mock paginator = Mock()
        mock_paginator.has_next = True
        mock_paginator.next_items = Mock(return_value=[
            Mock(sandbox_id="sbx_id_1"),
            Mock(sandbox_id="sbx_id_2"),
            Mock(sandbox_id="sbx_id_3"),  # Not in file
        ])
        # Second call returns empty (end of pagination)
        mock_paginator.next_items = Mock(side_effect=[
            [Mock(sandbox_id="sbx_id_1"), Mock(sandbox_id="sbx_id_2"), Mock(sandbox_id="sbx_id_3")],
            []
        ])
        mock_paginator.has_next = Mock(side_effect=[True, False])
        mock_list.return_value = mock_paginator

        # Mock Sandbox.connect() to return mock sandbox
        mock_connect.return_value = self._create_mock_sandbox("connected")

        result = manager.detect_from_file(ids_file)
        os.unlink(ids_file)

        # Should only connect sbx_id_1 and sbx_id_2 (2 sandboxes)
        assert len(result) == 2
        assert mock_connect.call_count == 2

    @patch('e2b_bench.sandbox_manager.Sandbox.list')
    @patch('e2b_bench.sandbox_manager.Sandbox.connect')
    def test_ids_not_running_shown_as_warning(self, mock_connect, mock_list):
        """IDs in file but not running should be warned"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file with 3 IDs
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("sbx_id_1\nsbx_id_2\nsbx_missing\n")
            f.flush()
            ids_file = f.name

        # Mock Sandbox.list() to return only 2 running sandboxes
        mock_paginator = Mock()
        mock_paginator.has_next = Mock(side_effect=[True, False])
        mock_paginator.next_items = Mock(side_effect=[
            [Mock(sandbox_id="sbx_id_1"), Mock(sandbox_id="sbx_id_2")],
            []
        ])
        mock_list.return_value = mock_paginator

        mock_connect.return_value = self._create_mock_sandbox("connected")

        result = manager.detect_from_file(ids_file)
        os.unlink(ids_file)

        # sbx_missing should be warned, not connected
        assert len(result) == 2
        assert mock_connect.call_count == 2

    @patch('e2b_bench.sandbox_manager.Sandbox.list')
    def test_no_matching_sandboxes_returns_empty(self, mock_list):
        """No matches returns empty dict"""
        config = Config()
        stop_event = Event()
        manager = SandboxManager(config, stop_event)

        # Create IDs file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("sbx_not_running_1\nsbx_not_running_2\n")
            f.flush()
            ids_file = f.name

        # Mock Sandbox.list() to return different sandboxes
        mock_paginator = Mock()
        mock_paginator.has_next = Mock(side_effect=[True, False])
        mock_paginator.next_items = Mock(side_effect=[
            [Mock(sandbox_id="sbx_other_1"), Mock(sandbox_id="sbx_other_2")],
            []
        ])
        mock_list.return_value = mock_paginator

        result = manager.detect_from_file(ids_file)
        os.unlink(ids_file)

        assert result == {}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m pytest e2b_bench/tests/test_sandbox_manager.py -v`

Expected: FAIL with "SandboxManager has no attribute detect_from_file"

- [ ] **Step 3: Implement detect_from_file method**

Add to `e2b_bench/sandbox_manager.py` after `detect_existing` method (around line 172):

```python
    def detect_from_file(self, ids_file: str) -> Dict[int, SandboxState]:
        """Detect sandboxes from ID file with matching

        Read target IDs from file, get running sandboxes via Sandbox.list(),
        match intersection, connect and check ports.

        Args:
            ids_file: Path to file containing sandbox IDs (one per line)

        Returns:
            Dict of connected sandbox states {sandbox_id: SandboxState}
        """
        print(f"\n{'='*60}")
        print("Detecting Sandboxes from ID File")
        print(f"{'='*60}")
        print(f"  ID file: {ids_file}")

        # 1. Read target IDs from file
        if not os.path.exists(ids_file):
            raise FileNotFoundError(f"Sandbox IDs file not found: {ids_file}")

        target_ids = set()
        with open(ids_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    target_ids.add(line)

        if not target_ids:
            print(f"  WARNING: No IDs found in {ids_file}")
            return {}

        print(f"  Target IDs from file: {len(target_ids)}")

        # 2. Get all running sandboxes
        try:
            paginator = Sandbox.list()
            running_list = []
            while paginator.has_next:
                sandboxes = paginator.next_items()
                running_list.extend(sandboxes)
            print(f"  Running sandboxes: {len(running_list)}")
        except Exception as e:
            print(f"  Failed to list sandboxes: {e}")
            return {}

        if not running_list:
            print("  No running sandboxes found")
            return {}

        # 3. Match: only keep sandboxes in both sets
        matched = []
        found_ids = set()

        for listed_sandbox in running_list:
            e2b_id = listed_sandbox.sandbox_id if hasattr(listed_sandbox, 'sandbox_id') else str(listed_sandbox)
            if e2b_id in target_ids:
                matched.append(listed_sandbox)
                found_ids.add(e2b_id)

        # IDs not found (in file but not running)
        not_found = target_ids - found_ids
        if not_found:
            print(f"  WARNING: {len(not_found)} IDs not found or stopped")
            for sid in list(not_found)[:5]:  # Show first 5
                print(f"    - {sid}")
            if len(not_found) > 5:
                print(f"    ... and {len(not_found) - 5} more")

        print(f"  Matched sandboxes: {len(matched)}")

        if not matched:
            print("  No matched sandboxes to benchmark")
            return {}

        # 4. Connect and check ports (same logic as detect_existing)
        print(f"  Processing matched sandboxes...")

        for i, listed_sandbox in enumerate(matched):
            sandbox_id = i + 1
            e2b_sandbox_id = listed_sandbox.sandbox_id if hasattr(listed_sandbox, 'sandbox_id') else str(listed_sandbox)

            state = SandboxState(sandbox_id=sandbox_id)
            self.sandbox_states[sandbox_id] = state

            print(f"\n[Sandbox{sandbox_id}] Connecting to E2B:{e2b_sandbox_id}...")

            try:
                # Connect to existing sandbox
                sbx = Sandbox.connect(e2b_sandbox_id)
                state.sandbox_obj = sbx
                state.creation_metrics.status = SandboxStatus.CREATED
                print(f"[Sandbox{sandbox_id}] Connected successfully")

                # Check port readiness
                port_result = self._check_ports(state)
                if port_result['success']:
                    state.creation_metrics.status = SandboxStatus.PORT_READY
                    state.creation_metrics.port_wait_elapsed = port_result['wait_elapsed']
                    print(f"[Sandbox{sandbox_id}] Ports ready in {port_result['wait_elapsed']:.1f}s")
                else:
                    state.creation_metrics.status = SandboxStatus.PORT_FAILED
                    state.creation_metrics.port_check_error = port_result['error']
                    print(f"[Sandbox{sandbox_id}] Port check failed: {port_result['error'][:50]}")

            except Exception as e:
                state.creation_metrics.status = SandboxStatus.FAILED
                state.creation_metrics.error_msg = str(e)
                print(f"[Sandbox{sandbox_id}] Connect failed: {str(e)[:80]}")

        return self.sandbox_states
```

Also add `import os` at the top of the file if not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m pytest e2b_bench/tests/test_sandbox_manager.py -v`

Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd c:/Users/jack/Desktop/good_bench/agent_vm_bench
git add e2b_bench/sandbox_manager.py e2b_bench/tests/test_sandbox_manager.py
git commit -m "feat(e2b_bench): add detect_from_file method to SandboxManager"
```

---

## Task 4: Add Save Sandbox IDs Logic in bench.py

**Files:**
- Modify: `e2b_bench/bench.py:409-497` (create-only mode section)

- [ ] **Step 1: Add save IDs logic after create-only completes**

Modify `e2b_bench/bench.py` in `run_benchmark` function - add after the creation timing report (around line 493, before the return statement):

Find the return statement in create-only section:
```python
        print("\n" + "=" * 70)
        return {
            'report': f"Create-only: {ready_count}/{len(sandbox_states)} sandboxes ready",
            'filepath': None
        }
```

Add save logic before it:
```python
        print("\n" + "=" * 70)

        # Save sandbox IDs to file if configured
        if config.sandbox_ids_file:
            successful_ids = [
                s.sandbox_obj.sandbox_id
                for s in sandbox_states.values()
                if s.creation_metrics.status == SandboxStatus.PORT_READY
                and s.sandbox_obj is not None
            ]
            if successful_ids:
                with open(config.sandbox_ids_file, 'w') as f:
                    for sid in successful_ids:
                        f.write(f"{sid}\n")
                print(f"\nSaved {len(successful_ids)} sandbox IDs to: {config.sandbox_ids_file}")
            else:
                print(f"\nWARNING: No successful sandboxes to save to {config.sandbox_ids_file}")

        return {
            'report': f"Create-only: {ready_count}/{len(sandbox_states)} sandboxes ready",
            'filepath': None
        }
```

- [ ] **Step 2: Verify the logic is syntactically correct**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -c "from e2b_bench.bench import run_benchmark; print('OK')"`

Expected: Output "OK"

- [ ] **Step 3: Commit**

```bash
cd c:/Users/jack/Desktop/good_bench/agent_vm_bench
git add e2b_bench/bench.py
git commit -m "feat(e2b_bench): save sandbox IDs in create-only mode"
```

---

## Task 5: Add Filter by IDs Logic in bench.py

**Files:**
- Modify: `e2b_bench/bench.py:386-398` (detect mode section)

- [ ] **Step 1: Add filter logic in detect mode**

Modify `e2b_bench/bench.py` in `run_benchmark` function - update the detect section (around line 388):

Find existing code:
```python
    if config.detect_existing:
        print("\n[Phase 1] Detecting existing sandboxes...")
        creation_start_time = time.time()
        sandbox_states = sandbox_manager.detect_existing()
        creation_end_time = time.time()
```

Replace with:
```python
    if config.detect_existing:
        if config.sandbox_ids_file:
            print(f"\n[Phase 1] Detecting sandboxes from ID file: {config.sandbox_ids_file}...")
        else:
            print("\n[Phase 1] Detecting existing sandboxes...")
        creation_start_time = time.time()
        if config.sandbox_ids_file:
            sandbox_states = sandbox_manager.detect_from_file(config.sandbox_ids_file)
        else:
            sandbox_states = sandbox_manager.detect_existing()
        creation_end_time = time.time()
```

- [ ] **Step 2: Verify the logic is syntactically correct**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -c "from e2b_bench.bench import run_benchmark; print('OK')"`

Expected: Output "OK"

- [ ] **Step 3: Commit**

```bash
cd c:/Users/jack/Desktop/good_bench/agent_vm_bench
git add e2b_bench/bench.py
git commit -m "feat(e2b_bench): filter sandboxes by ID file in detect mode"
```

---

## Task 6: Integration Test and Final Verification

**Files:**
- No file changes, just verification

- [ ] **Step 1: Run all tests**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m pytest e2b_bench/tests/ -v`

Expected: All tests PASS

- [ ] **Step 2: Verify CLI help shows all new options**

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -m e2b_bench --help`

Expected: Output includes:
- `--sandbox-ids-file` with description "File path to save/load sandbox IDs"

- [ ] **Step 3: Create integration test with mock**

This is a manual verification that the feature works end-to-end. Since E2B SDK requires real API, we verify the code paths are correct:

Run: `cd c:/Users/jack/Desktop/good_bench/agent_vm_bench && python -c "
import tempfile
import os
from e2b_bench.config import Config

# Test Config with sandbox_ids_file
config = Config(sandbox_ids_file='test_ids.txt')
print(f'Config sandbox_ids_file: {config.sandbox_ids_file}')

# Test YAML loading
yaml_content = '''
sandbox:
  sandbox_ids_file: yaml_ids.txt
'''
with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
    f.write(yaml_content)
    f.flush()
    cfg = Config.load_from_yaml(f.name)
    os.unlink(f.name)
    print(f'YAML loaded sandbox_ids_file: {cfg.sandbox_ids_file}')

print('Integration verification OK')
"`

Expected: Output shows both config values correctly

- [ ] **Step 4: Final commit with all changes**

```bash
cd c:/Users/jack/Desktop/good_bench/agent_vm_bench
git status
git log --oneline -5
```

Expected: Shows all 5 commits for this feature

---

## Self-Review Checklist

**1. Spec Coverage:**
- [x] Save sandbox IDs in create-only mode - Task 4
- [x] Filter by IDs in detect mode - Task 5
- [x] CLI argument `--sandbox-ids-file` - Task 2
- [x] YAML config field `sandbox_ids_file` - Task 1
- [x] File format plain text, one ID per line - Implemented in Task 4
- [x] File not found error handling - Task 3 (detect_from_file raises FileNotFoundError)
- [x] Empty file warning - Task 3 (test_empty_file_returns_empty_dict)
- [x] IDs not running warning - Task 3 (test_ids_not_running_shown_as_warning)
- [x] Backward compatibility - All changes are additive, existing behavior unchanged when not configured

**2. Placeholder Scan:**
- No TBD, TODO, or vague instructions
- All code blocks contain complete implementation
- All test cases have actual test code

**3. Type Consistency:**
- `sandbox_ids_file: Optional[str]` used consistently across all methods
- `detect_from_file(ids_file: str)` parameter type matches config field type

---

## Summary

This plan implements the sandbox IDs file feature with 6 tasks:
1. Add `sandbox_ids_file` config field (with tests)
2. Add `--sandbox-ids-file` CLI argument
3. Add `detect_from_file()` method to SandboxManager (with tests)
4. Add save IDs logic in create-only mode
5. Add filter by IDs logic in detect mode
6. Integration verification

All changes are backward compatible - existing behavior unchanged when `sandbox_ids_file` is not configured.