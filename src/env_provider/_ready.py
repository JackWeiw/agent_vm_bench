"""Workflow-driven sandbox readiness checks (shared across backends).

Readiness is a workflow concern, not a backend one. A sandbox is ready when its
workflow probe passes:

- browser  : poll the service ports until all are listening (ss/netstat + grep)
- coding   : run ``uname -a`` until it returns a non-empty stdout
- document : run ``document-bench-validate`` until it exits 0 (a completed
             non-zero exit is an image-validation failure, returned immediately
             rather than retried -- retrying would hide the actionable output)
- replay   : command responsiveness (reuses the coding probe -- a replay
             sandbox only needs ``exec``, just like coding)

The ONLY thing that varies by backend is the exec primitive: run a command in
the sandbox handle and get ``(exit_code, stdout, stderr)``. Each manager supplies
that as a ~3-line closure (``exec_fn``); this module owns the poll-until-ready
algorithm (stop-event cancellation, distinct-error-once logging, deadline/interval).

Timing is ``time.monotonic()`` + ``stop_event.wait()`` throughout, so the loop
cancels promptly when the kernel signals stop (harmonising the e2b manager's old
``time.time()``/``sleep`` split).
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from threading import Event
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Browser service ports (openclaw-gateway + llama-server). Host-agnostic: both
# the e2b and docker images expose the same services on the same ports.
BROWSER_REQUIRED_PORTS: list[tuple[int, str]] = [
    (18789, "openclaw-gateway"),
    (11436, "llama-server"),
]

# Default ready-check tuning (a backend overrides via Config).
READY_MAX_WAIT = 300  # seconds; the poll deadline
READY_INTERVAL = 5  # seconds; sleep between probe rounds

# Per-probe exec timeout caps (seconds). Quick probes (uname, ss) cap at 10; the
# document validator can be slow, so it adapts to the remaining deadline (<=60).
_QUICK_PROBE_TIMEOUT = 10
_DOCUMENT_PROBE_TIMEOUT_CAP = 60

# Run a command in a backend sandbox handle, returning (exit_code, stdout, stderr).
# The handle is opaque to ReadyChecker (e2b Sandbox object / docker container).
ExecFn = Callable[[Any, str, int], tuple[int, str, str]]


class ReadyResult(Protocol):
    """Result of a readiness check (a plain dict satisfies this structurally)."""

    success: bool
    wait_elapsed: float
    error: str


class ReadyChecker:
    """Run workflow-driven readiness probes against a sandbox handle.

    Constructed per manager with the backend's ``exec_fn`` + tuning. The manager
    calls :meth:`check` during create/detect; the returned dict carries
    ``success`` / ``wait_elapsed`` / ``error``, which the manager maps onto the
    sandbox's ``creation_metrics`` (PORT_READY / PORT_FAILED + port_check_error).
    """

    def __init__(
        self,
        stop_event: Event,
        exec_fn: ExecFn,
        *,
        max_wait: int = READY_MAX_WAIT,
        interval: int = READY_INTERVAL,
        ports: list[int] | None = None,
    ) -> None:
        self._stop = stop_event
        self._exec = exec_fn
        self._max_wait = max_wait
        self._interval = interval
        self._ports = ports if ports is not None else [p for p, _ in BROWSER_REQUIRED_PORTS]

    def check(self, handle: Any, workflow_type: str, label: str) -> dict:
        """Dispatch to the workflow's probe. ``label`` prefixes log lines."""
        if workflow_type == "coding":
            return self._check_command(handle, label)
        if workflow_type == "document":
            return self._check_document(handle, label)
        if workflow_type == "browser":
            return self._check_ports(handle, label)
        if workflow_type == "replay":
            return self._check_command(handle, label)
        raise ValueError(f"Unsupported workflow_type: {workflow_type}")

    # ------------------------------------------------------------------ coding
    def _check_command(self, handle: Any, label: str) -> dict:
        """Coding readiness: ``uname -a`` succeeds with non-empty stdout.

        Retries on exception (the command service may still be coming online on a
        freshly created sandbox); prints each distinct error once so a stuck
        ready-check surfaces the real cause instead of looping silently.
        """
        if not handle:
            return self._fail(0.0, "No sandbox handle")

        started = time.monotonic()
        seen_errors: set[tuple[str, str]] = set()

        while True:
            remaining = self._max_wait - (time.monotonic() - started)
            if remaining <= 0:
                break
            if self._stop.is_set():
                return self._fail(time.monotonic() - started, "Stop event")

            try:
                exit_code, stdout, _ = self._exec(handle, "uname -a", _QUICK_PROBE_TIMEOUT)
                if exit_code == 0 and stdout.strip():
                    wait = time.monotonic() - started
                    logger.info(f"[{label}] Command ready in {wait:.1f}s: {stdout.strip()[:50]}")
                    return {"success": True, "wait_elapsed": wait, "error": ""}
            except Exception as e:
                err_key = (type(e).__name__, str(e)[:80])
                if err_key not in seen_errors:
                    seen_errors.add(err_key)
                    logger.warning(f"[{label}] uname check error: {type(e).__name__}: {str(e)[:120]}")

            if self._stop.wait(min(self._interval, remaining)):
                return self._fail(time.monotonic() - started, "Stop event")

        return self._fail(time.monotonic() - started, "Timeout waiting for command response")

    # ---------------------------------------------------------------- document
    def _check_document(self, handle: Any, label: str) -> dict:
        """Document readiness: ``document-bench-validate`` exits 0.

        A completed command with a non-zero exit is a semantic image/asset
        validation failure -- returned immediately (retrying would only hide the
        actionable validator output and delay failure reporting). Exceptions
        raised before a result is available can occur while the command service
        is still coming online, so those are retried within the shared deadline.
        """
        if not handle:
            return self._fail(0.0, "No sandbox handle")

        started = time.monotonic()
        last_error = "document command service did not become ready"
        seen_errors: set[tuple[str, str]] = set()

        while True:
            remaining = self._max_wait - (time.monotonic() - started)
            if remaining <= 0:
                break
            if self._stop.is_set():
                return self._fail(time.monotonic() - started, "Stop event")

            # The validator can be slow; bound each call by the remaining deadline
            # (<=60s) so a transient call can't blow the whole budget alone.
            timeout = max(1, min(_DOCUMENT_PROBE_TIMEOUT_CAP, math.ceil(remaining)))
            try:
                exit_code, stdout, stderr = self._exec(handle, "sh -c 'document-bench-validate >/dev/null'", timeout)
            except Exception as exc:
                last_error = str(exc)
                err_key = (type(exc).__name__, last_error[:80])
                if err_key not in seen_errors:
                    seen_errors.add(err_key)
                    logger.warning(
                        f"[{label}] Document ready check error: " f"{type(exc).__name__}: {last_error[:120]}"
                    )
                remaining = self._max_wait - (time.monotonic() - started)
                if remaining <= 0:
                    break
                if self._stop.wait(min(self._interval, remaining)):
                    return self._fail(time.monotonic() - started, "Stop event")
                continue

            elapsed = time.monotonic() - started
            if exit_code == 0:
                logger.info(f"[{label}] Document runtime ready in {elapsed:.1f}s")
                return {"success": True, "wait_elapsed": elapsed, "error": ""}

            # Validator ran and failed -> image/asset failure, not transient.
            detail = (stderr or stdout or "no output").strip()
            return self._fail(
                elapsed,
                f"Document runtime validation failed: {detail[:200]}",
            )

        return self._fail(
            time.monotonic() - started,
            f"Timeout waiting for document runtime validation: {last_error[:200]}",
        )

    # ----------------------------------------------------------------- browser
    def _check_ports(self, handle: Any, label: str) -> dict:
        """Browser readiness: every required port is listening.

        Probes each port with ``ss | grep`` (netstat fallback) until all are
        found. Per-port exceptions are swallowed (a single transient probe error
        shouldn't abort the whole check); the deadline bounds the wait.
        """
        if not handle:
            return self._fail(0.0, "No sandbox handle")

        started = time.monotonic()
        ready_ports: set[int] = set()
        port_names = {p: n for p, n in BROWSER_REQUIRED_PORTS}

        while True:
            remaining = self._max_wait - (time.monotonic() - started)
            if remaining <= 0:
                break
            if self._stop.is_set():
                return self._fail(time.monotonic() - started, "Stop event")

            for port in self._ports:
                if port in ready_ports:
                    continue
                cmd = (
                    f"sh -c \"ss -tlnp 2>/dev/null | grep ':{port}' " f"|| netstat -tlnp 2>/dev/null | grep ':{port}'\""
                )
                try:
                    exit_code, stdout, _ = self._exec(handle, cmd, _QUICK_PROBE_TIMEOUT)
                    if exit_code == 0 and stdout.strip():
                        ready_ports.add(port)
                        name = port_names.get(port, "")
                        logger.info(f"[{label}] Port {port} ({name}) is listening")
                except Exception:
                    pass  # transient probe error; keep polling other ports

            if len(ready_ports) == len(self._ports):
                wait = time.monotonic() - started
                logger.info(f"[{label}] All required ports ready in {wait:.1f}s")
                return {"success": True, "wait_elapsed": wait, "error": ""}

            if self._stop.wait(min(self._interval, remaining)):
                return self._fail(time.monotonic() - started, "Stop event")

        missing = [p for p in self._ports if p not in ready_ports]
        return self._fail(
            time.monotonic() - started,
            f"Timeout waiting for ports: {missing}",
        )

    # ------------------------------------------------------------------- helper
    @staticmethod
    def _fail(wait_elapsed: float, error: str) -> dict:
        return {"success": False, "wait_elapsed": wait_elapsed, "error": error}
