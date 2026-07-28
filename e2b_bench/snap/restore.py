#!/usr/bin/env python3
"""
Batch Sandbox Restore from Snapshots

Reads snapshot IDs from a JSON file (created by snap_create.py),
batch-creates sandboxes from those snapshots, and generates an
Excel performance report with statistics.

Usage:
    python3 -m e2b_bench.snap.restore -i snapshots.json
    python3 -m e2b_bench.snap.restore -i snapshots.json -n 5
    python3 -m e2b_bench.snap.restore -i snapshots.json -k
"""

import argparse
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List

from e2b import Sandbox

from .common import compute_stats, load_env, print_summary, write_excel_report

# Global list of created sandbox handles for cleanup on signal
_created_sandboxes: List[Any] = []


def _signal_handler(*_args):
    """Handle Ctrl+C to cleanup created sandboxes."""
    print("\n\n[Signal] Ctrl+C received, cleaning up...")
    for sbx in _created_sandboxes:
        try:
            sbx.kill()
        except Exception as e:
            print(f"  Kill error: {e}")
    print(f"  Cleaned up {len(_created_sandboxes)} sandboxes")
    sys.exit(1)


def restore_single_sandbox(snapshot_id: str, timeout: int, index: int) -> Dict[str, Any]:
    """Create a single sandbox from a snapshot.

    Args:
        snapshot_id: E2B snapshot ID to restore from.
        timeout: Sandbox creation timeout in seconds.
        index: Sandbox index (1-based) for logging.

    Returns:
        Dict with: index, snapshot_id, sandbox_id, restore_elapsed_s,
        status, error.
    """
    result = {
        "index": index,
        "snapshot_id": snapshot_id,
        "sandbox_id": "",
        "restore_elapsed_s": 0.0,
        "status": "failed",
        "error": "",
    }

    try:
        t_start = time.time()
        sbx = Sandbox.create(snapshot_id, timeout=timeout)
        t_restore = time.time() - t_start
        result["sandbox_id"] = sbx.sandbox_id
        result["restore_elapsed_s"] = round(t_restore, 4)
        result["status"] = "success"
        _created_sandboxes.append(sbx)
        print(f"  [{index}] Restored in {t_restore:.2f}s: {sbx.sandbox_id[:16]}... (from {snapshot_id[:16]}...)")
    except Exception as e:
        result["error"] = str(e)
        print(f"  [{index}] Restore FAILED: {str(e)[:80]} (from {snapshot_id[:16]}...)")

    return result


def restore_batch(
    snapshot_ids: List[str],
    timeout: int,
    batch_size: int = None,
    batch_interval: int = 3,
) -> List[Dict[str, Any]]:
    """Create N sandboxes from snapshots in batches.

    Args:
        snapshot_ids: List of snapshot IDs to restore from.
        timeout: Sandbox creation timeout in seconds.
        batch_size: If set, create this many sandboxes per batch.
        batch_interval: Seconds to wait between batches.

    Returns:
        List of result dicts from restore_single_sandbox.
    """
    count = len(snapshot_ids)
    results: List[Dict[str, Any]] = []

    if batch_size and batch_size > 0:
        num_batches = (count + batch_size - 1) // batch_size
        print(f"\n  Batched restore: {count} sandboxes in {num_batches} batches of {batch_size}")
        print(f"  Batch interval: {batch_interval}s")

        for batch_id in range(num_batches):
            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, count)
            print(f"\n  [Batch {batch_id + 1}/{num_batches}] Sandboxes {start_idx + 1}-{end_idx}")

            batch_ids = snapshot_ids[start_idx:end_idx]
            batch_results = _restore_concurrent(batch_ids, timeout, start_idx)
            results.extend(batch_results)

            if batch_id < num_batches - 1:
                print(f"  Waiting {batch_interval}s before next batch...")
                time.sleep(batch_interval)
    else:
        print(f"\n  Full concurrent restore: {count} sandboxes")
        results = _restore_concurrent(snapshot_ids, timeout, 0)

    return results


def _restore_concurrent(snapshot_ids: List[str], timeout: int, start_index: int) -> List[Dict[str, Any]]:
    """Concurrently restore sandboxes from snapshots.

    Args:
        snapshot_ids: List of snapshot IDs for this batch.
        timeout: Sandbox creation timeout in seconds.
        start_index: 0-based start index for numbering.

    Returns:
        List of result dicts, ordered by index.
    """
    results: Dict[int, Dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=len(snapshot_ids)) as executor:
        futures = {}
        for i, snap_id in enumerate(snapshot_ids):
            future = executor.submit(restore_single_sandbox, snap_id, timeout, start_index + i + 1)
            futures[future] = start_index + i + 1

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results[idx] = result
            except Exception as e:
                results[idx] = {
                    "index": idx,
                    "snapshot_id": "",
                    "sandbox_id": "",
                    "restore_elapsed_s": 0.0,
                    "status": "failed",
                    "error": str(e),
                }

    return [results[i] for i in sorted(results.keys())]


def kill_all_sandboxes() -> int:
    """Kill all created sandboxes.

    Returns:
        Number of sandboxes killed.
    """
    killed = 0
    for sbx in _created_sandboxes:
        try:
            sbx.kill()
            killed += 1
        except Exception as e:
            print(f"  Kill error: {e}")
    _created_sandboxes.clear()
    return killed


