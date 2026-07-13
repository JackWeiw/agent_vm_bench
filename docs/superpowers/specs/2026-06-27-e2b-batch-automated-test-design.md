---
name: e2b-batch-automated-test
description: E2B 批量自动化测试采集流程设计
created: 2026-06-27
status: approved
---

# E2B 批量自动化测试采集流程设计

## 1. 背景与目标

### 1.1 背景

现有 `e2b_bench` 提供了 E2B Sandbox 的单次基准测试能力，但缺少：
1. 完整自动化流程（smap_tool 内存迁移监控、vm_monitor 性能采集）
2. 批量测试调度（多组参数组合的自动化测试）
3. 结果汇总报告（多测试结果的指标汇总）

现有 `auto_vm_test.py` + `batch_test_scheduler.py` 为 QEMU VM 提供了完整的自动化测试流程，可作为参考。

### 1.2 目标

为 `e2b_bench` 实现：
1. **单次测试流程增强**：创建沙箱 → smap_tool → 预热 → vm_monitor → 压测 → 汇总
2. **批量测试调度**：支持测试矩阵（total_count × benchmark_percent × ratio）
3. **沙箱复用策略**：同组内（相同 total_count + ratio）的测试复用沙箱
4. **工程模块化**：类结构设计，易于扩展和测试

---

## 2. 整体架构

### 2.1 文件组织

```
e2b_bench/
├── bench.py              # 修改：增加可选的 smap_tool + vm_monitor 启动阶段
├── batch_scheduler.py    # 新建：批量测试调度主入口
├── task_generator.py     # 新建：测试矩阵任务生成
├── metrics_extractor.py  # 新建：从 analysis_report.xlsx 提取指标
├── report_aggregator.py  # 新建：汇总多测试结果到 Excel
├── config.py             # 修改：增加 smap_tool / vm_monitor 配置字段
├── schemas.py            # 修改：增加 BatchTask / TaskGroup 数据结构
├── sandbox_manager.py    # 保持不变
├── task_runner.py        # 保持不变
├── stats_collector.py    # 保持不变

config/
├── e2b_bench.yaml              # 保持：单次测试配置
├── e2b_batch_template.yaml     # 新建：批量测试模板（固定参数）
├── e2b_batch_matrix.yaml       # 新建：测试矩阵配置（可变维度）
```

### 2.2 核心类职责

| 类 | 文件 | 职责 |
|----|------|------|
| `SmapToolManager` | bench.py | 启动/停止 smap_tool 进程，管理 PID |
| `VmMonitorManager` | bench.py | 启动 vm_monitor，等待 analysis_report.xlsx 生成 |
| `BatchTask` | schemas.py | 单个测试任务参数（total_count, benchmark_percent, ratio） |
| `TaskGroup` | schemas.py | 可复用沙箱的测试任务组（相同 total_count + ratio） |
| `TaskGenerator` | task_generator.py | 从测试矩阵生成 TaskGroup 和 BatchTask |
| `GroupRunner` | batch_scheduler.py | 执行一个 TaskGroup（创建沙箱 → 多次压测 → 清理） |
| `BatchScheduler` | batch_scheduler.py | 执行所有 TaskGroup，汇总结果 |
| `MetricsExtractor` | metrics_extractor.py | 从 analysis_report.xlsx 提取 60+ 指标 |
| `ReportAggregator` | report_aggregator.py | 汇总所有测试结果到一个 Excel |

---

## 3. 单次测试流程增强

### 3.1 流程顺序（批量模式）

```
Phase 1: 创建/检测沙箱        ← 先有沙箱才能内存迁移
Phase 2: 启动 smap_tool       ← 基于 firecracker PIDs
Phase 3: 预热                 ← 组内共享，只执行一次
Phase 4: 启动 vm_monitor      ← 使用 --stress-file 同步
Phase 5: 启动统计收集
Phase 6: 启动浏览器任务       ← 创建 stress_file 通知 vm_monitor
Phase 7: 运行指定时长
Phase 8: 停止 vm_monitor
Phase 9: 停止并统计           ← 不停止 smap_tool，不销毁沙箱
Phase 10: 停止 smap_tool      ← 仅在整组测试结束后
Phase 11: 销毁沙箱            ← 仅在整组测试结束后
```

