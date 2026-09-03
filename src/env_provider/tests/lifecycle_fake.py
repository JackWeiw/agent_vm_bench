"""FakeLifecycleProvider -- a FakeProvider that satisfies LifecycleCapable.

Used by replay lifecycle e2e/runner tests (no live SDK). pause/resume sleep a
small, fixed duration so resume_sec/pause_sec are measurably non-zero, and
record call counts so tests can assert idempotency and warmup-stays-exec-only.
"""
from __future__ import annotations

import time

from env_provider import SandboxInstance
from env_provider.fake import FakeProvider

# Small fixed cost so lifecycle overhead is measurable but tests stay fast.
_LIFECYCLE_DELAY = 0.02


class FakeLifecycleProvider(FakeProvider):
    """FakeProvider with sleep-based pause/resume + call counters."""

    default_replay_mode = "lifecycle"

    def __init__(self, count: int = 2) -> None:
        super().__init__(count=count)
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self, inst: SandboxInstance) -> None:
        self.pause_calls += 1
        time.sleep(_LIFECYCLE_DELAY)

    def resume(self, inst: SandboxInstance) -> None:
        self.resume_calls += 1
        time.sleep(_LIFECYCLE_DELAY)
