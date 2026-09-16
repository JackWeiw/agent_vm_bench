"""Shared report helpers used by both the host and per-workflow formatters.

Extracted from ``stats_collector`` so the per-workflow ``*ReportFormatter``
classes (which live in ``task_runner/<wf>.py``, co-located with their runner +
config + spec) can import these without reaching into the 1600-line host
module — and so ``stats_collector`` no longer needs to import ``task_runner.*``
(the workflow-specific narrows have moved into the formatters). This keeps the
observability layer one-directional: ``task_runner`` -> ``observability``,
never the reverse.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bench_core.config import KernelConfig

# Lifecycle-overhead samples below this are excluded from per-sample overhead
# (prevents 1.0 / 0.0001-style explosion). Duration percentile lists already
# exclude == 0 (synthesized failures); this additionally drops pathologically-
# fast real slices.
MIN_SLICE_SEC = 0.001


def replay_pool_size(config: KernelConfig) -> int:
    """Distinct-trajectory count in the replay pool, 0 if unresolvable.

    ``load_pool`` is module-cached, so this is O(1) after the first runner-thread
    call and shares one immutable tuple across the fleet. Used only for the final
    report's "One-pass Target" line as the distinct-trajectory pool size (context,
    not the completion target). Returns 0 for non-replay workflows or when the
    pool cannot be loaded (e.g. Mock configs in unit tests).
    """
    if config.workflow_type != "replay":
        return 0
    try:
        from bench_core.payload.replay_payload import load_pool

        return len(load_pool(config))
    except Exception:
        import logging

        logging.getLogger(__name__).debug("replay pool size unavailable", exc_info=True)
        return 0


def replay_traj_target(config: KernelConfig) -> int:
    """Expected trajectory completions for the whole run (the live ``traj=`` denominator).

    Replay runs one trajectory per sandbox per round (all concurrent), so over
    ``round_count`` rounds the fleet completes ``round_count * total_count``
    trajectories -- the cumulative ceiling the snapshot's ``traj=done/total``
    divides into. When ``round_count`` is None/0 (sustained-until-duration) there
    is no fixed ceiling, so returns 0 and the snapshot prints a bare ``traj={done}``
    count instead of a misleading ratio. Distinct from the per-round ``One-pass
    Target`` (= ``total_count``) in the final report.
    """
    rc = config.round_count
    if not rc:  # None or 0 -> unlimited/sustained: no fixed ceiling
        return 0
    return rc * config.total_count


# Error display order, selected by workflow. The shared ``ErrorClassifier`` may
# bucket an error into a category a given workflow does not display; such
# categories fold into "Other" so the report schema stays consistent. Each
# workflow's ``ReportFormatters.error_display_order`` is one of these.
BROWSER_ERROR_DISPLAY = [
    "Open tab failed",
    "Page load failed",
    "Snapshot failed",
    "Click failed",
    "Screenshot failed",
    "Chrome start failed",
    "D-Bus connection error",
    "Gateway connection error",
    "Sandbox unreachable",
    "Timeout",
    "Other",
]

CODING_ERROR_DISPLAY = [
    "Checkout failed",
    "Edit failed",
    "Verify failed",
    "OOM",
    "Sandbox unreachable",
    "Timeout",
    "Other",
]

DOCUMENT_ERROR_DISPLAY = ["Read failed", "Write failed", "Verifier failed", "Timeout", "Other"]


class ErrorClassifier:
    """Error type classification for sandbox failures."""

    # Error type definitions with patterns (order matters - first match wins).
    ERROR_TYPES: list[tuple[str, list[str]]] = [
        # Browser errors
        ("Open tab failed", ["open_tab failed"]),
        ("Page load failed", ["page_load failed"]),
        ("Snapshot failed", ["snapshot failed"]),
        ("Click failed", ["click failed"]),
        ("Screenshot failed", ["screenshot failed"]),
        ("Chrome start failed", ["failed to start chrome", "chrome_start"]),
        ("D-Bus connection error", ["d-bus", "dbus", "failed to connect to the bus"]),
        ("Gateway connection error", ["gateway", "cdp", "http_unreachable"]),
        ("Sandbox unreachable", ["failed to route", "sandbox unreachable"]),
        # Coding errors
        ("Find failed", ["find failed", "git checkout", "locate failed"]),
        ("Read failed", ["read failed", "head failed"]),
        ("Edit failed", ["edit failed", "sed failed"]),
        ("Verify failed", ["verify failed", "npx tsx", "go run", "exit code"]),
        ("Diff failed", ["diff failed", "git diff"]),
        ("Write failed", ["write failed", "create write directory"]),
        ("Verifier failed", ["verification", "verifier", "business_verification"]),
        ("OOM", ["oom", "out of memory", "cannot allocate"]),
        ("Timeout", ["timeout", "timed out"]),
    ]

    @classmethod
    def classify(cls, error: str) -> str:
        """Classify an error message into an error type."""
        error_lower = error.lower()
        for error_type, patterns in cls.ERROR_TYPES:
            if any(pattern in error_lower for pattern in patterns):
                return error_type
        return "Other"

    @classmethod
    def aggregate(cls, errors: list[tuple[int, int, str]]) -> tuple[dict[str, int], dict[str, list[int]]]:
        """Aggregate errors by type.

        Args:
            errors: List of (sandbox_index, count, error_message).

        Returns:
            Tuple of (error_counts, error_sandbox_ids).
        """
        error_counts: dict[str, int] = {}
        error_sandbox_ids: dict[str, list[int]] = {}

        for sid, count, error in errors:
            error_type = cls.classify(error)
            error_counts[error_type] = error_counts.get(error_type, 0) + count
            error_sandbox_ids.setdefault(error_type, []).append(sid)

        return error_counts, error_sandbox_ids


class TableFormatter:
    """Simple table formatter for plain text output."""

    @staticmethod
    def format_table(headers: list[str], rows: list[list[str]], title: str = "") -> list[str]:
        """Format a table with aligned columns."""
        if not rows:
            return []

        lines: list[str] = []
        if title:
            lines.append(title)

        # Calculate column widths
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        # Header row
        lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        # Separator
        lines.append("  ".join("-" * w for w in widths))
        # Data rows
        for row in rows:
            lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))

        return lines


__all__ = [
    "BROWSER_ERROR_DISPLAY",
    "CODING_ERROR_DISPLAY",
    "DOCUMENT_ERROR_DISPLAY",
    "MIN_SLICE_SEC",
    "ErrorClassifier",
    "TableFormatter",
    "replay_pool_size",
    "replay_traj_target",
]
