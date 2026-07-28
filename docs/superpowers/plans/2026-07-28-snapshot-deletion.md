# Snapshot Deletion Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a batch snapshot deletion tool (`e2b_bench/snap/delete.py`) that cleans up persistent E2B snapshots, plus a shared terminal summary printer used by all three `snap.*` scripts.

**Architecture:** New `snap.delete` module mirrors `create`/`restore` (CLI + concurrent batched execution + Excel report). Deletion uses `Sandbox.delete_snapshot(snapshot_id)` — a staticmethod needing only the ID + env vars. The `--all` mode lists server-wide snapshots via `Sandbox._cls_list_snapshots(sandbox_id=None)`. A new `common.print_summary` helper renders the same stats dict that `write_excel_report` consumes as a fixed-width terminal table; `create.py`/`restore.py` get a small refactor to compute `summary_data` once and pass it to both Excel and terminal output.

**Tech Stack:** Python 3, `e2b` SDK, `openpyxl`, `concurrent.futures`, `argparse`, `pytest` + `pytest-mock`. Ruff for formatting (project uses `ruff-format`).

**Spec:** `docs/superpowers/specs/2026-07-28-snapshot-deletion-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `e2b_bench/snap/common.py` | Shared utils. ADD `print_summary`. | Modify |
| `e2b_bench/snap/create.py` | Batch snapshot creation. Refactor summary construction; call `print_summary`. | Modify |
| `e2b_bench/snap/restore.py` | Batch restore. Refactor summary construction; call `print_summary`. | Modify |
| `e2b_bench/snap/delete.py` | NEW: batch snapshot deletion (JSON + `--all` modes). | Create |
| `e2b_bench/snap/__init__.py` | Package docstring; mention `delete`. | Modify |
| `e2b_bench/snap/README.md` | Document `snap.delete` + terminal summary. | Modify |
| `e2b_bench/tests/test_snap_common.py` | NEW: unit tests for `print_summary`. | Create |
| `e2b_bench/tests/test_snap_delete.py` | NEW: unit tests for delete logic (mocked SDK). | Create |

Tests are pure-function / mocked — no live E2B server required for the test suite.

---

### Task 1: Add `print_summary` helper to `common.py` (TDD)

**Files:**
- Create: `e2b_bench/tests/test_snap_common.py`
- Modify: `e2b_bench/snap/common.py` (append `print_summary` after `write_excel_report`)

- [ ] **Step 1: Write the failing test**

Create `e2b_bench/tests/test_snap_common.py`:

```python
"""Unit tests for e2b_bench.snap.common.print_summary."""

from e2b_bench.snap.common import print_summary


class TestPrintSummary:
    """Tests for print_summary terminal output."""

    def test_prints_title_and_borders(self, capsys):
        summary = {
            "restore_sandbox_s": {"count": 5, "success_rate": 1.0, "avg": 2.5},
        }
        print_summary(summary, "Restore Summary")
        out = capsys.readouterr().out
        assert "Restore Summary" in out
        assert out.startswith("=")
        assert out.rstrip().endswith("=")

    def test_prints_metric_rows_and_series_columns(self, capsys):
        summary = {
            "create_sandbox_s": {"count": 10, "avg": 12.5},
            "create_snapshot_s": {"count": 10, "avg": 3.2},
        }
        print_summary(summary, "Title")
        out = capsys.readouterr().out
        # metric column header present
        assert "metric" in out
        # both series names appear as column headers
        assert "create_sandbox_s" in out
        assert "create_snapshot_s" in out
        # stat rows present
        assert "count" in out
        assert "avg" in out

    def test_empty_summary_still_prints_title(self, capsys):
        print_summary({}, "Empty")
        out = capsys.readouterr().out
        assert "Empty" in out

    def test_missing_stat_key_shows_blank_not_crash(self, capsys):
        # series with only one stat key — other rows must show blank, not KeyError
        summary = {"total_s": {"count": 3}}
        print_summary(summary, "Title")
        out = capsys.readouterr().out
        assert "count" in out
        assert "total_s" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest e2b_bench/tests/test_snap_common.py -v`
Expected: FAIL with `ImportError: cannot import name 'print_summary' from 'e2b_bench.snap.common'`

- [ ] **Step 3: Write minimal implementation**

Append to `e2b_bench/snap/common.py` (after `write_excel_report`):

```python
def print_summary(summary_data: Dict[str, Dict[str, Any]], title: str) -> None:
    """Print a paste-friendly stats table to the terminal.

    Renders the same summary_data that write_excel_report consumes
    as a fixed-width table (stdlib only). Rows are stat keys; columns
    are data series — identical layout to the Excel Summary sheet so
    on-screen and spreadsheet views match.

    Args:
        summary_data: {series_name: {stat_key: value, ...}, ...}.
        title: Header line printed above the table.
    """
    stat_keys = ["count", "success_rate", "avg", "min", "max", "p50", "p90", "p99", "std"]
    series_names = list(summary_data.keys())

    # Column widths: metric name column + one per series
    metric_col_w = max(len("metric"), max((len(k) for k in stat_keys), default=0))
    series_col_w = {name: max(len(name), 12) for name in series_names}

    border = "=" * (metric_col_w + sum(series_col_w.values()) + len(series_col_w) * 3 + 2)
    print(border)
    print(f"{title.center(len(border))}")
    print(border)

    # Header row
    header = f"{'metric':<{metric_col_w}}  "
    header += "  ".join(f"{name:>{series_col_w[name]}" for name in series_names)
    print(header)
    print("-" * len(border))

    # Data rows
    for key in stat_keys:
        row = f"{key:<{metric_col_w}}  "
        cells = []
        for name in series_names:
            value = summary_data[name].get(key, "")
            cells.append(f"{_fmt(value):>{series_col_w[name]}}" if value != "" else " " * series_col_w[name])
        row += "  ".join(cells)
        print(row)

    print(border)


