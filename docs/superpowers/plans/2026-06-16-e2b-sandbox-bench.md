# E2B Sandbox Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建E2B沙箱批量性能测试套件，实现沙箱创建性能收集、浏览器任务执行、存活监控和报告生成。

**Architecture:** 模块化设计，7个独立文件各司其职。通过Config对象传递配置，SandboxState保留沙箱句柄供后续使用，StatsCollector收集实时快照和生成最终报告。

**Tech Stack:** Python 3.10+, E2B SDK, YAML (PyYAML), dataclasses, threading, argparse

---

## File Structure

```
e2b_bench/
├── __init__.py           # 包初始化
├── bench.py              # 主入口
├── config.py             # 配置管理
├── sandbox_manager.py    # 沙箱管理
├── task_runner.py        # 任务执行
├── stats_collector.py    # 统计收集
├── schemas.py            # 数据结构
└── utils.py              # 工具函数

config/
└── default.yaml          # 默认配置示例
```

---

### Task 1: 创建项目目录结构和配置文件

**Files:**
- Create: `e2b_bench/__init__.py`
- Create: `config/default.yaml`

- [ ] **Step 1: 创建e2b_bench目录和__init__.py**

```python
# e2b_bench/__init__.py
"""
E2B Sandbox Bench - E2B沙箱批量性能测试套件

功能：
- 批量创建E2B沙箱，收集启动性能
- 执行浏览器任务，收集执行性能
- 监控沙箱存活情况
- 支持分批启动和随机任务间隔
- 实时统计快照 + 最终报告
"""

__version__ = "1.0.0"
__all__ = [
    'Config',
    'SandboxState',
    'SandboxStatus',
    'BrowserMetrics',
    'run_benchmark',
]
```

- [ ] **Step 2: 创建config目录和default.yaml配置文件**

```yaml
# config/default.yaml
# E2B SDK 环境变量配置
e2b_env:
  E2B_ACCESS_TOKEN: "sk_e2b_17bd3933af21f80dc10bba686691c4fcd7057123"
  E2B_API_KEY: "e2b_5ec17bd3933af21f80dc10bba686691c4fcd7057"
  E2B_DOMAIN: "e2b.app"
  E2B_API_URL: "http://localhost:3000"
  E2B_HTTP_SSL: "false"

# 沙箱配置
sandbox:
  template: "openclaw-browser-v1"
  create_timeout: 86400
  total_count: 100

# 批量启动控制（可选，不配置则全并发）
batch:
  size: 20
  interval: 30

# 浏览器任务配置
browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  task_timeout: 200
  interval_min: 0.5
  interval_max: 3.0

# 测试运行配置
test:
  duration: 600
  stats_interval: 10

# 报告配置
report:
  output_dir: "results/e2b"
  filename_prefix: "e2b_bench"
```

- [ ] **Step 3: Commit**

```bash
git add e2b_bench/__init__.py config/default.yaml
git commit -m "feat(e2b_bench): initialize project structure and default config"
```

---

### Task 2: 实现数据结构模块 (schemas.py)

**Files:**
- Create: `e2b_bench/schemas.py`

- [ ] **Step 1: 创建schemas.py，定义所有数据结构**

```python
# e2b_bench/schemas.py
"""
数据结构定义模块

定义SandboxStatus、CreationMetrics、BrowserMetrics、SandboxState、TestSnapshot
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum
import statistics


class SandboxStatus(Enum):
    """沙箱状态枚举"""
    PENDING = "pending"       # 等待创建
    CREATING = "creating"     # 正在创建
    ACTIVE = "active"         # 已激活，可执行任务
    FAILED = "failed"         # 创建失败
    OFFLINE = "offline"       # 运行时离线
    CLOSED = "closed"         # 已关闭


@dataclass
class CreationMetrics:
    """沙箱创建性能指标"""
    submit_time: float = 0.0       # 提交创建时间
    ready_time: float = 0.0        # 沙箱就绪时间
    elapsed: float = 0.0           # 创建耗时（秒）
    status: SandboxStatus = SandboxStatus.PENDING
    error_msg: str = ""


@dataclass
class BrowserMetrics:
    """浏览器任务指标"""
    total_tasks: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    
    def add(self, latency: float, success: bool, timeout: bool = False) -> None:
        """添加一次任务结果"""
        self.total_tasks += 1
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
        """平均延迟（秒）"""
        return statistics.mean(self.latencies) if self.latencies else 0.0
    
    @property
    def p99_latency(self) -> float:
        """P99延迟（秒）"""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        if len(sorted_lat) >= 100:
            return sorted_lat[int(len(sorted_lat) * 0.99)]
        return sorted_lat[-1]


@dataclass
class SandboxState:
    """沙箱完整状态"""
    sandbox_id: int              # 序号（1, 2, 3...）
    sandbox_obj: Optional[object] = None  # E2B Sandbox对象引用（句柄）
    batch_id: int = -1           # 所属批次
    
    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    
    is_alive: bool = True        # 沙箱是否存活
    last_task_time: float = 0.0  # 最后一次任务执行时间
    consecutive_failures: int = 0  # 连续失败次数


@dataclass
class TestSnapshot:
    """测试快照"""
    timestamp: float             # 快照时间戳
    elapsed: float               # 从测试开始到现在的时间（秒）
    total_sandboxes: int         # 沙箱总数
    active_sandboxes: int        # 活跃沙箱数
    offline_sandboxes: int       # 离线沙箱数
    creation_stats: Dict[str, float] = field(default_factory=dict)  # {min, max, avg, p50, p95, p99}
    browser_total: int = 0       # 浏览器任务总数
    browser_success: int = 0     # 成功任务数
    browser_avg_latency: float = 0.0  # 平均延迟
    browser_p99_latency: float = 0.0  # P99延迟
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/schemas.py
git commit -m "feat(e2b_bench): add data structure definitions (schemas.py)"
```

