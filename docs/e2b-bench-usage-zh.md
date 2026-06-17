# E2B Sandbox Bench - 使用指南 (中文)

E2B沙箱批量性能测试工具，用于测试E2B沙箱的启动性能和浏览器任务执行性能。

## 功能特性

- **批量沙箱创建** - 支持分批启动或全并发启动
- **端口检查** - 自动检查18789 (openclaw-gateway) 和 11436 (llama-server) 端口就绪
- **浏览器任务执行** - 执行浏览器任务并收集性能数据
- **实时统计** - 实时显示创建时间、端口等待时间、任务延迟
- **性能报告** - 生成详细的性能报告（P50/P95/P99延迟）

## 架构设计

```
e2b_bench/
├── __init__.py         # 包初始化
├── __main__.py         # 模块入口
├── bench.py            # 主入口 - 测试流程控制
├── config.py           # 配置管理
├── sandbox_manager.py  # 沙箱生命周期（创建、端口检查、关闭）
├── task_runner.py      # 浏览器任务执行
├── stats_collector.py  # 统计收集与报告生成
├── schemas.py          # 数据结构定义
├── utils.py            # 工具函数
├── debug_demo.py       # 调试工具
└── requirements.txt    # 依赖说明

config/
└── e2b_bench.yaml      # 配置文件模板
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r e2b_bench/requirements.txt
```

依赖：`e2b>=0.15.0`, `PyYAML>=6.0`

### 2. 配置凭证

编辑 `config/e2b_bench.yaml`：

```yaml
e2b_env:
  E2B_ACCESS_TOKEN: "你的真实token"
  E2B_API_KEY: "你的真实key"
  E2B_DOMAIN: "e2b.app"
  E2B_API_URL: "http://localhost:3000"  # E2B API服务器地址
  E2B_HTTP_SSL: "false"

sandbox:
  template: "openclaw-browser-v1"  # E2B模板名称
  create_timeout: 86400
  total_count: 100
```

### 3. 运行测试

```bash
# 使用配置文件
python -m e2b_bench --config config/e2b_bench.yaml

# 命令行参数覆盖
python -m e2b_bench --config config/e2b_bench.yaml --total 50 --duration 300

# 完全命令行模式（无配置文件）
python -m e2b_bench \
    --template openclaw-browser-v1 \
    --e2b-access-token your_token \
    --total 100 \
    --duration 600
```

## 配置说明

### YAML配置参数

| 配置项 | 参数 | 说明 | 默认值 |
|-------|------|------|-------|
| `e2b_env` | `E2B_ACCESS_TOKEN` | E2B访问令牌 | 必填 |
| `e2b_env` | `E2B_API_KEY` | E2B API密钥 | 必填 |
| `e2b_env` | `E2B_API_URL` | E2B API服务器地址 | `http://localhost:3000` |
| `sandbox` | `template` | E2B模板名称 | `openclaw-browser-v1` |
| `sandbox` | `total_count` | 沙箱总数 | 100 |
| `batch` | `size` | 每批沙箱数量 | 20（可选） |
| `batch` | `interval` | 批次间隔（秒） | 30（可选） |
| `browser` | `urls` | 浏览器测试URL列表 | 必填 |
| `browser` | `task_timeout` | 任务超时（秒） | 200 |
| `browser` | `interval_min` | 任务间隔最小值（秒） | 0.5 |
| `browser` | `interval_max` | 任务间隔最大值（秒） | 3.0 |
| `test` | `duration` | 测试持续时间（秒） | 600 |
| `report` | `output_dir` | 报告输出目录 | `results/e2b` |

### 命令行参数

```bash
python -m e2b_bench --help

选项：
  --config                  YAML配置文件路径
  --e2b-access-token        E2B访问令牌
  --e2b-api-key             E2B API密钥
  --template                E2B模板名称
  --total                   沙箱总数
  --batch-size              每批沙箱数量（不设置则全并发）
  --batch-interval          批次间隔秒数
  --browser-url             浏览器URL（可多次指定）
  --browser-timeout         浏览器任务超时
  --duration                测试持续时间秒数
  --output-dir              报告输出目录
```

