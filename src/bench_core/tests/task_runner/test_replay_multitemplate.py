"""Tests for multi-template replay support (FakeProvider contract).

These tests verify that the FakeProvider correctly handles template parameters
in both create_all (batch mode) and create_one (ephemeral/trajectory mode).
"""
from __future__ import annotations

from env_provider.fake import FakeProvider


def test_fake_create_all_sets_template_per_slot():
    """create_all with templates dict sets template on each SandboxInstance."""
    provider = FakeProvider(count=3)
    instances = provider.create_all(templates={0: "swb-a", 1: "swb-b", 2: "swb-a"})

    assert instances[0].template == "swb-a"
    assert instances[1].template == "swb-b"
    assert instances[2].template == "swb-a"


def test_fake_create_all_without_templates_leaves_template_none():
    """create_all without templates parameter leaves template as None."""
    provider = FakeProvider()
    instances = provider.create_all()

    for inst in instances.values():
        assert inst.template is None


def test_fake_create_one_accepts_template():
    """create_one accepts template parameter and sets it on the instance."""
    provider = FakeProvider()
    inst = provider.create_one(5, template="swb-x", metadata={"trajectory_id": "t1"})

    assert inst.template == "swb-x"
    assert inst.index == 5


def test_fake_create_one_without_template_leaves_template_none():
    """create_one without template parameter leaves template as None."""
    provider = FakeProvider()
    inst = provider.create_one(5, metadata={"trajectory_id": "t1"})

    assert inst.template is None
