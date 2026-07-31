"""Path-existence tests for the reorganized config layout.

Guards against config path drift: each moved e2b config must exist at its
new location under config/e2b/ and load successfully via Config.load_from_yaml.
"""

import os.path

from e2b_bench.config import Config

# Moved single-test e2b configs that must exist and load at their new paths.
MOVED_E2B_CONFIGS = [
    "config/e2b/bench.yaml",
    "config/e2b/coding_bench.yaml",
    "config/e2b/coding_go_bench.yaml",
]


def test_moved_e2b_configs_exist():
    """Every moved e2b single-test config exists at its new path."""
    for path in MOVED_E2B_CONFIGS:
        assert os.path.exists(path), f"missing moved config: {path}"


def test_moved_e2b_configs_load():
    """Every moved e2b config loads via Config.load_from_yaml without error."""
    for path in MOVED_E2B_CONFIGS:
        config = Config.load_from_yaml(path)
        assert config is not None, f"config did not load: {path}"


def test_e2b_batch_template_exists():
    """The batch template (referenced by batch_scheduler default) exists."""
    assert os.path.exists("config/e2b/batch_template.yaml")