### 3.2 stress_file 同步机制

- vm_monitor 启动时带 `--stress-file` 参数，等待锁文件出现才开始采样
- bench.py 在 Phase 6（启动浏览器任务前）创建锁文件
- vm_monitor 检测到锁文件后开始采样，确保采样窗口与压测窗口对齐
- 压测结束后删除锁文件，下一次压测重新创建（同组内多次压测时）

### 3.3 SmapToolManager 类

```python
class SmapToolManager:
    """管理 smap_tool 进程的生命周期"""

    def __init__(self, config: Config):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None

    def start(self, sandbox_count: int) -> bool:
        """
        启动 smap_tool，获取 firecracker PIDs

        命令格式：
        ./smap_tool <count> `pidof firecracker` \
            --swap-size <size> --ratio <ratio> \
            --src-nid <nid> --dest-nid <nid>
        """
        # 获取所有 firecracker 进程 PID
        # 构建命令并执行，输出重定向到日志文件

    def stop(self) -> None:
        """停止 smap_tool 进程"""

    def is_running(self) -> bool:
        """检查进程是否存活"""
```

### 3.4 VmMonitorManager 类

```python
class VmMonitorManager:
    """管理 vm_monitor 进程的生命周期"""

    def __init__(self, config: Config):
        self.config = config
        self.process: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        """
        启动 vm_monitor，使用 --stress-file 等待同步

        命令格式：
        python3 vm_monitor/cli.py --vmm firecracker \
            -t <duration> --stress-file <stress_file> \
            --log-dir <log_dir>
        """

    def wait_for_report(self, timeout: int = 300) -> Optional[str]:
        """
        等待 analysis_report.xlsx 生成，返回文件路径
        超时返回 None
        """

    def stop(self) -> None:
        """停止 vm_monitor 进程"""
```

---

## 4. 批量测试调度器

### 4.1 数据结构

#### BatchTask

```python
@dataclass
class BatchTask:
    """单个测试任务的参数"""
    task_id: str              # 唯一标识，如 "tc10_ratio10_bp0.5"
    total_count: int          # 沙箱数量
    benchmark_percent: float  # 参与压测比例
    ratio: int                # 内存迁移比例 (%)

    # 运行时状态（执行后填充）
    result_dir: Optional[str] = None      # 结果目录路径
    report_file: Optional[str] = None     # bench_report.txt 路径
    analysis_file: Optional[str] = None   # analysis_report.xlsx 路径
    success: bool = False
    error_msg: Optional[str] = None
```

#### TaskGroup

```python
@dataclass
class TaskGroup:
    """可复用沙箱的测试任务组"""
    group_id: str             # 如 "tc10_ratio10"
    total_count: int          # 组内所有任务共享
    ratio: int                # 组内所有任务共享
    tasks: List[BatchTask]    # 不同 benchmark_percent 的任务列表

    # 运行时状态
    sandbox_states: Optional[Dict] = None  # 共享的沙箱状态
    smap_tool_process: Optional[Popen] = None
```

### 4.2 TaskGenerator 类

```python
class TaskGenerator:
    """从测试矩阵生成 TaskGroup 和 BatchTask"""

    def __init__(self, matrix_config: dict):
        self.total_counts = matrix_config['total_counts']
        self.benchmark_percentages = matrix_config['benchmark_percentages']
        self.ratios = matrix_config['ratios']

    def generate_groups(self) -> List[TaskGroup]:
        """
        按 (total_count, ratio) 分组生成 TaskGroup

        分组逻辑：
        - 相同 total_count + ratio 的测试归为一组
        - 组内不同 benchmark_percent 的测试复用沙箱
        """
```

### 4.3 GroupRunner 类

```python
class GroupRunner:
    """执行一个 TaskGroup：创建沙箱 → smap_tool → 预热 → 多次压测 → 清理"""

    def __init__(self, group: TaskGroup, config: Config, template_path: str):
        self.group = group
        self.config = config
        self.template_path = template_path

    def run(self) -> List[BatchTask]:
        """
        执行整组测试，返回完成的任务列表

        流程：
        1. 创建沙箱（total_count 个，组内共享）
        2. 启动 smap_tool（ratio 参数）
        3. 预热（组内共享，只执行一次）
        4. 遍历 benchmark_percent 执行压测：
           - 启动 vm_monitor
           - 压测（benchmark_percent 比例的沙箱）
           - 停止 vm_monitor，收集 analysis_report.xlsx
           - 沙箱继续运行，smap_tool 继续运行
        5. 清理：停止 smap_tool，销毁沙箱
        """
```

