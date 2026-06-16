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