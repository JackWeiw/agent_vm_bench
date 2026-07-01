# vm_bench 模块使用指南

> **注意**：本文档描述模块化重构后的 `vm_bench` 包。原有的 `create_server.py` 和 `vm_bench_lite.py` 已重构整合为统一模块。

## 概述

`vm_bench` 模块提供统一的模块化接口，用于：
- **阶段 0**：OpenStack VM 创建，支持批量控制
- **阶段 1**：SSH 连接到 VM
- **阶段 2**：浏览器/QA/Stress 任务执行，支持预热
- **阶段 3**：实时统计收集与报告生成

## 快速开始

### 模块导入

```python
from vm_bench import Config, VMManager, run_benchmark
from vm_bench.schemas import VMStatus, VMState
```

### 命令行入口

```bash
python -m vm_bench --config config/vm_bench.yaml
```

---

## 配置

### YAML 配置文件

推荐使用 YAML 配置文件：

```yaml
# config/vm_bench.yaml

# OpenStack 环境
openstack:
  auth_source: "~/.admin-openrc"

# VM 创建（阶段 0）
vm_create:
  flavor: "2U_4G_40G"
  image: "ubuntu-24.04"
  network_id: "2661422b-37c4-4d84-90ce-521167c676c0"
  availability_zone: "nova_zone:controller"
  start_ip: "192.168.110.11"
  subnet_prefix: "192.168.110."
  vm_prefix: "test_openclaw"
  total_count: 80
  create_timeout: 1200
  create_only: false
  detect_existing: false

# 批量控制
create_batch:
  size: 20
  interval: 3

task_batch:
  size: 10
  interval: 5

# 任务模式
task:
  mode: "browser"
  duration: 600

# 浏览器配置
browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  timeout: 200
  interval_min: 5
  interval_max: 10
  benchmark_percent: 1.0
  warmup_urls:
    - "http://192.168.110.10:8080/page1.html"
    - "http://192.168.110.10:8080/page2.html"
  warmup_loops: 1
  warmup_delay: 3

# SSH 配置
ssh:
  port: 22
  username: "root"
  password: "openEuler12#$"
```

### 配置优先级

```
命令行参数 > YAML 配置 > dataclass 默认值
```

CLI 覆盖示例：

```bash
# YAML 配置 total_count=80，CLI 覆盖为 10
python -m vm_bench --config config/vm_bench.yaml -n 10 -t 300
```

---

## 使用模式

### 1. 仅创建 VM（阶段 0）

通过 OpenStack 创建 VM，创建完成后退出：

```bash
# 使用 YAML 配置
python -m vm_bench --config config/vm_bench.yaml --create-only

# 纯 CLI 模式
python -m vm_bench --create-only \
    -n 20 \
    --start-ip 192.168.110.11 \
    --flavor 2U_4G_40G \
    --image ubuntu-24.04 \
    --network-id <network-id>
```

输出包含创建时间报告：
```
[Creation Performance]
  Min:  15.2s
  Max:  45.8s
  Avg:  28.5s
  P50:  26.3s
  P95:  42.1s
  P99:  45.2s
```

### 2. 检测已有 VM

连接到已有的 VM，无需创建：

```bash
python -m vm_bench --detect \
    -n 20 \
    --start-ip 192.168.110.11 \
    -t 300
```

适用场景：VM 已从之前的 `--create-only` 会话运行。

### 3. 仅预热

仅执行预热阶段：

```bash
python -m vm_bench --warmup-only \
    --config config/vm_bench.yaml \
    -n 50
```

预热将 QEMU 进程内存提升到目标值。

### 4. 检测 + 预热

连接已有 VM 并执行预热：

```bash
python -m vm_bench --detect --warmup-only \
    -n 50 \
    --start-ip 192.168.110.11
```

### 5. 完整压测流程

创建 VM、连接、预热、压测：

```bash
python -m vm_bench --config config/vm_bench.yaml
```

### 6. 检测 + 压测

跳过创建，连接已有 VM 并压测：

```bash
python -m vm_bench --detect \
    --config config/vm_bench.yaml \
    -bsp 0.5 \
    -t 300
```

---

## CLI 参数参考