---

### Task 3: 实现工具函数模块 (utils.py)

**Files:**
- Create: `e2b_bench/utils.py`

- [ ] **Step 1: 创建utils.py，定义辅助函数**

```python
# e2b_bench/utils.py
"""
工具函数模块

提供日志格式化、时间处理、百分位计算等辅助函数
"""

import logging
import statistics
from datetime import datetime
from typing import List, Dict


def setup_logging(level: int = logging.INFO) -> None:
    """设置日志格式"""
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def format_timestamp(ts: float) -> str:
    """格式化时间戳为 HH:MM:SS"""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_duration(seconds: float) -> str:
    """格式化时长为易读格式"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def calc_percentiles(values: List[float]) -> Dict[str, float]:
    """计算百分位统计
    
    返回: {"min": x, "max": x, "avg": x, "p50": x, "p95": x, "p99": x}
    """
    if not values:
        return {"min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}
    
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    
    def percentile(p: float) -> float:
        idx = int(n * p / 100)
        idx = min(idx, n - 1)
        return sorted_vals[idx]
    
    return {
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "avg": statistics.mean(values),
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99)
    }


def calc_p99(values: List[float]) -> float:
    """计算P99延迟"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) >= 100:
        return sorted_vals[int(len(sorted_vals) * 0.99)]
    return sorted_vals[-1]
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/utils.py
git commit -m "feat(e2b_bench): add utility functions (utils.py)"
```

---

### Task 4: 实现配置管理模块 (config.py)

**Files:**
- Create: `e2b_bench/config.py`

- [ ] **Step 1: 创建config.py，实现Config类和配置加载**

```python
# e2b_bench/config.py
"""
配置管理模块

支持YAML配置文件加载、命令行参数覆盖、E2B环境变量设置
"""

import os
import argparse
import yaml
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class Config:
    """测试配置"""
    # E2B环境变量
    e2b_access_token: str = ""
    e2b_api_key: str = ""
    e2b_domain: str = "e2b.app"
    e2b_api_url: str = "http://localhost:3000"
    e2b_http_ssl: str = "false"
    
    # 沙箱配置
    template: str = "openclaw-browser-v1"
    create_timeout: int = 86400
    total_count: int = 100
    
    # 批量控制（None表示全并发）
    batch_size: Optional[int] = 20
    batch_interval: Optional[int] = 30
    
    # 浏览器任务
    browser_urls: List[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200
    browser_interval_min: float = 0.5
    browser_interval_max: float = 3.0
    
    # 测试运行
    test_duration: int = 600
    stats_interval: int = 10
    
    # 报告
    output_dir: str = "results/e2b"
    filename_prefix: str = "e2b_bench"
    
    @classmethod
    def load_from_yaml(cls, path: str) -> cls:
        """从YAML文件加载配置"""
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        return cls._from_dict(data)
    
    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> cls:
        """从字典构建Config"""
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
    def merge_with_args(cls, yaml_config: cls, args: argparse.Namespace) -> cls:
        """合并命令行参数（命令行优先级更高）"""
        # 命令行参数覆盖YAML配置
        return cls(
            e2b_access_token=args.e2b_access_token if args.e2b_access_token else yaml_config.e2b_access_token,
            e2b_api_key=args.e2b_api_key if args.e2b_api_key else yaml_config.e2b_api_key,
            e2b_domain=args.e2b_domain if args.e2b_domain else yaml_config.e2b_domain,
            e2b_api_url=args.e2b_api_url if args.e2b_api_url else yaml_config.e2b_api_url,
            e2b_http_ssl=args.e2b_http_ssl if args.e2b_http_ssl else yaml_config.e2b_http_ssl,
            
            template=args.template if args.template else yaml_config.template,
            create_timeout=args.create_timeout if args.create_timeout else yaml_config.create_timeout,
            total_count=args.total if args.total else yaml_config.total_count,
            
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
    def from_args(cls, args: argparse.Namespace) -> cls:
        """仅从命令行参数构建Config（无YAML文件时）"""
        return cls(
            e2b_access_token=args.e2b_access_token or "",
            e2b_api_key=args.e2b_api_key or "",
            e2b_domain=args.e2b_domain or "e2b.app",
            e2b_api_url=args.e2b_api_url or "http://localhost:3000",
            e2b_http_ssl=args.e2b_http_ssl or "false",
            
            template=args.template or "openclaw-browser-v1",
            create_timeout=args.create_timeout or 86400,
            total_count=args.total or 100,
            
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
        """设置E2B SDK环境变量"""
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
        """计算批次数量"""
        if not self.batch_size:
            return 1  # 全并发视为1批
        return (self.total_count + self.batch_size - 1) // self.batch_size
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/config.py
git commit -m "feat(e2b_bench): add configuration management (config.py)"
```

