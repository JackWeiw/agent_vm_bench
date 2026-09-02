from __future__ import annotations

import os
import sys
from pathlib import Path

from env_provider.aenv._snapshot import scan_snapshot_sizes


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def test_scan_dedups_inherited_hardlinks(tmp_path: Path) -> None:
    """Hardlinked inode is counted once: its apparent size goes to inherited,
    not re-counted as unique disk in the inheriting generation."""
    # gen0: a 1 MiB file (unique inode). gen1 hardlinks it (inherited).
    gen0 = tmp_path / "g0-uuid"
    gen1 = tmp_path / "g1-uuid"
    base = gen0 / "layer.bin"
    _write(base, 1024 * 1024)
    gen1.mkdir()
    os.link(base, gen1 / "layer.bin")  # hardlink -> same inode

    out = scan_snapshot_sizes(tmp_path)
    assert out is not None
    # Newest generation (gen1, sorted last by name) sees the inode as
    # already-seen (first encountered in gen0) -> inherited.
    assert out["generations"] == 2
    # files = newest gen's file count (gen1 has 1 hardlinked file)
    assert out["files"] == 1
    # logical_bytes = apparent size in newest gen (st_size works everywhere)
    assert out["logical_bytes"] == 1024 * 1024
    # The inode-dedup invariant: hardlinked file in gen1 is inherited (not unique)
    assert out["inherited_bytes"] == 1024 * 1024
    # disk_bytes is unique-to-newest-gen physical blocks; the inode was first
    # seen in gen0, so gen1 contributes nothing unique.
    assert out["disk_bytes"] == 0

    # cumulative_bytes = sum of unique_disk across all generations.
    # On Windows, st_blocks is unavailable -> 0 everywhere; cumulative == 0.
    # On Linux, cumulative == st_blocks*512 for the single first-seen inode.
    # Either way: cumulative equals disk_bytes (gen1 unique) plus whatever
    # gen0 reported as unique for the inode -- counted exactly once.
    assert out["cumulative_bytes"] == out["disk_bytes"] + _gen0_disk_bytes(tmp_path)


def _gen0_disk_bytes(root: Path) -> int:
    """Compute what the algorithm attributes to gen0's unique disk."""
    gen0 = sorted(p for p in root.iterdir() if p.is_dir())[0]
    total = 0
    seen: set[tuple[int, int]] = set()
    for _dirpath, _dirs, files in os.walk(gen0):
        for name in files:
            full = os.path.join(_dirpath, name)
            st = os.lstat(full)
            key = (st.st_dev, st.st_ino)
            if key not in seen:
                seen.add(key)
                total += getattr(st, "st_blocks", 0) * 512
    return total


def test_scan_missing_dir_returns_none(tmp_path: Path) -> None:
    assert scan_snapshot_sizes(tmp_path / "nope") is None


def test_scan_empty_dir_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty-sandbox"
    empty.mkdir()
    assert scan_snapshot_sizes(empty) is None


# ------------------------------------------------------------------ Config + provider integration


from env_provider.aenv import AenvProvider
from env_provider.e2b.config import Config


def test_config_from_raw_reads_snapshot_dir() -> None:
    cfg = Config.from_raw({"aenv": {"snapshot_dir": "/tmp/snap"}}, block="aenv")
    assert cfg.snapshot_dir == "/tmp/snap"


def test_config_snapshot_dir_defaults_none() -> None:
    cfg = Config.from_raw({"aenv": {}}, block="aenv")
    assert cfg.snapshot_dir is None


def test_aenv_provider_snapshot_sizes_uses_dir(tmp_path, monkeypatch) -> None:
    # Build a provider with a fake config pointing at tmp_path; place a gen dir.
    cfg = Config.from_raw({"aenv": {"snapshot_dir": str(tmp_path)}}, block="aenv")
    # gen0 with one 1MiB file under <sandbox_id>/
    gen = tmp_path / "abc" / "g0"
    gen.mkdir(parents=True)
    (gen / "layer.bin").write_bytes(b"\0" * (1024 * 1024))

    # Minimal provider: bypass the E2B SDK/manager by setting _config directly.
    p = AenvProvider.__new__(AenvProvider)
    p._config = cfg

    inst = type("I", (), {"id": "abc", "index": 0})()
    out = p.snapshot_sizes(inst)
    assert out is not None
    assert out["logical_bytes"] == 1024 * 1024
    assert out["generations"] == 1


def test_resolve_snapshot_base_explicit_dir_wins(monkeypatch) -> None:
    from env_provider.aenv import _resolve_snapshot_base

    monkeypatch.setenv("AENV_HOME_PATH", "/srv/aenv")
    cfg = Config.from_raw({"aenv": {"snapshot_dir": "/explicit/snap"}}, block="aenv")
    assert _resolve_snapshot_base(cfg) == "/explicit/snap"


def test_resolve_snapshot_base_from_aenv_home_path(monkeypatch) -> None:
    from env_provider.aenv import _resolve_snapshot_base

    monkeypatch.delenv("AENV_HOME_PATH", raising=False)
    monkeypatch.delenv("AENV_HOME", raising=False)
    monkeypatch.setenv("AENV_HOME_PATH", "/srv/aenv")
    cfg = Config.from_raw({"aenv": {}}, block="aenv")
    assert _resolve_snapshot_base(cfg) == str(Path("/srv/aenv/persisted-sandboxes/artifacts"))


def test_resolve_snapshot_base_falls_back_to_aenv_home(monkeypatch) -> None:
    from env_provider.aenv import _resolve_snapshot_base

    # AENV_HOME_PATH unset -> AENV_HOME is the fallback.
    monkeypatch.delenv("AENV_HOME_PATH", raising=False)
    monkeypatch.setenv("AENV_HOME", "/data/aenv")
    cfg = Config.from_raw({"aenv": {}}, block="aenv")
    assert _resolve_snapshot_base(cfg) == str(Path("/data/aenv/persisted-sandboxes/artifacts"))


def test_resolve_snapshot_base_default_when_nothing_set(monkeypatch) -> None:
    from env_provider.aenv import DEFAULT_SNAPSHOT_DIR, _resolve_snapshot_base

    monkeypatch.delenv("AENV_HOME_PATH", raising=False)
    monkeypatch.delenv("AENV_HOME", raising=False)
    cfg = Config.from_raw({"aenv": {}}, block="aenv")
    assert _resolve_snapshot_base(cfg) == DEFAULT_SNAPSHOT_DIR


def test_aenv_provider_snapshot_sizes_from_env_home(tmp_path, monkeypatch) -> None:
    """AENV_HOME_PATH drives the scan path end-to-end (not just the resolver)."""
    from env_provider.aenv import AenvProvider

    # Point AENV_HOME_PATH at tmp_path; the provider scans
    # <tmp>/persisted-sandboxes/artifacts/<id>/<gen>/...
    monkeypatch.delenv("AENV_HOME", raising=False)
    monkeypatch.setenv("AENV_HOME_PATH", str(tmp_path))
    base = tmp_path / "persisted-sandboxes" / "artifacts" / "abc" / "g0"
    base.mkdir(parents=True)
    (base / "layer.bin").write_bytes(b"\0" * 512)

    p = AenvProvider.__new__(AenvProvider)
    p._config = Config.from_raw({"aenv": {}}, block="aenv")
    inst = type("I", (), {"id": "abc", "index": 0})()
    out = p.snapshot_sizes(inst)
    assert out is not None
    assert out["generations"] == 1
