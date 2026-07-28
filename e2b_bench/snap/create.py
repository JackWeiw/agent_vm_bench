#!/usr/bin/env python3
"""
Batch Snapshot Creation Script

Creates N sandboxes from an E2B template, creates a snapshot for each,
saves snapshot IDs to a JSON file, and generates an Excel performance report.

Usage:
    python3 -m e2b_bench.snap.create -t uu -n 10 -o snapshots.json
    python3 -m e2b_bench.snap.create -t openclaw-browser-v1 -n 10 -bs 5
    # Then restore from the JSON output:
    python3 -m e2b_bench.snap.restore -i snapshots.json
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


def create_single_sandbox(template: str, timeout: int, index: int) -> Dict[str, Any]:
    """Create a single sandbox and take a snapshot.

    Args:
        template: E2B template name.
        timeout: Sandbox creation timeout in seconds.
        index: Sandbox index (1-based) for logging.

    Returns:
        Dict with: index, sandbox_id, snapshot_id, create_elapsed_s,
        snapshot_elapsed_s, total_elapsed_s, status, error.
    """
    result = {
        "index": index,
        "sandbox_id": "",
        "snapshot_id": "",
        "create_elapsed_s": 0.0,
        "snapshot_elapsed_s": 0.0,
        "total_elapsed_s": 0.0,
        "status": "sandbox_failed",
        "error": "",
    }

    # Step 1: Create sandbox
    try:
        t_start = time.time()
        sbx = Sandbox.create(template, timeout=timeout)
        t_create = time.time() - t_start
        result["sandbox_id"] = sbx.sandbox_id
        result["create_elapsed_s"] = round(t_create, 4)
        result["status"] = "created"
        _created_sandboxes.append(sbx)
        print(f"  [{index}] Sandbox created in {t_create:.2f}s: {sbx.sandbox_id[:16]}...")
    except Exception as e:
        result["status"] = "sandbox_failed"
        result["error"] = str(e)
        print(f"  [{index}] Sandbox creation FAILED: {str(e)[:80]}")
        return result

    # Step 2: Create snapshot
    try:
        t_start = time.time()
        snapshot = sbx.create_snapshot()
        t_snapshot = time.time() - t_start
        result["snapshot_id"] = snapshot.snapshot_id
        result["snapshot_elapsed_s"] = round(t_snapshot, 4)
        result["total_elapsed_s"] = round(result["create_elapsed_s"] + t_snapshot, 4)
        result["status"] = "success"
        print(f"  [{index}] Snapshot created in {t_snapshot:.2f}s: {snapshot.snapshot_id[:16]}...")
    except Exception as e:
        result["status"] = "snapshot_failed"
        result["error"] = str(e)
        result["total_elapsed_s"] = result["create_elapsed_s"]
        print(f"  [{index}] Snapshot creation FAILED: {str(e)[:80]}")

    return result


def create_batch(
    template: str,
    count: int,
    timeout: int,
    batch_size: int = None,
    batch_interval: int = 3,
) -> List[Dict[str, Any]]:
    """Create N sandboxes with snapshots in batches.

    Args:
        template: E2B template name.
        count: Number of sandboxes/snapshots to create.
        timeout: Sandbox creation timeout in seconds.
        batch_size: If set, create this many sandboxes per batch.
        batch_interval: Seconds to wait between batches.

    Returns:
        List of result dicts from create_single_sandbox.
    """
    results: List[Dict[str, Any]] = []

    if batch_size and batch_size > 0:
        num_batches = (count + batch_size - 1) // batch_size
        print(f"\n  Batched creation: {count} sandboxes in {num_batches} batches of {batch_size}")
        print(f"  Batch interval: {batch_interval}s")

        for batch_id in range(num_batches):
            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, count)
            print(f"\n  [Batch {batch_id + 1}/{num_batches}] Sandboxes {start_idx + 1}-{end_idx}")

            batch_results = _create_concurrent(template, timeout, start_idx, end_idx)
            results.extend(batch_results)

            if batch_id < num_batches - 1:
                print(f"  Waiting {batch_interval}s before next batch...")
                time.sleep(batch_interval)
    else:
        print(f"\n  Full concurrent creation: {count} sandboxes")
        results = _create_concurrent(template, timeout, 0, count)

    return results


def _create_concurrent(template: str, timeout: int, start: int, end: int) -> List[Dict[str, Any]]:
    """Concurrently create sandboxes in a range.

    Args:
        template: E2B template name.
        timeout: Sandbox creation timeout in seconds.
        start: Start index (0-based).
        end: End index (exclusive, 0-based).

    Returns:
        List of result dicts, ordered by index.
    """
    results: Dict[int, Dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=end - start) as executor:
        futures = {}
        for i in range(start, end):
            future = executor.submit(create_single_sandbox, template, timeout, i + 1)
            futures[future] = i + 1

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results[idx] = result
            except Exception as e:
                results[idx] = {
                    "index": idx,
                    "sandbox_id": "",
                    "snapshot_id": "",
                    "create_elapsed_s": 0.0,
                    "snapshot_elapsed_s": 0.0,
                    "total_elapsed_s": 0.0,
                    "status": "sandbox_failed",
                    "error": str(e),
                }

    # Return in order
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


def save_snapshot_json(results: List[Dict[str, Any]], template: str, output_path: str) -> None:
    """Save snapshot IDs to a JSON file.

    Only includes entries with status='success' (valid snapshots).
    Failed entries are excluded from the JSON but remain in the Excel report.

    Args:
        results: List of result dicts from create_single_sandbox.
        template: Template name used for creation.
        output_path: Path to write the JSON file.
    """
    snapshots = []
    for r in results:
        if r["status"] == "success":
            snapshots.append(
                {
                    "snapshot_id": r["snapshot_id"],
                    "sandbox_id": r["sandbox_id"],
                    "create_elapsed_s": r["create_elapsed_s"],
                    "snapshot_elapsed_s": r["snapshot_elapsed_s"],
                    "total_elapsed_s": r["total_elapsed_s"],
                    "status": r["status"],
                }
            )

    data = {
        "template": template,
        "created_at": datetime.now().isoformat(),
        "count": len(snapshots),
        "snapshots": snapshots,
    }

    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"  Snapshot IDs saved to {output_path} ({len(snapshots)} successful)")


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


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    default_env = "e2b_bench/scripts/.env"
    default_config = os.path.join(os.path.expanduser("~"), ".e2b", "config.json")

    parser = argparse.ArgumentParser(
        description="Batch Snapshot Creation — create N sandboxes, snapshot each, save IDs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m e2b_bench.snap.create -t uu -n 10 -o snapshots.json
  python3 -m e2b_bench.snap.create -t openclaw-browser-v1 -n 10 -bs 5
  # Then restore from the JSON:
  python3 -m e2b_bench.snap.restore -i snapshots.json
        """,
    )
    parser.add_argument("-e", "--env-file", default=default_env, help=f"Path to .env file (default: {default_env})")
    parser.add_argument("--config", default=default_config, help=f"Path to E2B config JSON (default: {default_config})")
    parser.add_argument("-t", "--template", default="3g", help="E2B template name (default: 3g)")
    parser.add_argument(
        "-n", "--count", type=int, default=1, help="Number of sandboxes/snapshots to create (default: 1)"
    )
    parser.add_argument(
        "-bs", "--batch-size", type=int, default=None, help="Sandboxes per creation batch (default: full concurrent)"
    )
    parser.add_argument("--batch-interval", type=int, default=3, help="Seconds between batches (default: 3)")
    parser.add_argument(
        "-o", "--output-json", default="snapshots.json", help="Path to save snapshot IDs JSON (default: snapshots.json)"
    )
    parser.add_argument(
        "--output-xlsx",
        default=None,
        help="Path to save Excel report (default: results/snap/snap_create_n<count>_bsz<bsz>_<timestamp>.xlsx)",
    )
    parser.add_argument(
        "--timeout", type=int, default=86400, help="Sandbox creation timeout in seconds (default: 86400)"
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
        bsz = args.batch_size if args.batch_size else "full"
        args.output_xlsx = f"results/snap/snap_create_n{args.count}_bsz{bsz}_{timestamp}.xlsx"

    # Register signal handler
    signal.signal(signal.SIGINT, _signal_handler)

    print("=" * 60)
    print("Batch Snapshot Creation")
    print("=" * 60)

    # Load environment
    print("\n[1/5] Loading configuration...")
    try:
        env = load_env(args.env_file, args.config, args.api_key, args.access_token)
    except ValueError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    print(f"  E2B API URL: {env['e2b_api_url']}")
    print(f"  Template: {args.template}")
    print(f"  Count: {args.count}")
    print(f"  Batch size: {args.batch_size or 'full concurrent'}")
    print(f"  Output JSON: {args.output_json}")
    print(f"  Output XLSX: {args.output_xlsx}")

    # Create sandboxes and snapshots
    print(f"\n[2/5] Creating {args.count} sandboxes with snapshots...")
    t_total_start = time.time()
    results = create_batch(
        template=args.template,
        count=args.count,
        timeout=args.timeout,
        batch_size=args.batch_size,
        batch_interval=args.batch_interval,
    )
    t_total_elapsed = time.time() - t_total_start

    # Print summary
    success_count = sum(1 for r in results if r["status"] == "success")
    sandbox_failed = sum(1 for r in results if r["status"] == "sandbox_failed")
    snapshot_failed = sum(1 for r in results if r["status"] == "snapshot_failed")
    print(
        f"\n[3/5] Results: {success_count} success, {sandbox_failed} sandbox failed, {snapshot_failed} snapshot failed"
    )
    print(f"  Total wall time: {t_total_elapsed:.2f}s")

    # Kill all sandboxes
    print(f"\n[4/5] Cleaning up sandboxes...")
    killed = kill_all_sandboxes()
    print(f"  Killed {killed} sandboxes")

    # Save snapshot IDs and report
    print(f"\n[5/5] Saving results...")
    save_snapshot_json(results, args.template, args.output_json)
    build_report(results, args.template, args.output_xlsx)

    summary_data = compute_summary(results)
    print()
    print_summary(summary_data, "Batch Snapshot Creation Summary")

    print(f"\n{'=' * 60}")
    print(f"Done — {success_count}/{args.count} snapshots created successfully")
    print(f"{'=' * 60}")
    if success_count > 0:
        print(f"\nNext step — restore sandboxes from snapshots:")
        print(f"  python3 -m e2b_bench.snap.restore -i {args.output_json}")


if __name__ == "__main__":
    main()