---

### Task 5: 实现沙箱管理模块 (sandbox_manager.py)

**Files:**
- Create: `e2b_bench/sandbox_manager.py`

- [ ] **Step 1: 创建sandbox_manager.py，实现SandboxManager类**

```python
# e2b_bench/sandbox_manager.py
"""
沙箱管理模块

负责E2B沙箱的创建、健康检查、批量控制和关闭
保留沙箱句柄供后续任务执行使用
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple, Optional
from threading import Event

try:
    from e2b import Sandbox
except ImportError:
    # Mock for development/testing without E2B SDK
    class Sandbox:
        @staticmethod
        def create(template, timeout=86400):
            class MockSandbox:
                def commands_run(self, cmd, timeout=60, user="root"):
                    class Result:
                        exit_code = 0
                    return Result()
                def close(self):
                    pass
            return MockSandbox()

from .config import Config
from .schemas import SandboxState, SandboxStatus


class SandboxManager:
    """沙箱生命周期管理"""
    
    def __init__(self, config: Config, stop_event: Event):
        self.config = config
        self.stop_event = stop_event
        self.sandbox_states: Dict[int, SandboxState] = {}
    
    def create_all(self) -> Dict[int, SandboxState]:
        """批量创建沙箱
        
        根据batch配置决定策略：
        - 有batch_size：分批创建，避免资源突增
        - 无配置：全并发创建，测试极限性能
        
        返回: {sandbox_id: SandboxState}
        """
        if self.config.batch_size and self.config.batch_size > 0:
            return self._create_batched()
        else:
            return self._create_concurrent()
    
    def _create_batched(self) -> Dict[int, SandboxState]:
        """分批创建沙箱"""
        total = self.config.total_count
        batch_size = self.config.batch_size
        batch_count = self.config.batch_count
        
        print(f"\n{'='*60}")
        print(f"Batched Sandbox Creation")
        print(f"  Total: {total} sandboxes")
        print(f"  Batches: {batch_count} x {batch_size}")
        print(f"  Interval: {self.config.batch_interval}s")
        print(f"{'='*60}")
        
        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                print("Stop event detected, aborting creation")
                break
            
            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)
            
            print(f"\n[Batch {batch_id}/{batch_count-1}] Creating sandboxes {start_idx+1}-{end_idx}")
            
            # 并发创建当前批次
            batch_states = self._create_batch_concurrent(batch_id, start_idx, end_idx)
            self.sandbox_states.update(batch_states)
            
            # 批次间等待（最后一批不等待）
            if batch_id < batch_count - 1 and self.config.batch_interval:
                print(f"Waiting {self.config.batch_interval}s before next batch...")
                time.sleep(self.config.batch_interval)
        
        return self.sandbox_states
    
    def _create_batch_concurrent(self, batch_id: int, start: int, end: int) -> Dict[int, SandboxState]:
        """并发创建一个批次的沙箱"""
        states: Dict[int, SandboxState] = {}
        
        with ThreadPoolExecutor(max_workers=end - start) as executor:
            futures = {}
            
            for i in range(start, end):
                sandbox_id = i + 1
                state = SandboxState(sandbox_id=sandbox_id, batch_id=batch_id)
                self.sandbox_states[sandbox_id] = state
                future = executor.submit(self._create_single, state)
                futures[future] = sandbox_id
            
            for future in as_completed(futures):
                sandbox_id = futures[future]
                state = self.sandbox_states[sandbox_id]
                
                try:
                    success, elapsed, error = future.result()
                    if success:
                        state.creation_metrics.status = SandboxStatus.ACTIVE
                        state.creation_metrics.elapsed = elapsed
                        print(f"[Sandbox{sandbox_id}] Created in {elapsed:.1f}s")
                    else:
                        state.creation_metrics.status = SandboxStatus.FAILED
                        state.creation_metrics.error_msg = error
                        print(f"[Sandbox{sandbox_id}] Failed: {error[:80]}")
                except Exception as e:
                    state.creation_metrics.status = SandboxStatus.FAILED
                    state.creation_metrics.error_msg = str(e)
                    print(f"[Sandbox{sandbox_id}] Exception: {str(e)[:80]}")
        
        return {i + 1: self.sandbox_states[i + 1] for i in range(start, end)}
    
    def _create_concurrent(self) -> Dict[int, SandboxState]:
        """全并发创建所有沙箱"""
        total = self.config.total_count
        
        print(f"\n{'='*60}")
        print(f"Concurrent Sandbox Creation")
        print(f"  Total: {total} sandboxes (full concurrent)")
        print(f"{'='*60}")
        
        return self._create_batch_concurrent(batch_id=0, start=0, end=total)
    
    def _create_single(self, state: SandboxState) -> Tuple[bool, float, str]:
        """创建单个沙箱
        
        关键：保留沙箱句柄到 state.sandbox_obj
        
        返回: (success, elapsed_seconds, error_message)
        """
        state.creation_metrics.status = SandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()
        
        try:
            sbx = Sandbox.create(
                self.config.template,
                timeout=self.config.create_timeout
            )
            # 保留沙箱句柄
            state.sandbox_obj = sbx
            state.creation_metrics.ready_time = time.time()
            elapsed = state.creation_metrics.ready_time - state.creation_metrics.submit_time
            return True, elapsed, ""
        except Exception as e:
            state.creation_metrics.ready_time = time.time()
            return False, 0.0, str(e)
    
    def check_alive(self, state: SandboxState) -> bool:
        """检查沙箱是否存活"""
        sbx = state.sandbox_obj
        if not sbx or not state.is_alive:
            return False
        try:
            result = sbx.commands.run("echo alive", timeout=10, user="root")
            return result.exit_code == 0
        except Exception:
            return False
    
    def close_all(self) -> None:
        """关闭所有沙箱"""
        print("\nClosing all sandboxes...")
        closed_count = 0
        for state in self.sandbox_states.values():
            if state.sandbox_obj:
                try:
                    state.sandbox_obj.close()
                    state.creation_metrics.status = SandboxStatus.CLOSED
                    closed_count += 1
                except Exception as e:
                    print(f"[Sandbox{state.sandbox_id}] Close error: {str(e)[:50]}")
        print(f"Closed {closed_count} sandboxes")
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/sandbox_manager.py
git commit -m "feat(e2b_bench): add sandbox manager with handle preservation"
```

