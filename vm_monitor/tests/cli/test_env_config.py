"""save_env_config write-independence: each tool's path lands in .env on its own.

Regression: UPROF_PATH used to be nested under the SMAP_BW_PATH if-block in
save_env_config, so a configured uprof_path was silently dropped from .env
unless smap_bw_path was also set.
"""

import os
import shutil
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
