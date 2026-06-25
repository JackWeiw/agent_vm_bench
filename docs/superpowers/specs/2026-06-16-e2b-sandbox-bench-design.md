---
name: e2b-sandbox-bench-design
description: E2B沙箱批量性能测试套件设计文档
metadata:
  type: project
---

# E2B Sandbox Bench - 设计文档

## 1. 概述

### 1.1 目标

构建E2B沙箱批量性能测试套件，对标现有vm_bench_lite的架构风格，实现：

- 批量创建E2B沙箱，收集启动性能（时间、成功率、P50/P95/P99延迟）
- 执行浏览器任务，收集执行性能（延迟、吞吐量）
- 监控沙箱存活情况
- 支持分批启动和随机任务间隔
- 实时统计快照 + 最终报告

### 1.2 与vm_bench_lite对比

| 特性 | vm_bench_lite | e2b_bench |
|------|---------------|-----------|
| 执行环境 | VM（SSH连接） | E2B Sandbox（SDK调用） |
| 任务类型 | QA + Browser + Stress | Browser only |
| 连接方式 | SSH paramiko | E2B SDK (sbx.commands.run) |
| 批量控制 | batch_size + batch_interval | 同样支持 |
| 任务间隔 | 随机间隔避免突增 | 同样支持 |
| 报告格式 | TXT实时+最终报告 | TXT实时+最终报告 |

---

## 2. 架构设计

### 2.1 目录结构

```
e2b_bench/
├── __init__.py           # 包初始化，导出主要类和函数
├── bench.py              # 主入口 - 整合所有组件，运行测试流程
├── config.py             # 配置管理 - YAML加载 + 命令行参数覆盖
├── sandbox_manager.py    # 沙箱管理 - 创建、健康检查、批量控制、关闭
├── task_runner.py        # 任务执行 - 浏览器任务执行、结果收集
├── stats_collector.py    # 统计收集 - 实时快照、最终报告生成
├── schemas.py            # 数据结构 - SandboxState、BrowserMetrics等
└── utils.py              # 工具函数 - 日志格式化、时间计算等

config/
└── default.yaml          # 默认配置文件示例
```

### 2.2 模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| `config.py` | 加载YAML配置、解析命令行参数、合并配置、设置E2B环境变量 | 无 |
| `schemas.py` | 定义所有数据结构：SandboxState、BrowserMetrics、TestSnapshot等 | 无 |
| `sandbox_manager.py` | 调用E2B SDK创建沙箱、批量启动控制、健康检查、沙箱关闭、保留沙箱句柄 | config, schemas, e2b SDK |
| `task_runner.py` | 执行浏览器任务命令、收集执行结果、处理超时和异常 | config, schemas, sandbox_manager |
| `stats_collector.py` | 定期收集统计快照、生成最终报告、保存报告文件 | config, schemas |
| `bench.py` | 整合所有组件、控制测试流程（创建→执行→停止→报告） | 所有模块 |
| `utils.py` | 日志格式化、时间戳处理、辅助函数 | 无 |

### 2.3 数据流向

```
config.py → Config对象
    ↓
bench.py (主控制器)
    ↓
sandbox_manager.py → 创建SandboxState列表（保留sbx句柄）
    ↓
task_runner.py → 使用sbx.commands.run执行任务 → 更新BrowserMetrics
    ↓
stats_collector.py → 收集快照 → 生成报告 → 保存文件
```

---

## 3. 配置管理设计

### 3.1 YAML配置文件

```yaml
# E2B SDK 环境变量配置
e2b_env:
  E2B_ACCESS_TOKEN: "sk_e2b_xxx"
  E2B_API_KEY: "e2b_xxx"
  E2B_DOMAIN: "e2b.app"
  E2B_API_URL: "http://localhost:3000"
  E2B_HTTP_SSL: "false"

# 沙箱配置
sandbox:
  template: "openclaw-browser-v1"    # E2B模板名称
  create_timeout: 86400              # 沙箱创建超时（秒）
  total_count: 100                   # 沙箱总数

# 批量启动控制（可选，不配置则全并发）
batch:
  size: 20                           # 每批沙箱数量
  interval: 30                       # 批次间隔（秒）

# 浏览器任务配置
browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  task_timeout: 200                  # 单个任务超时（秒）
  interval_min: 0.5                  # 任务随机间隔最小值（秒）
  interval_max: 3.0                  # 任务随机间隔最大值（秒）

# 测试运行配置
test:
  duration: 600                      # 测试持续时间（秒）
  stats_interval: 10                 # 统计快照间隔（秒）

# 报告配置
report:
  output_dir: "results/e2b"          # 报告保存目录
  filename_prefix: "e2b_bench"       # 报告文件名前缀
```

### 3.2 命令行参数

```bash
# 使用配置文件
python -m e2b_bench --config config/default.yaml

# 命令行参数覆盖
python -m e2b_bench --config config/default.yaml --total 50 --duration 300

# 指定报告路径
python -m e2b_bench --config config/default.yaml --output-dir custom_results

# 不使用配置文件
python -m e2b_bench --template my-template --total 100 --duration 600
```