---

### Task 6: 实现任务执行模块 (task_runner.py)

**Files:**
- Create: `e2b_bench/task_runner.py`

- [ ] **Step 1: 创建task_runner.py，实现BrowserTaskRunner和TaskManager**

```python
# e2b_bench/task_runner.py
"""
任务执行模块

负责浏览器任务的执行、结果收集和异常处理
每个沙箱一个独立线程
"""

import time
import random
import threading
from typing import Tuple, List, Dict

from .config import Config
from .schemas import SandboxState, SandboxStatus


class BrowserTaskRunner(threading.Thread):
    """浏览器任务执行器（每个沙箱一个独立线程）"""
    
    def __init__(
        self,
        state: SandboxState,
        config: Config,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.consecutive_errors = 0
    
    def run(self) -> None:
        """任务执行主循环"""
        # 等待沙箱创建完成
        while not self.stop_event.is_set():
            if self.state.creation_metrics.status == SandboxStatus.ACTIVE:
                break
            if self.state.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.OFFLINE, SandboxStatus.CLOSED):
                print(f"[Sandbox{self.state.sandbox_id}] Cannot start tasks: {self.state.creation_metrics.status.value}")
                return
            time.sleep(0.5)
        
        # 执行浏览器任务循环
        while not self.stop_event.is_set():
            if not self.state.is_alive:
                print(f"[Sandbox{self.state.sandbox_id}] Sandbox offline, stopping tasks")
                break
            
            # 执行单个浏览器任务
            success, latency = self._run_single_task()
            
            # 更新指标
            timeout = latency > self.config.browser_timeout
            self.state.browser_metrics.add(latency, success and not timeout, timeout)
            self.state.last_task_time = time.time()
            
            # 错误处理
            if success and not timeout:
                self.consecutive_errors = 0
            else:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.state.is_alive = False
                    print(f"[Sandbox{self.state.sandbox_id}] Marked offline (3 consecutive failures)")
                    break
            
            # 随机间隔，避免请求突增
            sleep_time = random.uniform(
                self.config.browser_interval_min,
                self.config.browser_interval_max
            )
            time.sleep(sleep_time)
        
        print(f"[Sandbox{self.state.sandbox_id}] Task runner ended")
    
    def _run_single_task(self) -> Tuple[bool, float]:
        """执行单个浏览器任务
        
        使用 state.sandbox_obj 句柄执行命令
        
        返回: (success, latency_seconds)
        """
        sbx = self.state.sandbox_obj
        if not sbx:
            return False, 0.0
        
        # 获取当前URL（轮询方式）
        url_idx = self.state.browser_metrics.total_tasks % len(self.config.browser_urls)
        url = self.config.browser_urls[url_idx]
        
        # 构建浏览器命令
        cmd = f"openclaw browser --browser-profile openclaw open '{url}'"
        
        start_time = time.perf_counter()
        try:
            result = sbx.commands.run(
                cmd,
                timeout=self.config.browser_timeout + 30,
                user="root"
            )
            elapsed = time.perf_counter() - start_time
            
            success = result.exit_code == 0
            return success, elapsed
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            print(f"[Sandbox{self.state.sandbox_id}] Task error: {str(e)[:50]}")
            return False, elapsed


class TaskManager:
    """任务管理器 - 管理所有沙箱的任务执行线程"""
    
    def __init__(
        self,
        config: Config,
        sandbox_states: Dict[int, SandboxState],
        stop_event: threading.Event,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.runners: List[BrowserTaskRunner] = []
    
    def start_all(self) -> None:
        """启动所有ACTIVE沙箱的任务执行线程"""
        active_count = 0
        for state in self.sandbox_states.values():
            if state.creation_metrics.status == SandboxStatus.ACTIVE:
                runner = BrowserTaskRunner(state, self.config, self.stop_event)
                self.runners.append(runner)
                runner.start()
                active_count += 1
        
        print(f"\nStarted {active_count} task runners")
    
    def wait_all(self, timeout: float = 5.0) -> None:
        """等待所有任务线程结束"""
        for runner in self.runners:
            runner.join(timeout=timeout)
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/task_runner.py
git commit -m "feat(e2b_bench): add browser task runner and task manager"
```

