"""Data collection & observation pipeline.

- monitor: host-level vm_monitor subprocess orchestration
- stats_collector: real-time snapshots + final report
- replay_obs: ReplayObservability assembly (was observability.py)
- obs_xlsx: 11-sheet replay observability workbook
- lifecycle_series: thread-safe JSONL lifecycle event writer
- lifecycle_reconstruct: pure transforms over loaded series events
"""
