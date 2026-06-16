#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Models Module

All data classes, enums, and constants for VM bench testing.
"""

import time
import statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum


# ==================== Enums ====================

class OOMType(Enum):
    """OOM Type Classification"""
    NONE = "none"
    START_OOM = "start_oom"          # OOM at startup (memory allocation failed)
    RUNTIME_OOM = "runtime_oom"      # OOM at runtime (killed by OOM Killer)
    CRASH = "crash"                   # Program crash (segmentation fault etc.)
    UNKNOWN = "unknown"               # Unknown cause


# ==================== Metrics Classes ====================

@dataclass
class QAMetrics:
    """QA performance metrics"""
    total_queries: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    memory_init_done: bool = False
    memory_init_time: float = 0.0
    current_query_index: int = 0
    query_round: int = 0

    def add(self, latency: float, success: bool, timeout: bool = False):
        self.total_queries += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
        else:
            self.failed_count += 1

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(self.latencies) < 100:
            return sorted_lat[-1]
        return sorted_lat[int(len(self.latencies) * 0.99)]


@dataclass
class BrowserMetrics:
    """Browser task metrics"""
    total_tasks: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    task_type_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def add(self, latency: float, success: bool, timeout: bool = False, task_type: str = ""):
        self.total_tasks += 1
        if timeout:
            self.timeout_count += 1
            self.failed_count += 1
        elif success:
            self.success_count += 1
            self.latencies.append(latency)
        else:
            self.failed_count += 1
        if task_type:
            if task_type not in self.task_type_counts:
                self.task_type_counts[task_type] = {"success": 0, "failed": 0}
            if success and not timeout:
                self.task_type_counts[task_type]["success"] += 1
            else:
                self.task_type_counts[task_type]["failed"] += 1

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(self.latencies) < 100:
            return sorted_lat[-1]
        return sorted_lat[int(len(self.latencies) * 0.99)]


@dataclass
class StressMetrics:
    """Stress process metrics"""
    start_count: int = 0
    restart_count: int = 0
    oom_events: Dict[OOMType, int] = field(default_factory=lambda: {t: 0 for t in OOMType})
    last_start_time: float = 0.0
    current_pid: Optional[str] = None


@dataclass
class VMHealth:
    """VM health status"""
    is_connected: bool = True
    last_seen: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    error_history: List[Tuple[float, str]] = field(default_factory=list)

    def mark_failure(self, error: str):
        self.consecutive_failures += 1
        self.last_error = error
        self.error_history.append((time.time(), error))
        if len(self.error_history) > 10:
            self.error_history.pop(0)

    def mark_success(self):
        self.consecutive_failures = 0
        self.last_error = ""

    def check_offline(self, threshold: int = 2) -> bool:
        return self.consecutive_failures >= threshold


@dataclass
class VMState:
    """VM complete state"""
    vm_id: int
    host: str
    is_stress_vm: bool = False
    batch_id: int = 0

    # QA state
    qa_metrics: QAMetrics = field(default_factory=QAMetrics)
    last_qa_time: float = 0.0

    # Stress state
    stress_metrics: StressMetrics = field(default_factory=StressMetrics)
    stress_started: bool = False
    last_stress_check: float = 0.0

    # Health state
    health: VMHealth = field(default_factory=VMHealth)

    qa_failure_count: int = 0
    stress_failure_count: int = 0
    browser_failure_count: int = 0

    # Browser state
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    last_browser_time: float = 0.0
    warmup_done: bool = False  # Warmup completion flag

    @property
    def has_task_failure(self) -> bool:
        return self.qa_failure_count > 0 or self.stress_failure_count > 0 or self.browser_failure_count > 0

    def record_qa_failure(self):
        self.qa_failure_count += 1

    def record_stress_failure(self):
        self.stress_failure_count += 1

    def record_browser_failure(self):
        self.browser_failure_count += 1

    def __post_init__(self):
        self.health.last_seen = time.time()


# ==================== Task Constants ====================

QA_MEMORY_TEXT = """Please remember the following information:
Employee Attendance: Work hours are 9:00-18:00, lunch break 12:00-13:00. Late arrivals within 30 minutes are not counted as absenteeism, with 3 late arrival exemptions allowed per month. Field employees do not need to clock in but must submit field work records daily.
Travel Reimbursement: Domestic travel accommodation standards are 500 yuan/day for tier-1 cities, 400 yuan/day for tier-2 cities, and 300 yuan/day for tier-3 and below. Transportation expenses are reimbursed as incurred, taxi fares are limited to urgent official business only. Reimbursements must be submitted within 7 working days after return, late submissions will not be accepted.
Overtime Policy: Weekday overtime pays 1.5x salary, weekends 2x, statutory holidays 3x. Overtime must be pre-approved via the OA system, unapproved overtime will not be counted.
Product Information: Standard edition supports 100 concurrent users, annual fee 9800 yuan; Enterprise edition supports 500 concurrent users, annual fee 29800 yuan."""

QA_QUESTIONS = [
    "What are the work hours for our company? Do field employees need to clock in?",
    "I'm traveling to Shanghai, what is the accommodation standard? How long do I have to submit the reimbursement?",
    "How is overtime pay calculated in our company? Is overtime pay automatically given for any overtime work?",
]

BROWSER_TASKS = [
    ("Page Access", "Please use chromium browser to visit {url} and tell me the page title")
]