---

### Task 7: 实现统计收集模块 (stats_collector.py)

**Files:**
- Create: `e2b_bench/stats_collector.py`

- [ ] **Step 1: 创建stats_collector.py，实现StatsCollector类**

```python
# e2b_bench/stats_collector.py
"""
统计收集模块

负责实时快照收集、终端输出和最终报告生成
"""

import time
import threading
import statistics
import os
from datetime import datetime
from typing import List, Dict, Optional

from .config import Config
from .schemas import SandboxState, SandboxStatus, TestSnapshot
from .utils import calc_percentiles, calc_p99


class StatsCollector:
    """统计收集器 - 实时快照 + 最终报告"""
    
    def __init__(self, config: Config, sandbox_states: Dict[int, SandboxState]):
        self.config = config
        self.sandbox_states = sandbox_states
        self.snapshots: List[TestSnapshot] = []
        self.start_time: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
    
    def start(self) -> None:
        """启动后台收集线程"""
        self.start_time = time.time()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()
    
    def stop(self) -> None:
        """停止收集"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
    
    def _collect_loop(self) -> None:
        """定期收集快照"""
        while not self._stop.is_set():
            self._take_snapshot()
            time.sleep(self.config.stats_interval)
    
    def _take_snapshot(self) -> None:
        """收集当前时刻的统计快照"""
        now = time.time()
        elapsed = now - self.start_time
        
        # 沙箱状态统计
        active_count = sum(
            1 for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.ACTIVE and s.is_alive
        )
        offline_count = sum(
            1 for s in self.sandbox_states.values()
            if not s.is_alive or s.creation_metrics.status in (SandboxStatus.FAILED, SandboxStatus.OFFLINE)
        )
        
        # 创建性能统计（仅计算成功的沙箱）
        creation_times = [
            s.creation_metrics.elapsed for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.ACTIVE and s.creation_metrics.elapsed > 0
        ]
        creation_stats = calc_percentiles(creation_times)
        
        # 浏览器任务统计
        browser_total = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        browser_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        
        # 收集最近的延迟数据（每个沙箱最近10条）
        all_latencies: List[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.browser_metrics.latencies[-10:])
        
        browser_avg = statistics.mean(all_latencies) if all_latencies else 0.0
        browser_p99 = calc_p99(all_latencies)
        
        snapshot = TestSnapshot(
            timestamp=now,
            elapsed=elapsed,
            total_sandboxes=len(self.sandbox_states),
            active_sandboxes=active_count,
            offline_sandboxes=offline_count,
            creation_stats=creation_stats,
            browser_total=browser_total,
            browser_success=browser_success,
            browser_avg_latency=browser_avg,
            browser_p99_latency=browser_p99
        )
        self.snapshots.append(snapshot)
        
        # 实时终端输出
        self._print_snapshot(snapshot)
    
    def _print_snapshot(self, snapshot: TestSnapshot) -> None:
        """打印实时快照"""
        print(f"\n{'─'*70}")
        print(f"T+{snapshot.elapsed:6.1f}s  Status Snapshot")
        print(f"{'─'*70}")
        print(f"  Sandboxes: {snapshot.active_sandboxes:3d} active / {snapshot.offline_sandboxes:2d} offline")
        
        if snapshot.creation_stats["avg"] > 0:
            print(f"  Creation:  avg={snapshot.creation_stats['avg']:.1f}s  "
                  f"p50={snapshot.creation_stats['p50']:.1f}s  "
                  f"p99={snapshot.creation_stats['p99']:.1f}s")
        
        print(f"  Browser:   {snapshot.browser_success:3d}/{snapshot.browser_total:3d}  "
              f"avg={snapshot.browser_avg_latency:.2f}s  p99={snapshot.browser_p99_latency:.2f}s")
        print(f"{'─'*70}")
    
    def generate_report(self) -> str:
        """生成最终TXT报告"""
        lines: List[str] = []
        lines.append("=" * 80)
        lines.append("E2B Sandbox Bench - Performance Report")
        lines.append("=" * 80)
        
        # 配置信息
        lines.append(f"\n[Test Configuration]")
        lines.append(f"  Template:        {self.config.template}")
        lines.append(f"  Total Sandboxes: {self.config.total_count}")
        if self.config.batch_size:
            lines.append(f"  Batch Strategy:  {self.config.batch_count} batches x {self.config.batch_size} sandboxes")
            lines.append(f"  Batch Interval:  {self.config.batch_interval}s")
        else:
            lines.append(f"  Batch Strategy:  Full concurrent creation")
        lines.append(f"  Test Duration:   {self.config.test_duration}s")
        
        # 沙箱状态统计
        active_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.ACTIVE
        ]
        failed_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.FAILED
        ]
        offline_states = [
            s for s in self.sandbox_states.values() if not s.is_alive
        ]
        
        lines.append(f"\n[Sandbox Status]")
        lines.append(f"  Created:   {len(active_states)} / {len(self.sandbox_states)}")
        lines.append(f"  Failed:    {len(failed_states)}")
        lines.append(f"  Offline:   {len(offline_states)} (during test)")
        if failed_states:
            lines.append(f"  Failed IDs:  {[s.sandbox_id for s in failed_states[:10]]}")
        if offline_states:
            lines.append(f"  Offline IDs: {[s.sandbox_id for s in offline_states[:10]]}")
        
        # 创建性能统计
        creation_times = [
            s.creation_metrics.elapsed for s in active_states if s.creation_metrics.elapsed > 0
        ]
        if creation_times:
            stats = calc_percentiles(creation_times)
            lines.append(f"\n[Creation Performance]")
            lines.append(f"  Min:  {stats['min']:.1f}s")
            lines.append(f"  Max:  {stats['max']:.1f}s")
            lines.append(f"  Avg:  {stats['avg']:.1f}s")
            lines.append(f"  P50:  {stats['p50']:.1f}s")
            lines.append(f"  P95:  {stats['p95']:.1f}s")
            lines.append(f"  P99:  {stats['p99']:.1f}s")
        
        # 浏览器任务统计
        all_latencies: List[float] = []
        for s in self.sandbox_states.values():
            all_latencies.extend(s.browser_metrics.latencies)
        
        total_tasks = sum(s.browser_metrics.total_tasks for s in self.sandbox_states.values())
        total_success = sum(s.browser_metrics.success_count for s in self.sandbox_states.values())
        total_failed = sum(s.browser_metrics.failed_count for s in self.sandbox_states.values())
        total_timeout = sum(s.browser_metrics.timeout_count for s in self.sandbox_states.values())
        
        lines.append(f"\n[Browser Task Statistics]")
        lines.append(f"  Total Tasks:   {total_tasks}")
        lines.append(f"  Success:       {total_success}")
        lines.append(f"  Failed:        {total_failed} (timeout: {total_timeout})")
        lines.append(f"  Success Rate:  {total_success / max(1, total_tasks) * 100:.1f}%")
        
        if all_latencies:
            avg_ms = statistics.mean(all_latencies) * 1000
            p99_ms = calc_p99(all_latencies) * 1000
            lines.append(f"  Avg Latency:   {avg_ms:.1f}ms")
            lines.append(f"  P99 Latency:   {p99_ms:.1f}ms")
        
        lines.append("\n" + "=" * 80)
        return '\n'.join(lines)
    
    def save_report(self, report: str) -> str:
        """保存报告到文件"""
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.config.filename_prefix}_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)
        
        return filepath
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/stats_collector.py
git commit -m "feat(e2b_bench): add stats collector with real-time snapshot and report"
```

