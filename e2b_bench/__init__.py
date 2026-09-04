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
from .coding_task_runner import CodingRoundRunner, CodingTaskRunner, CodingWarmupRunner
from .config import Config
from .document_task_runner import DocumentRoundRunner, DocumentTaskRunner, DocumentWarmupRunner
from .helpers import wait_for_port_ready
from .metrics_extractor import MetricsExtractor
from .report_aggregator import ReportAggregator
from .schemas import (
    DEFAULT_CODING_SOURCE_FILES,
    BatchTask,
    BrowserMetrics,
    CodingMetrics,
    DocumentMetrics,
    SandboxState,
    SandboxStatus,
    TaskGroup,
    TaskMetricsBase,
)
from .task_generator import TaskGenerator, load_matrix_config

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