### 4.4 BatchScheduler 类

```python
class BatchScheduler:
    """批量测试调度主入口"""

    def __init__(self, matrix_path: str, template_path: str, output_dir: str):
        self.matrix_config = self._load_matrix(matrix_path)
        self.template_config = self._load_template(template_path)
        self.output_dir = output_dir
        self.task_generator = TaskGenerator(self.matrix_config)
        self.metrics_extractor = MetricsExtractor()
        self.report_aggregator = ReportAggregator(output_dir)

    def run(self) -> str:
        """
        执行所有测试组，汇总结果

        流程：
        1. 生成测试组（TaskGenerator.generate_groups）
        2. 逐组执行（GroupRunner.run）
        3. 提取指标（MetricsExtractor.extract）
        4. 汇总报告（ReportAggregator.aggregate）
        """
```

---

## 5. MetricsExtractor 指标提取类

### 5.1 类结构

```python
class MetricsExtractor:
    """从 vm_monitor 的 analysis_report.xlsx 提取所有指标"""

    # 指标来源定义
    SHEET_MAPPING = {
        'Summary': ['VM_CPU_Mean', 'VM_CPU_Max'],
        'DevKit_TopDown': 13,   # 项指标
        'DevKit_Memory': 6,     # 项指标
        'NUMA_Bandwidth': ['Read_MB', 'Write_MB'],
        'KSys': 11,             # 项指标
        'UBWatch_Latency': 7,   # 项指标
        'UBWatch_Bandwidth': 'dynamic',  # per-chip + per-port
        'SMAPBW_Summary': ['Total_Cycles', 'Swap_Cycles', ...],
        'SMAPBW_Cycles': 'cycle_metrics',
        'Getfre_Summary': 'per-numa',
    }

    def extract(self, analysis_file: str) -> Dict[str, Any]:
        """从 analysis_report.xlsx 提取所有指标"""

    def _extract_summary(self, xls: pd.ExcelFile) -> Dict:
        """提取 Summary sheet 指标"""

    def _extract_devkit_topdown(self, xls: pd.ExcelFile) -> Dict:
        """提取 DevKit TopDown 指标（13项）"""

    def _extract_devkit_memory(self, xls: pd.ExcelFile) -> Dict:
        """提取 DevKit Memory 指标（6项）"""

    def _extract_numa_bandwidth(self, xls: pd.ExcelFile) -> Dict:
        """提取 NUMA Bandwidth 指标"""

    def _extract_ksys(self, xls: pd.ExcelFile) -> Dict:
        """提取 KSys 指标（11项）"""

    def _extract_ubwatch_latency(self, xls: pd.ExcelFile) -> Dict:
        """提取 UBWatch Latency 指标（7项）"""

    def _extract_ubwatch_bandwidth(self, xls: pd.ExcelFile) -> Dict:
        """提取 UBWatch Bandwidth 指标（动态列）"""

    def _extract_smapbw(self, xls: pd.ExcelFile) -> Dict:
        """提取 SMAPBW Summary 和 Cycles 指标"""

    def _extract_getfre(self, xls: pd.ExcelFile) -> Dict:
        """提取 Getfre Summary 指标（per-NUMA）"""
```

### 5.2 关键处理逻辑

1. **动态列提取（UBWatch_Bandwidth）**：
   - 列名模式：`Chip\d_Port\d_Read` / `Chip\d_Port\d_Write`
   - 动态识别并提取

2. **数据格式处理**：
   - 百分比指标：字符串 "45.2%" → float 45.2
   - 缺失值：NaN → None

3. **指标命名规范**：
   - 前缀标记来源：`DevKit_TopDown_Frontend_Bound`

---

## 6. ReportAggregator 汇总报告类

### 6.1 类结构

