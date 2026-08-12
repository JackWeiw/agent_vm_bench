"""Tests for the RoundRobinTaskManager orchestrator (host-agnostic).

The round-robin manager rotates sandbox execution across rounds. Each round
launches one one-shot ``TabOperationRunner`` per sandbox in the round's group;
those runners exit after a single tab cycle, so a 2-round run with a
:class:`FakeProvider` completes in milliseconds. The bench-core StatsCollector
exposes a polymorphic ``set_round`` + ``_take_snapshot`` path that the manager
calls directly, so we construct a collector without starting its background
thread (only its ``start_time`` is seeded for sane elapsed values).
"""
from __future__ import annotations

import threading
import time

from bench_core.config import KernelConfig
from bench_core.provider import CreationMetrics, SandboxStatus
from bench_core.round_robin import RoundRobinTaskManager
from bench_core.schemas import BenchSandbox
from bench_core.stats_collector import StatsCollector
from bench_core.tests.fake_provider import FakeProvider


def _ready_sbx(i: int) -> BenchSandbox:
    return BenchSandbox(
        id=f"f-{i}",
        index=i,
        ready=True,
        is_alive=True,
        creation_metrics=CreationMetrics(status=SandboxStatus.READY),
    )


def _states(n: int) -> dict[int, BenchSandbox]:
    return {i: _ready_sbx(i) for i in range(n)}


def _new_stats(config: KernelConfig, states: dict[int, BenchSandbox]) -> StatsCollector:
    """Collector with no background thread; start_time seeded for sane elapsed."""
    stats = StatsCollector(config, states, "fake")
    stats.start_time = time.time()
    return stats


class TestPrepareSandboxGroups:
    def test_even_split(self):
        config = KernelConfig(workflow_type="browser", round_size=2, browser_urls=["http://x"])
        states = _states(4)
        mgr = RoundRobinTaskManager(config, states, threading.Event(), _new_stats(config, states), FakeProvider())

        mgr._prepare_sandbox_groups()

        assert len(mgr.sandbox_groups) == 2
        assert [len(g) for g in mgr.sandbox_groups] == [2, 2]

    def test_remainder_distributed_front_loaded(self):
        config = KernelConfig(workflow_type="browser", round_size=2, browser_urls=["http://x"])
        states = _states(5)
        mgr = RoundRobinTaskManager(config, states, threading.Event(), _new_stats(config, states), FakeProvider())

        mgr._prepare_sandbox_groups()

        assert len(mgr.sandbox_groups) == 3  # ceil(5/2)
        assert [len(g) for g in mgr.sandbox_groups] == [2, 2, 1]

    def test_empty_when_none_ready(self):
        config = KernelConfig(workflow_type="browser", round_size=2, browser_urls=["http://x"])
        states = _states(3)
        for s in states.values():
            s.ready = False
        mgr = RoundRobinTaskManager(config, states, threading.Event(), _new_stats(config, states), FakeProvider())

        mgr._prepare_sandbox_groups()

        assert mgr.sandbox_groups == []
        assert mgr.all_ready_states == []


class TestCalculateRounds:
    def test_explicit_round_count(self):
        config = KernelConfig(workflow_type="browser", round_count=3, browser_urls=["http://x"])
        mgr = RoundRobinTaskManager(
            config, _states(2), threading.Event(), _new_stats(config, _states(2)), FakeProvider()
        )
        assert mgr._calculate_rounds() == 3

    def test_none_falls_back_to_large_sentinel(self):
        # round_count=None relies on the duration check in run() to stop.
        config = KernelConfig(workflow_type="browser", round_count=None, browser_urls=["http://x"])
        mgr = RoundRobinTaskManager(
            config, _states(2), threading.Event(), _new_stats(config, _states(2)), FakeProvider()
        )
        assert mgr._calculate_rounds() == 10000


class TestRunTwoRoundsBrowser:
    def test_runs_two_rounds_and_records_baselines(self):
        config = KernelConfig(
            workflow_type="browser",
            round_count=2,
            round_size=2,
            round_interval=0,
            test_duration=60,
            browser_urls=["http://x"],
        )
        states = _states(5)
        stats = _new_stats(config, states)
        mgr = RoundRobinTaskManager(config, states, threading.Event(), stats, FakeProvider(count=5))

        mgr.run()

        # Last round_id executed was 1 (0-indexed).
        assert mgr.current_round == 1
        # Start baselines: round 0 (start), round 1 (end boundary of round 0),
        # round 2 (end boundary of round 1). set_round is idempotent, so the
        # _start_round call for round 1 does not overwrite the boundary.
        assert {0, 1, 2} <= set(stats._round_start_totals.keys())
        # Both rounds took at least one snapshot each in _stop_round.
        assert 0 in stats.round_snapshots
        assert 1 in stats.round_snapshots

    def test_stops_on_stop_event(self):
        config = KernelConfig(
            workflow_type="browser",
            round_count=5,
            round_size=2,
            round_interval=0,
            test_duration=60,
            browser_urls=["http://x"],
        )
        states = _states(4)
        stop = threading.Event()
        stop.set()  # set before run -> loop exits at the first iteration guard
        stats = _new_stats(config, states)
        mgr = RoundRobinTaskManager(config, states, stop, stats, FakeProvider(count=4))

        mgr.run()

        # No round was ever started.
        assert mgr.current_round == 0
        assert stats._round_start_totals == {}

    def test_no_groups_returns_early(self):
        config = KernelConfig(
            workflow_type="browser",
            round_count=2,
            round_size=2,
            browser_urls=["http://x"],
        )
        states = _states(3)
        for s in states.values():
            s.ready = False
        stats = _new_stats(config, states)
        mgr = RoundRobinTaskManager(config, states, threading.Event(), stats, FakeProvider(count=3))

        mgr.run()

        assert mgr.sandbox_groups == []
        assert stats._round_start_totals == {}
