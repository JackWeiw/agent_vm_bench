"""
Configuration Management Module

Supports YAML config file loading, CLI argument override, OpenStack environment setup
"""

import argparse
import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class Config:
    """Test configuration (unified for VM creation + benchmark)"""

    # === OpenStack Configuration (Phase 0: VM Creation) ===
    auth_source: str = "~/.admin-openrc"  # OpenStack credentials file path
    flavor: str = "2U_4G_40G"
    image: str = "ubuntu-24.04"
    network_id: str = "2661422b-37c4-4d84-90ce-521167c676c0"
    availability_zone: str = "nova_zone:controller"
    start_ip: str = "192.168.110.11"
    subnet_prefix: str = "192.168.110."
    vm_prefix: str = "test_openclaw"
    total_count: int = 80
    create_timeout: int = 1200

    # Special modes
    create_only: bool = False  # Create VMs only, don't benchmark
    detect_existing: bool = False  # Detect existing VMs instead of creating

    # === Create Batch Control ===
    create_batch_size: int = 20  # Default: 20 VMs per batch
    create_batch_interval: int = 3  # Default: 3 seconds between batches

    # === SSH Configuration (Phase 1: Connection) ===
    ssh_port: int = 22
    ssh_username: str = "root"
    ssh_password: str = "openEuler12#$"
    ssh_connect_timeout: int = 30
    ssh_execute_timeout: int = 300

    # === Connect Batch Control ===
    connect_batch_size: Optional[int] = None
    connect_batch_interval: Optional[int] = None

    # === Task Batch Control ===
    task_batch_size: int = 10  # Default: 10 tasks per batch
    task_batch_interval: int = 5  # Default: 5 seconds between batches

    # === Task Mode Selection ===
    task_mode: str = "browser"  # "qa", "stress", "browser", "mixed"

    # === Browser Task Configuration ===
    browser_urls: List[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200
    browser_interval_min: float = 5.0  # Default: 5 seconds
    browser_interval_max: float = 10.0  # Default: 10 seconds
    browser_use_llm: bool = False
    benchmark_percent: float = 1.0

    # Warmup configuration
    warmup_urls: List[str] = field(default_factory=list)
    warmup_loops: int = 1  # Default: 1 loop
    warmup_delay: int = 3  # Default: 3 seconds delay
    warmup_only: bool = False

    # === QA Task Configuration ===
    qa_timeout: int = 600
    qa_init_timeout: int = 600
    qa_interval: float = 0.5
    qa_mode: str = "cli"  # "cli" or "http"

    # === Stress Task Configuration ===
    stress_percent: float = 0.5
    stress_memory_mb: int = 2048
    stress_duration: int = 300
    stress_keepalive: bool = True

    # === Test Run Configuration ===
    test_duration: int = 600
    stats_interval: int = 10
    health_check_interval: float = 5.0

    # === Report Configuration ===
    output_dir: str = "results/vm"
    filename_prefix: str = "vm_bench"

    # === Cleanup Configuration ===
    delete_after_test: bool = False

    @classmethod
    def load_from_yaml(cls, path: str) -> "Config":
        """Load configuration from YAML file"""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Build Config from dictionary"""
        openstack = data.get("openstack", {})
        vm_create = data.get("vm_create", {})
        create_batch = data.get("create_batch", {})
        ssh = data.get("ssh", {})
        connect_batch = data.get("connect_batch", {})
        task_batch = data.get("task_batch", {})
        task = data.get("task", {})
        browser = data.get("browser", {})
        qa = data.get("qa", {})
        stress = data.get("stress", {})
        test = data.get("test", {})
        report = data.get("report", {})
        cleanup = data.get("cleanup", {})

        return cls(
            auth_source=openstack.get("auth_source", "~/.admin-openrc"),
            flavor=vm_create.get("flavor", "2U_4G_40G"),
            image=vm_create.get("image", "ubuntu-24.04"),
            network_id=vm_create.get("network_id", ""),
            availability_zone=vm_create.get("availability_zone", "nova_zone:controller"),
            start_ip=vm_create.get("start_ip", "192.168.110.11"),
            subnet_prefix=vm_create.get("subnet_prefix", "192.168.110."),
            vm_prefix=vm_create.get("vm_prefix", "test_openclaw"),
            total_count=vm_create.get("total_count", 80),
            create_timeout=vm_create.get("create_timeout", 1200),
            create_only=vm_create.get("create_only", False),
            detect_existing=vm_create.get("detect_existing", False),
            create_batch_size=create_batch.get("size", 20) if create_batch else 20,
            create_batch_interval=create_batch.get("interval", 3) if create_batch else 3,
            ssh_port=ssh.get("port", 22),
            ssh_username=ssh.get("username", "root"),
            ssh_password=ssh.get("password", "openEuler12#$"),
            ssh_connect_timeout=ssh.get("connect_timeout", 30),
            ssh_execute_timeout=ssh.get("execute_timeout", 300),
            connect_batch_size=connect_batch.get("size") if connect_batch else None,
            connect_batch_interval=connect_batch.get("interval") if connect_batch else None,
            task_batch_size=task_batch.get("size", 10) if task_batch else 10,
            task_batch_interval=task_batch.get("interval", 5) if task_batch else 5,
            task_mode=task.get("mode", "browser"),
            test_duration=task.get("duration", 600),
            browser_urls=browser.get("urls", ["http://192.168.110.10:8080/Weibo.html"]),
            browser_timeout=browser.get("timeout", 200),
            browser_interval_min=browser.get("interval_min", 5.0),
            browser_interval_max=browser.get("interval_max", 10.0),
            browser_use_llm=browser.get("use_llm", False),
            benchmark_percent=browser.get("benchmark_percent", 1.0),
            warmup_urls=browser.get("warmup_urls", []),
            warmup_loops=browser.get("warmup_loops", 1),
            warmup_delay=browser.get("warmup_delay", 3),
            warmup_only=browser.get("warmup_only", False),
            qa_timeout=qa.get("timeout", 600),
            qa_init_timeout=qa.get("init_timeout", 600),
            qa_interval=qa.get("interval", 0.5),
            qa_mode=qa.get("mode", "cli"),
            stress_percent=stress.get("percent", 0.5),
            stress_memory_mb=stress.get("memory_mb", 2048),
            stress_duration=stress.get("duration", 300),
            stress_keepalive=stress.get("keepalive", True),
            stats_interval=test.get("stats_interval", 10),
            health_check_interval=test.get("health_check_interval", 5.0),
            output_dir=report.get("output_dir", "results/vm"),
            filename_prefix=report.get("filename_prefix", "vm_bench"),
            delete_after_test=cleanup.get("delete_after_test", False),
        )

    @classmethod
    def merge_with_args(cls, yaml_config: "Config", args: argparse.Namespace) -> "Config":
        """Merge CLI arguments (CLI has higher priority than YAML)

        Priority order: CLI > YAML > dataclass defaults
        For Optional/int fields: use 'is not None' check to allow 0 as valid override
        """
        return cls(
            auth_source=args.auth_source if args.auth_source else yaml_config.auth_source,
            flavor=args.flavor if args.flavor else yaml_config.flavor,
            image=args.image if args.image else yaml_config.image,
            network_id=args.network_id if args.network_id else yaml_config.network_id,
            availability_zone=args.az if args.az else yaml_config.availability_zone,
            start_ip=args.start_ip if args.start_ip else yaml_config.start_ip,
            subnet_prefix=args.subnet_prefix if args.subnet_prefix else yaml_config.subnet_prefix,
            vm_prefix=args.vm_prefix if args.vm_prefix else yaml_config.vm_prefix,
            total_count=args.total if args.total is not None else yaml_config.total_count,
            create_timeout=args.create_timeout if args.create_timeout is not None else yaml_config.create_timeout,
            create_only=args.create_only
            if hasattr(args, "create_only") and args.create_only
            else yaml_config.create_only,
            detect_existing=args.detect if hasattr(args, "detect") and args.detect else yaml_config.detect_existing,
            create_batch_size=args.create_batch_size
            if args.create_batch_size is not None
            else yaml_config.create_batch_size,
            create_batch_interval=args.create_batch_interval
            if args.create_batch_interval is not None
            else yaml_config.create_batch_interval,
            ssh_port=args.ssh_port if args.ssh_port is not None else yaml_config.ssh_port,
            ssh_username=args.ssh_username if args.ssh_username else yaml_config.ssh_username,
            ssh_password=args.ssh_password if args.ssh_password else yaml_config.ssh_password,
            ssh_connect_timeout=args.ssh_connect_timeout
            if args.ssh_connect_timeout is not None
            else yaml_config.ssh_connect_timeout,
            connect_batch_size=args.connect_batch_size
            if args.connect_batch_size is not None
            else yaml_config.connect_batch_size,
            connect_batch_interval=args.connect_batch_interval
            if args.connect_batch_interval is not None
            else yaml_config.connect_batch_interval,
            task_batch_size=args.task_batch_size if args.task_batch_size is not None else yaml_config.task_batch_size,
            task_batch_interval=args.task_batch_interval
            if args.task_batch_interval is not None
            else yaml_config.task_batch_interval,
            task_mode=args.task_mode if args.task_mode else yaml_config.task_mode,
            test_duration=args.duration if args.duration is not None else yaml_config.test_duration,
            browser_urls=args.browser_url if args.browser_url else yaml_config.browser_urls,
            browser_timeout=args.browser_timeout if args.browser_timeout is not None else yaml_config.browser_timeout,
            browser_interval_min=args.browser_interval_min
            if args.browser_interval_min is not None
            else yaml_config.browser_interval_min,
            browser_interval_max=args.browser_interval_max
            if args.browser_interval_max is not None
            else yaml_config.browser_interval_max,
            browser_use_llm=args.browser_use_llm
            if hasattr(args, "browser_use_llm") and args.browser_use_llm
            else yaml_config.browser_use_llm,
            benchmark_percent=args.benchmark_percent
            if args.benchmark_percent is not None
            else yaml_config.benchmark_percent,
            warmup_urls=args.warmup_url if args.warmup_url else yaml_config.warmup_urls,
            warmup_loops=args.warmup_loops if args.warmup_loops is not None else yaml_config.warmup_loops,
            warmup_delay=args.warmup_delay if args.warmup_delay is not None else yaml_config.warmup_delay,
            warmup_only=args.warmup_only
            if hasattr(args, "warmup_only") and args.warmup_only
            else yaml_config.warmup_only,
            qa_timeout=args.qa_timeout if args.qa_timeout is not None else yaml_config.qa_timeout,
            qa_init_timeout=args.qa_init_timeout if args.qa_init_timeout is not None else yaml_config.qa_init_timeout,
            qa_interval=args.qa_interval if args.qa_interval is not None else yaml_config.qa_interval,
            qa_mode=args.qa_mode if args.qa_mode else yaml_config.qa_mode,
            stress_percent=args.stress_percent if args.stress_percent is not None else yaml_config.stress_percent,
            stress_memory_mb=args.stress_memory if args.stress_memory is not None else yaml_config.stress_memory_mb,
            stress_keepalive=not args.no_keepalive
            if hasattr(args, "no_keepalive") and args.no_keepalive
            else yaml_config.stress_keepalive,
            stats_interval=args.stats_interval if args.stats_interval is not None else yaml_config.stats_interval,
            output_dir=args.output_dir if args.output_dir else yaml_config.output_dir,
            filename_prefix=args.filename_prefix if args.filename_prefix else yaml_config.filename_prefix,
            delete_after_test=args.delete_after_test
            if hasattr(args, "delete_after_test") and args.delete_after_test
            else yaml_config.delete_after_test,
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        """Build Config from CLI arguments only (no YAML file)

        Uses dataclass defaults when CLI args are not provided.
        """
        return cls(
            auth_source=args.auth_source or "~/.admin-openrc",
            flavor=args.flavor or "2U_4G_40G",
            image=args.image or "ubuntu-24.04",
            network_id=args.network_id or "",
            availability_zone=args.az or "nova_zone:controller",
            start_ip=args.start_ip or "192.168.110.11",
            subnet_prefix=args.subnet_prefix or "192.168.110.",
            vm_prefix=args.vm_prefix or "test_openclaw",
            total_count=args.total if args.total is not None else 80,
            create_timeout=args.create_timeout if args.create_timeout is not None else 1200,
            create_only=args.create_only if hasattr(args, "create_only") and args.create_only else False,
            detect_existing=args.detect if hasattr(args, "detect") and args.detect else False,
            create_batch_size=args.create_batch_size if args.create_batch_size is not None else 20,
            create_batch_interval=args.create_batch_interval if args.create_batch_interval is not None else 3,
            ssh_port=args.ssh_port if hasattr(args, "ssh_port") and args.ssh_port is not None else 22,
            ssh_username=args.ssh_username if hasattr(args, "ssh_username") and args.ssh_username else "root",
            ssh_password=args.ssh_password if hasattr(args, "ssh_password") and args.ssh_password else "openEuler12#$",
            ssh_connect_timeout=args.ssh_connect_timeout
            if hasattr(args, "ssh_connect_timeout") and args.ssh_connect_timeout is not None
            else 30,
            ssh_execute_timeout=args.ssh_execute_timeout
            if hasattr(args, "ssh_execute_timeout") and args.ssh_execute_timeout is not None
            else 300,
            connect_batch_size=args.connect_batch_size
            if hasattr(args, "connect_batch_size") and args.connect_batch_size is not None
            else None,
            connect_batch_interval=args.connect_batch_interval
            if hasattr(args, "connect_batch_interval") and args.connect_batch_interval is not None
            else None,
            task_batch_size=args.task_batch_size
            if hasattr(args, "task_batch_size") and args.task_batch_size is not None
            else 10,
            task_batch_interval=args.task_batch_interval
            if hasattr(args, "task_batch_interval") and args.task_batch_interval is not None
            else 5,
            task_mode=args.task_mode if hasattr(args, "task_mode") and args.task_mode else "browser",
            test_duration=args.duration if hasattr(args, "duration") and args.duration is not None else 600,
            browser_urls=args.browser_url
            if hasattr(args, "browser_url") and args.browser_url
            else ["http://192.168.110.10:8080/Weibo.html"],
            browser_timeout=args.browser_timeout
            if hasattr(args, "browser_timeout") and args.browser_timeout is not None
            else 200,
            browser_interval_min=args.browser_interval_min
            if hasattr(args, "browser_interval_min") and args.browser_interval_min is not None
            else 5.0,
            browser_interval_max=args.browser_interval_max
            if hasattr(args, "browser_interval_max") and args.browser_interval_max is not None
            else 10.0,
            browser_use_llm=args.browser_use_llm
            if hasattr(args, "browser_use_llm") and args.browser_use_llm
            else False,
            benchmark_percent=args.benchmark_percent
            if hasattr(args, "benchmark_percent") and args.benchmark_percent is not None
            else 1.0,
            warmup_urls=args.warmup_url if hasattr(args, "warmup_url") and args.warmup_url else [],
            warmup_loops=args.warmup_loops if hasattr(args, "warmup_loops") and args.warmup_loops is not None else 1,
            warmup_delay=args.warmup_delay if hasattr(args, "warmup_delay") and args.warmup_delay is not None else 3,
            warmup_only=args.warmup_only if hasattr(args, "warmup_only") and args.warmup_only else False,
            qa_timeout=args.qa_timeout if hasattr(args, "qa_timeout") and args.qa_timeout is not None else 600,
            qa_init_timeout=args.qa_init_timeout
            if hasattr(args, "qa_init_timeout") and args.qa_init_timeout is not None
            else 600,
            qa_interval=args.qa_interval if hasattr(args, "qa_interval") and args.qa_interval is not None else 0.5,
            qa_mode=args.qa_mode if hasattr(args, "qa_mode") and args.qa_mode else "cli",
            stress_percent=args.stress_percent
            if hasattr(args, "stress_percent") and args.stress_percent is not None
            else 0.5,
            stress_memory_mb=args.stress_memory
            if hasattr(args, "stress_memory") and args.stress_memory is not None
            else 2048,
            stress_keepalive=not args.no_keepalive if hasattr(args, "no_keepalive") and args.no_keepalive else True,
            stats_interval=args.stats_interval
            if hasattr(args, "stats_interval") and args.stats_interval is not None
            else 10,
            output_dir=args.output_dir if hasattr(args, "output_dir") and args.output_dir else "results/vm",
            filename_prefix=args.filename_prefix
            if hasattr(args, "filename_prefix") and args.filename_prefix
            else "vm_bench",
            delete_after_test=args.delete_after_test
            if hasattr(args, "delete_after_test") and args.delete_after_test
            else False,
        )

    def load_openrc(self) -> dict:
        """Load OpenStack environment variables from openrc file"""
        openrc_path = os.path.expanduser(self.auth_source)
        if not os.path.exists(openrc_path):
            print(f"[OpenStack] {openrc_path} not found")
            return {}

        env = {}
        with open(openrc_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    m = re.match(r"export\s+(\w+)=(.*)", line)
                    if m:
                        key, val = m.group(1), m.group(2).strip("'\"")
                        env[key] = val
        return env

    def get_os_env(self) -> dict:
        """Load openrc environment variables, remove http proxy"""
        env = os.environ.copy()
        env.update(self.load_openrc())
        env.pop("http_proxy", None)
        env.pop("https_proxy", None)
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        return env

    def get_ip_range(self) -> List[str]:
        """Generate IP list based on start_ip and total_count"""
        start = ipaddress.IPv4Address(self.start_ip)
        return [str(start + i) for i in range(self.total_count)]

    @property
    def create_batch_count(self) -> int:
        """Calculate create batch count"""
        if not self.create_batch_size:
            return 1
        return (self.total_count + self.create_batch_size - 1) // self.create_batch_size

    @property
    def connect_batch_count(self) -> int:
        """Calculate connect batch count"""
        if not self.connect_batch_size:
            return 1
        return (self.total_count + self.connect_batch_size - 1) // self.connect_batch_size

    @property
    def task_batch_count(self) -> int:
        """Calculate task batch count"""
        if not self.task_batch_size:
            return 1
        return (self.total_count + self.task_batch_size - 1) // self.task_batch_size

    @property
    def stress_vm_count(self) -> int:
        """Calculate stress VM count"""
        return int(self.total_count * self.stress_percent)

    @property
    def benchmark_vm_count(self) -> int:
        """Calculate actual VM count for benchmark"""
        return max(1, int(self.total_count * self.benchmark_percent))
