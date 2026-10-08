"""CubeSandbox backend configuration (backend-only fields).

Host-agnostic stress params (total_count, create_batch, task_batch,
benchmark_*, browser, test, warmup, coding, document, report, workflow_type)
live on :class:`bench_core.config.KernelConfig`, read from the shared sections
of the unified YAML by :meth:`KernelConfig.from_raw`. This Config holds only the
CubeSandbox-specific knobs -- template, idle TTL, sandbox IDs file, the
``CUBE_*`` SDK env vars -- read from the ``cubesandbox:`` block of that YAML.

Credential handling: CubeSandbox auth is **optional** (CubeAPI only enforces it
when started with an auth-callback URL). ``CUBE_API_KEY`` is exported only when
the YAML supplies a non-placeholder value; a blank or
``your_cube_api_key_here`` (as shipped in the example YAMLs) is treated as
unset, so the SDK sends no ``X-API-Key`` header and behaviour is unchanged
against a non-auth backend. Unlike the e2b provider, there is no CLI-config
fallback (CubeSandbox has no ``~/.cube/config.json`` convention).

The SDK's :class:`cubesandbox.Config` reads these env vars at instantiation
time (``default_factory=lambda: os.environ.get(...)``); :meth:`setup_cube_env`
exports them before any ``Sandbox.create`` / ``list`` / ``connect`` call (the
manager does not pass ``config=``, so the SDK builds a default ``Config()`` from
the environment).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Placeholder value the example YAMLs ship for an unset API key. A user who
# copied the template verbatim would send this to the SDK as a real key; treat
# it as unset so the env var is simply not exported (cube auth is optional).
PLACEHOLDER_CREDENTIALS = frozenset({"your_cube_api_key_here"})


def _resolved_credential(explicit: str) -> str:
    """Return the credential to export, or "" to leave the env var unset.

    A blank OR placeholder explicit value counts as "not set" (the example
    YAMLs ship ``your_cube_api_key_here``), so the env var is not exported and
    the SDK sends no auth header -- the correct behaviour against a
    non-auth-enabled CubeAPI backend.
    """
    if explicit and explicit not in PLACEHOLDER_CREDENTIALS:
        return explicit
    return ""


@dataclass
class Config:
    """CubeSandbox backend configuration (backend-specific fields only)."""

    # CubeSandbox SDK environment variables. All optional: the SDK's Config
    # has sensible defaults (api_url http://127.0.0.1:3000, domain cube.app).
    cube_api_url: str = "http://127.0.0.1:3000"
    cube_api_key: str = ""  # optional (placeholder -> unset, no auth header)
    cube_template_id: str = ""  # default template ID when `template` is None
    cube_sandbox_domain: str = "cube.app"

    # Sandbox creation knob. CubeSandbox's ``Sandbox.create(timeout=...)`` sets
    # the sandbox *idle TTL* (seconds) -- NOT a create-call wait (create is a
    # synchronous HTTP POST). 86400 = 24h; the SDK's NEVER_TIMEOUT (-1) disables
    # idle expiry. Long-lived replay sandboxes want a long TTL so the sandbox
    # survives pauses between steps.
    timeout: int = 86400

    # Default template ID. None -> the SDK falls back to $CUBE_TEMPLATE_ID (set
    # by setup_cube_env from cube_template_id). The YAML block normally sets
    # this explicitly.
    template: str | None = None

    # Sandbox IDs file (for save/load sandbox IDs across runs).
    sandbox_ids_file: str | None = None

    # Snapshot dir override (Phase 2 snapshot_sizes; v1 unused -> returns None).
    # CubeSandbox's SnapshotInfo carries no size fields, so snapshot sizing must
    # stat the CubeCoW snapshot tree on the host; the path is
    # deployment-specific and deferred until a real deployment confirms it.
    snapshot_dir: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], block: str = "cubesandbox") -> Config:
        """Build from the unified YAML's backend block (``cubesandbox:``).

        ``env`` nests under the block. Missing keys fall back to the dataclass
        defaults, so a minimal block still loads.
        """
        backend = raw.get(block) or {}
        env = backend.get("env") or {}
        return cls(
            cube_api_url=env.get("CUBE_API_URL", "http://127.0.0.1:3000"),
            cube_api_key=env.get("CUBE_API_KEY", ""),
            cube_template_id=env.get("CUBE_TEMPLATE_ID", ""),
            cube_sandbox_domain=env.get("CUBE_SANDBOX_DOMAIN", "cube.app"),
            timeout=backend.get("timeout", 86400),
            template=backend.get("template"),
            sandbox_ids_file=backend.get("sandbox_ids_file"),
            snapshot_dir=backend.get("snapshot_dir"),
        )

    def setup_cube_env(self) -> None:
        """Set CubeSandbox SDK environment variables.

        Exports ``CUBE_API_URL`` / ``CUBE_SANDBOX_DOMAIN`` unconditionally
        (they have safe defaults); ``CUBE_TEMPLATE_ID`` when set (the SDK's
        default-template fallback); ``CUBE_API_KEY`` only when a non-placeholder
        value is supplied (cube auth is optional -- a blank/placeholder key is
        treated as unset so the SDK sends no ``X-API-Key`` header).
        """
        if self.cube_api_url:
            os.environ["CUBE_API_URL"] = self.cube_api_url
        if self.cube_sandbox_domain:
            os.environ["CUBE_SANDBOX_DOMAIN"] = self.cube_sandbox_domain
        if self.cube_template_id:
            os.environ["CUBE_TEMPLATE_ID"] = self.cube_template_id
        api_key = _resolved_credential(self.cube_api_key)
        if api_key:
            os.environ["CUBE_API_KEY"] = api_key
        else:
            os.environ.pop("CUBE_API_KEY", None)
