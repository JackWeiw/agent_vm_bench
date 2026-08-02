"""
E2B Bench - E2B Sandbox Batch Performance Testing Tool

Provides:
- Single sandbox benchmark (bench.py)
- Batch test scheduler with sandbox reuse (batch_scheduler.py)
- Metrics extraction from vm_monitor (metrics_extractor.py)
- Report aggregation (report_aggregator.py)
"""

from .batch_scheduler import BatchScheduler, GroupRunner
from .bench import SmapToolManager, VmMonitorManager, run_benchmark
from .config import Config
from .metrics_extractor import MetricsExtractor
from .report_aggregator import ReportAggregator
from .schemas import (
    BatchTask,
    CodingMetrics,
    DocumentMetrics,
    DEFAULT_CODING_SOURCE_FILES,
    SandboxState,
    SandboxStatus,
    TaskGroup,
    TaskMetricsBase,
    BrowserMetrics,
)
from .coding_task_runner import CodingRoundRunner, CodingTaskRunner, CodingWarmupRunner
from .document_task_runner import DocumentRoundRunner, DocumentTaskRunner, DocumentWarmupRunner
from .task_generator import TaskGenerator, load_matrix_config
from .helpers import wait_for_port_ready

__all__ = [
    "Config",
    "SandboxState",
    "SandboxStatus",
    "BatchTask",
    "TaskGroup",
    "TaskMetricsBase",
    "BrowserMetrics",
    "CodingMetrics",
    "DocumentMetrics",
    "DEFAULT_CODING_SOURCE_FILES",
    "wait_for_port_ready",
    "run_benchmark",
    "SmapToolManager",
    "VmMonitorManager",
    "BatchScheduler",
    "GroupRunner",
    "TaskGenerator",
    "CodingWarmupRunner",
    "CodingTaskRunner",
    "CodingRoundRunner",
    "DocumentWarmupRunner",
    "DocumentTaskRunner",
    "DocumentRoundRunner",
    "load_matrix_config",
    "MetricsExtractor",
    "ReportAggregator",
]
