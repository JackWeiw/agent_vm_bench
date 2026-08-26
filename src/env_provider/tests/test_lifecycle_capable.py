"""Tests for the LifecycleCapable Protocol + default_replay_mode contract."""
from __future__ import annotations

from env_provider import EnvironmentProvider, LifecycleCapable, SandboxInstance


class _Capable(EnvironmentProvider):
    """Minimal provider implementing pause/resume (structural Protocol)."""

    name = "capable"

    def create_all(self):
        return {}

    def detect_existing(self):
        return {}

    def check_alive(self, inst):
        return True

    def cleanup_all(self):
        return None

    def prepare(self, inst):
        return None

    def exec(self, inst, command, *, timeout=None, cwd=None, env=None):
        return None

    def pause(self, inst: SandboxInstance) -> None:
        return None

    def resume(self, inst: SandboxInstance) -> None:
        return None


class _LifecycleProvider(EnvironmentProvider):
    """Provider that overrides default_replay_mode to 'lifecycle'."""

    name = "lifecycle_provider"
    default_replay_mode = "lifecycle"

    def create_all(self):
        return {}

    def detect_existing(self):
        return {}

    def check_alive(self, inst):
        return True

    def cleanup_all(self):
        return None

    def prepare(self, inst):
        return None

    def exec(self, inst, command, *, timeout=None, cwd=None, env=None):
        return None


class _Plain(EnvironmentProvider):
    """Provider without pause/resume (does not satisfy LifecycleCapable)."""

    name = "plain"

    def create_all(self):
        return {}

    def detect_existing(self):
        return {}

    def check_alive(self, inst):
        return True

    def cleanup_all(self):
        return None

    def prepare(self, inst):
        return None

    def exec(self, inst, command, *, timeout=None, cwd=None, env=None):
        return None


def test_lifecycle_capable_isinstance_structural():
    capable = _Capable()
    plain = _Plain()
    assert isinstance(capable, LifecycleCapable)
    assert not isinstance(plain, LifecycleCapable)


def test_default_replay_mode_on_base():
    assert EnvironmentProvider.default_replay_mode == "exec_only"


def test_capable_inherits_default():
    assert _Capable.default_replay_mode == "exec_only"


def test_provider_can_override_default_replay_mode():
    assert _LifecycleProvider.default_replay_mode == "lifecycle"