## 测试流程

```
阶段1：创建沙箱
    ├── 调用sandbox.create() API
    ├── 记录create_elapsed时间（沙箱拉起时间）
    └── 启动端口检查（18789 + 11436）

阶段2：端口检查
    ├── 检查18789 (openclaw-gateway)
    ├── 检查11436 (llama-server)
    ├── 记录port_wait_elapsed时间
    └── 两个端口都就绪后标记PORT_READY

阶段3：启动浏览器任务
    ├── 为每个沙箱启动任务执行线程
    └── 随机间隔避免请求突增

阶段4：运行测试
    └── 收集实时统计数据

阶段5：停止并生成报告
    ├── 关闭所有沙箱（kill）
    └── 生成性能报告
```

## 性能报告

### 报告内容

1. **测试配置** - 模板名称、批次策略、测试时长
2. **沙箱状态** - 创建数量、失败数量、端口失败
3. **sandbox.create性能** - API调用时间（不含端口等待）
4. **端口等待性能** - 等待18789 + 11436端口就绪时间
5. **总启动性能** - create + port_wait总时间
6. **浏览器任务统计** - 成功率、延迟（P50/P95/P99）

### 报告示例

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
  Created (API):       98 / 100
  Ports Ready:         95 / 100
  Create Failed:       2
  Port Check Failed:   3

[Sandbox.create Performance]
  (sandbox.create API调用时间，不含端口等待)
  Min:  1.5s
  Max:  8.2s
  Avg:  2.1s
  P50:  1.8s
  P95:  5.3s
  P99:  7.6s

[Port Check Wait Performance]
  (等待18789 openclaw-gateway + 11436 llama-server端口就绪)
  Min:  5.0s
  Max:  45.0s
  Avg:  12.3s
  P50:  10.0s
  P95:  35.0s
  P99:  42.0s

[Total Startup Performance]
  (sandbox.create + 端口等待)
  Min:  6.5s
  Max:  53.2s
  Avg:  14.4s
  P50:  11.8s
  P95:  40.3s
  P99:  49.6s

[Browser Task Statistics]
  Total Tasks:   1250
  Success:       1180
  Failed:        70 (timeout: 25)
  Success Rate:  94.4%
  Avg Latency:   2345.6ms
  P99 Latency:   5678.2ms

================================================================================
```

## 调试工具

### 使用调试脚本

当浏览器命令执行失败时，使用调试工具排查问题：

```bash
# 设置环境变量
export E2B_ACCESS_TOKEN="你的token"
export E2B_API_KEY="你的key"
export E2B_TEMPLATE="openclaw-browser-v1"

# 运行调试
python e2b_bench/debug_demo.py
```

### 调试步骤

1. **基本命令测试** - echo、whoami、pwd、which openclaw
2. **端口检查** - 18789、11436状态
3. **浏览器组件测试** - openclaw --help、browser --help
4. **浏览器命令测试** - 完整的browser open命令
5. **替代方案测试** - 不同命令格式

### 常见问题

| 错误 | 可能原因 | 解决方案 |
|-----|---------|---------|
| `Response 400` | 模板名称无效 | 检查E2B模板是否存在 |
| `GatewayClient error` | Gateway服务未启动 | 检查18789端口状态 |
| `Port check failed` | 端口检查超时 | 增加PORT_CHECK_MAX_WAIT |
| `Command exit_code=1` | 命令语法错误 | 检查openclaw版本 |

## 沙箱状态流转

```
PENDING → CREATING → CREATED → PORT_READY → (ACTIVE) → KILLED
                     ↓
                  FAILED
                     ↓
               PORT_FAILED
                     ↓
                  OFFLINE
```

## 相关文档

- [E2B Bench Usage (EN)](e2b-bench-usage.md)
- [E2B Bench 设计文档](superpowers/specs/2026-06-16-e2b-sandbox-bench-design.md)
- [E2B Bench 实现计划](superpowers/plans/2026-06-16-e2b-sandbox-bench.md)