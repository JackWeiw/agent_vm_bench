"""Path-existence tests for the reorganized config layout.

Guards against config path drift: each moved e2b config must exist at its
new location under config/e2b/ and load successfully via Config.load_from_yaml.
"""

import os.path
import pytest

from e2b_bench.config import Config

# Moved single-test e2b configs that must exist and load at their new paths.
MOVED_E2B_CONFIGS = [
    "config/e2b/bench.yaml",
    "config/e2b/coding_bench.yaml",
    "config/e2b/coding_go_bench.yaml",
]

DOCUMENT_CONFIGS = [
    "config/e2b/pdf_bench.yaml",
    "config/e2b/xlsx_bench.yaml",
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


def test_document_configs_are_isolated_and_loadable():
    pdf = Config.load_from_yaml(DOCUMENT_CONFIGS[0])
    xlsx = Config.load_from_yaml(DOCUMENT_CONFIGS[1])
    pdf.validate()
    xlsx.validate()
    assert pdf.workflow_type == xlsx.workflow_type == "document"
    assert pdf.document_case_kind == "pdf"
    assert xlsx.document_case_kind == "xlsx"
    assert pdf.sandbox_ids_file != xlsx.sandbox_ids_file
    assert pdf.output_dir != xlsx.output_dir
    assert pdf.filename_prefix != xlsx.filename_prefix


@pytest.mark.parametrize("forbidden", ["operations_file", "max_repair_attempts"])
def test_document_yaml_rejects_host_recipe_overrides(forbidden):
    with pytest.raises(ValueError, match="fixed by case_kind"):
        Config._from_dict(
            {
                "workflow_type": "document",
                "document": {"case_kind": "pdf", forbidden: "forbidden"},
            }
        )


def test_document_derived_paths_do_not_enter_config_dict():
    config = Config.load_from_yaml(DOCUMENT_CONFIGS[0])
    clone = Config(**config.__dict__)
    assert clone.document_seed_dir == config.document_seed_dir
    assert clone.document_workspace_dir == config.document_workspace_dir
    assert "document_seed_dir" not in config.__dict__
    assert "document_workspace_dir" not in config.__dict__
    assert "document_operations_file" not in config.__dict__


def test_fixed_document_recipe_path_does_not_depend_on_cwd(tmp_path, monkeypatch):
    from e2b_bench.document_task_runner import get_document_operations_path, load_scene_recipe

    monkeypatch.chdir(tmp_path)
    path = get_document_operations_path("xlsx")
    assert path.is_file()
    assert path.parent.as_posix().endswith("dockerfile_build/document/assets/operations")
    assert load_scene_recipe("xlsx")["case_kind"] == "xlsx"
