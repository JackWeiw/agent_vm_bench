#!/usr/bin/env python3
"""Batch Snapshot Deletion Script

Deletes E2B snapshots that survive sandbox deletion. Two modes:
  --input-json  (default): delete snapshots recorded in a JSON ledger.
  --all:                   list + delete every snapshot on the server.

Note: This module currently contains the logic layer only. The CLI entry
point (main, argparse, batch execution) is added in a follow-up task.

Usage:
    python3 -m e2b_bench.snap.delete -i snapshots.json
    python3 -m e2b_bench.snap.delete --all
    python3 -m e2b_bench.snap.delete --all --yes
"""

import time
from datetime import datetime
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
        Dict with: index, snapshot_id, delete_elapsed_s, status, error.
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
    t_start = time.time()
    try:
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
        The same data dict, mutated in place: matched entries get status + deleted_at set.
    """
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