### VM 创建参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--config` | YAML 配置文件路径 | 无 |
| `-n, --total` | VM 总数 | 80 |
| `--start-ip` | 起始 IP 地址 | 192.168.110.11 |
| `--flavor` | OpenStack flavor | 2U_4G_40G |
| `--image` | OpenStack image | ubuntu-24.04 |
| `--network-id` | OpenStack 网络 ID | （从 YAML） |
| `--az` | Availability zone | nova_zone:controller |
| `--create-timeout` | VM 创建超时 | 1200 |
| `--create-only` | 仅创建 VM，不压测 | false |
| `--detect` | 检测已有 VM | false |

### 批量控制参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--create-batch-size` | 每批创建 VM 数 | 20 |
| `--create-batch-interval` | 创建批次间隔（秒） | 3 |
| `--task-batch-size` | 每批任务数 | 10 |
| `--task-batch-interval` | 任务批次间隔（秒） | 5 |

### SSH 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--ssh-port` | SSH 端口 | 22 |
| `--ssh-username` | SSH 用户名 | root |
| `--ssh-password` | SSH 密码 | openEuler12#$ |

### 浏览器任务参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--browser-url` | 浏览器 URL（可重复） | Weibo.html |
| `--browser-timeout` | 浏览器任务超时（秒） | 200 |
| `--browser-interval-min` | 任务间隔最小值（秒） | 5 |
| `--browser-interval-max` | 任务间隔最大值（秒） | 10 |
| `--browser-use-llm` | 使用 LLM 进行浏览器任务 | false |
| `--benchmark-percent` | 压测 VM 百分比 | 1.0 |

### 预热参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--warmup-url` | 预热 URL（可重复） | （从 YAML） |
| `--warmup-loops` | 预热循环次数 | 1 |
| `--warmup-delay` | 预热页面间隔（秒） | 3 |
| `--warmup-only` | 仅运行预热 | false |

### 任务模式参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--task-mode` | 任务模式：browser/qa/stress/mixed | browser |
| `-t, --duration` | 压测时长（秒） | 600 |

### QA 任务参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--qa-timeout` | QA 查询超时（秒） | 600 |
| `--qa-interval` | QA 间隔（秒） | 0.5 |
| `--qa-mode` | QA 模式：cli/http | cli |

### Stress 任务参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--stress-percent` | Stress VM 百分比 | 0.5 |
| `--stress-memory` | Stress 内存 MB | 2048 |
| `--no-keepalive` | 禁用 Stress keepalive | false |

### 报告参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--output-dir` | 报告输出目录 | results/vm |
| `--filename-prefix` | 报告文件前缀 | vm_bench |
| `--stats-interval` | 统计快照间隔（秒） | 10 |

### 清理参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--delete-after-test` | 测试后删除 VM | false |

---

## Python API

### 使用 Config

```python
from vm_bench import Config

# 从 YAML 文件加载
config = Config.load_from_yaml('config/vm_bench.yaml')

# 用 CLI 参数覆盖
from vm_bench.bench import build_arg_parser
parser = build_arg_parser()
args = parser.parse_args(['-n', '10', '-t', '300'])
config = Config.merge_with_args(config, args)

# 纯 CLI 模式
args = parser.parse_args(['-n', '20', '--start-ip', '192.168.110.50'])
config = Config.from_args(args)
```

### 使用 VMManager

```python
import threading
from vm_bench import Config, VMManager
from vm_bench.schemas import VMStatus

config = Config(
    total_count=20,
    start_ip="192.168.110.11",
    network_id="...",
    flavor="2U_4G_40G",
    create_only=True  # 仅创建，不压测
)

stop_event = threading.Event()
manager = VMManager(config, stop_event)

# 创建 VM
vm_states = manager.create_all()

# 检查创建状态
ready_count = sum(
    1 for s in vm_states.values()
    if s.creation_metrics.status == VMStatus.ACTIVE
)

print(f"创建完成: {ready_count}/{config.total_count}")
```

### 使用 run_benchmark

```python
from vm_bench import Config, run_benchmark

config = Config(
    start_ip="192.168.110.11",
    total_count=10,
    task_mode="browser",
    test_duration=60,
    detect_existing=True,  # 连接已有 VM
    benchmark_percent=0.5,  # 50% VM 参与压测
)

result = run_benchmark(config)
print(result['report'])
```

---

## 典型工作流