def load_snapshot_ids(input_json: str, count: int = None) -> List[Dict[str, Any]]:
    """Load snapshot IDs from JSON file.

    Args:
        input_json: Path to JSON file created by snap_create.py.
        count: If set, only load the first N snapshots.

    Returns:
        List of snapshot dicts with snapshot_id and metadata.
    """
    if not os.path.exists(input_json):
        raise FileNotFoundError(f"Snapshot JSON file not found: {input_json}")

    with open(input_json, encoding="utf-8") as f:
        data = json.load(f)

    snapshots = data.get("snapshots", [])
    if not snapshots:
        raise ValueError(f"No snapshots found in {input_json}")

    if count is not None and count < len(snapshots):
        print(f"  Using first {count} of {len(snapshots)} snapshots")
        snapshots = snapshots[:count]

    # Validate that all entries have snapshot_id
    valid = [s for s in snapshots if s.get("snapshot_id")]
    if len(valid) < len(snapshots):
        print(f"  WARNING: {len(snapshots) - len(valid)} entries without snapshot_id, skipping")

    return valid


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


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    default_env = "e2b_bench/scripts/.env"
    default_config = os.path.join(os.path.expanduser("~"), ".e2b", "config.json")

    parser = argparse.ArgumentParser(
        description="Batch Sandbox Restore — create sandboxes from snapshot IDs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m e2b_bench.snap.restore -i snapshots.json
  python3 -m e2b_bench.snap.restore -i snapshots.json -n 5
  python3 -m e2b_bench.snap.restore -i snapshots.json -k -bs 3
        """,
    )
    parser.add_argument("-e", "--env-file", default=default_env, help=f"Path to .env file (default: {default_env})")
    parser.add_argument("--config", default=default_config, help=f"Path to E2B config JSON (default: {default_config})")
    parser.add_argument(
        "-i", "--input-json", default="snapshots.json", help="Path to snapshot IDs JSON (default: snapshots.json)"
    )
    parser.add_argument(
        "-n", "--count", type=int, default=None, help="Number of sandboxes to create (default: all in JSON)"
    )
    parser.add_argument(
        "-bs", "--batch-size", type=int, default=None, help="Sandboxes per creation batch (default: full concurrent)"
    )
    parser.add_argument("--batch-interval", type=int, default=3, help="Seconds between batches (default: 3)")
    parser.add_argument(
        "--output-xlsx",
        default=None,
        help="Path to save Excel report (default: results/snap/snap_restore_n<count>_bsz<bsz>_<timestamp>.xlsx)",
    )
    parser.add_argument(
        "--timeout", type=int, default=86400, help="Sandbox creation timeout in seconds (default: 86400)"
    )
    parser.add_argument(
        "-k", "--keep", action="store_true", help="Keep sandboxes alive after creation (default: kill after timing)"
    )
    parser.add_argument("--api-key", default=None, help="Override E2B API key (highest priority)")
    parser.add_argument("--access-token", default=None, help="Override E2B access token (highest priority)")

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Auto-generate output-xlsx with timestamp if not specified
    if args.output_xlsx is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        n = args.count if args.count else "all"
        bsz = args.batch_size if args.batch_size else "full"
        args.output_xlsx = f"results/snap/snap_restore_n{n}_bsz{bsz}_{timestamp}.xlsx"

    # Register signal handler
    signal.signal(signal.SIGINT, _signal_handler)

    print("=" * 60)
    print("Batch Sandbox Restore from Snapshots")
    print("=" * 60)

    # Load environment
    print("\n[1/5] Loading configuration...")
    try:
        env = load_env(args.env_file, args.config, args.api_key, args.access_token)
    except ValueError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    print(f"  E2B API URL: {env['e2b_api_url']}")
    print(f"  Input JSON: {args.input_json}")

    # Load snapshot IDs
    print(f"\n[2/5] Loading snapshot IDs...")
    try:
        snapshots = load_snapshot_ids(args.input_json, args.count)
    except (FileNotFoundError, ValueError) as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    snapshot_ids = [s["snapshot_id"] for s in snapshots]
    print(f"  Loaded {len(snapshot_ids)} snapshot IDs")
    if args.keep:
        print(f"  Mode: KEEP (sandboxes will stay alive)")
    else:
        print(f"  Mode: AUTO-KILL (sandboxes will be killed after timing)")

    # Create sandboxes from snapshots
    print(f"\n[3/5] Restoring {len(snapshot_ids)} sandboxes from snapshots...")
    t_total_start = time.time()
    results = restore_batch(
        snapshot_ids=snapshot_ids,
        timeout=args.timeout,
        batch_size=args.batch_size,
        batch_interval=args.batch_interval,
    )
    t_total_elapsed = time.time() - t_total_start

    # Print summary
    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    print(f"\n[4/5] Results: {success_count} success, {failed_count} failed")
    print(f"  Total wall time: {t_total_elapsed:.2f}s")

    # Kill sandboxes (unless --keep)
    if not args.keep:
        print(f"\n[5/5] Cleaning up sandboxes...")
        killed = kill_all_sandboxes()
        print(f"  Killed {killed} sandboxes")
    else:
        print(f"\n[5/5] Keeping {len(_created_sandboxes)} sandboxes alive")
        # Print sandbox IDs for reference
        for sbx in _created_sandboxes:
            print(f"  - {sbx.sandbox_id}")

    # Build Excel report
    build_report(results, args.output_xlsx)

    summary_data = compute_summary(results)
    print()
    print_summary(summary_data, "Batch Sandbox Restore Summary")

    print(f"\n{'=' * 60}")
    print(f"Done — {success_count}/{len(snapshot_ids)} sandboxes restored successfully")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