### 3.3 配置优先级

从高到低：
1. 命令行参数
2. YAML配置文件
3. 默认值（Config类内置）

---

## 4. 数据结构设计

### 4.1 SandboxStatus

```python
class SandboxStatus(Enum):
    PENDING = "pending"       # 等待创建
    CREATING = "creating"     # 正在创建
    ACTIVE = "active"         # 已激活，可执行任务
    FAILED = "failed"         # 创建失败
    OFFLINE = "offline"       # 运行时离线
    CLOSED = "closed"         # 已关闭
```

### 4.2 CreationMetrics

```python
@dataclass
class CreationMetrics:
    submit_time: float = 0.0       # 提交创建时间
    ready_time: float = 0.0        # 沙箱就绪时间
    elapsed: float = 0.0           # 创建耗时
    status: SandboxStatus = SandboxStatus.PENDING
    error_msg: str = ""
```

### 4.3 BrowserMetrics

```python
@dataclass
class BrowserMetrics:
    total_tasks: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    latencies: List[float] = field(default_factory=list)
    
    def add(self, latency: float, success: bool, timeout: bool = False)
    
    @property
    def avg_latency(self) -> float
    
    @property
    def p99_latency(self) -> float
```

### 4.4 SandboxState（核心状态）

```python
@dataclass
class SandboxState:
    sandbox_id: int              # 序号（1, 2, 3...）
    sandbox_obj: Optional[object] = None  # E2B Sandbox对象引用（句柄）
    batch_id: int = -1           # 所属批次
    
    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    
    is_alive: bool = True        # 沙箱是否存活
    last_task_time: float = 0.0  # 最后一次任务执行时间
    consecutive_failures: int = 0  # 连续失败次数
```

**关键设计**：`sandbox_obj` 字段保留E2B沙箱句柄，供后续 `sbx.commands.run()` 使用。

### 4.5 TestSnapshot

```python
@dataclass
class TestSnapshot:
    timestamp: float
    elapsed: float               # 从测试开始到现在的时间
    total_sandboxes: int
    active_sandboxes: int
    offline_sandboxes: int
    creation_stats: dict         # {min, max, avg, p50, p95, p99}
    browser_total: int
    browser_success: int
    browser_avg_latency: float
    browser_p99_latency: float
```

---

## 5. 沙箱管理模块设计

### 5.1 SandboxManager

```python
class SandboxManager:
    """沙箱生命周期管理"""
    
    def __init__(self, config: Config, stop_event: Event):
        self.config = config
        self.stop_event = stop_event
        self.sandbox_states: Dict[int, SandboxState] = {}
    
    def create_all(self) -> Dict[int, SandboxState]:
        """批量创建沙箱"""
        if self.config.batch_size:
            return self._create_batched()   # 分批创建
        else:
            return self._create_concurrent() # 全并发创建
    
    def _create_single(self, state: SandboxState) -> Tuple[bool, float, str]:
        """创建单个沙箱，保留句柄到state.sandbox_obj"""
        state.creation_metrics.status = SandboxStatus.CREATING
        state.creation_metrics.submit_time = time.time()
        
        try:
            sbx = Sandbox.create(self.config.template, timeout=self.config.create_timeout)
            state.sandbox_obj = sbx  # 保留句柄
            state.creation_metrics.ready_time = time.time()
            elapsed = state.creation_metrics.ready_time - state.creation_metrics.submit_time
            return True, elapsed, ""
        except Exception as e:
            return False, 0.0, str(e)
    
    def check_alive(self, state: SandboxState) -> bool:
        """检查沙箱存活"""
        sbx = state.sandbox_obj
        if not sbx:
            return False
        try:
            result = sbx.commands.run("echo alive", timeout=10)
            return result.exit_code == 0
        except:
            return False
    
    def close_all(self) -> None:
        """关闭所有沙箱"""
        for state in self.sandbox_states.values():
            if state.sandbox_obj:
                try:
                    state.sandbox_obj.close()
                    state.creation_metrics.status = SandboxStatus.CLOSED
                except:
                    pass
```

---

## 6. 任务执行模块设计

### 6.1 BrowserTaskRunner

```python
class BrowserTaskRunner(threading.Thread):
    """每个沙箱一个独立线程执行浏览器任务"""
    
    def __init__(self, state: SandboxState, config: Config, stop_event: Event):
        super().__init__(daemon=True)
        self.state = state
        self.config = config
        self.stop_event = stop_event
    
    def run(self):
        # 等待沙箱ACTIVE
        # 循环执行浏览器任务
        # 随机间隔 sleep
        # 连续3次失败则标记离线
    
    def _run_single_task(self) -> Tuple[bool, float]:
        """执行单个任务，使用state.sandbox_obj句柄"""
        sbx = self.state.sandbox_obj
        url = self.config.browser_urls[idx]
        cmd = f"openclaw browser --browser-profile openclaw open '{url}'"
        
        start = time.perf_counter()
        result = sbx.commands.run(cmd, timeout=..., user="root")
        elapsed = time.perf_counter() - start
        
        return result.exit_code == 0, elapsed
```

### 6.2 TaskManager

