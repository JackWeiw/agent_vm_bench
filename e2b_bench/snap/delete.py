#!/usr/bin/env python3
"""Batch Snapshot Deletion Script

Deletes E2B snapshots that survive sandbox deletion. Two modes:
  --input-json  (default): delete snapshots recorded in a JSON ledger.
  --all:                   list + delete every snapshot on the server.

Usage:
    python3 -m e2b_bench.snap.delete -i snapshots.json
    python3 -m e2b_bench.snap.delete --all
    python3 -m e2b_bench.snap.delete --all --yes
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from e2b import Sandbox

from .common import compute_stats, load_env, print_summary, write_excel_report


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
            entry["status"] = "deleted" if status_map[snap_id] in ("success", "not_found") else "delete_failed"
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
    parser.add_argument(
        "-bs", "--batch-size", type=int, default=None, help="Deletions per batch (default: full concurrent)"
    )
    parser.add_argument("--batch-interval", type=int, default=3, help="Seconds between batches (default: 3)")
    parser.add_argument(
        "--output-xlsx",
        default=None,
        help="Excel report path (default: results/snap/snap_delete_n<count>_bsz<bsz>_<timestamp>.xlsx)",
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
        n = "all" if args.all or args.count is None else args.count
        bsz = args.batch_size if args.batch_size else "full"
        args.output_xlsx = f"results/snap/snap_delete_n{n}_bsz{bsz}_{timestamp}.xlsx"

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
