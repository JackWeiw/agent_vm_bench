"""Per-workflow task-runner package.

One module per workflow -- ``browser``, ``coding``, ``document`` -- each holding
that workflow's warmup / fixed / round-robin runner threads. Callers import from
the submodule they need (``from bench_core.task_runner.browser import
BrowserTaskRunner``) so a browser-only run does not load the coding or document
modules. The package itself stays a pure namespace: no eager re-exports.
"""
