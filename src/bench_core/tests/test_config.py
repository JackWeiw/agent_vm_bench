"""KernelConfig tests (host-agnostic core fields only)."""
import pytest

from bench_core.config import KernelConfig


def test_defaults():
    c = KernelConfig()
    assert c.total_count == 100
    assert c.detect_existing is False
    assert c.create_only is False
    assert c.benchmark_mode == "fixed"
    assert c.workflow_type == "browser"
    assert c.test_duration == 600


def test_benchmark_count():
    c = KernelConfig(total_count=100, benchmark_percent=0.5)
    assert c.benchmark_count == 50


def test_benchmark_count_floor_is_one():
    c = KernelConfig(total_count=1, benchmark_percent=0.0)
    assert c.benchmark_count == 1


def test_validation_rejects_bad_workflow():
    with pytest.raises(ValueError):
        KernelConfig(workflow_type="bogus").validate()


def test_validation_rejects_bad_round_size():
    with pytest.raises(ValueError):
        KernelConfig(round_size=0).validate()


def test_create_batch_count_concurrent_when_unset():
    assert KernelConfig().create_batch_count == 1


def test_create_batch_count_rounds_up():
    c = KernelConfig(total_count=10, create_batch_size=3)
    assert c.create_batch_count == 4
