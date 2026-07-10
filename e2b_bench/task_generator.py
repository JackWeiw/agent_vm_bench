"""
Task Generator Module

Generates TaskGroups and BatchTasks from test matrix configuration.
Groups tasks by (total_count, ratio) for sandbox reuse.
"""

from typing import Any, Dict, List

from .schemas import BatchTask, TaskGroup


class TaskGenerator:
    """Generate test tasks from parameter matrix"""

    def __init__(self, matrix_config: Dict[str, Any]):
        """
        Initialize TaskGenerator with matrix configuration

        Args:
            matrix_config: Dict containing test_matrix and reuse_strategy
        """
        self.matrix = matrix_config.get("test_matrix", {})
        self.reuse_strategy = matrix_config.get("reuse_strategy", {})

        self.total_counts = self.matrix.get("total_counts", [10])
        self.benchmark_percentages = self.matrix.get("benchmark_percentages", [1.0])
        self.ratios = self.matrix.get("ratios", [15])

    def generate_groups(self) -> List[TaskGroup]:
        """
        Generate TaskGroups grouped by (total_count, ratio)

        Tasks in same group can reuse sandbox and smap_tool.

        Returns:
            List of TaskGroup objects
        """
        groups = []

        for total_count in self.total_counts:
            for ratio in self.ratios:
                group_id = f"tc{total_count}_ratio{ratio}"

                # Generate all tasks for this group
                tasks = []
                for bp in self.benchmark_percentages:
                    task_id = f"{group_id}_bp{bp}"
                    task = BatchTask(task_id=task_id, total_count=total_count, benchmark_percent=bp, ratio=ratio)
                    tasks.append(task)

                group = TaskGroup(group_id=group_id, total_count=total_count, ratio=ratio, tasks=tasks)
                groups.append(group)

        return groups

    def get_total_task_count(self) -> int:
        """Calculate total number of tasks across all groups"""
        return len(self.total_counts) * len(self.ratios) * len(self.benchmark_percentages)

    def get_group_count(self) -> int:
        """Calculate number of groups"""
        return len(self.total_counts) * len(self.ratios)


def load_matrix_config(path: str) -> Dict[str, Any]:
    """Load matrix configuration from YAML file"""
    import yaml

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
