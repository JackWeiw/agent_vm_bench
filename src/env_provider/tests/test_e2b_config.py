"""Tests for E2B Config credential resolution and the CLI-config fallback.

``_load_e2b_cli_config`` reads ``~/.e2b/config.json`` (the E2B CLI's config),
mirroring ``e2b_bench/scripts/delete_sandbox.sh``: ``teamApiKey`` -> API key,
``accessToken`` -> access token, path overridable via ``E2B_CONFIG``. It must
never raise on a missing / unreadable file; callers fall back transparently.

``setup_e2b_env`` layers YAML/CLI credentials over that file: explicit Config
values win, the file fills the gaps, and absent credentials leave the SDK env
vars unset (same as before this fallback existed).
"""
from __future__ import annotations

import json
import os

from env_provider.e2b.config import Config, _load_e2b_cli_config


class TestLoadE2BCliConfig:
    def test_reads_team_api_key_and_access_token(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"teamApiKey": "key-123", "accessToken": "tok-456"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg))

        creds = _load_e2b_cli_config()

        assert creds == {"api_key": "key-123", "access_token": "tok-456"}

    def test_missing_file_yields_empty(self, tmp_path, monkeypatch):
        # No file at the path -> empty creds, not an exception.
        monkeypatch.setenv("E2B_CONFIG", str(tmp_path / "absent.json"))

        assert _load_e2b_cli_config() == {"api_key": "", "access_token": ""}

    def test_bad_json_yields_empty(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg))

        assert _load_e2b_cli_config() == {"api_key": "", "access_token": ""}

    def test_missing_keys_yield_empty_strings(self, tmp_path, monkeypatch):
        # A valid file lacking the credential keys -> empty strings (not None),
        # so the ``or`` fallback in setup_e2b_env works uniformly.
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"other": "stuff"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg))

        assert _load_e2b_cli_config() == {"api_key": "", "access_token": ""}

    def test_explicit_path_overrides_env(self, tmp_path, monkeypatch):
        env_cfg = tmp_path / "env.json"
        env_cfg.write_text(json.dumps({"teamApiKey": "env-key"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(env_cfg))

        explicit_cfg = tmp_path / "explicit.json"
        explicit_cfg.write_text(
            json.dumps({"teamApiKey": "explicit-key", "accessToken": "explicit-tok"}), encoding="utf-8"
        )

        creds = _load_e2b_cli_config(str(explicit_cfg))

        assert creds["api_key"] == "explicit-key"
        assert creds["access_token"] == "explicit-tok"

    def test_null_values_treated_as_empty(self, tmp_path, monkeypatch):
        # JSON nulls (data.get -> None) must coerce to "", not "None".
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"teamApiKey": None, "accessToken": None}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg))

        assert _load_e2b_cli_config() == {"api_key": "", "access_token": ""}


