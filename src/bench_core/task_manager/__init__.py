"""Task-orchestrator package.

One module per ``benchmark_mode`` -- ``fixed`` (:class:`TaskManager`) and
``round_robin`` (:class:`RoundRobinTaskManager`) -- each holding that mode's
warmup / dispatch / wait lifecycle. Callers import from the submodule they
need (``from bench_core.task_manager.fixed import TaskManager``) so a
fixed-only run does not load the round-robin module. The package itself stays
a pure namespace: no eager re-exports (mirrors ``bench_core.task_runner``).
"""
