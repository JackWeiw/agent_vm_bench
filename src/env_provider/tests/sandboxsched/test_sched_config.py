"""Tests for SandboxSched Config credential resolution and env setup.

Mirrors ``test_e2b_config.py``: ``Config.from_raw`` reads the ``sandboxsched:``
block (credentials nested under ``env:``); ``resolved_api_key`` layers
YAML/shell-env credentials (a blank OR placeholder YAML value falls back to the
``OPEN_SANDBOX_API_KEY`` env var); ``setup_env`` exports the SDK env vars.
"""

from __future__ import annotations

import os

from env_provider.sandboxsched.config import (
    PLACEHOLDER_API_KEY,
    Config,
)


class TestConfigFromRaw:
    def test_reads_block_fields(self):
        raw = {
            "sandboxsched": {
                "template": "ubuntu-22-04-custom",
                "create_timeout": 300,
                "sandbox_group": "sg-2u4g",
                "sandbox_ids_file": "ids.txt",
                "domain": "10.0.0.1:80",
                "protocol": "https",
                "env": {"OPEN_SANDBOX_API_KEY": "real-key"},
            }
        }
        cfg = Config.from_raw(raw)
        assert cfg.template == "ubuntu-22-04-custom"
        assert cfg.create_timeout == 300
        assert cfg.sandbox_group == "sg-2u4g"
        assert cfg.sandbox_ids_file == "ids.txt"
        assert cfg.domain == "10.0.0.1:80"
        assert cfg.protocol == "https"
        assert cfg.api_key == "real-key"

    def test_env_nests_under_block(self):
        raw = {"sandboxsched": {"env": {"OPEN_SANDBOX_API_KEY": "k", "OPEN_SANDBOX_DOMAIN": "h:9"}}}
        cfg = Config.from_raw(raw)
        assert cfg.api_key == "k"
        assert cfg.domain == "h:9"

    def test_domain_field_wins_over_env(self):
        raw = {
            "sandboxsched": {
                "domain": "10.0.0.1:80",
                "env": {"OPEN_SANDBOX_DOMAIN": "h:9"},
            }
        }
        assert Config.from_raw(raw).domain == "10.0.0.1:80"

    def test_missing_block_yields_defaults(self):
        cfg = Config.from_raw({})
        assert cfg.template == "python:3.11"
        assert cfg.create_timeout == 86400
        assert cfg.sandbox_group == ""
        assert cfg.skip_health_check is True
        assert cfg.api_key == ""

    def test_explicit_block_arg(self):
        cfg = Config.from_raw({"sched": {"sandbox_group": "g"}}, block="sched")
        assert cfg.sandbox_group == "g"


class TestResolvedApiKey:
    def test_real_yaml_value_wins(self, monkeypatch):
        monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "shell-key")
        cfg = Config(api_key="yaml-key")
        assert cfg.resolved_api_key() == "yaml-key"

    def test_blank_yaml_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "shell-key")
        cfg = Config(api_key="")
        assert cfg.resolved_api_key() == "shell-key"

    def test_placeholder_yaml_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "shell-key")
        cfg = Config(api_key=PLACEHOLDER_API_KEY)
        assert cfg.resolved_api_key() == "shell-key"

    def test_no_source_yields_empty(self, monkeypatch):
        monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
        cfg = Config(api_key="")
        assert cfg.resolved_api_key() == ""


class TestSetupEnv:
    def test_exports_key_and_domain(self, monkeypatch):
        monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
        monkeypatch.delenv("OPEN_SANDBOX_DOMAIN", raising=False)
        cfg = Config(api_key="k", domain="h:1")
        cfg.setup_env()
        assert os.environ["OPEN_SANDBOX_API_KEY"] == "k"
        assert os.environ["OPEN_SANDBOX_DOMAIN"] == "h:1"

    def test_placeholder_not_exported_as_key(self, monkeypatch):
        monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
        monkeypatch.delenv("OPEN_SANDBOX_DOMAIN", raising=False)
        cfg = Config(api_key=PLACEHOLDER_API_KEY, domain="h:1")
        cfg.setup_env()
        # placeholder -> not exported (no shell env either here)
        assert "OPEN_SANDBOX_API_KEY" not in os.environ
        assert os.environ["OPEN_SANDBOX_DOMAIN"] == "h:1"
