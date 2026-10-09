from __future__ import annotations


def test_base_default_is_none():
    from env_provider import EnvironmentProvider

    assert EnvironmentProvider.vmm_type is None


def test_fake_provider_has_no_vmm():
    from env_provider.fake import FakeProvider

    assert FakeProvider.vmm_type is None


def test_e2b_provider_is_firecracker():
    e2b = __import__("pytest").importorskip("e2b")  # noqa: F841
    from env_provider.e2b import E2BProvider

    assert E2BProvider.vmm_type == "firecracker"


def test_aenv_inherits_firecracker():
    __import__("pytest").importorskip("e2b")
    from env_provider.aenv import AenvProvider

    assert AenvProvider.vmm_type == "firecracker"


def test_cubesandbox_provider_is_cubesandbox():
    # cubesandbox SDK is optional; the provider module is importable without it
    # (the Sandbox import is mocked in __init__), so this needs no importorskip.
    from env_provider.cubesandbox import CubesandboxProvider

    assert CubesandboxProvider.vmm_type == "cubesandbox"
