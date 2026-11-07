"""SandboxSched backend configuration (backend-only fields).

Host-agnostic stress params (total_count, create_batch, task_batch,
benchmark_*, browser, test, warmup, coding, document, report, workflow_type)
live on :class:`bench_core.config.KernelConfig`, read from the shared sections
of the unified YAML by :meth:`KernelConfig.from_raw`. This Config holds only
the sandboxsched-specific knobs -- the OpenSandbox server address / API key,
the mandatory ``sandbox_group`` (``extensions.sandboxGroup``), the image /
template, the create timeout, and the sandbox IDs file -- read from the
``sandboxsched:`` block of that YAML.

Credential fallback: when ``api_key`` is blank OR holds a template placeholder
(``your_opensandbox_api_key_here``, as shipped in the example YAMLs) on this
Config, :meth:`setup_env` falls back to the ``OPEN_SANDBOX_API_KEY`` env var
already exported in the shell (the SDK reads it via ``ConnectionConfig.
get_api_key``), so a user can authenticate via the shell without repeating the
key in YAML -- and a copied template still falls back rather than sending the
placeholder to the SDK as a real key.

The OpenSandbox SDK (``opensandbox`` package, PyPI ``opensandbox==1.2.0``,
declared as the ``sandboxsched`` optional extra) exposes an async client
(``opensandbox.Sandbox`` / ``opensandbox.manager.SandboxManager``) driven from
sync code through the manager's bridge loop; this Config just builds a
:class:`opensandbox.config.ConnectionConfig`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Placeholder value the example YAMLs ship for an unset API key
# (config/common/*.yaml). A user who copied the template verbatim would send
# this to the OpenSandbox SDK as a real key; treat it as unset so the shell
# env var (OPEN_SANDBOX_API_KEY) fills the gap instead.
PLACEHOLDER_API_KEY = "your_opensandbox_api_key_here"

# Env var the OpenSandbox SDK reads for the API key (ConnectionConfig.
# _ENV_API_KEY). setup_env exports it so the SDK's ConnectionConfig picks
# it up transparently without the manager passing api_key= explicitly.
_ENV_API_KEY = "OPEN_SANDBOX_API_KEY"
# Env var the OpenSandbox SDK reads for the server domain.
_ENV_DOMAIN = "OPEN_SANDBOX_DOMAIN"


@dataclass
class Config:
    """SandboxSched backend configuration (backend-specific fields only)."""

    # OpenSandbox server connection. ``domain`` is the ``<host>:<port>`` of the
    # OpenSandbox Server (Chart default Service port 80). ``protocol`` is http
    # for a plain port-forward / in-cluster address, https for TLS.
    api_key: str = ""
    domain: str = "localhost:8080"
    protocol: str = "http"

    # SandboxGroup the SDK routes creates to (extensions.sandboxGroup --
    # REQUIRED by the OpenSandbox Server; a missing group returns 400).
    sandbox_group: str = ""

    # Sandbox image / template. E2B runtime: an E2B template name
    # (e.g. ``ubuntu-22-04-custom``). containerd runtime: a container image
    # reference (e.g. ``python:3.11``). Passed as the positional ``image`` arg
    # to ``Sandbox.create``.
    template: str = "python:3.11"

    # Sandbox creation timeout (seconds) -> timedelta for Sandbox.create.
    create_timeout: int = 86400

    # Optional resource dict (e.g. {"cpu": "1", "memory": "2Gi"}) forwarded to
    # Sandbox.create as ``resource=``. None -> SDK default
    # ({"cpu": "1", "memory": "2Gi"}).
    resource: dict[str, str] | None = None

    # Sandbox IDs file (for save/load sandbox IDs across runs).
    sandbox_ids_file: str | None = None

    # How long (seconds) the SDK waits for the sandbox's execd/egress endpoints
    # to publish after the create POST (and for connect/resume readiness). The
    # SDK default is 30s -- fine for a single create, but the kernel brings up
    # a fleet concurrently and endpoint publication queues behind the rest of
    # the batch, so budget generously.
    ready_timeout: int = 300

    # Whether sandbox-level requests (command exec, file ops) go through the
    # OpenSandbox Server proxy instead of hitting the sandbox's execd endpoint
    # directly. Enable when the resolved sandbox endpoints are not routable
    # from the bench machine (port-forward / NAT / wildcard-DNS setups);
    # direct access is the SDK default.
    use_server_proxy: bool = False

    # Whether to skip the SDK's built-in health check at create/connect. The
    # kernel's own ReadyChecker (uname probe for replay/coding) is the source
    # of truth for readiness, so skip the SDK's ping-based check to avoid a
    # double wait. The SDK default is skip_health_check=False; we default True.
    skip_health_check: bool = True

    @classmethod
    def from_raw(cls, raw: dict[str, Any], block: str = "sandboxsched") -> Config:
        """Build from the unified YAML's backend block (``sandboxsched:``).

        ``block`` selects which top-level YAML key to read. ``env`` nests under
        the block (mirrors the e2b/aenv convention: credentials live under
        ``env:`` so a copied template's placeholder is obvious). Missing keys
        fall back to the dataclass defaults, so a minimal block still loads.
        """
        backend = raw.get(block) or {}
        env = backend.get("env") or {}
        api_key = env.get("OPEN_SANDBOX_API_KEY", "")
        # ``domain:`` (the explicit field) wins over ``env.OPEN_SANDBOX_DOMAIN``
        # -- the env entry mirrors the value into the SDK's own env-var
        # fallback, but a user uncommenting ``domain:`` expects it to take
        # effect, not to be shadowed by a stale env value.
        domain = backend.get("domain") or env.get("OPEN_SANDBOX_DOMAIN", "localhost:8080")
        return cls(
            api_key=api_key,
            domain=domain,
            protocol=str(backend.get("protocol", "http")).lower(),
            sandbox_group=backend.get("sandbox_group", ""),
            template=backend.get("template", "python:3.11"),
            create_timeout=int(backend.get("create_timeout", 86400)),
            resource=backend.get("resource"),
            sandbox_ids_file=backend.get("sandbox_ids_file"),
            skip_health_check=bool(backend.get("skip_health_check", True)),
            ready_timeout=int(backend.get("ready_timeout", 300)),
            use_server_proxy=bool(backend.get("use_server_proxy", False)),
        )

    def resolved_api_key(self) -> str:
        """The API key to use: this Config's value, else the shell env var.

        A blank OR placeholder value (the example YAML ships
        ``your_opensandbox_api_key_here``) counts as "not set", so the shell
        env var (``OPEN_SANDBOX_API_KEY``) fills the gap instead of being
        shadowed by the placeholder. Returns "" when neither source supplies
        one -- the SDK then raises a clear auth error at first create.
        """
        if self.api_key and self.api_key != PLACEHOLDER_API_KEY:
            return self.api_key
        return os.environ.get(_ENV_API_KEY, "")

    def setup_env(self) -> None:
        """Export OpenSandbox SDK environment variables.

        Credentials fall back to the shell env var when not set on this Config
        (YAML), so a user can authenticate via ``export OPEN_SANDBOX_API_KEY=``
        in the shell without repeating the key in YAML. A blank OR placeholder
        value counts as "not set". The domain is exported the same way so the
        SDK's ``ConnectionConfig.get_domain`` picks it up.
        """
        api_key = self.resolved_api_key()
        if api_key:
            os.environ[_ENV_API_KEY] = api_key
        if self.domain:
            os.environ[_ENV_DOMAIN] = self.domain