### 两阶段浏览器测试

```bash
# 阶段 0：创建 VM
python -m vm_bench --create-only -n 100 --config config/vm_bench.yaml

# 阶段 1a：预热（全部 100 个 VM）
python -m vm_bench --detect --warmup-only -n 100 --config config/vm_bench.yaml

# 阶段 1b：压测（50% VM，即 50 个）
python -m vm_bench --detect -bsp 0.5 -t 300 --config config/vm_bench.yaml
```

### 与 auto_vm_test.py 集成

`auto_vm_test.py` 现已内部使用 `vm_bench` 模块：

```bash
python auto_vm_test.py --config config/test_config.yaml
```

流程：
1. 删除已有 VM
2. 创建 VM（通过 `vm_bench.create_all()`）
3. 启动 smap_tool
4. 等待就绪
5. 预热阶段（通过 `vm_bench.run_benchmark(warmup_only=True)`）
6. 启动监控
7. 压测阶段（通过 `vm_bench.run_benchmark()`）
8. 收集结果
9. 清理

---

## 报告输出

### 报告结构

```
results/vm/
├── vm_bench_20240601_143052.txt
```

### 报告内容示例

```
================================================================================
VM Bench - 性能报告
================================================================================

[测试配置]
  VM 总数:       80
  任务模式:      browser
  测试时长:      600s

[VM 状态]
  创建成功:      80
  SSH 连接:      78
  离线:          2

[VM 创建性能]
  最小:  15.2s
  最大:  45.8s
  平均:  28.5s
  P50:  26.3s
  P95:  42.1s
  P99:  45.2s

[SSH 连接性能]
  最小:  1.2s
  最大:  5.8s
  平均:  2.5s

[浏览器任务统计]
  总任务数:      500
  成功:          485
  失败:          15 (超时: 5)
  成功率:        97.0%
  平均延迟:      8500.0ms
  P99 延迟:      12000.0ms
================================================================================
```

---

## 默认参数汇总

| 参数 | 默认值 |
|------|--------|
| `create_batch_size` | 20 |
| `create_batch_interval` | 3 |
| `task_batch_size` | 10 |
| `task_batch_interval` | 5 |
| `browser_interval_min` | 5 |
| `browser_interval_max` | 10 |
| `warmup_loops` | 1 |
| `warmup_delay` | 3 |

---

## 与原脚本对比

| 原脚本 | vm_bench 模块 |
|--------|---------------|
| `create_server.py` | `VMManager.create_all()` |
| `vm_bench_lite.py --browser-mode -wp` | `run_benchmark(warmup_only=True)` |
| `vm_bench_lite.py --browser-mode -bsp 0.5` | `run_benchmark(benchmark_percent=0.5)` |
| 仅 CLI 参数 | YAML 配置 + CLI 覆盖 |

---

## 故障排查

### VM 创建失败

```bash
# 检查 OpenStack 环境
source ~/.admin-openrc
openstack server list

# 检查 network_id
openstack network show <network-id>
```

### SSH 连接失败

```bash
# 检查 VM 状态
openstack server show <vm-uuid> -c status

# 手动 SSH 测试
ssh root@192.168.110.11
```

### 导入错误

```bash
# 安装依赖
pip install paramiko pyyaml

# 验证导入
python -c "from vm_bench import Config, VMManager; print('OK')"
```

---

## 相关文档

- [配置参考](config/vm_bench.yaml)
- [测试套件](vm_bench/tests/)
- [原使用指南](usage-guide.md)（旧版脚本）

---

## 单元测试

```bash
# 运行所有测试
pytest vm_bench/tests/

# 运行单个测试文件
pytest vm_bench/tests/test_config.py

# 带覆盖率报告
pytest vm_bench/tests/ --cov=vm_bench --cov-report=term-missing
```

测试覆盖范围：
- `test_config.py`：配置默认值、YAML 加载、CLI 参数、合并优先级
- `test_schemas.py`：所有数据结构（VMState、Metrics、枚举）
- `test_utils.py`：百分位计算函数
- `test_vm_manager.py`：SSH 连接、VM 生命周期管理
- `test_task_runner.py`：QA、Stress、Browser 任务管理器
- `test_stats_collector.py`：快照收集、报告生成
- `test_bench.py`：CLI 参数解析、批量控制器