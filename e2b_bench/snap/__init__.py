"""
E2B Snapshot Management Package

Provides batch snapshot creation and sandbox restoration utilities
for E2B sandbox performance benchmarking.

Modules:
    common: Shared utilities (env loading, stats, Excel report writer)
    create: Batch snapshot creation script
    restore: Batch sandbox restoration from snapshots

Usage:
    python -m e2b_bench.snap.create --env-file .env --count 5
    python -m e2b_bench.snap.restore --env-file .env
"""
