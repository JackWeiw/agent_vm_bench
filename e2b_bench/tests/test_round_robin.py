"""
Test Round-Robin Task Manager Module

Tests for RoundRobinTaskManager: group preparation, round calculation, and termination logic.
"""

import threading
import unittest

from e2b_bench.config import Config
from e2b_bench.round_robin import RoundRobinTaskManager
from e2b_bench.schemas import BrowserMetrics, CreationMetrics, SandboxState, SandboxStatus
from e2b_bench.stats_collector import StatsCollector


def _make_ready_state(sandbox_id: int) -> SandboxState:
    """Create a SandboxState with PORT_READY status for testing."""
    state = SandboxState(sandbox_id=sandbox_id)
    state.creation_metrics = CreationMetrics(status=SandboxStatus.PORT_READY)
    return state


def _make_manager(config: Config, num_ready: int) -> RoundRobinTaskManager:
    """Create a RoundRobinTaskManager with N ready sandbox states."""
    sandbox_states = {}
    for i in range(1, num_ready + 1):
        sandbox_states[i] = _make_ready_state(i)

    stop_event = threading.Event()
    stats_collector = StatsCollector(config, sandbox_states)
    manager = RoundRobinTaskManager(config, sandbox_states, stop_event, stats_collector)
    return manager


class TestPrepareSandboxGroups(unittest.TestCase):
    """Tests for _prepare_sandbox_groups logic"""

    def test_prepare_groups_with_round_size_5(self):
        """20 sandboxes with round_size=5 → 4 groups of 5"""
        config = Config(benchmark_mode="round_robin", round_size=5)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == 4
        for group in manager.sandbox_groups:
            assert len(group) == 5

    def test_prepare_groups_round_size_not_divisible(self):
        """23 sandboxes with round_size=5 → ceil(23/5)=5 groups, distribution [5,5,5,5,3]"""
        config = Config(benchmark_mode="round_robin", round_size=5)
        manager = _make_manager(config, num_ready=23)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == 5
        sizes = [len(g) for g in manager.sandbox_groups]
        # Distribution: 23 ÷ 5 groups = base 4, remainder 3 → first 3 groups get 5, last 2 get 4
        assert sizes == [5, 5, 5, 4, 4]
        # All sandboxes covered
        assert sum(sizes) == 23

    def test_prepare_groups_round_size_1(self):
        """round_size=1 → each sandbox is its own group"""
        config = Config(benchmark_mode="round_robin", round_size=1)
        manager = _make_manager(config, num_ready=10)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == 10
        for group in manager.sandbox_groups:
            assert len(group) == 1

    def test_prepare_groups_round_size_greater_than_total(self):
        """round_size=100 with 20 sandboxes → 1 group of 20"""
        config = Config(benchmark_mode="round_robin", round_size=100)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == 1
        assert len(manager.sandbox_groups[0]) == 20

    def test_prepare_groups_round_count_does_not_affect_groups(self):
        """round_count=3 with round_size=5 → groups determined by round_size, not round_count"""
        config = Config(benchmark_mode="round_robin", round_size=5, round_count=3)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        # Group count is determined by round_size only: ceil(20/5) = 4
        # round_count only controls max rounds (termination), not group count
        assert len(manager.sandbox_groups) == 4  # NOT 3

    def test_prepare_groups_no_round_size_default(self):
        """No round_size (or round_size=0) → min(total, 10) groups"""
        # round_size=0 triggers fallback to default
        config = Config(benchmark_mode="round_robin", round_size=0)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == min(20, 10)  # 10 groups

    def test_prepare_groups_no_round_size_small_total(self):
        """No round_size with fewer than 10 sandboxes → total groups"""
        config = Config(benchmark_mode="round_robin", round_size=0)
        manager = _make_manager(config, num_ready=5)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == 5  # min(5, 10) = 5

    def test_prepare_groups_no_ready_sandboxes(self):
        """0 ready sandboxes → empty groups"""
        config = Config(benchmark_mode="round_robin", round_size=5)
        manager = _make_manager(config, num_ready=0)
        manager._prepare_sandbox_groups()

        assert len(manager.sandbox_groups) == 0

    def test_prepare_groups_all_sandboxes_covered(self):
        """All sandboxes appear in exactly one group"""
        config = Config(benchmark_mode="round_robin", round_size=5)
        manager = _make_manager(config, num_ready=17)
        manager._prepare_sandbox_groups()

        # ceil(17/5) = 4 groups
        assert len(manager.sandbox_groups) == 4
        total_covered = sum(len(g) for g in manager.sandbox_groups)
        assert total_covered == 17

        # No duplicate sandbox IDs
        all_ids = [s.sandbox_id for g in manager.sandbox_groups for s in g]
        assert len(all_ids) == len(set(all_ids))


class TestCalculateRounds(unittest.TestCase):
    """Tests for _calculate_rounds logic"""

    def test_calculate_rounds_with_round_count(self):
        """round_count=5 → returns 5"""
        config = Config(benchmark_mode="round_robin", round_size=5, round_count=5)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        assert manager._calculate_rounds() == 5

    def test_calculate_rounds_no_round_count(self):
        """round_count=None → returns 10000 (duration-based termination)"""
        config = Config(benchmark_mode="round_robin", round_size=5)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        assert manager._calculate_rounds() == 10000

    def test_calculate_rounds_round_count_zero(self):
        """round_count=0 → treated as not specified, returns 10000"""
        config = Config(benchmark_mode="round_robin", round_size=5, round_count=0)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        assert manager._calculate_rounds() == 10000

    def test_calculate_rounds_round_count_with_round_size(self):
        """Both round_count and round_size coexist — round_count determines max rounds"""
        config = Config(benchmark_mode="round_robin", round_size=3, round_count=7)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        # Group count determined by round_size: ceil(20/3) = 7
        assert len(manager.sandbox_groups) == 7
        # Max rounds determined by round_count
        assert manager._calculate_rounds() == 7

    def test_calculate_rounds_round_count_less_than_groups(self):
        """round_count < number of groups → stops early, some groups not visited"""
        config = Config(benchmark_mode="round_robin", round_size=5, round_count=3)
        manager = _make_manager(config, num_ready=20)
        manager._prepare_sandbox_groups()

        # 4 groups but only 3 rounds planned
        assert len(manager.sandbox_groups) == 4
        assert manager._calculate_rounds() == 3


if __name__ == "__main__":
    unittest.main()
