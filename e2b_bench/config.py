"""
Configuration Management Module

Supports YAML config file loading, CLI argument override, E2B environment variable setup
"""

import argparse
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class Config:
    """Test configuration"""

    # E2B environment variables
    e2b_access_token: str = ""
    e2b_api_key: str = ""
    e2b_domain: str = "e2b.app"
    e2b_api_url: str = "http://localhost:3000"
    e2b_http_ssl: str = "false"

    # Sandbox configuration
    template: str = "openclaw-browser-v1"
    create_timeout: int = 86400
    total_count: int = 100

    # NUMA binding for sandbox creation (None = no binding, int = bind to specific NUMA node)
    numa_bind: Optional[int] = 2

    # Detect existing sandboxes mode
    detect_existing: bool = False  # Detect existing sandboxes instead of creating new ones

    # Create-only mode (create sandboxes without running tasks)
    create_only: bool = False

    # Sandbox IDs file (for save/load sandbox IDs)
    sandbox_ids_file: Optional[str] = None

    # Create batch control (for sandbox creation, None means full concurrent)
    create_batch_size: Optional[int] = None
    create_batch_interval: Optional[int] = None

    # Task batch control (for browser task execution, None means full concurrent)
    task_batch_size: Optional[int] = None
    task_batch_interval: Optional[int] = None

    # Benchmark stress percent (percentage of sandboxes to run benchmark)
    benchmark_percent: float = 1.0  # Percentage of sandboxes for benchmark (default 100%)

    # Round-robin mode configuration
    benchmark_mode: str = "fixed"  # "fixed" (default) or "round_robin"
    round_count: Optional[int] = None  # Number of sandbox groups (mutually exclusive with round_size)
    round_size: Optional[int] = None  # Sandboxes per round (mutually exclusive with round_count)
    round_interval: int = 30  # Round interval in seconds for round_robin mode (default: 30s)

    # smap_tool configuration (memory migration monitoring)
    smap_tool_enabled: bool = False
    smap_tool_path: str = ""
    smap_tool_swap_size: int = 81920
    smap_tool_ratio: int = 15
    smap_tool_src_nid: int = 2
    smap_tool_dest_nid: int = 5

    # vm_monitor configuration (performance monitoring)
    vm_monitor_enabled: bool = False
    vm_monitor_vmm_type: str = "firecracker"
    vm_monitor_duration: int = 600
    vm_monitor_numa: str = "1"  # NUMA nodes to monitor, comma-separated (e.g., "0,1")
    vm_monitor_log_dir: str = "results/e2b/vm_monitor"
    vm_monitor_stress_file: str = "/dev/shm/e2b_benchmark_lock"

    # Browser task
    browser_urls: List[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200
    browser_interval_min: float = 0.5
    browser_interval_max: float = 3.0

    # Warmup phase configuration
    warmup_urls: List[str] = field(default_factory=list)  # Warmup page URL list
    warmup_loops: int = 2  # Warmup loop count
    warmup_delay: int = 10  # Delay between warmup pages (seconds)
    warmup_only: bool = False  # Run warmup phase only, then exit

    # Test run
    test_duration: int = 600
    stats_interval: int = 10

    # Report
    output_dir: str = "results/e2b"
    filename_prefix: str = "e2b_bench"

    @classmethod
    def load_from_yaml(cls, path: str) -> "Config":
        """Load configuration from YAML file"""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Build Config from dictionary"""
        e2b_env = data.get("e2b_env", {})
        sandbox = data.get("sandbox", {})
        create_batch = data.get("create_batch", {})
        task_batch = data.get("task_batch", {})
        browser = data.get("browser", {})
        test = data.get("test", {})
        report = data.get("report", {})
        smap_tool = data.get("smap_tool", {})
        vm_monitor = data.get("vm_monitor", {})

        return cls(
            e2b_access_token=e2b_env.get("E2B_ACCESS_TOKEN", ""),
            e2b_api_key=e2b_env.get("E2B_API_KEY", ""),
            e2b_domain=e2b_env.get("E2B_DOMAIN", "e2b.app"),
            e2b_api_url=e2b_env.get("E2B_API_URL", "http://localhost:3000"),
            e2b_http_ssl=e2b_env.get("E2B_HTTP_SSL", "false"),
            template=sandbox.get("template", "openclaw-browser-v1"),
            create_timeout=sandbox.get("create_timeout", 86400),
            total_count=sandbox.get("total_count", 100),
            detect_existing=sandbox.get("detect_existing", False),
            create_only=sandbox.get("create_only", False),
            sandbox_ids_file=sandbox.get("sandbox_ids_file", None),
            numa_bind=sandbox.get("numa_bind", 2),
            create_batch_size=create_batch.get("size") if create_batch else None,
            create_batch_interval=create_batch.get("interval") if create_batch else None,
            task_batch_size=task_batch.get("size") if task_batch else None,
            task_batch_interval=task_batch.get("interval") if task_batch else None,
            benchmark_percent=test.get("benchmark_percent", 1.0),
            # Round-robin mode configuration
            benchmark_mode=test.get("benchmark_mode", "fixed"),
            round_count=test.get("round_count"),
            round_size=test.get("round_size"),
            round_interval=test.get("round_interval", 30),
            browser_urls=browser.get("urls", ["http://192.168.110.10:8080/Weibo.html"]),
            browser_timeout=browser.get("task_timeout", 200),
            browser_interval_min=browser.get("interval_min", 0.5),
            browser_interval_max=browser.get("interval_max", 3.0),
            # Warmup configuration
            warmup_urls=browser.get("warmup_urls", []),
            warmup_loops=browser.get("warmup_loops", 2),
            warmup_delay=browser.get("warmup_delay", 10),
            warmup_only=browser.get("warmup_only", False),
            test_duration=test.get("duration", 600),
            stats_interval=test.get("stats_interval", 10),
            output_dir=report.get("output_dir", "results/e2b"),
            filename_prefix=report.get("filename_prefix", "e2b_bench"),
            # smap_tool configuration
            smap_tool_enabled=smap_tool.get("enabled", False),
            smap_tool_path=smap_tool.get("path", ""),
            smap_tool_swap_size=smap_tool.get("swap_size", 81920),
            smap_tool_ratio=smap_tool.get("ratio", 15),
            smap_tool_src_nid=smap_tool.get("src_nid", 2),
            smap_tool_dest_nid=smap_tool.get("dest_nid", 5),
            # vm_monitor configuration
            vm_monitor_enabled=vm_monitor.get("enabled", False),
            vm_monitor_vmm_type=vm_monitor.get("vmm_type", "firecracker"),
            vm_monitor_duration=vm_monitor.get("duration", 600),
            vm_monitor_numa=vm_monitor.get("numa", "1"),
            vm_monitor_log_dir=vm_monitor.get("log_dir", "results/e2b/vm_monitor"),
            vm_monitor_stress_file=vm_monitor.get("stress_file", "/dev/shm/e2b_benchmark_lock"),
        )

    @classmethod
    def merge_with_args(cls, yaml_config: "Config", args: argparse.Namespace) -> "Config":
        """Merge CLI arguments (CLI has higher priority)"""
        return cls(
            e2b_access_token=args.e2b_access_token if args.e2b_access_token else yaml_config.e2b_access_token,
            e2b_api_key=args.e2b_api_key if args.e2b_api_key else yaml_config.e2b_api_key,
            e2b_domain=args.e2b_domain if args.e2b_domain else yaml_config.e2b_domain,
            e2b_api_url=args.e2b_api_url if args.e2b_api_url else yaml_config.e2b_api_url,
            e2b_http_ssl=args.e2b_http_ssl if args.e2b_http_ssl else yaml_config.e2b_http_ssl,
            template=args.template if args.template else yaml_config.template,
            create_timeout=args.create_timeout if args.create_timeout else yaml_config.create_timeout,
            total_count=args.total if args.total else yaml_config.total_count,
            detect_existing=args.detect if hasattr(args, "detect") and args.detect else yaml_config.detect_existing,
            create_only=args.create_only
            if hasattr(args, "create_only") and args.create_only
            else yaml_config.create_only,
            sandbox_ids_file=args.sandbox_ids_file if args.sandbox_ids_file else yaml_config.sandbox_ids_file,
            numa_bind=yaml_config.numa_bind,  # Use yaml config for numa_bind
            create_batch_size=args.create_batch_size
            if args.create_batch_size is not None
            else yaml_config.create_batch_size,
            create_batch_interval=args.create_batch_interval
            if args.create_batch_interval is not None
            else yaml_config.create_batch_interval,
            task_batch_size=args.task_batch_size if args.task_batch_size is not None else yaml_config.task_batch_size,
            task_batch_interval=args.task_batch_interval
            if args.task_batch_interval is not None
            else yaml_config.task_batch_interval,
            browser_urls=args.browser_url if args.browser_url else yaml_config.browser_urls,
            browser_timeout=args.browser_timeout if args.browser_timeout else yaml_config.browser_timeout,
            browser_interval_min=args.browser_interval_min
            if args.browser_interval_min
            else yaml_config.browser_interval_min,
            browser_interval_max=args.browser_interval_max
            if args.browser_interval_max
            else yaml_config.browser_interval_max,
            # Warmup configuration
            warmup_urls=args.warmup_url if args.warmup_url else yaml_config.warmup_urls,
            warmup_loops=args.warmup_loops if args.warmup_loops else yaml_config.warmup_loops,
            warmup_delay=args.warmup_delay if args.warmup_delay else yaml_config.warmup_delay,
            warmup_only=args.warmup_only
            if hasattr(args, "warmup_only") and args.warmup_only
            else yaml_config.warmup_only,
            benchmark_percent=args.benchmark_percent
            if args.benchmark_percent is not None
            else yaml_config.benchmark_percent,
            # Round-robin mode configuration (CLI takes priority over YAML)
            benchmark_mode=getattr(args, "benchmark_mode", None) or yaml_config.benchmark_mode or "fixed",
            round_count=getattr(args, "round_count", None) or yaml_config.round_count,
            round_size=getattr(args, "round_size", None) or yaml_config.round_size,
            round_interval=getattr(args, "round_interval", None) or yaml_config.round_interval,
            test_duration=args.duration if args.duration else yaml_config.test_duration,
            stats_interval=args.stats_interval if args.stats_interval else yaml_config.stats_interval,
            output_dir=args.output_dir if args.output_dir else yaml_config.output_dir,
            filename_prefix=args.filename_prefix if args.filename_prefix else yaml_config.filename_prefix,
            # smap_tool and vm_monitor - use yaml values (no CLI override for these)
            smap_tool_enabled=yaml_config.smap_tool_enabled,
            smap_tool_path=yaml_config.smap_tool_path,
            smap_tool_swap_size=yaml_config.smap_tool_swap_size,
            smap_tool_ratio=yaml_config.smap_tool_ratio,
            smap_tool_src_nid=yaml_config.smap_tool_src_nid,
            smap_tool_dest_nid=yaml_config.smap_tool_dest_nid,
            vm_monitor_enabled=yaml_config.vm_monitor_enabled,
            vm_monitor_vmm_type=yaml_config.vm_monitor_vmm_type,
            vm_monitor_duration=yaml_config.vm_monitor_duration,
            vm_monitor_numa=yaml_config.vm_monitor_numa,
            vm_monitor_log_dir=yaml_config.vm_monitor_log_dir,
            vm_monitor_stress_file=yaml_config.vm_monitor_stress_file,
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        """Build Config from CLI arguments only (no YAML file)"""
        return cls(
            e2b_access_token=args.e2b_access_token or "",
            e2b_api_key=args.e2b_api_key or "",
            e2b_domain=args.e2b_domain or "e2b.app",
            e2b_api_url=args.e2b_api_url or "http://localhost:3000",
            e2b_http_ssl=args.e2b_http_ssl or "false",
            template=args.template or "openclaw-browser-v1",
            create_timeout=args.create_timeout or 86400,
            total_count=args.total or 100,
            detect_existing=args.detect if hasattr(args, "detect") and args.detect else False,
            create_only=args.create_only if hasattr(args, "create_only") and args.create_only else False,
            sandbox_ids_file=args.sandbox_ids_file if args.sandbox_ids_file else None,
            numa_bind=2,  # Default to NUMA node 2 when using CLI args only
            create_batch_size=args.create_batch_size,
            create_batch_interval=args.create_batch_interval,
            task_batch_size=args.task_batch_size,
            task_batch_interval=args.task_batch_interval,
            browser_urls=args.browser_url or ["http://192.168.110.10:8080/Weibo.html"],
            browser_timeout=args.browser_timeout or 200,
            browser_interval_min=args.browser_interval_min or 0.5,
            browser_interval_max=args.browser_interval_max or 3.0,
            # Warmup configuration
            warmup_urls=args.warmup_url or [],
            warmup_loops=args.warmup_loops or 2,
            warmup_delay=args.warmup_delay or 10,
            warmup_only=args.warmup_only if hasattr(args, "warmup_only") and args.warmup_only else False,
            benchmark_percent=args.benchmark_percent if args.benchmark_percent is not None else 1.0,
            # Round-robin mode configuration
            benchmark_mode=getattr(args, "benchmark_mode", None) or "fixed",
            round_count=getattr(args, "round_count", None),
            round_size=getattr(args, "round_size", None),
            round_interval=getattr(args, "round_interval", None) or 30,
            test_duration=args.duration or 600,
            stats_interval=args.stats_interval or 10,
            output_dir=args.output_dir or "results/e2b",
            filename_prefix=args.filename_prefix or "e2b_bench",
            smap_tool_enabled=False,
            smap_tool_path="",
            smap_tool_swap_size=81920,
            smap_tool_ratio=15,
            smap_tool_src_nid=2,
            smap_tool_dest_nid=5,
            vm_monitor_enabled=False,
            vm_monitor_vmm_type="firecracker",
            vm_monitor_duration=600,
            vm_monitor_numa="1",
            vm_monitor_log_dir="results/e2b/vm_monitor",
            vm_monitor_stress_file="/dev/shm/e2b_benchmark_lock",
        )

    def setup_e2b_env(self) -> None:
        """Setup E2B SDK environment variables"""
        if self.e2b_access_token:
            os.environ["E2B_ACCESS_TOKEN"] = self.e2b_access_token
        if self.e2b_api_key:
            os.environ["E2B_API_KEY"] = self.e2b_api_key
        if self.e2b_domain:
            os.environ["E2B_DOMAIN"] = self.e2b_domain
        if self.e2b_api_url:
            os.environ["E2B_API_URL"] = self.e2b_api_url
        if self.e2b_http_ssl:
            os.environ["E2B_HTTP_SSL"] = self.e2b_http_ssl

    @property
    def create_batch_count(self) -> int:
        """Calculate create batch count"""
        if not self.create_batch_size:
            return 1  # Full concurrent treated as 1 batch
        return (self.total_count + self.create_batch_size - 1) // self.create_batch_size

    @property
    def task_batch_count(self) -> int:
        """Calculate task batch count (based on ready sandboxes)"""
        # This will be calculated dynamically based on actual ready sandboxes
        # For planning purposes, use total_count as estimate
        if not self.task_batch_size:
            return 1
        return (self.total_count + self.task_batch_size - 1) // self.task_batch_size

    @property
    def benchmark_count(self) -> int:
        """Calculate actual sandbox count for benchmark phase

        Based on benchmark_percent (e.g., 0.5 = 50% of sandboxes)
        """
        return max(1, int(self.total_count * self.benchmark_percent))
