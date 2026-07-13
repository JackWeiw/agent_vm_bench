"""
Test TaskGenerator Module

Tests for TaskGenerator: group generation, task counts, load_matrix_config
"""

import os
import tempfile

import pytest

from e2b_bench.task_generator import TaskGenerator, load_matrix_config


class TestTaskGeneratorBasic:
    """Tests for basic TaskGenerator functionality"""

    def test_single_group_single_task(self):
        """Minimal configuration"""
        matrix_config = {"test_matrix": {"total_counts": [10], "benchmark_percentages": [0.5], "ratios": [15]}}
        generator = TaskGenerator(matrix_config)
        groups = generator.generate_groups()

        assert len(groups) == 1
        assert groups[0].group_id == "tc10_ratio15"
        assert groups[0].total_count == 10
        assert groups[0].ratio == 15
        assert len(groups[0].tasks) == 1

    def test_task_id_format(self):
        """Task ID follows expected format"""
        matrix_config = {"test_matrix": {"total_counts": [100], "benchmark_percentages": [0.5], "ratios": [15]}}
        generator = TaskGenerator(matrix_config)
        groups = generator.generate_groups()

        task = groups[0].tasks[0]
        assert task.task_id == "tc100_ratio15_bp0.5"


class TestTaskGeneratorMultiple:
    """Tests for multiple values"""

    def test_multiple_total_counts(self):
        """Multiple total_counts creates multiple groups"""
        matrix_config = {"test_matrix": {"total_counts": [10, 20], "benchmark_percentages": [1.0], "ratios": [15]}}
        generator = TaskGenerator(matrix_config)
        groups = generator.generate_groups()

        assert len(groups) == 2
        expected_ids = ["tc10_ratio15", "tc20_ratio15"]
        actual_ids = [g.group_id for g in groups]
        assert sorted(actual_ids) == sorted(expected_ids)

    def test_multiple_ratios(self):
        """Multiple ratios creates multiple groups"""
        matrix_config = {"test_matrix": {"total_counts": [10], "benchmark_percentages": [1.0], "ratios": [10, 20]}}
        generator = TaskGenerator(matrix_config)
        groups = generator.generate_groups()

        assert len(groups) == 2
        expected_ids = ["tc10_ratio10", "tc10_ratio20"]
        actual_ids = [g.group_id for g in groups]
        assert sorted(actual_ids) == sorted(expected_ids)

    def test_multiple_benchmark_percentages(self):
        """Multiple benchmark_percentages creates multiple tasks per group"""
        matrix_config = {
            "test_matrix": {"total_counts": [10], "benchmark_percentages": [0.5, 0.75, 1.0], "ratios": [15]}
        }
        generator = TaskGenerator(matrix_config)
        groups = generator.generate_groups()

        assert len(groups) == 1
        assert len(groups[0].tasks) == 3

        expected_task_ids = ["tc10_ratio15_bp0.5", "tc10_ratio15_bp0.75", "tc10_ratio15_bp1.0"]
        actual_task_ids = [t.task_id for t in groups[0].tasks]
        assert sorted(actual_task_ids) == sorted(expected_task_ids)

    def test_full_matrix(self):
        """Full matrix: 2 total_counts * 2 ratios * 2 benchmark_percentages"""
        matrix_config = {
            "test_matrix": {"total_counts": [10, 20], "benchmark_percentages": [0.5, 1.0], "ratios": [10, 20]}
        }
        generator = TaskGenerator(matrix_config)
        groups = generator.generate_groups()

        # 2 * 2 = 4 groups
        assert len(groups) == 4

        # Each group has 2 tasks
        for group in groups:
            assert len(group.tasks) == 2

        # Total tasks = 4 * 2 = 8
        total_tasks = sum(len(g.tasks) for g in groups)
        assert total_tasks == 8


class TestTaskGeneratorCounts:
    """Tests for count calculations"""

    def test_total_task_count(self):
        """Total task count calculation"""
        matrix_config = {
            "test_matrix": {
                "total_counts": [10, 20, 50],  # 3
                "benchmark_percentages": [0.5, 0.75, 1.0],  # 3
                "ratios": [10, 20],  # 2
            }
        }
        generator = TaskGenerator(matrix_config)
        # 3 * 3 * 2 = 18
        assert generator.get_total_task_count() == 18

    def test_group_count(self):
        """Group count = total_counts * ratios"""
        matrix_config = {
            "test_matrix": {
                "total_counts": [10, 20, 50],  # 3
                "ratios": [10, 20],  # 2
            }
        }
        generator = TaskGenerator(matrix_config)
        # 3 * 2 = 6
        assert generator.get_group_count() == 6


class TestLoadMatrixConfig:
    """Tests for load_matrix_config function"""

    def test_load_matrix(self):
        """Load from YAML file"""
        yaml_content = """
test_matrix:
  total_counts: [10, 20]
  benchmark_percentages: [0.5, 1.0]
  ratios: [10, 20]

reuse_strategy:
  reuse_sandbox: true

result:
  template_path: config/template.yaml
  output_dir: results/e2b/batch
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            temp_path = f.name
            f.write(yaml_content)

        try:
            config = load_matrix_config(temp_path)
            assert config["test_matrix"]["total_counts"] == [10, 20]
            assert config["test_matrix"]["benchmark_percentages"] == [0.5, 1.0]
            assert config["reuse_strategy"]["reuse_sandbox"] == True
            assert config["result"]["output_dir"] == "results/e2b/batch"
        finally:
            os.unlink(temp_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
