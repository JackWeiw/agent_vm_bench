# E2B Bench Round-Robin 模式设计文档

## 概述

**目标**: 为 e2b_bench 添加 round-robin 轮换模式,支持每轮切换不同的沙箱子集进行内存迁移压力测试。

**设计日期**: 2026-07-17

---

## 背景

### 当前问题

现有 `benchmark_percent` 参数在测试开始时固定选择一组沙箱,整个测试期间访问相同的沙箱和内存区域。无法模拟真实场景中不同用户轮换访问不同沙箱的情况。

### 需求

1. **自动轮换**: 每个 interval 自动切换不同的沙箱子集
2. **均匀分配**: 确保每轮沙箱数相同,避免最后一轮负载骤降
3. **无重叠**: 每轮访问完全不同的沙箱,实现内存访问均衡
4. **向后兼容**: 不影响现有的 `fixed` 模式功能

---

## 设计方案

### 方案 A: 基于轮次的 Round-Robin

用户指定轮次数,系统均匀分配沙箱到每轮。

**示例**:
```
总沙箱: 100
round_count: 5
每轮沙箱数: 100 ÷ 5 = 20

Round 0 (0-30s):  沙箱 [0-19]
Round 1 (30-60s): 沙箱 [20-39]
Round 2 (60-90s): 沙箱 [40-59]
Round 3 (90-120s): 沙箱 [60-79]
Round 4 (120-150s): 沙箱 [80-99]
```

---

## 配置设计

### YAML 配置

```yaml
test:
  duration: 160                    # 总测试时长 (fixed 模式使用)
  stats_interval: 10
  benchmark_percent: 0.3          # 活跃沙箱比例

  # 新增 round-robin 配置 (可选)
  benchmark_mode: "fixed"         # "fixed" (默认) | "round_robin"
  round_count: 5                  # 轮次数 (round_robin 模式使用)
  round_interval: 30              # 每轮持续时间 (秒)
```

### 配置字段说明

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `benchmark_mode` | string | `"fixed"` | 模式选择 |
| `round_count` | int | None | 轮次数 (round_robin 模式必填) |
| `round_interval` | int | 30 | 每轮持续时间 |

### 向后兼容保证

1. **默认行为不变**: `benchmark_mode = "fixed"` 时,完全使用原有逻辑
2. **可选字段**: 新字段都有默认值,不影响现有 YAML 配置
3. **CLI 参数可选**: 新增参数都有默认值

---

## 实现设计

### 核心类: RoundRobinTaskManager

**文件位置**: `e2b_bench/round_robin.py`