---

### Task 8: 实现主入口模块 (bench.py)

**Files:**
- Create: `e2b_bench/bench.py`

- [ ] **Step 1: 创建bench.py，实现主入口和命令行解析**

```python
#!/usr/bin/env python3
"""
E2B Sandbox Bench - 主入口

整合所有组件，运行测试流程：
创建沙箱 → 启动统计 → 启动任务 → 运行时长 → 停止 → 报告
"""

import time
import argparse
import threading

from .config import Config
from .sandbox_manager import SandboxManager
from .task_runner import TaskManager
from .stats_collector import StatsCollector
from .schemas import SandboxStatus


def run_benchmark(config: Config) -> dict:
    """运行E2B沙箱性能测试
    
    Args:
        config: 测试配置对象
        
    Returns:
        {'report': str, 'filepath': str}
    """
    # 1. 设置E2B环境变量
    config.setup_e2b_env()
    
    print("=" * 80)
    print("E2B Sandbox Bench - Batch Performance Test")
    print("=" * 80)
    print(f"  Template: {config.template}")
    print(f"  Total:    {config.total_count} sandboxes")
    if config.batch_size:
        print(f"  Batch:    {config.batch_count} batches x {config.batch_size} (interval {config.batch_interval}s)")
    else:
        print(f"  Batch:    Full concurrent creation")
    print(f"  Duration: {config.test_duration}s")
    print("=" * 80)
    
    # 停止信号
    stop_event = threading.Event()
    
    # 2. 创建沙箱
    print("\n[Phase 1] Creating sandboxes...")
    sandbox_manager = SandboxManager(config, stop_event)
    sandbox_states = sandbox_manager.create_all()
    
    created_count = sum(
        1 for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.ACTIVE
    )
    if created_count == 0:
        print("No sandboxes created successfully, exiting.")
        return {}
    
    print(f"\nSuccessfully created: {created_count}/{config.total_count} sandboxes")
    
    # 3. 启动统计收集
    print("\n[Phase 2] Starting stats collector...")
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()
    
    # 4. 启动任务执行
    print("\n[Phase 3] Starting browser tasks...")
    task_manager = TaskManager(config, sandbox_states, stop_event)
    task_manager.start_all()
    
    # 5. 运行指定时长
    print(f"\n[Phase 4] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt, stopping...")
    
    # 6. 停止所有组件
    print("\n[Phase 5] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()
    sandbox_manager.close_all()
    
    time.sleep(0.5)  # 让守护线程完成最后的输出
    
    # 7. 生成并保存报告
    report = stats_collector.generate_report()
    print("\n" + report)
    
    filepath = stats_collector.save_report(report)
    print(f"\nReport saved to: {filepath}")
    
    return {'report': report, 'filepath': filepath}


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        description='E2B Sandbox Bench - E2B沙箱批量性能测试工具'
    )
    
    # 配置文件
    parser.add_argument('--config', type=str, default=None,
                        help='YAML configuration file path')
    
    # E2B环境变量
    parser.add_argument('--e2b-access-token', type=str, help='E2B access token')
    parser.add_argument('--e2b-api-key', type=str, help='E2B API key')
    parser.add_argument('--e2b-domain', type=str, help='E2B domain')
    parser.add_argument('--e2b-api-url', type=str, help='E2B API URL')
    parser.add_argument('--e2b-http-ssl', type=str, help='E2B HTTP SSL setting')
    
    # 沙箱配置
    parser.add_argument('--template', type=str, help='E2B template name')
    parser.add_argument('--total', type=int, help='Total sandbox count')
    parser.add_argument('--create-timeout', type=int, help='Sandbox creation timeout')
    
    # 批量控制
    parser.add_argument('--batch-size', type=int, help='Sandboxes per batch (None = full concurrent)')
    parser.add_argument('--batch-interval', type=int, help='Batch interval seconds')
    
    # 浏览器任务
    parser.add_argument('--browser-url', type=str, action='append', help='Browser URL (can specify multiple)')
    parser.add_argument('--browser-timeout', type=int, help='Browser task timeout')
    parser.add_argument('--browser-interval-min', type=float, help='Task interval minimum')
    parser.add_argument('--browser-interval-max', type=float, help='Task interval maximum')
    
    # 测试运行
    parser.add_argument('--duration', type=int, help='Test duration seconds')
    parser.add_argument('--stats-interval', type=int, help='Stats snapshot interval')
    
    # 报告
    parser.add_argument('--output-dir', type=str, help='Report output directory')
    parser.add_argument('--filename-prefix', type=str, help='Report filename prefix')
    
    return parser


def main() -> None:
    """命令行入口"""
    parser = build_arg_parser()
    args = parser.parse_args()
    
    # 加载配置
    if args.config:
        config = Config.load_from_yaml(args.config)
        config = Config.merge_with_args(config, args)
    else:
        # 无配置文件时，使用命令行参数
        config = Config.from_args(args)
    
    # 验证必填参数
    if not config.e2b_access_token and not args.config:
        print("Error: E2B access token is required. Use --e2b-access-token or --config")
        return
    
    # 运行测试
    run_benchmark(config)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/bench.py
git commit -m "feat(e2b_bench): add main entry point with CLI parser"
```

