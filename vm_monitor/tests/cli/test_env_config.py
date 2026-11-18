"""save_env_config write-independence: each tool's path lands in .env on its own.

Regression: UPROF_PATH used to be nested under the SMAP_BW_PATH if-block in
save_env_config, so a configured uprof_path was silently dropped from .env
unless smap_bw_path was also set.
"""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from vm_monitor import config as vm_config


class TestSaveEnvConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="env_config_")
        self._old_path = vm_config.ENV_FILE_PATH
        vm_config.ENV_FILE_PATH = os.path.join(self.tmp, ".env")

    def tearDown(self):
        vm_config.ENV_FILE_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self):
        with open(vm_config.ENV_FILE_PATH, encoding="utf-8") as f:
            return f.read()

    @unittest.skipUnless(vm_config.DOTENV_AVAILABLE, "python-dotenv not installed")
    def test_uprof_written_without_smap_bw(self):
        vm_config.save_env_config({"uprof_path": "/opt/AMDuProf_5.3-521/bin/AMDuProfPcm"})
        content = self._read()
        self.assertIn("UPROF_PATH", content)
        self.assertNotIn("SMAP_BW_PATH", content)

    @unittest.skipUnless(vm_config.DOTENV_AVAILABLE, "python-dotenv not installed")
    def test_smap_bw_written_without_uprof(self):
        vm_config.save_env_config({"smap_bw_path": "/fake/smap_bw.py"})
        content = self._read()
        self.assertIn("SMAP_BW_PATH", content)
        self.assertNotIn("UPROF_PATH", content)


def _write_required_paths(tmp):
    """Create real files for the seven required tool paths so the validation
    loop never reaches its prompt for them."""
    paths = {}
    for name in (
        "devkit_path",
        "ksys_path",
        "ksys_config_path",
        "ub_watch_path",
        "smap_bw_path",
        "getfre_path",
        "getfre_config_path",
    ):
        path = os.path.join(tmp, name)
        with open(path, "w"):
            pass
        paths[name] = path
    return paths


class TestValidateUprofPath(unittest.TestCase):
    """UPROF_PATH is registered in ENV_REQUIRED_KEYS but stays an optional tool:
    it must validate without ever prompting -- an unset/invalid path degrades to
    None (the uprof rotation slot is skipped) and never blocks the session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uprof_validate_")
        self.required = _write_required_paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _validate(self, uprof_path, non_interactive=False, **kwargs):
        config = dict(self.required)
        if uprof_path is not None:
            config["uprof_path"] = uprof_path
        with (
            mock.patch("builtins.input", side_effect=AssertionError("UPROF_PATH must not prompt")),
            mock.patch("vm_monitor.config.save_env_config"),
        ):
            return vm_config.validate_and_prompt_missing(config, non_interactive=non_interactive, **kwargs)

    def test_env_required_keys_contains_uprof_path(self):
        self.assertIn("UPROF_PATH", vm_config.ENV_REQUIRED_KEYS)

    def test_valid_path_kept(self):
        uprof = os.path.join(self.tmp, "AMDuProfPcm")
        with open(uprof, "w"):
            pass
        self.assertEqual(self._validate(uprof)["uprof_path"], uprof)

    def test_invalid_path_warns_and_sets_none_without_prompt(self):
        with redirect_stdout(io.StringIO()) as out:
            result = self._validate(os.path.join(self.tmp, "missing_AMDuProfPcm"))
        self.assertIsNone(result["uprof_path"])
        self.assertIn("UPROF_PATH", out.getvalue())

    def test_unset_path_warns_and_sets_none_without_prompt(self):
        with redirect_stdout(io.StringIO()) as out:
            result = self._validate(None)
        self.assertIsNone(result["uprof_path"])
        self.assertIn("UPROF_PATH", out.getvalue())

    def test_non_interactive_invalid_path_sets_none(self):
        with redirect_stdout(io.StringIO()):
            result = self._validate(os.path.join(self.tmp, "missing_AMDuProfPcm"), non_interactive=True)
        self.assertIsNone(result["uprof_path"])

    def test_check_uprof_false_skips_validation_entirely(self):
        # Parallel mode (no --log-rotation): the CLI passes check_uprof=False,
        # so no WARN is printed and uprof_path is left untouched.
        missing = os.path.join(self.tmp, "missing_AMDuProfPcm")
        with redirect_stdout(io.StringIO()) as out:
            result = self._validate(missing, check_uprof=False)
        self.assertEqual(result["uprof_path"], missing)
        self.assertNotIn("UPROF_PATH", out.getvalue())


if __name__ == "__main__":
    unittest.main()
