from __future__ import annotations

from env_provider import SnapshotSizeCapable, SandboxInstance


def test_snapshot_size_capable_isinstance_check() -> None:
    class FakeProvider:
        def snapshot_sizes(self, inst: SandboxInstance) -> dict | None:
            return {"logical_bytes": 0}

    p = FakeProvider()
    assert isinstance(p, SnapshotSizeCapable)


def test_non_capable_provider_not_instance() -> None:
    class Bare:
        pass

    assert not isinstance(Bare(), SnapshotSizeCapable)
