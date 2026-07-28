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
