"""
Configuration Management Module

Supports YAML config file loading, CLI argument override, E2B environment variable setup
"""

import os
import argparse
import yaml
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


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

    # Detect existing sandboxes mode
    detect_existing: bool = False  # Detect existing sandboxes instead of creating new ones

    # Batch control (None means full concurrent)
    batch_size: Optional[int] = 20
    batch_interval: Optional[int] = 30

    # Browser task
    browser_urls: List[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200
    browser_interval_min: float = 0.5
    browser_interval_max: float = 3.0

    # Test run
    test_duration: int = 600
    stats_interval: int = 10

    # Report
    output_dir: str = "results/e2b"
    filename_prefix: str = "e2b_bench"

    @classmethod
    def load_from_yaml(cls, path: str) -> 'Config':
        """Load configuration from YAML file"""
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> 'Config':
        """Build Config from dictionary"""
        e2b_env = data.get('e2b_env', {})
        sandbox = data.get('sandbox', {})
        batch = data.get('batch', {})
        browser = data.get('browser', {})
        test = data.get('test', {})
        report = data.get('report', {})

        return cls(
            e2b_access_token=e2b_env.get('E2B_ACCESS_TOKEN', ""),
            e2b_api_key=e2b_env.get('E2B_API_KEY', ""),
            e2b_domain=e2b_env.get('E2B_DOMAIN', "e2b.app"),
            e2b_api_url=e2b_env.get('E2B_API_URL', "http://localhost:3000"),
            e2b_http_ssl=e2b_env.get('E2B_HTTP_SSL', "false"),

            template=sandbox.get('template', "openclaw-browser-v1"),
            create_timeout=sandbox.get('create_timeout', 86400),
            total_count=sandbox.get('total_count', 100),
            detect_existing=sandbox.get('detect_existing', False),

            batch_size=batch.get('size') if batch else None,
            batch_interval=batch.get('interval') if batch else None,

            browser_urls=browser.get('urls', ["http://192.168.110.10:8080/Weibo.html"]),
            browser_timeout=browser.get('task_timeout', 200),
            browser_interval_min=browser.get('interval_min', 0.5),
            browser_interval_max=browser.get('interval_max', 3.0),

            test_duration=test.get('duration', 600),
            stats_interval=test.get('stats_interval', 10),

            output_dir=report.get('output_dir', "results/e2b"),
            filename_prefix=report.get('filename_prefix', "e2b_bench"),
        )

    @classmethod
    def merge_with_args(cls, yaml_config: 'Config', args: argparse.Namespace) -> 'Config':
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
            detect_existing=args.detect if hasattr(args, 'detect') and args.detect else yaml_config.detect_existing,

            batch_size=args.batch_size if args.batch_size is not None else yaml_config.batch_size,
            batch_interval=args.batch_interval if args.batch_interval is not None else yaml_config.batch_interval,

            browser_urls=args.browser_url if args.browser_url else yaml_config.browser_urls,
            browser_timeout=args.browser_timeout if args.browser_timeout else yaml_config.browser_timeout,
            browser_interval_min=args.browser_interval_min if args.browser_interval_min else yaml_config.browser_interval_min,
            browser_interval_max=args.browser_interval_max if args.browser_interval_max else yaml_config.browser_interval_max,

            test_duration=args.duration if args.duration else yaml_config.test_duration,
            stats_interval=args.stats_interval if args.stats_interval else yaml_config.stats_interval,

            output_dir=args.output_dir if args.output_dir else yaml_config.output_dir,
            filename_prefix=args.filename_prefix if args.filename_prefix else yaml_config.filename_prefix,
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Config':
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
            detect_existing=args.detect if hasattr(args, 'detect') and args.detect else False,

            batch_size=args.batch_size,
            batch_interval=args.batch_interval,

            browser_urls=args.browser_url or ["http://192.168.110.10:8080/Weibo.html"],
            browser_timeout=args.browser_timeout or 200,
            browser_interval_min=args.browser_interval_min or 0.5,
            browser_interval_max=args.browser_interval_max or 3.0,

            test_duration=args.duration or 600,
            stats_interval=args.stats_interval or 10,

            output_dir=args.output_dir or "results/e2b",
            filename_prefix=args.filename_prefix or "e2b_bench",
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
    def batch_count(self) -> int:
        """Calculate batch count"""
        if not self.batch_size:
            return 1  # Full concurrent treated as 1 batch
        return (self.total_count + self.batch_size - 1) // self.batch_size