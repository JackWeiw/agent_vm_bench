"""
Configuration Management Module

Supports YAML config file loading, CLI argument override, Docker configuration
"""

import os
import argparse
import yaml
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class Config:
    """Test configuration"""
    # Docker configuration
    docker_image: str = "ubuntu-openclaw-chromium:arm64"
    container_prefix: str = "oc-bench"    # Container name prefix
    cpu_limit: float = 2.0                # CPU limit per container (--cpus)
    memory_limit: str = "2g"              # Memory limit per container (-m)
    create_timeout: int = 300             # Container creation timeout (seconds)

    # Container configuration
    total_count: int = 10                 # Total container count

    # Detect existing containers mode
    detect_existing: bool = False         # Detect existing containers instead of creating new ones

    # Create-only mode (create containers without running tasks)
    create_only: bool = False

    # Create batch control (for container creation, None means full concurrent)
    create_batch_size: Optional[int] = None
    create_batch_interval: Optional[int] = None

    # Task batch control (for browser task execution, None means full concurrent)
    task_batch_size: Optional[int] = None
    task_batch_interval: Optional[int] = None

    # Benchmark stress percent (percentage of containers to run benchmark)
    benchmark_percent: float = 1.0        # Percentage of containers for benchmark (default 100%)

    # Browser task configuration
    browser_urls: List[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200            # Browser task timeout (seconds)
    browser_interval_min: float = 0.5     # Task interval minimum (seconds)
    browser_interval_max: float = 3.0     # Task interval maximum (seconds)
    browser_open_timeout: int = 60        # Browser open + wait timeout (seconds)

    # Port check configuration
    required_ports: List[int] = field(default_factory=lambda: [18789, 11436])  # Ports to check
    port_check_max_wait: int = 300        # Max wait time for ports (seconds)
    port_check_interval: int = 5          # Port check interval (seconds)

    # Test run
    test_duration: int = 600              # Test duration (seconds)
    stats_interval: int = 10              # Stats snapshot interval (seconds)

    # Report
    output_dir: str = "results/docker"    # Report output directory
    filename_prefix: str = "docker_bench" # Report filename prefix

    @classmethod
    def load_from_yaml(cls, path: str) -> 'Config':
        """Load configuration from YAML file"""
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> 'Config':
        """Build Config from dictionary"""
        docker = data.get('docker', {})
        container = data.get('container', {})
        create_batch = data.get('create_batch', {})
        task_batch = data.get('task_batch', {})
        browser = data.get('browser', {})
        port_check = data.get('port_check', {})
        test = data.get('test', {})
        report = data.get('report', {})

        return cls(
            docker_image=docker.get('image', "ubuntu-openclaw-chromium:arm64"),
            container_prefix=docker.get('container_prefix', "oc-bench"),
            cpu_limit=docker.get('cpu_limit', 2.0),
            memory_limit=docker.get('memory_limit', "2g"),
            create_timeout=docker.get('create_timeout', 300),

            total_count=container.get('total_count', 10),
            detect_existing=container.get('detect_existing', False),
            create_only=container.get('create_only', False),

            create_batch_size=create_batch.get('size') if create_batch else None,
            create_batch_interval=create_batch.get('interval') if create_batch else None,

            task_batch_size=task_batch.get('size') if task_batch else None,
            task_batch_interval=task_batch.get('interval') if task_batch else None,

            benchmark_percent=test.get('benchmark_percent', 1.0),

            browser_urls=browser.get('urls', ["http://192.168.110.10:8080/Weibo.html"]),
            browser_timeout=browser.get('task_timeout', 200),
            browser_interval_min=browser.get('interval_min', 0.5),
            browser_interval_max=browser.get('interval_max', 3.0),
            browser_open_timeout=browser.get('open_timeout', 60),

            required_ports=port_check.get('ports', [18789, 11436]),
            port_check_max_wait=port_check.get('max_wait', 300),
            port_check_interval=port_check.get('interval', 5),

            test_duration=test.get('duration', 600),
            stats_interval=test.get('stats_interval', 10),

            output_dir=report.get('output_dir', "results/docker"),
            filename_prefix=report.get('filename_prefix', "docker_bench"),
        )

    @classmethod
    def merge_with_args(cls, yaml_config: 'Config', args: argparse.Namespace) -> 'Config':
        """Merge CLI arguments (CLI has higher priority)"""
        return cls(
            docker_image=args.image if args.image else yaml_config.docker_image,
            container_prefix=args.prefix if args.prefix else yaml_config.container_prefix,
            cpu_limit=args.cpu if args.cpu else yaml_config.cpu_limit,
            memory_limit=args.memory if args.memory else yaml_config.memory_limit,
            create_timeout=args.create_timeout if args.create_timeout else yaml_config.create_timeout,

            total_count=args.total if args.total else yaml_config.total_count,
            detect_existing=args.detect if hasattr(args, 'detect') and args.detect else yaml_config.detect_existing,
            create_only=args.create_only if hasattr(args, 'create_only') and args.create_only else yaml_config.create_only,

            create_batch_size=args.create_batch_size if args.create_batch_size is not None else yaml_config.create_batch_size,
            create_batch_interval=args.create_batch_interval if args.create_batch_interval is not None else yaml_config.create_batch_interval,

            task_batch_size=args.task_batch_size if args.task_batch_size is not None else yaml_config.task_batch_size,
            task_batch_interval=args.task_batch_interval if args.task_batch_interval is not None else yaml_config.task_batch_interval,

            browser_urls=args.browser_url if args.browser_url else yaml_config.browser_urls,
            browser_timeout=args.browser_timeout if args.browser_timeout else yaml_config.browser_timeout,
            browser_interval_min=args.browser_interval_min if args.browser_interval_min else yaml_config.browser_interval_min,
            browser_interval_max=args.browser_interval_max if args.browser_interval_max else yaml_config.browser_interval_max,

            benchmark_percent=args.benchmark_percent if args.benchmark_percent is not None else yaml_config.benchmark_percent,

            test_duration=args.duration if args.duration else yaml_config.test_duration,
            stats_interval=args.stats_interval if args.stats_interval else yaml_config.stats_interval,

            output_dir=args.output_dir if args.output_dir else yaml_config.output_dir,
            filename_prefix=args.filename_prefix if args.filename_prefix else yaml_config.filename_prefix,

            # Keep YAML values for these
            required_ports=yaml_config.required_ports,
            port_check_max_wait=yaml_config.port_check_max_wait,
            port_check_interval=yaml_config.port_check_interval,
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Config':
        """Build Config from CLI arguments only (no YAML file)"""
        return cls(
            docker_image=args.image or "ubuntu-openclaw-chromium:arm64",
            container_prefix=args.prefix or "oc-bench",
            cpu_limit=args.cpu or 2.0,
            memory_limit=args.memory or "2g",
            create_timeout=args.create_timeout or 300,

            total_count=args.total or 10,
            detect_existing=args.detect if hasattr(args, 'detect') and args.detect else False,
            create_only=args.create_only if hasattr(args, 'create_only') and args.create_only else False,

            create_batch_size=args.create_batch_size,
            create_batch_interval=args.create_batch_interval,

            task_batch_size=args.task_batch_size,
            task_batch_interval=args.task_batch_interval,

            browser_urls=args.browser_url or ["http://192.168.110.10:8080/Weibo.html"],
            browser_timeout=args.browser_timeout or 200,
            browser_interval_min=args.browser_interval_min or 0.5,
            browser_interval_max=args.browser_interval_max or 3.0,

            benchmark_percent=args.benchmark_percent if args.benchmark_percent is not None else 1.0,

            test_duration=args.duration or 600,
            stats_interval=args.stats_interval or 10,

            output_dir=args.output_dir or "results/docker",
            filename_prefix=args.filename_prefix or "docker_bench",
        )

    @property
    def create_batch_count(self) -> int:
        """Calculate create batch count"""
        if not self.create_batch_size:
            return 1  # Full concurrent treated as 1 batch
        return (self.total_count + self.create_batch_size - 1) // self.create_batch_size

    @property
    def task_batch_count(self) -> int:
        """Calculate task batch count (based on ready containers)"""
        # This will be calculated dynamically based on actual ready containers
        # For planning purposes, use total_count as estimate
        if not self.task_batch_size:
            return 1
        return (self.total_count + self.task_batch_size - 1) // self.task_batch_size

    @property
    def benchmark_count(self) -> int:
        """Calculate actual container count for benchmark phase

        Based on benchmark_percent (e.g., 0.5 = 50% of containers)
        """
        return max(1, int(self.total_count * self.benchmark_percent))