```python
class ReportAggregator:
    """汇总所有测试结果到一个 Excel 报告"""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir

    def aggregate(self, metrics_data: List[Dict]) -> str:
        """
        汇总所有测试指标数据，生成 Excel

        流程：
        1. 构建 DataFrame（每行一个测试任务）
        2. 排序（按 total_count, ratio, benchmark_percent）
        3. 添加数据源标记行（合并单元格标题）
        4. 导出 Excel（带样式）
        """

    def _build_dataframe(self, metrics_data: List[Dict]) -> pd.DataFrame:
        """将指标数据列表转换为 DataFrame"""

    def _add_source_headers(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加数据源合并单元格标题行"""

    def _export_excel(self, df: pd.DataFrame) -> str:
        """导出带样式的 Excel（不同数据源用不同颜色）"""
```

### 6.2 输出报告结构

```
e2b_batch_summary_YYYYMMDD_HHMMSS.xlsx

Sheet: Summary
列分组：
| Basic (4列) | Browser (4列) | VM_CPU (2列) | DevKit_TopDown (13列) | DevKit_Memory (6列) | ...
| task_id | total_count | ratio | benchmark_percent | browser_queries | ... | VM_CPU_Mean | ...

排序：total_count → ratio → benchmark_percent
样式：不同数据源用不同背景色
```

---

## 7. 配置文件设计

### 7.1 测试矩阵配置 (e2b_batch_matrix.yaml)

```yaml
# 测试矩阵配置 - 可变维度
test_matrix:
  total_counts: [10, 20, 50]
  benchmark_percentages: [0.5, 0.75, 1.0]
  ratios: [10, 20]

# 复用策略
reuse_strategy:
  reuse_sandbox: true      # 同组内复用沙箱
  reuse_smap_tool: true    # 同组内复用 smap_tool
```

### 7.2 模板配置 (e2b_batch_template.yaml)

```yaml
# 批量测试模板 - 固定参数
e2b_env:
  E2B_ACCESS_TOKEN: "your_token"
  E2B_API_KEY: "your_key"
  E2B_DOMAIN: "e2b.app"
  E2B_API_URL: "http://localhost:3000"
  E2B_HTTP_SSL: "false"

sandbox:
  template: "openclaw-browser-v1"
  create_timeout: 86400

browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  task_timeout: 200
  interval_min: 5
  interval_max: 15
  warmup_urls:
    - "http://192.168.110.10:8080/page1.html"
  warmup_loops: 1
  warmup_delay: 2

test:
  duration: 600
  stats_interval: 10

# smap_tool 配置（ratio 在批量测试时覆盖）
smap_tool:
  enabled: true
  path: "/path/to/smap_tool"
  swap_size: 81920
  src_nid: 2
  dest_nid: 5

# vm_monitor 配置
vm_monitor:
  enabled: true
  vmm_type: "firecracker"
  duration: 600
  log_dir: "results/e2b/vm_monitor"
  stress_file: "/dev/shm/e2b_benchmark_lock"

report:
  output_dir: "results/e2b/batch"
  filename_prefix: "e2b_batch"
```

---

## 8. CLI 入口设计

### 8.1 批量测试命令

```bash
# 执行批量测试
python -m e2b_bench.batch \
    --matrix config/e2b_batch_matrix.yaml \
    --template config/e2b_batch_template.yaml \
    --output-dir results/e2b/batch \
    --continue-on-failure
```

### 8.2 参数说明

| 参数 | 必需 | 说明 |
|------|------|------|
| `--matrix` | 是 | 测试矩阵配置文件路径 |
| `--template` | 是 | 模板配置文件路径 |
| `--output-dir` | 否 | 结果输出目录，默认 `results/e2b/batch` |
| `--continue-on-failure` | 否 | 测试失败时继续执行后续测试 |

---

## 9. 实现优先级

### Phase 1: 单次测试流程增强
1. `SmapToolManager` 类实现
2. `VmMonitorManager` 类实现
3. `bench.py` 流程集成（可选启用）
4. `config.py` 新增配置字段

### Phase 2: 批量调度框架
1. `schemas.py` 新增 `BatchTask` / `TaskGroup`
2. `task_generator.py` 实现
3. `GroupRunner` 类实现
4. `BatchScheduler` 类实现

### Phase 3: 指标提取与汇总
1. `metrics_extractor.py` 实现
2. `report_aggregator.py` 实现
3. CLI 入口完善

### Phase 4: 测试与文档
1. 单元测试
2. 使用文档
