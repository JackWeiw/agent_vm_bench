"""E2B backend configuration (backend-only fields).

Host-agnostic stress params (total_count, create_batch, task_batch,
benchmark_*, browser, test, warmup, coding, document, report, workflow_type)
live on :class:`bench_core.config.KernelConfig`, read from the shared sections
of the unified YAML by :meth:`KernelConfig.from_raw`. This Config holds only the
e2b-specific knobs -- template, NUMA binding, sandbox IDs file, E2B SDK env --
read from the ``e2b:`` block of that YAML.

Credential fallback: when ``e2b_access_token`` / ``e2b_api_key`` are empty on
this Config, :meth:`setup_e2b_env` reads them from the E2B CLI config
(``~/.e2b/config.json``), so a user can authenticate via the CLI config without
repeating key/token in YAML. Mirrors ``e2b_bench/scripts/delete_sandbox.sh``.

Legacy ``e2b_bench`` keeps its own ``e2b_bench/config.py`` (with the old-schema
load_from_yaml / merge_with_args / from_args CLI flow and the full _FieldSpec
table); this module is the src kernel's backend config and is consumed only by
:func:`env_provider.e2b.build_provider`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _normalize_numa_bind(value: Any) -> list[int] | None:
    """Normalize numa_bind input to a canonical list of node IDs or None.

    Accepts an int (single node), a list of ints, None, or an empty string.
    Returns None (no binding) for None / empty / all-negative input. NUMA
    node 0 is valid and kept. Negative node IDs are dropped. Duplicate IDs
    are removed, preserving first-seen order.
    """
    # Treat empty string as "no binding" (defensive; YAML null is the norm)
    if value is None or value == "" or value == []:
        return None

    # Single int -> singleton list
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            return None
        return [value]

    # List of ints: dedup preserving order, drop negative (node 0 is valid)
    if isinstance(value, list):
        seen = set()
        normalized: list[int] = []
        for item in value:
            # bool is a subclass of int; reject it explicitly
            if not isinstance(item, int) or isinstance(item, bool):
                raise TypeError(f"numa_bind list items must be ints, got {type(item).__name__}")
            if item < 0:
                continue
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized if normalized else None

    raise TypeError(f"numa_bind must be int, list[int], or null, got {type(value).__name__}")


def numa_node_for_index(index: int, nodes: int | list[int] | None) -> int | None:
    """Return the NUMA node for a sandbox at the given 0-based index.

    Round-robins across `nodes`. Accepts a list of ints, a single int (treated
    as a one-element list), or None (no binding). NUMA node 0 is valid. Returns
    None when `nodes` is None, an empty list, an empty string, or a negative
    int. Raises TypeError for other types, including bool.
    """
    if nodes is None or nodes == "" or nodes == []:
        return None

    # bool is a subclass of int; reject it explicitly (consistent with
    # _normalize_numa_bind) rather than letting len() fail later.
    if isinstance(nodes, bool):
        raise TypeError(f"numa_bind nodes must be int, list[int], or null, got {type(nodes).__name__}")

    # Tolerate a raw int (constructor passthrough) by treating it as a single node
    if isinstance(nodes, int):
        return nodes if nodes >= 0 else None

    return nodes[index % len(nodes)]


# Default E2B CLI config path. The E2B_CONFIG env var overrides it (mirrors
# e2b_bench/scripts/delete_sandbox.sh), so tests / alternate installs can point
# elsewhere without touching the real ~/.e2b/config.json.
DEFAULT_E2B_CONFIG_PATH = str(Path.home() / ".e2b" / "config.json")


def _load_e2b_cli_config(path: str | None = None) -> dict[str, str]:
    """Read the E2B CLI config (~/.e2b/config.json) for credentials.

    Mirrors ``e2b_bench/scripts/delete_sandbox.sh``: the path defaults to
    ``~/.e2b/config.json`` and is overridable via the ``E2B_CONFIG`` env var.
    Returns ``{"api_key": ..., "access_token": ...}``; each is ``""`` when the
    file is absent, unreadable, or the key is missing, so callers fall back to
    it transparently without raising.
    """
    cfg_path = path or os.environ.get("E2B_CONFIG") or DEFAULT_E2B_CONFIG_PATH
    creds: dict[str, str] = {"api_key": "", "access_token": ""}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = json.load(f)
        creds["api_key"] = str(data.get("teamApiKey") or "")
        creds["access_token"] = str(data.get("accessToken") or "")
    except (OSError, ValueError):
        # File absent / unreadable / bad JSON -> stay empty; setup_e2b_env then
        # just leaves the env vars unset (same behavior as before this fallback).
        pass
    return creds


@dataclass
class Config:
    """E2B backend configuration (backend-specific fields only)."""

    # E2B SDK environment variables (key/token fall back to ~/.e2b/config.json
    # in setup_e2b_env when left empty here).
    e2b_access_token: str = ""
    e2b_api_key: str = ""
    e2b_domain: str = "e2b.app"
    e2b_api_url: str = "http://localhost:3000"
    e2b_http_ssl: str = "false"

    # Sandbox creation knobs
    template: str = "openclaw-browser-v1"
    create_timeout: int = 86400  # Sandbox creation timeout (seconds)

    # NUMA binding for sandbox creation. Accepts an int (single node), a list of
    # ints (round-robin across nodes), or null (no binding). Defaults to node 2.
    # Normalized to a list or None by from_raw (see _normalize_numa_bind); a bare
    # Config() leaves the int default, which numa_node_for_index tolerates.
    numa_bind: int | list[int] | None = 2

    # Sandbox IDs file (for save/load sandbox IDs across runs)
    sandbox_ids_file: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Config:
        """Build from the unified YAML's ``e2b:`` block.

        ``env`` nests under ``e2b:`` in the unified schema (E2B SDK env vars).
        ``numa_bind`` is normalized to a canonical list/None. Missing keys fall
        back to the dataclass defaults, so a minimal ``e2b:`` block still loads.
        """
        e2b = raw.get("e2b") or {}
        env = e2b.get("env") or {}
        return cls(
            e2b_access_token=env.get("E2B_ACCESS_TOKEN", ""),
            e2b_api_key=env.get("E2B_API_KEY", ""),
            e2b_domain=env.get("E2B_DOMAIN", "e2b.app"),
            e2b_api_url=env.get("E2B_API_URL", "http://localhost:3000"),
            e2b_http_ssl=env.get("E2B_HTTP_SSL", "false"),
            template=e2b.get("template", "openclaw-browser-v1"),
            create_timeout=e2b.get("create_timeout", 86400),
            numa_bind=_normalize_numa_bind(e2b.get("numa_bind", 2)),
            sandbox_ids_file=e2b.get("sandbox_ids_file"),
        )

    def setup_e2b_env(self) -> None:
        """Set E2B SDK environment variables.

        Credentials fall back to the E2B CLI config (~/.e2b/config.json) when not
        set on this Config (YAML/CLI), so a user can authenticate via the CLI
        config without repeating key/token in YAML. Mirrors
        ``e2b_bench/scripts/delete_sandbox.sh``.
        """
        file_creds = _load_e2b_cli_config()
        token = self.e2b_access_token or file_creds["access_token"]
        api_key = self.e2b_api_key or file_creds["api_key"]
        if token:
            os.environ["E2B_ACCESS_TOKEN"] = token
        if api_key:
            os.environ["E2B_API_KEY"] = api_key
        if self.e2b_domain:
            os.environ["E2B_DOMAIN"] = self.e2b_domain
        if self.e2b_api_url:
            os.environ["E2B_API_URL"] = self.e2b_api_url
        if self.e2b_http_ssl:
            os.environ["E2B_HTTP_SSL"] = self.e2b_http_ssl