```python
class RoundRobinTaskManager:
    """轮换式任务管理器 - 每轮切换不同的沙箱子集"""

    def __init__(
        self,
        config: Config,
        sandbox_states: Dict[int, SandboxState],
        stop_event: threading.Event,
        stats_collector: StatsCollector,
    ):
        self.config = config
        self.sandbox_states = sandbox_states
        self.stop_event = stop_event
        self.stats_collector = stats_collector

        # 沙箱分组
        self.all_ready_states: List[SandboxState] = []
        self.sandbox_groups: List[List[SandboxState]] = []

        # 当前轮次状态
        self.current_round: int = 0
        self.active_runners: List[BrowserTaskRunner] = []
        self.round_stop_event: Optional[threading.Event] = None

    def run(self) -> None:
        """主循环 - 轮换执行"""
        # 1. 准备沙箱分组
        self._prepare_sandbox_groups()

        # 2. 计算轮次参数
        rounds = self._calculate_rounds()

        # 3. 轮换执行
        for round_id in range(rounds):
            if self.stop_event.is_set():
                break

            self._start_round(round_id)
            time.sleep(self.config.round_interval)
            self._stop_round()

    def _prepare_sandbox_groups(self) -> None:
        """准备沙箱分组 - 均匀分配"""
        # 获取所有就绪沙箱
        self.all_ready_states = [
            s for s in self.sandbox_states.values()
            if s.creation_metrics.status == SandboxStatus.PORT_READY
        ]

        total = len(self.all_ready_states)
        round_count = self.config.round_count
        base_per_round = total // round_count
        remainder = total % round_count

        # 均匀分配,余数分散到前几轮
        # 示例: 103 沙箱 ÷ 5 轮 = [21, 21, 21, 20, 20]
        self.sandbox_groups = []
        start_idx = 0

        for i in range(round_count):
            # 前几轮多分配 1 个沙箱
            per_round = base_per_round + (1 if i < remainder else 0)
            end_idx = start_idx + per_round
            group = self.all_ready_states[start_idx:end_idx]
            self.sandbox_groups.append(group)
            start_idx = end_idx

    def _start_round(self, round_id: int) -> None:
        """启动指定轮次"""
        # 选择当前轮次的沙箱组
        current_states = self.sandbox_groups[round_id]

        print(f"\n[Round {round_id}] Starting {len(current_states)} sandboxes")

        # 标记当前轮次 (用于统计)
        self.stats_collector.set_round(round_id)

        # 创建轮次停止信号
        self.round_stop_event = threading.Event()

        # 启动任务 runners
        for state in current_states:
            runner = BrowserTaskRunner(state, self.config, self.round_stop_event)
            self.active_runners.append(runner)
            runner.start()

    def _stop_round(self) -> None:
        """停止当前轮次"""
        # 设置停止信号
        self.round_stop_event.set()

        # 等待 runners 结束
        for runner in self.active_runners:
            runner.join(timeout=2)

        # 清理
        self.active_runners.clear()

        # 清除轮次标记
        self.stats_collector.set_round(None)

    def _calculate_rounds(self) -> int:
        """计算总轮次数"""
        return self.config.round_count
```

### 文件修改清单

| 文件 | 修改内容 |
|------|----------|
| `e2b_bench/round_robin.py` | 新增: RoundRobinTaskManager 类 |
| `e2b_bench/bench.py` | 修改: 添加 round_robin 模式分支 |
| `e2b_bench/config.py` | 修改: 添加新字段和解析 |
| `e2b_bench/stats_collector.py` | 修改: 添加轮次统计功能 |
| `e2b_bench/__main__.py` | 修改: 添加 CLI 参数 |

---

## 集成设计

### bench.py 集成

```python
def run_benchmark(config: Config) -> dict:
    # ... Phase 1-3 不变 ...

    # Phase 4: 根据模式选择执行方式
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()

    if config.benchmark_mode == "round_robin":
        # 新增: Round-Robin 模式
        print(f"\n[Phase 4] Starting round-robin browser tasks...")
        print(f"  Rounds: {config.round_count}")
        print(f"  Interval: {config.round_interval}s per round")

        round_robin_manager = RoundRobinTaskManager(
            config, sandbox_states, stop_event, stats_collector
        )
        round_robin_manager.run()
    else:
        # 原有: Fixed 模式
        task_manager.start_all()
        time.sleep(config.test_duration)
        stop_event.set()
        task_manager.wait_all(timeout=5)

    stats_collector.stop()

    # ... Phase 7-9 不变 ...
```

### stats_collector.py 集成

```python
class StatsCollector:
    def __init__(self, config, sandbox_states):
        # ... 原有字段 ...

        # 新增: 轮次标记
        self.current_round: Optional[int] = None
        self.round_snapshots: Dict[int, List[TestSnapshot]] = {}

    def set_round(self, round_id: Optional[int]) -> None:
        """设置当前轮次"""
        self.current_round = round_id
        if round_id is not None and round_id not in self.round_snapshots:
            self.round_snapshots[round_id] = []

    def _take_snapshot(self) -> None:
        """收集快照"""
        snapshot = TestSnapshot(...)
        self.snapshots.append(snapshot)

        # 新增: 轮次分组
        if self.current_round is not None:
            self.round_snapshots[self.current_round].append(snapshot)

    def generate_report(self) -> str:
        """生成报告 (原有 + 轮次对比)"""
        lines = []
        # ... 原有内容 ...

        # 新增: 轮次对比表格
        if self.round_snapshots:
            lines.append("\n[Round Comparison]")
            # ... 表格内容 ...

        return "\n".join(lines)
```