---

### Task 9: 更新__init__.py导出模块

**Files:**
- Modify: `e2b_bench/__init__.py`

- [ ] **Step 1: 更新__init__.py导出所有公共接口**

```python
# e2b_bench/__init__.py
"""
E2B Sandbox Bench - E2B沙箱批量性能测试套件

功能：
- 批量创建E2B沙箱，收集启动性能（时间、成功率、P50/P95/P99延迟）
- 执行浏览器任务，收集执行性能（延迟、吞吐量）
- 监控沙箱存活情况
- 支持分批启动和随机任务间隔
- 实时统计快照 + 最终报告

使用示例：
    python -m e2b_bench --config config/default.yaml
    python -m e2b_bench --config config/default.yaml --total 50 --duration 300
"""

from .config import Config
from .schemas import (
    SandboxState,
    SandboxStatus,
    CreationMetrics,
    BrowserMetrics,
    TestSnapshot,
)
from .bench import run_benchmark, main

__version__ = "1.0.0"

__all__ = [
    'Config',
    'SandboxState',
    'SandboxStatus',
    'CreationMetrics',
    'BrowserMetrics',
    'TestSnapshot',
    'run_benchmark',
    'main',
]
```

- [ ] **Step 2: Commit**

```bash
git add e2b_bench/__init__.py
git commit -m "feat(e2b_bench): update __init__.py to export all public interfaces"
```