```python
class TaskManager:
    """管理所有沙箱的任务执行线程"""
    
    def start_all(self) -> None:
        """启动所有ACTIVE沙箱的任务线程"""
    
    def wait_all(self, timeout: float) -> None:
        """等待所有线程结束"""
```

---

## 7. 统计收集模块设计

### 7.1 StatsCollector

```python
class StatsCollector:
    """实时快照 + 最终报告"""
    
    def start(self) -> None:
        """启动后台收集线程"""
    
    def _collect_loop(self) -> None:
        """每隔stats_interval收集快照"""
    
    def _take_snapshot(self) -> None:
        """收集并打印当前统计"""
    
    def _print_snapshot(self, snapshot: TestSnapshot) -> None:
        """实时终端输出"""
        print(f"T+{elapsed}s  Status Snapshot")
        print(f"  Sandboxes: {active} active / {offline} offline")
        print(f"  Creation:  avg=Xs p50=Xs p99=Xs")
        print(f"  Browser:   {success}/{total} avg=Xs p99=Xs")
    
    def generate_report(self) -> str:
        """生成最终TXT报告"""
    
    def save_report(self, report: str) -> str:
        """保存报告到output_dir"""
```

### 7.2 报告输出示例

```
================================================================================
E2B Sandbox Bench - Performance Report
================================================================================

[Test Configuration]
  Template:        openclaw-browser-v1
  Total Sandboxes: 100
  Batch Strategy:  5 batches x 20 sandboxes
  Batch Interval:  30s
  Test Duration:   600s

[Sandbox Status]
  Created:   98 / 100
  Failed:    2
  Offline:   3 (during test)

[Creation Performance]
  Min:  12.3s
  Max:  45.6s
  Avg:  18.5s
  P50:  17.2s
  P95:  32.1s
  P99:  42.8s

[Browser Task Statistics]
  Total Tasks:   1250
  Success:       1180
  Failed:        70 (timeout: 25)
  Success Rate:  94.4%
  Avg Latency:   2345.6ms
  P99 Latency:   5678.2ms

================================================================================
```

---

## 8. 主入口设计

### 8.1 bench.py

```python
def run_benchmark(config: Config) -> dict:
    """运行测试流程"""
    
    # 1. 设置E2B环境变量
    config.setup_e2b_env()
    
    # 2. 创建沙箱
    sandbox_manager = SandboxManager(config, stop_event)
    sandbox_states = sandbox_manager.create_all()
    
    # 3. 启动统计收集
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()
    
    # 4. 启动任务执行
    task_manager = TaskManager(config, sandbox_states, stop_event)
    task_manager.start_all()
    
    # 5. 运行指定时长
    time.sleep(config.test_duration)
    
    # 6. 停止所有组件
    stop_event.set()
    task_manager.wait_all()
    stats_collector.stop()
    sandbox_manager.close_all()
    
    # 7. 生成并保存报告
    report = stats_collector.generate_report()
    filepath = stats_collector.save_report(report)
    
    return {'report': report, 'filepath': filepath}
```

---

## 9. 关键设计决策

### 9.1 沙箱句柄保留

- `SandboxState.sandbox_obj` 字段保留 `Sandbox.create()` 返回的句柄
- 任务执行时通过 `state.sandbox_obj.commands.run()` 调用
- 健康检查、关闭操作同样使用句柄

### 9.2 E2B环境变量

在测试启动前设置：
```python
os.environ["E2B_ACCESS_TOKEN"] = config.e2b_access_token
os.environ["E2B_API_KEY"] = config.e2b_api_key
os.environ["E2B_DOMAIN"] = config.e2b_domain
os.environ["E2B_API_URL"] = config.e2b_api_url
os.environ["E2B_HTTP_SSL"] = config.e2b_http_ssl
```

### 9.3 批量启动策略

- 有 `batch_size` 配置：分批创建，批次间等待 `batch_interval`
- 无配置：全并发创建（测试极限性能）

### 9.4 任务间隔

随机间隔 `browser_interval_min` ~ `browser_interval_max` 秒，避免所有沙箱同时发起请求。

---

## 10. 使用示例

```bash
# 默认配置运行
python -m e2b_bench --config config/default.yaml

# 自定义参数
python -m e2b_bench --config config/default.yaml \
    --total 50 \
    --batch-size 10 \
    --batch-interval 20 \
    --duration 300 \
    --output-dir my_results

# 无配置文件运行（需提供必填参数）
python -m e2b_bench \
    --template openclaw-browser-v1 \
    --e2b-access-token sk_e2b_xxx \
    --e2b-api-key e2b_xxx \
    --total 100 \
    --duration 600
```

---

## 11. 后续扩展方向

1. **添加QA任务**：扩展 `task_runner.py`，新增 `QATaskRunner`
2. **添加Stress压力测试**：新增 `stress_runner.py`
3. **Warmup/Benchmark两阶段**：参考vm_bench_lite的浏览器两阶段设计
4. **JSON/CSV结构化报告**：扩展 `stats_collector.py` 输出格式
5. **实时Dashboard**：添加WebSocket推送统计数据