### CLI 参数

```python
# 新增参数
parser.add_argument(
    "-bm", "--benchmark-mode",
    type=str,
    choices=["fixed", "round_robin"],
    default="fixed",
    help="Benchmark mode: 'fixed' (default) or 'round_robin'"
)
parser.add_argument(
    "-rc", "--round-count",
    type=int,
    help="Round count for round_robin mode"
)
parser.add_argument(
    "-ri", "--round-interval",
    type=int,
    default=30,
    help="Round interval in seconds for round_robin mode (default: 30)"
)
```

---

## 执行流程

### Fixed 模式 (原有)

```
Phase 1: 创建/检测沙箱
Phase 2: Warmup (可选)
Phase 3: 启动 StatsCollector
Phase 4: 启动任务 (固定沙箱子集)
Phase 5: 运行 test_duration 秒
Phase 6: 停止
Phase 7: 生成报告
```

### Round-Robin 模式 (新增)

```
Phase 1: 创建/检测沙箱
Phase 2: Warmup (可选)
Phase 3: 启动 StatsCollector
Phase 4: 轮换执行
  ├── Round 0: 启动沙箱 [0-19]  → 运行 30s → 停止
  ├── Round 1: 启动沙箱 [20-39] → 运行 30s → 停止
  ├── Round 2: 启动沙箱 [40-59] → 运行 30s → 停止
  └── ... 循环直到 round_count 结束
Phase 5: 停止 StatsCollector
Phase 6: 生成报告 (包含轮次对比)
```

---

## 报告输出示例

```
================================================================================
E2B Sandbox Bench - Performance Report
================================================================================

[Test Configuration]
  Template:        openclaw-browser-v1
  Total Sandboxes: 100
  Mode:            Full workflow
  Benchmark Mode:  round_robin
  Rounds:          5
  Round Interval:  30s

[Sandbox Status]
  Created (API):       100 / 100
  Ports Ready:         100 / 100

[Browser Task Statistics]
  Total Tasks:   789
  Success:       782
  Failed:        7
  Success Rate:  99.1%
  Avg Latency:   2.34s
  P99 Latency:   4.56s

[Round Comparison]
Round    Tasks    Success%   Avg(s)     P99(s)
--------------------------------------------------
0        156      98.7       2.34       4.56
1        162      99.1       2.28       4.21
2        158      98.9       2.41       4.68
3        161      99.4       2.31       4.35
4        159      98.8       2.37       4.52
================================================================================
```

---

## 测试用例

### 单元测试

1. **沙箱分组测试**
   - 100 沙箱 ÷ 5 轮 = 每轮 20 个
   - 103 沙箱 ÷ 5 轮 = [21, 21, 21, 20, 20]
   - 10 沙箱 ÷ 3 轮 = [4, 3, 3]

2. **轮次切换测试**
   - 验证每轮启动/停止正确
   - 验证轮次间沙箱无重叠

### 集成测试

1. **向后兼容测试**
   - 使用旧配置文件运行,行为不变
   - 不指定 benchmark_mode 时使用 fixed 模式

2. **Round-Robin 端到端测试**
   - 完整执行多轮测试
   - 验证报告包含轮次对比

---

## 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 轮换间隔过短导致沙箱启动/停止开销大 | 建议最小间隔 20s+ |
| 沙箱状态在轮换期间变化 | 每轮启动前检查沙箱健康状态 |
| 轮次过多导致日志过多 | 按轮次聚合日志,只打印关键信息 |

---

## 后续优化

1. **部分重叠轮换**: 支持每轮部分沙箱重叠 (如 10% 重叠)
2. **动态调整**: 根据沙箱健康状态动态调整每轮沙箱数量
3. **进度显示**: 实时显示当前轮次进度

---

## 实现计划

1. **Phase 1**: 配置和 CLI 参数
2. **Phase 2**: RoundRobinTaskManager 核心逻辑
3. **Phase 3**: StatsCollector 轮次统计
4. **Phase 4**: bench.py 集成
5. **Phase 5**: 测试和文档
