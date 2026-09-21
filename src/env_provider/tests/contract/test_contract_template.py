from __future__ import annotations

import inspect

from env_provider import EnvironmentProvider, SandboxInstance


def test_sandbox_instance_has_template_field():
    inst = SandboxInstance(id="x", index=0)
    assert inst.template is None
    inst2 = SandboxInstance(id="y", index=1, template="swb-a")
    assert inst2.template == "swb-a"


def test_create_all_accepts_templates_kwarg():
    sig = inspect.signature(EnvironmentProvider.create_all)
    assert "templates" in sig.parameters
    assert sig.parameters["templates"].default is None


def test_create_one_accepts_template_kwarg():
    from env_provider import EphemeralCapable

    sig = inspect.signature(EphemeralCapable.create_one)
    assert "template" in sig.parameters
    assert sig.parameters["template"].default is None
