"""Tests for the EphemeralCapable protocol + FakeProvider implementation."""
from __future__ import annotations

from env_provider import EphemeralCapable, SandboxInstance, SandboxStatus
from env_provider import CreationMetrics
from env_provider.fake import FakeProvider

from env_provider.tests.test_lifecycle_capable import _Plain


def test_fake_provider_satisfies_ephemeral_capable():
    provider = FakeProvider(count=0)
    assert isinstance(provider, EphemeralCapable)


def test_create_one_returns_ready_instance_with_metadata():
    provider = FakeProvider(count=0)
    inst = provider.create_one(7, metadata={"trajectory_id": "traj-42"})
    assert isinstance(inst, SandboxInstance)
    assert inst.index == 7
    assert inst.ready is True
    assert inst.is_alive is True
    assert inst.creation_metrics.status == SandboxStatus.READY
    # metadata is carried through for operator visibility (not an idempotency key)
    assert provider._meta_log[7] == {"trajectory_id": "traj-42"}


def test_kill_one_clears_handle_and_marks_dead():
    provider = FakeProvider(count=0)
    inst = provider.create_one(3)
    assert provider.check_alive(inst)
    provider.kill_one(inst)
    assert inst.is_alive is False


def test_kill_one_unknown_index_is_noop_safe():
    provider = FakeProvider(count=0)
    # A shell whose create never ran must not raise on kill (runner finally-path).
    shell = SandboxInstance(id="ghost", index=99, ready=False, is_alive=True)
    provider.kill_one(shell)  # must not raise


def test_plain_provider_does_not_satisfy_ephemeral_capable():
    provider = _Plain()
    assert not isinstance(provider, EphemeralCapable)