---

### Task 10: 添加依赖说明和最终验证

**Files:**
- Create: `e2b_bench/requirements.txt`

- [ ] **Step 1: 创建requirements.txt**

```
# E2B Sandbox Bench Dependencies
e2b>=0.15.0
PyYAML>=6.0
```

- [ ] **Step 2: 验证模块导入**

```bash
python -c "from e2b_bench import Config, run_benchmark; print('Import OK')"
```

Expected: `Import OK`

- [ ] **Step 3: 验证命令行帮助**

```bash
python -m e2b_bench --help
```

Expected: 显示命令行帮助信息

- [ ] **Step 4: Commit**

```bash
git add e2b_bench/requirements.txt
git commit -m "feat(e2b_bench): add requirements.txt"
```

---

## Self-Review Checklist

**1. Spec Coverage:**
- ✅ 批量创建沙箱 - Task 5 (sandbox_manager.py)
- ✅ 沙箱启动性能收集 - Task 5 + Task 7
- ✅ 浏览器任务执行 - Task 6 (task_runner.py)
- ✅ 沙箱存活监控 - Task 5 (check_alive) + Task 6 (consecutive_failures)
- ✅ 分批启动支持 - Task 5 (_create_batched)
- ✅ 随机任务间隔 - Task 6 (random.uniform)
- ✅ 实时统计快照 - Task 7 (_take_snapshot)
- ✅ 最终报告生成 - Task 7 (generate_report)
- ✅ YAML配置 + 命令行覆盖 - Task 4 (config.py)
- ✅ E2B环境变量设置 - Task 4 (setup_e2b_env)
- ✅ 沙箱句柄保留 - Task 5 (state.sandbox_obj)

**2. Placeholder Scan:**
- 无TBD、TODO、未完成代码块
- 所有代码步骤包含完整实现

**3. Type Consistency:**
- `SandboxState.sandbox_obj` 在所有模块中一致使用
- `BrowserMetrics.add()` 方法签名一致
- `Config` 属性名称与YAML配置键对应

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-16-e2b-sandbox-bench.md`.**

**Two execution options:**

**1. Subagent-Driven (recommended)** - 我为每个Task派遣独立子agent，任务间进行review，快速迭代

**2. Inline Execution** - 在当前会话中使用executing-plans执行，批量执行带有checkpoint review

**选择哪种方式？**