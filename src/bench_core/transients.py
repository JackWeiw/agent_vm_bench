"""Transient-sandbox-error classification for replay lifecycle retry.

Classifies exceptions as transient (retryable) by lowercased substring match
against a marker list of gateway/rate-limit/timeout/connection-reset strings
ported from the reference ``control_plane_scheduler.py``. Works across
e2b/aenv/docker SDKs without importing them.
"""
from __future__ import annotations

# Lowercased marker substrings. Order does not matter; first match wins.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "502",
    "503",
    "504",
    "429",
    "rate limit",
    "rate-limit",
    "timeout",
    "timed out",
    "context deadline exceeded",
    "connection reset",
    "connection refused",
    "aborted",
    "temporarily unavailable",
)


def is_transient_sandbox_error(exc: BaseException | None) -> bool:
    """Return True if ``exc`` looks like a transient, retryable sandbox failure.

    Classifies by lowercased ``str(exc)`` substring match against a marker list
    ported from the reference scheduler. ``None`` -> False (no error to retry).
    """
    if exc is None:
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)