def _fmt(value: Any) -> str:
    """Format a stat value for terminal display."""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest e2b_bench/tests/test_snap_common.py -v`
Expected: 4 PASS

- [ ] **Step 5: Run ruff format**

Run: `ruff format e2b_bench/snap/common.py e2b_bench/tests/test_snap_common.py && ruff check --fix e2b_bench/snap/common.py e2b_bench/tests/test_snap_common.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add e2b_bench/snap/common.py e2b_bench/tests/test_snap_common.py
git commit -m "feat(e2b_bench/snap): add print_summary terminal helper"
```

---

### Task 2: Wire `print_summary` into `create.py` + refactor summary construction

**Files:**
- Modify: `e2b_bench/snap/create.py` (function `build_report` → split into `compute_summary` + `build_report`; `main` calls both + `print_summary`)

- [ ] **Step 1: Refactor `build_report` to extract `compute_summary`**

In `e2b_bench/snap/create.py`, replace the existing `build_report` function (lines ~248-299) with two functions. First the summary computation:

```python
def compute_summary(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Compute summary statistics dict from creation results.

    Args:
        results: List of result dicts from create_single_sandbox.

    Returns:
        {series_name: {stat_key: value, ...}} dict, same shape used by
        write_excel_report's Summary sheet and print_summary.
    """
    create_times = [r["create_elapsed_s"] for r in results if r["create_elapsed_s"] > 0]
    snapshot_times = [r["snapshot_elapsed_s"] for r in results if r["status"] == "success"]
    total_times = [r["total_elapsed_s"] for r in results if r["status"] == "success"]

    success_count = sum(1 for r in results if r["status"] == "success")
    total_count = len(results)

    create_stats = compute_stats(create_times)
    create_stats["success_rate"] = (
        round(sum(1 for r in results if r["create_elapsed_s"] > 0) / total_count, 4) if total_count > 0 else 0.0
    )

    snapshot_stats = compute_stats(snapshot_times)
    snapshot_stats["success_rate"] = round(success_count / total_count, 4) if total_count > 0 else 0.0

    total_stats = compute_stats(total_times)
    total_stats["success_rate"] = round(success_count / total_count, 4) if total_count > 0 else 0.0

    return {
        "create_sandbox_s": create_stats,
        "create_snapshot_s": snapshot_stats,
        "total_s": total_stats,
    }
```

Then update `build_report` to consume it:

```python
def build_report(results: List[Dict[str, Any]], template: str, output_path: str) -> None:
    """Build Excel report from results.

    Args:
        results: List of result dicts from create_single_sandbox.
        template: Template name.
        output_path: Path to write the Excel file.
    """
    raw_data = results
    summary_data = compute_summary(results)

    # Sheet 3: Snapshots registry (only successful ones)
    snapshots_data = []
    for r in results:
        if r["status"] == "success":
            snapshots_data.append(
                {
                    "snapshot_id": r["snapshot_id"],
                    "sandbox_id": r["sandbox_id"],
                    "template": template,
                    "create_elapsed_s": r["create_elapsed_s"],
                    "snapshot_elapsed_s": r["snapshot_elapsed_s"],
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    write_excel_report(raw_data, summary_data, snapshots_data, output_path, "Batch Snapshot Creation Report")
```

- [ ] **Step 2: Update `main` to call `print_summary`**

In `e2b_bench/snap/create.py`, update the import line (line ~27) and the saving-results block (lines ~402-405).

Import line change:

```python
from .common import compute_stats, load_env, print_summary, write_excel_report
```

Replace the saving-results block in `main`:

```python
    # Save snapshot IDs and report
    print(f"\n[5/5] Saving results...")
    save_snapshot_json(results, args.template, args.output_json)
    build_report(results, args.template, args.output_xlsx)

    summary_data = compute_summary(results)
    print()
    print_summary(summary_data, "Batch Snapshot Creation Summary")
```

- [ ] **Step 3: Verify the script still imports and runs `--help`**

Run: `python -m e2b_bench.snap.create --help`
Expected: prints usage, no import error.

- [ ] **Step 4: Run existing tests + ruff**

Run: `python -m pytest e2b_bench/tests/ -k "snap" -v && ruff format e2b_bench/snap/create.py && ruff check e2b_bench/snap/create.py`
Expected: tests pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add e2b_bench/snap/create.py
git commit -m "feat(e2b_bench/snap): print terminal summary in create script"
```

---

### Task 3: Wire `print_summary` into `restore.py` + refactor summary construction

**Files:**
- Modify: `e2b_bench/snap/restore.py` (function `build_report` → split into `compute_summary` + `build_report`; `main` calls both + `print_summary`)

- [ ] **Step 1: Refactor `build_report` to extract `compute_summary`**

In `e2b_bench/snap/restore.py`, replace the existing `build_report` function (lines ~212-248) with two functions:

```python
def compute_summary(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Compute summary statistics dict from restore results.

    Args:
        results: List of result dicts from restore_single_sandbox.

    Returns:
        {series_name: {stat_key: value, ...}} dict.
    """
    restore_times = [r["restore_elapsed_s"] for r in results if r["status"] == "success"]
    success_count = sum(1 for r in results if r["status"] == "success")
    total_count = len(results)

    restore_stats = compute_stats(restore_times)
    restore_stats["success_rate"] = round(success_count / total_count, 4) if total_count > 0 else 0.0

    return {
        "restore_sandbox_s": restore_stats,
        "total_s": restore_stats,
    }


def build_report(results: List[Dict[str, Any]], output_path: str) -> None:
    """Build Excel report from restore results.

    Args:
        results: List of result dicts from restore_single_sandbox.
        output_path: Path to write the Excel file.
    """
    raw_data = results
    summary_data = compute_summary(results)

    # Sheet 3: Snapshots (source snapshot -> restored sandbox mapping)
    snapshots_data = []
    for r in results:
        if r["status"] == "success":
            snapshots_data.append(
                {
                    "snapshot_id": r["snapshot_id"],
                    "sandbox_id": r["sandbox_id"],
                    "restore_elapsed_s": r["restore_elapsed_s"],
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    write_excel_report(raw_data, summary_data, snapshots_data, output_path, "Batch Sandbox Restore Report")
```

- [ ] **Step 2: Update `main` to call `print_summary`**

In `e2b_bench/snap/restore.py`, update the import line (line ~27):

```python
from .common import compute_stats, load_env, print_summary, write_excel_report
```

Replace the `build_report(results, args.output_xlsx)` call at the end of `main` (line ~366) with:

```python
    # Build Excel report
    build_report(results, args.output_xlsx)

    summary_data = compute_summary(results)
    print()
    print_summary(summary_data, "Batch Sandbox Restore Summary")
```

- [ ] **Step 3: Verify `--help` runs**

Run: `python -m e2b_bench.snap.restore --help`
Expected: prints usage, no import error.

- [ ] **Step 4: Run tests + ruff**

Run: `python -m pytest e2b_bench/tests/ -k "snap" -v && ruff format e2b_bench/snap/restore.py && ruff check e2b_bench/snap/restore.py`
Expected: pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add e2b_bench/snap/restore.py
git commit -m "feat(e2b_bench/snap): print terminal summary in restore script"
```

---

### Task 4: Implement `delete.py` core logic (TDD, mocked SDK)

**Files:**
- Create: `e2b_bench/tests/test_snap_delete.py`
- Create: `e2b_bench/snap/delete.py`

This task builds the pure/logic layer of `delete.py` with unit tests against a mocked `Sandbox.delete_snapshot` and `_cls_list_snapshots`. CLI `main` wiring comes in Task 5.

- [ ] **Step 1: Write the failing tests**

Create `e2b_bench/tests/test_snap_delete.py`:

```python
"""Unit tests for e2b_bench.snap.delete logic (SDK mocked)."""

from unittest.mock import patch

from e2b_bench.snap import delete


class TestFilterActiveSnapshots:
    """Tests for load_active_snapshot_ids_from_json."""

    def test_skips_deleted_entries(self):
        data = {
            "template": "uu",
            "count": 3,
            "snapshots": [
                {"snapshot_id": "snap1", "status": "success"},
                {"snapshot_id": "snap2", "status": "deleted"},
                {"snapshot_id": "snap3", "status": "success"},
            ],
        }
        ids = delete.load_active_snapshot_ids_from_json(data)
        assert ids == ["snap1", "snap3"]

    def test_skips_entries_without_snapshot_id(self):
        data = {"snapshots": [{"status": "success"}, {"snapshot_id": "snap1", "status": "success"}]}
        ids = delete.load_active_snapshot_ids_from_json(data)
        assert ids == ["snap1"]

    def test_count_limit_applies(self):
        data = {"snapshots": [{"snapshot_id": f"s{i}", "status": "success"} for i in range(5)]}
        ids = delete.load_active_snapshot_ids_from_json(data, count=2)
        assert ids == ["s0", "s1"]

    def test_empty_returns_empty(self):
        assert delete.load_active_snapshot_ids_from_json({"snapshots": []}) == []


class TestDeleteSingle:
    """Tests for delete_single_snapshot."""

    def _fake_deleted_true(self, snapshot_id, **opts):
        return True

    def _fake_deleted_false(self, snapshot_id, **opts):
        return False

    def _fake_raises(self, snapshot_id, **opts):
        raise RuntimeError("boom")

    def test_success_status(self):
        with patch.object(delete, "Sandbox") as sandbox_cls:
            sandbox_cls.delete_snapshot = self._fake_deleted_true
            r = delete.delete_single_snapshot("snap1", index=1)
        assert r["status"] == "success"
        assert r["snapshot_id"] == "snap1"
        assert r["error"] == ""
        assert r["delete_elapsed_s"] >= 0

    def test_not_found_status(self):
        with patch.object(delete, "Sandbox") as sandbox_cls:
            sandbox_cls.delete_snapshot = self._fake_deleted_false
            r = delete.delete_single_snapshot("snap1", index=1)
        assert r["status"] == "not_found"
        assert r["error"] == ""

    def test_failed_status_captures_error(self):
        with patch.object(delete, "Sandbox") as sandbox_cls:
            sandbox_cls.delete_snapshot = self._fake_raises
            r = delete.delete_single_snapshot("snap1", index=1)
        assert r["status"] == "failed"
        assert "boom" in r["error"]


class TestUpdateJsonLedger:
    """Tests for update_json_ledger."""

    def test_marks_deleted_entries(self):
        data = {
            "snapshots": [
                {"snapshot_id": "snap1", "status": "success"},
                {"snapshot_id": "snap2", "status": "success"},
            ]
        }
        results = [
            {"snapshot_id": "snap1", "status": "success"},
            {"snapshot_id": "snap2", "status": "failed", "error": "x"},
        ]
        updated = delete.update_json_ledger(data, results)
        assert updated["snapshots"][0]["status"] == "deleted"
        assert "deleted_at" in updated["snapshots"][0]
        assert updated["snapshots"][1]["status"] == "delete_failed"
        assert "deleted_at" in updated["snapshots"][1]

    def test_leaves_unmatched_entries_alone(self):
        data = {"snapshots": [{"snapshot_id": "snap9", "status": "success"}]}
        results = [{"snapshot_id": "snap1", "status": "success"}]
        updated = delete.update_json_ledger(data, results)
        assert updated["snapshots"][0]["status"] == "success"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest e2b_bench/tests/test_snap_delete.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'e2b_bench.snap.delete'`

- [ ] **Step 3: Write minimal implementation**

Create `e2b_bench/snap/delete.py` (logic functions only — `main` added in Task 5):

```python
#!/usr/bin/env python3
"""
Batch Snapshot Deletion Script

Deletes E2B snapshots that survive sandbox deletion. Two modes:
  --input-json  (default): delete snapshots recorded in a JSON ledger.
  --all:                   list + delete every snapshot on the server.

Usage:
    python3 -m e2b_bench.snap.delete -i snapshots.json
    python3 -m e2b_bench.snap.delete --all
    python3 -m e2b_bench.snap.delete --all --yes
"""

import time
from typing import Any, Dict, List, Optional

from e2b import Sandbox


def load_active_snapshot_ids_from_json(data: Dict[str, Any], count: Optional[int] = None) -> List[str]:
    """Collect deletable snapshot IDs from a snapshots.json ledger.

    Skips entries with no snapshot_id and entries already marked deleted.

    Args:
        data: Parsed snapshots.json dict.
        count: If set, return only the first N IDs.

    Returns:
        Ordered list of snapshot IDs to delete.
    """
    ids = []
    for entry in data.get("snapshots", []):
        if entry.get("status") == "deleted":
            continue
        snap_id = entry.get("snapshot_id")
        if snap_id:
            ids.append(snap_id)
    if count is not None:
        ids = ids[:count]
    return ids


def delete_single_snapshot(snapshot_id: str, index: int, timeout: int = 86400) -> Dict[str, Any]:
    """Delete one snapshot via the E2B SDK staticmethod.

    Args:
        snapshot_id: Snapshot ID to delete.
        index: 1-based index for logging.
        timeout: API call timeout (passed through SDK opts).

    Returns:
        Dict with: index, snapshot_id, status, delete_elapsed_s, error.
        status is 'success' (deleted), 'not_found' (SDK returned False),
        or 'failed' (exception).
    """
    result = {
        "index": index,
        "snapshot_id": snapshot_id,
        "delete_elapsed_s": 0.0,
        "status": "failed",
        "error": "",
    }
    try:
        t_start = time.time()
        deleted = Sandbox.delete_snapshot(snapshot_id, request_timeout=timeout)
        elapsed = time.time() - t_start
        result["delete_elapsed_s"] = round(elapsed, 4)
        if deleted:
            result["status"] = "success"
        else:
            result["status"] = "not_found"
        print(f"  [{index}] {snapshot_id[:16]}... -> {result['status']} ({elapsed:.2f}s)")
    except Exception as e:
        result["delete_elapsed_s"] = round(time.time() - t_start, 4)
        result["error"] = str(e)
        print(f"  [{index}] {snapshot_id[:16]}... -> FAILED: {str(e)[:80]}")
    return result


def update_json_ledger(data: Dict[str, Any], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Mark ledger entries deleted/failed based on deletion results.

    Args:
        data: Original parsed snapshots.json dict.
        results: Per-snapshot result dicts from delete_single_snapshot.

    Returns:
        Mutated copy of data with status + deleted_at set per matched entry.
    """
    from datetime import datetime

    now = datetime.now().isoformat()
    status_map = {r["snapshot_id"]: r["status"] for r in results}
    for entry in data.get("snapshots", []):
        snap_id = entry.get("snapshot_id")
        if snap_id in status_map:
            entry["status"] = "deleted" if status_map[snap_id] == "success" else "delete_failed"
            entry["deleted_at"] = now
    return data


def list_all_snapshot_ids(timeout: int = 86400) -> List[str]:
    """List every snapshot on the E2B server (global, paginated).

    Uses the SDK's static list path with sandbox_id=None.

    Args:
        timeout: Per-request timeout passed through SDK opts.

    Returns:
        Ordered list of all snapshot IDs on the server.
    """
    ids: List[str] = []
    paginator = Sandbox._cls_list_snapshots(sandbox_id=None, request_timeout=timeout)
    # Fallback if a future SDK removes _cls_list_snapshots: construct paginator directly.
    while paginator.has_next:
        page = paginator.next_items()
        for snap in page:
            ids.append(snap.snapshot_id)
    return ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest e2b_bench/tests/test_snap_delete.py -v`
Expected: 7 PASS

- [ ] **Step 5: Run ruff**

Run: `ruff format e2b_bench/snap/delete.py e2b_bench/tests/test_snap_delete.py && ruff check --fix e2b_bench/snap/delete.py e2b_bench/tests/test_snap_delete.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add e2b_bench/snap/delete.py e2b_bench/tests/test_snap_delete.py
git commit -m "feat(e2b_bench/snap): add delete logic (json filter, single delete, ledger update, list-all)"
```

---

### Task 5: Add `delete.py` batch execution, CLI, and `main`

**Files:**
- Modify: `e2b_bench/snap/delete.py` (add `delete_batch`, `_delete_concurrent`, `confirm`, `parse_args`, `main`)

- [ ] **Step 1: Add batch deletion + concurrency helpers**

Append to `e2b_bench/snap/delete.py` (after `list_all_snapshot_ids`):

```python
from concurrent.futures import ThreadPoolExecutor, as_completed


def _delete_concurrent(snapshot_ids: List[str], timeout: int, start_index: int) -> List[Dict[str, Any]]:
    """Concurrently delete a batch of snapshots.

    Args:
        snapshot_ids: Snapshot IDs in this batch.
        timeout: Per-call timeout.
        start_index: 0-based start for 1-based numbering.

    Returns:
        Result dicts ordered by index.
    """
    results: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(snapshot_ids))) as executor:
        futures = {}
        for i, snap_id in enumerate(snapshot_ids):
            future = executor.submit(delete_single_snapshot, snap_id, start_index + i + 1, timeout)
            futures[future] = start_index + i + 1
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = {
                    "index": idx,
                    "snapshot_id": "",
                    "delete_elapsed_s": 0.0,
                    "status": "failed",
                    "error": str(e),
                }
    return [results[i] for i in sorted(results.keys())]


def delete_batch(
    snapshot_ids: List[str],
    timeout: int,
    batch_size: Optional[int] = None,
    batch_interval: int = 3,
) -> List[Dict[str, Any]]:
    """Delete N snapshots, optionally in batches.

    Args:
        snapshot_ids: Snapshot IDs to delete.
        timeout: Per-call timeout.
        batch_size: If set, deletions per batch.
        batch_interval: Seconds between batches.

    Returns:
        List of result dicts ordered by index.
    """
    count = len(snapshot_ids)
    results: List[Dict[str, Any]] = []

    if batch_size and batch_size > 0:
        num_batches = (count + batch_size - 1) // batch_size
        print(f"\n  Batched delete: {count} snapshots in {num_batches} batches of {batch_size}")
        for batch_id in range(num_batches):
            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, count)
            print(f"\n  [Batch {batch_id + 1}/{num_batches}] Snapshots {start_idx + 1}-{end_idx}")
            results.extend(_delete_concurrent(snapshot_ids[start_idx:end_idx], timeout, start_idx))
            if batch_id < num_batches - 1:
                print(f"  Waiting {batch_interval}s before next batch...")
                time.sleep(batch_interval)
    else:
        print(f"\n  Full concurrent delete: {count} snapshots")
        results = _delete_concurrent(snapshot_ids, timeout, 0)
    return results


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    """Prompt y/N unless assume_yes is set.

    Args:
        prompt: Question to print.
        assume_yes: If True, skip prompt and return True.

    Returns:
        True to proceed, False to abort.
    """
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} (y/N): ").strip().lower()
    except EOFError:
        return False
    return answer == "y"
```

- [ ] **Step 2: Add `compute_summary` + `build_report`**

Append to `e2b_bench/snap/delete.py`:

```python
import argparse
import json
import os
import sys

from datetime import datetime

from .common import compute_stats, load_env, print_summary, write_excel_report


def compute_summary(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Compute summary statistics dict from deletion results.

    success_rate counts success + not_found as non-failures (deleting an
    already-gone snapshot is a no-op success).
    """
    delete_times = [r["delete_elapsed_s"] for r in results if r["status"] in ("success", "not_found")]
    total_count = len(results)
    ok_count = sum(1 for r in results if r["status"] in ("success", "not_found"))

    stats = compute_stats(delete_times)
    stats["success_rate"] = round(ok_count / total_count, 4) if total_count > 0 else 0.0
    return {"delete_snapshot_s": stats}


def build_report(results: List[Dict[str, Any]], output_path: str) -> None:
    """Build Excel report from deletion results."""
    summary_data = compute_summary(results)
    snapshots_data = [
        {
            "snapshot_id": r["snapshot_id"],
            "status": r["status"],
            "deleted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        for r in results
        if r.get("snapshot_id")
    ]
    write_excel_report(results, summary_data, snapshots_data, output_path, "Batch Snapshot Deletion Report")
```

- [ ] **Step 3: Add `parse_args` and `main`**

Append to `e2b_bench/snap/delete.py`:

```python
def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    default_env = "e2b_bench/scripts/.env"
    default_config = os.path.join(os.path.expanduser("~"), ".e2b", "config.json")

    parser = argparse.ArgumentParser(
        description="Batch Snapshot Deletion — delete snapshots by JSON ledger or server-wide",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m e2b_bench.snap.delete -i snapshots.json
  python3 -m e2b_bench.snap.delete --all
  python3 -m e2b_bench.snap.delete --all --yes
        """,
    )
    parser.add_argument("-e", "--env-file", default=default_env, help=f"Path to .env file (default: {default_env})")
    parser.add_argument("--config", default=default_config, help=f"Path to E2B config JSON (default: {default_config})")
    parser.add_argument(
        "-i", "--input-json", default="snapshots.json", help="Delete snapshots listed in this JSON (default mode)"
    )
    parser.add_argument("--all", action="store_true", help="List and delete ALL snapshots on the server")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("-n", "--count", type=int, default=None, help="Limit: delete only first N matching snapshots")
    parser.add_argument("-bs", "--batch-size", type=int, default=None, help="Deletions per batch (default: full concurrent)")
    parser.add_argument("--batch-interval", type=int, default=3, help="Seconds between batches (default: 3)")
    parser.add_argument(
        "--output-xlsx", default=None, help="Excel report path (default: results/snap/snap_delete_<timestamp>.xlsx)"
    )
    parser.add_argument("--timeout", type=int, default=86400, help="API call timeout in seconds (default: 86400)")
    parser.add_argument("--api-key", default=None, help="Override E2B API key")
    parser.add_argument("--access-token", default=None, help="Override E2B access token")
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    if args.output_xlsx is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output_xlsx = f"results/snap/snap_delete_{timestamp}.xlsx"

    print("=" * 60)
    print("Batch Snapshot Deletion")
    print("=" * 60)

    print("\n[1/6] Loading configuration...")
    try:
        env = load_env(args.env_file, args.config, args.api_key, args.access_token)
    except ValueError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)
    print(f"  E2B API URL: {env['e2b_api_url']}")
    print(f"  Mode: {'ALL (server-wide)' if args.all else 'JSON ledger'}")

    # Build target list
    print(f"\n[2/6] Building target list...")
    ledger_data = None
    if args.all:
        snapshot_ids = list_all_snapshot_ids(timeout=args.timeout)
    else:
        if not os.path.exists(args.input_json):
            print(f"  ERROR: JSON file not found: {args.input_json}")
            sys.exit(1)
        with open(args.input_json, encoding="utf-8") as f:
            ledger_data = json.load(f)
        snapshot_ids = load_active_snapshot_ids_from_json(ledger_data, count=args.count)

    if args.count is not None and args.all:
        snapshot_ids = snapshot_ids[: args.count]

    if not snapshot_ids:
        print("  Nothing to delete. Exiting.")
        sys.exit(0)

    # Safety gate
    print(f"\n[3/6] {len(snapshot_ids)} snapshots will be deleted.")
    for sid in snapshot_ids[:10]:
        print(f"  - {sid}")
    if len(snapshot_ids) > 10:
        print(f"  ... and {len(snapshot_ids) - 10} more")
    if not confirm("Proceed?", assume_yes=args.yes):
        print("  Aborted.")
        sys.exit(0)

    # Delete
    print(f"\n[4/6] Deleting {len(snapshot_ids)} snapshots...")
    t_total_start = time.time()
    results = delete_batch(
        snapshot_ids=snapshot_ids,
        timeout=args.timeout,
        batch_size=args.batch_size,
        batch_interval=args.batch_interval,
    )
    t_total_elapsed = time.time() - t_total_start

    success = sum(1 for r in results if r["status"] == "success")
    not_found = sum(1 for r in results if r["status"] == "not_found")
    failed = sum(1 for r in results if r["status"] == "failed")
    print(f"\n[5/6] Results: {success} deleted, {not_found} not found, {failed} failed")
    print(f"  Total wall time: {t_total_elapsed:.2f}s")

    # Update JSON ledger (input-json mode only)
    if ledger_data is not None:
        updated = update_json_ledger(ledger_data, results)
        with open(args.input_json, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, ensure_ascii=False)
        print(f"  Updated ledger: {args.input_json}")

    # Report
    print(f"\n[6/6] Saving report...")
    build_report(results, args.output_xlsx)
    summary_data = compute_summary(results)
    print()
    print_summary(summary_data, "Batch Snapshot Deletion Summary")

    print(f"\n{'=' * 60}")
    print(f"Done — {success} deleted, {not_found} not found, {failed} failed")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Verify `--help` runs and imports cleanly**

Run: `python -m e2b_bench.snap.delete --help`
Expected: prints usage with `--all`, `-i`, `-y`, `-n`, `-bs` options; no import error.

- [ ] **Step 5: Run full test suite + ruff**

Run: `python -m pytest e2b_bench/tests/test_snap_delete.py e2b_bench/tests/test_snap_common.py -v && ruff format e2b_bench/snap/delete.py && ruff check e2b_bench/snap/delete.py`
Expected: all tests pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add e2b_bench/snap/delete.py
git commit -m "feat(e2b_bench/snap): add delete CLI with json/all modes, safety gate, report"
```

---

### Task 6: Update `__init__.py` and README

**Files:**
- Modify: `e2b_bench/snap/__init__.py`
- Modify: `e2b_bench/snap/README.md`

- [ ] **Step 1: Update package docstring**

In `e2b_bench/snap/__init__.py`, update the module list and usage block to mention `delete`:

```python
"""
E2B Snapshot Management Package

Provides batch snapshot creation, sandbox restoration, and snapshot deletion
utilities for E2B sandbox performance benchmarking.

Modules:
    common: Shared utilities (env loading, stats, Excel + terminal report)
    create: Batch snapshot creation script
    restore: Batch sandbox restoration from snapshots
    delete: Batch snapshot deletion (JSON ledger or server-wide)

Usage:
    python -m e2b_bench.snap.create --env-file .env --count 5
    python -m e2b_bench.snap.restore --env-file .env
    python -m e2b_bench.snap.delete --env-file .env -i snapshots.json
"""
```

- [ ] **Step 2: Add `snap.delete` section to README**

In `e2b_bench/snap/README.md`, after the `snap.restore` section (after line ~99), insert a new command-reference block:

````markdown
### `snap.delete` — Batch Snapshot Deletion

```
python3 -m e2b_bench.snap.delete [OPTIONS]
```

Deletes E2B snapshots (which persist after sandbox deletion). Two modes: delete by JSON ledger (default) or delete all snapshots on the server.

| Short | Long | Default | Description |
|-------|------|---------|-------------|
| `-e` | `--env-file` | `e2b_bench/scripts/.env` | Path to .env file |
| | `--config` | `~/.e2b/config.json` | Path to E2B config JSON |
| `-i` | `--input-json` | `snapshots.json` | Delete snapshots listed in this JSON (default) |
| | `--all` | False | List and delete ALL snapshots on the server |
| `-y` | `--yes` | False | Skip confirmation prompt |
| `-n` | `--count` | None (all) | Limit: delete only first N matching |
| `-bs` | `--batch-size` | full concurrent | Deletions per batch |
| | `--batch-interval` | `3` | Seconds between batches |
| | `--output-xlsx` | auto | Excel report path (default: `results/snap/snap_delete_<timestamp>.xlsx`) |
| | `--timeout` | `86400` | API call timeout (seconds) |
| | `--api-key` | None | Override E2B API key |
| | `--access-token` | None | Override E2B access token |

**Examples:**

```bash
# Delete snapshots recorded in snapshots.json
python3 -m e2b_bench.snap.delete -i snapshots.json

# Delete ALL snapshots on the server (will prompt)
python3 -m e2b_bench.snap.delete --all

# Delete all, no prompt (scripting)
python3 -m e2b_bench.snap.delete --all --yes
```

**JSON ledger:** In `--input-json` mode, deleted entries are marked `status: "deleted"` with a `deleted_at` timestamp in place — so the file is safely re-runnable.
````

- [ ] **Step 3: Update README Architecture + Quick Start sections**

In `e2b_bench/snap/README.md`:
- In the Quick Start section (after the restore command), add:

```bash
# Step 3 (optional): Delete the snapshots when done
python3 -m e2b_bench.snap.delete -i snapshots.json
```

- In the Architecture block, add the `delete.py` line:

```
e2b_bench/snap/
├── __init__.py    # Package documentation
├── common.py      # load_env, compute_stats, write_excel_report, print_summary
├── create.py      # Batch sandbox + snapshot creation → JSON + Excel
├── restore.py     # Load JSON → batch restore → Excel
└── delete.py      # Batch snapshot deletion (JSON ledger or --all)
```

- Add a one-line note under the "Output" section's Excel table:

```markdown
All three scripts also print a paste-friendly summary table to the terminal at the end of the run (same stats as the Summary sheet).
```

- [ ] **Step 4: Commit**

```bash
git add e2b_bench/snap/__init__.py e2b_bench/snap/README.md
git commit -m "docs(e2b_bench/snap): document delete command and terminal summary"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full snap test suite**

Run: `python -m pytest e2b_bench/tests/test_snap_common.py e2b_bench/tests/test_snap_delete.py -v`
Expected: all pass.

- [ ] **Step 2: Verify all three scripts import and show `--help`**

Run:
```bash
python -m e2b_bench.snap.create --help > /dev/null
python -m e2b_bench.snap.restore --help > /dev/null
python -m e2b_bench.snap.delete --help > /dev/null
```
Expected: no errors.

- [ ] **Step 3: Run ruff across the whole snap package**

Run: `ruff format e2b_bench/snap/ && ruff check e2b_bench/snap/`
Expected: clean.

- [ ] **Step 4: Verify git status — only snap-package files + tests are staged/modified**

Run: `git status --short`
Expected: only `e2b_bench/snap/*`, `e2b_bench/tests/test_snap_*`, and the already-committed spec/plan docs. No stray `=` file, no unrelated `llm_replay/` sessions.

No commit in this task — it's pure verification.