class TestSetupE2BEnvFallback:
    def _isolate(self, monkeypatch, tmp_path):
        """Pin E2B_CONFIG to a nonexistent path so no real ~/.e2b leaks in."""
        monkeypatch.setenv("E2B_CONFIG", str(tmp_path / "no_such_config.json"))
        for var in (
            "E2B_ACCESS_TOKEN",
            "E2B_API_KEY",
            "E2B_DOMAIN",
            "E2B_API_URL",
            "E2B_HTTP_SSL",
            "E2B_SANDBOX_URL",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_falls_back_to_config_file_when_yaml_empty(self, tmp_path, monkeypatch):
        self._isolate(monkeypatch, tmp_path)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"teamApiKey": "file-key", "accessToken": "file-tok"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg_file))

        Config().setup_e2b_env()

        assert os.environ.get("E2B_API_KEY") == "file-key"
        assert os.environ.get("E2B_ACCESS_TOKEN") == "file-tok"

    def test_explicit_credentials_win_over_file(self, tmp_path, monkeypatch):
        self._isolate(monkeypatch, tmp_path)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"teamApiKey": "file-key", "accessToken": "file-tok"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg_file))

        config = Config()
        config.e2b_api_key = "yaml-key"
        config.e2b_access_token = "yaml-tok"
        config.setup_e2b_env()

        assert os.environ.get("E2B_API_KEY") == "yaml-key"
        assert os.environ.get("E2B_ACCESS_TOKEN") == "yaml-tok"

    def test_no_file_no_credentials_leaves_vars_unset(self, tmp_path, monkeypatch):
        # The pre-fallback behavior: nothing set -> env vars stay unset (we
        # don't export empty strings the SDK would then see as explicit blanks).
        self._isolate(monkeypatch, tmp_path)

        Config().setup_e2b_env()

        assert "E2B_ACCESS_TOKEN" not in os.environ
        assert "E2B_API_KEY" not in os.environ

    def test_only_token_in_file_sets_only_token(self, tmp_path, monkeypatch):
        # A file with just an access token fills that gap alone; api_key stays unset.
        self._isolate(monkeypatch, tmp_path)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"accessToken": "file-tok"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg_file))

        Config().setup_e2b_env()

        assert os.environ.get("E2B_ACCESS_TOKEN") == "file-tok"
        assert "E2B_API_KEY" not in os.environ

    def test_domain_and_api_url_still_exported(self, tmp_path, monkeypatch):
        # Non-credential SDK env vars are independent of the fallback path.
        self._isolate(monkeypatch, tmp_path)
        config = Config()
        config.e2b_domain = "example.com"
        config.e2b_api_url = "https://example.com/api"
        config.setup_e2b_env()

        assert os.environ.get("E2B_DOMAIN") == "example.com"
        assert os.environ.get("E2B_API_URL") == "https://example.com/api"

    def test_placeholder_token_falls_back_to_file(self, tmp_path, monkeypatch):
        # The example YAMLs ship ``your_e2b_access_token_here`` as a fill-in.
        # A user who copied the template verbatim must NOT send that placeholder
        # to the SDK as a real token; it must fall back to the CLI config file.
        self._isolate(monkeypatch, tmp_path)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"accessToken": "file-tok", "teamApiKey": "file-key"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg_file))

        config = Config(e2b_access_token="your_e2b_access_token_here", e2b_api_key="your_e2b_api_key_here")
        config.setup_e2b_env()

        assert os.environ.get("E2B_ACCESS_TOKEN") == "file-tok"
        assert os.environ.get("E2B_API_KEY") == "file-key"

    def test_placeholder_token_with_no_file_leaves_unset(self, tmp_path, monkeypatch):
        # Placeholder + no CLI config file -> nothing to fall back to -> unset
        # (not the placeholder string leaking into the environment).
        self._isolate(monkeypatch, tmp_path)

        config = Config(e2b_access_token="your_e2b_access_token_here")
        config.setup_e2b_env()

        assert "E2B_ACCESS_TOKEN" not in os.environ

    def test_real_token_wins_over_file(self, tmp_path, monkeypatch):
        # A real (non-placeholder) explicit token is NOT shadowed by the file.
        self._isolate(monkeypatch, tmp_path)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"accessToken": "file-tok"}), encoding="utf-8")
        monkeypatch.setenv("E2B_CONFIG", str(cfg_file))

        config = Config(e2b_access_token="real-yaml-tok")
        config.setup_e2b_env()

        assert os.environ.get("E2B_ACCESS_TOKEN") == "real-yaml-tok"

    def test_sandbox_url_exported_when_set(self, tmp_path, monkeypatch):
        # E2B_SANDBOX_URL (the AENV sandbox data-plane URL) is exported when the
        # Config carries it, so the SDK routes sandbox exec at the AENV server
        # instead of the {49983-id}.{domain} subdomain build.
        self._isolate(monkeypatch, tmp_path)
        config = Config()
        config.e2b_sandbox_url = "http://127.0.0.1:8000"
        config.setup_e2b_env()

        assert os.environ.get("E2B_SANDBOX_URL") == "http://127.0.0.1:8000"

    def test_sandbox_url_unset_keeps_existing_env(self, tmp_path, monkeypatch):
        # When the Config has no sandbox_url (cloud e2b / absent key), a
        # pre-existing E2B_SANDBOX_URL env var survives -- mirrors api_url/domain
        # (only exported when truthy, never clobbered to empty). This preserves
        # the shell-env override path for ad-hoc runs.
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("E2B_SANDBOX_URL", "http://pre-existing:9000")
        Config().setup_e2b_env()

        assert os.environ.get("E2B_SANDBOX_URL") == "http://pre-existing:9000"


def test_from_raw_block_selects_yaml_block():
    """from_raw reads the block named by `block` (aenv, not e2b, when block='aenv')."""
    raw = {
        "e2b": {
            "template": "e2b-tpl",
            "sandbox_ids_file": "e2b-ids.txt",
            "env": {"E2B_API_URL": "http://e2b-host:3000", "E2B_DOMAIN": "e2b.app"},
        },
        "aenv": {
            "template": "aenv-tpl",
            "sandbox_ids_file": "aenv-ids.txt",
            "env": {"E2B_API_URL": "http://127.0.0.1:8000", "E2B_DOMAIN": "aenv.local"},
        },
    }
    e2b_cfg = Config.from_raw(raw)  # default block
    aenv_cfg = Config.from_raw(raw, block="aenv")

    assert e2b_cfg.template == "e2b-tpl"
    assert e2b_cfg.e2b_api_url == "http://e2b-host:3000"
    assert e2b_cfg.sandbox_ids_file == "e2b-ids.txt"

    assert aenv_cfg.template == "aenv-tpl"
    assert aenv_cfg.e2b_api_url == "http://127.0.0.1:8000"
    assert aenv_cfg.e2b_domain == "aenv.local"
    assert aenv_cfg.sandbox_ids_file == "aenv-ids.txt"


def test_from_raw_reads_sandbox_url():
    """from_raw reads E2B_SANDBOX_URL from the block's env (AENV data-plane URL)."""
    raw = {"aenv": {"env": {"E2B_SANDBOX_URL": "http://127.0.0.1:8000"}}}
    cfg = Config.from_raw(raw, block="aenv")
    assert cfg.e2b_sandbox_url == "http://127.0.0.1:8000"

    # Absent key -> empty default (not exported by setup_e2b_env; SDK uses its
    # subdomain build, which is what cloud e2b wants).
    cfg2 = Config.from_raw({"aenv": {"env": {}}}, block="aenv")
    assert cfg2.e2b_sandbox_url == ""
