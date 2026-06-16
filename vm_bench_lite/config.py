#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration Module

Stress test configuration and settings management.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    """Stress test configuration"""
    total_vms: int = 80
    stress_percent: float = 0.5
    batch_size: int = 10
    batch_interval: int = 30

    stress_memory_mb: int = 2048
    stress_duration: int = 300
    stress_keepalive: bool = True    # Stress process keepalive

    qa_interval: float = 0.5
    qa_timeout: int = 600
    qa_init_timeout: int = 600       # Memory entry timeout
    mode: str = "cli"             # Interaction mode: "cli" or "http" (for QA and Browser)
    browser_mode: bool = False        # Browser test mode (mutually exclusive with QA)
    browser_timeout: int = 200        # Single browser task timeout (seconds)
    browser_url: str = ""             # Browser test target URL
    browser_use_llm: bool = False     # Browser task use LLM (True: HTTP/CLI call prompt, False: direct openclaw browser)

    # Browser phase control (two-phase execution: warmup then benchmark)
    is_warmup_phase: bool = False     # True: warmup phase (all VMs), False: benchmark phase (partial VMs)
    browser_stress_percent: float = 1.0  # Percentage of VMs to run browser benchmark (in benchmark phase)

    # Browser warmup configuration
    warmup_urls: List[str] = field(default_factory=list)  # Warmup page URL list
    warmup_loops: int = 2             # Warmup loop count
    warmup_delay: int = 10            # Delay between warmup pages (seconds)

    test_duration: int = 600
    stats_interval: int = 10
    health_check_interval: float = 5.0  # Health check interval
    task_interval: float = 1.0          # VM task interval, stagger different VM task execution times
    browser_task_interval_min: float = 0.5  # Browser task random interval minimum
    browser_task_interval_max: float = 3.0  # Browser task random interval maximum

    # SSH configuration
    start_ip: str = "192.168.110.11"
    port: int = 22
    username: str = "root"
    password: str = "openEuler12#$"

    @property
    def stress_vm_count(self) -> int:
        return int(self.total_vms * self.stress_percent)

    @property
    def browser_benchmark_vm_count(self) -> int:
        """Actual VM count for browser benchmark phase"""
        if self.browser_mode and not self.is_warmup_phase:
            return max(1, int(self.total_vms * self.browser_stress_percent))
        return self.total_vms

    @property
    def batch_count(self) -> int:
        if self.browser_mode:
            # Browser mode: use actual connected VM count
            count = self.browser_benchmark_vm_count
        else:
            count = self.stress_vm_count
        return (count + self.batch_size - 1) // self.batch_size