# Docker Container Bench - 使用指南 (中文)

Docker 容器浏览器自动化性能测试工具，用于验证 OpenClaw 浏览器自动化能力在 Docker 容器化部署环境下的并发性能与稳定性。

## 功能特性

- **批量容器创建** - 支持分批创建或全并发创建，支持 CPU/Memory 资源限制
- **端口检查** - 自动检查 18789 (openclaw-gateway) 和 11436 (llama-server) 端口就绪
- **完整浏览器流程** - 执行 5 步浏览器操作流程（open → focus → snapshot → click → screenshot）
- **QPS 统计** - 以每秒成功请求数（QPS）作为核心性能指标
- **实时统计** - 实时显示创建时间、端口等待时间、任务延迟
- **性能报告** - 生成详细的性能报告（P50/P95/P99 延迟）
- **三种运行模式** - 完整流程、仅创建、检测已有

## 测试场景

验证 OpenClaw 浏览器自动化能力在容器化部署环境下的并发性能：
- 基于 `ubuntu-openclaw-chromium` 镜像
- 在宿主机上批量创建 2vCPU/2G 规格的容器实例
- 每个容器独立运行 Chromium 浏览器
- 通过 OpenClaw CLI 执行完整的网页操作流程
- 以总体 QPS 评估系统吞吐能力

## 架构设计

```text
docker_bench/
├── __init__.py           # 包初始化
├── __main__.py           # 模块入口
├── bench.py              # 主入口 - 测试流程控制
├── config.py             # 配置管理
├── container_manager.py  # 容器生命周期（创建、端口检查、清理）
├── task_runner.py        # 浏览器任务执行（5步流程）
├── stats_collector.py    # 统计收集与报告生成
├── schemas.py            # 数据结构定义
└── utils.py              # 工具函数

config/
└── docker_bench.yaml     # 配置文件模板
```

## 浏览器操作流程（5 步 = 1 次查询）

```text
前置: openclaw browser status && start（热启动，复用后台进程）
Step 1: openclaw browser open [URL] --label [NAME]  → 页面打开
Step 2: openclaw browser focus [TAB_ID]             → 标签聚焦
Step 3: openclaw browser snapshot --limit 200       → DOM快照
Step 4: openclaw browser click e218                 → 元素点击（失败重试）
Step 5: openclaw browser screenshot                 → 视觉截图
后置: rm -rf /root/.openclaw/browser/openclaw/user-data（擦除缓存）
```

## 快速开始

### 1. 准备 Docker 镜像

参考 [dockerfile_build](../dockerfile_build/README.md) 制作镜像：

```bash
cd dockerfile_build
docker build -t ubuntu-openclaw-chromium:arm64 .
```

### 2. 安装依赖

```bash
pip install -r docker_bench/requirements.txt
```

依赖：`docker>=6.0.0`, `PyYAML>=6.0`

### 3. 配置测试参数

编辑 `config/docker_bench.yaml`：

```yaml
docker:
  image: "ubuntu-openclaw-chromium:arm64"
  container_prefix: "oc-bench"
  cpu_limit: 2.0          # 2vCPU
  memory_limit: "2g"      # 2G 内存

container:
  total_count: 10         # 创建 10 个容器

browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"

test:
  duration: 160           # 测试持续 160 秒
```

### 4. 运行测试

#### 完整流程模式

创建容器、检查端口、执行任务、生成报告：

```bash
# 使用配置文件
python -m docker_bench --config config/docker_bench.yaml

# 命令行参数覆盖
python -m docker_bench --config config/docker_bench.yaml --total 20 --duration 300

# 完全命令行模式（无配置文件）
python -m docker_bench \
    --image ubuntu-openclaw-chromium:arm64 \
    --total 10 \
    --cpu 2 \
    --memory 2g \
    --duration 160
```

#### 仅创建模式（Phase 0）

只创建容器，不执行任务。容器保持运行供后续使用：

```bash
python -m docker_bench --config config/docker_bench.yaml --create-only

# 配合创建批量控制
python -m docker_bench --config config/docker_bench.yaml \
    --create-only \
    --create-batch-size 5 \
    --create-batch-interval 10
```

#### 检测已有模式

检测当前运行的容器并在其上执行压测：

```bash
python -m docker_bench --config config/docker_bench.yaml --detect

# 配合压测批量控制
python -m docker_bench --config config/docker_bench.yaml \
    --detect \
    --task-batch-size 5 \
    --task-batch-interval 5
```

## 运行模式对比

| 模式 | 参数 | 描述 | 容器行为 |
|------|------|------|----------|
| **完整流程** | (默认) | 创建→端口检查→压测→报告 | 测试后删除 |
| **仅创建** | `--create-only` | 创建→端口检查→退出 | 保持运行 |
| **检测已有** | `--detect` | 检测→压测→报告 | 保持运行 |

## 批量控制

### 两个独立的批量控制

| 控制项 | 目的 | 保护对象 |
|--------|------|----------|
| `create_batch` | 容器创建分批 | 宿主机资源 |
| `task_batch` | 任务执行分批 | 目标 Web 服务器 |

### 示例配置

```yaml
# 创建 20 个容器，分 4 批每批 5 个
create_batch:
  size: 5
  interval: 10  # 创建批次间隔 10 秒

# 压测分 4 批，每批 5 个容器开始任务
task_batch:
  size: 5
  interval: 5  # 压测批次间隔 5 秒
```

## 配置说明

### YAML 配置参数

| 配置项 | 参数 | 说明 | 默认值 |
|-------|------|------|-------|
| `docker` | `image` | Docker 镜像名称 | `ubuntu-openclaw-chromium:arm64` |
| `docker` | `container_prefix` | 容器名称前缀 | `oc-bench` |
| `docker` | `cpu_limit` | 每容器 CPU 限制 | `2.0` |
| `docker` | `memory_limit` | 每容器内存限制 | `2g` |
| `container` | `total_count` | 容器总数 | 10 |
| `container` | `detect_existing` | 检测已有容器 | false |
| `container` | `create_only` | 仅创建模式 | false |
| `create_batch` | `size` | 创建批次大小 | 可选 |
| `create_batch` | `interval` | 创建批次间隔 | 可选 |
| `task_batch` | `size` | 压测批次大小 | 可选 |
| `task_batch` | `interval` | 压测批次间隔 | 可选 |
| `browser` | `urls` | 浏览器测试 URL 列表 | 必填 |
| `browser` | `task_timeout` | 任务超时（秒） | 200 |
| `browser` | `interval_min` | 任务间隔最小值（秒） | 0.5 |
| `browser` | `interval_max` | 任务间隔最大值（秒） | 3.0 |
| `port_check` | `ports` | 待检查端口列表 | [18789, 11436] |
| `test` | `duration` | 测试持续时间（秒） | 160 |
| `test` | `benchmark_percent` | 压测容器百分比 | 1.0 |
| `report` | `output_dir` | 报告输出目录 | `results/docker` |

### 命令行参数

```bash
python -m docker_bench --help

选项：
  --config                  YAML 配置文件路径

  # Docker 配置
  --image                   Docker 镜像名称
  --prefix                  容器名称前缀
  --cpu                     每容器 CPU 限制 (--cpus)
  --memory                  每容器内存限制 (-m)
  --create-timeout          容器创建超时

  # 容器配置
  --total                   容器总数
  --detect                  检测已有容器模式
  --create-only             仅创建模式（Phase 0）

  # 创建批量控制
  --create-batch-size       创建批次大小（不设置则全并发）
  --create-batch-interval   创建批次间隔秒数

  # 压测批量控制
  --task-batch-size         压测批次大小（不设置则全并发）
  --task-batch-interval     压测批次间隔秒数

  # 浏览器任务
  --browser-url             浏览器 URL（可多次指定）
  --browser-timeout         浏览器任务超时
  --browser-interval-min    任务间隔最小值
  --browser-interval-max    任务间隔最大值

  # 测试运行
  --duration                测试持续时间秒数
  --stats-interval          统计快照间隔
  --benchmark-percent       压测容器百分比

  # 报告
  --output-dir              报告输出目录
  --filename-prefix         报告文件名前缀
```

## 测试流程

```text
阶段1：创建/检测容器
    ├── [完整/仅创建] 调用 docker run --cpus -m image
    ├── [检测] 通过 docker ps 查询已有容器
    ├── 记录 create_elapsed 时间（容器创建时间）
    └── 启动端口检查（18789 + 11436）

阶段2：端口检查
    ├── 检查 18789 (openclaw-gateway)
    ├── 检查 11436 (llama-server)
    ├── 记录 port_wait_elapsed 时间
    └── 两个端口都就绪后标记 PORT_READY

[仅创建模式：在此退出]

阶段3：启动浏览器任务
    ├── [有 task_batch] 分批启动任务
    ├── [无配置] 全并发启动
    └── 每个容器独立任务线程

阶段4：运行测试
    └── 收集实时统计数据

阶段5：停止并生成报告
    ├── [创建的容器] 删除所有容器
    ├── [检测模式] 保持容器运行
    └── 生成性能报告（包含 QPS）
```

## 容器状态流转

```text
PENDING → CREATING → CREATED → PORT_READY → (ACTIVE) → KILLED
                     ↓
                  FAILED
                     ↓
               PORT_FAILED
                     ↓
                  OFFLINE
```

## 性能报告

### 报告内容

1. **测试配置** - 镜像名称、容器规格、批量策略、测试时长
2. **容器状态** - 创建数量、失败数量、端口失败
3. **容器创建性能** - docker run 时间（不含端口等待）
4. **端口等待性能** - 等待端口就绪时间
5. **总启动性能** - create + port_wait 总时间
6. **浏览器查询统计** - 成功率、延迟（P50/P95/P99）
7. **QPS 统计** - 每秒成功查询数
8. **分步延迟分析** - 每步操作的平均延迟

### 报告示例

```text
================================================================================
Docker Container Bench - Browser Automation Performance Report
================================================================================

[Test Configuration]
  Image:           ubuntu-openclaw-chromium:arm64
  Container Spec:  2.0vCPU / 2g
  Total Containers:10
  Mode:            Full workflow
  Create Batch:    2 batches x 5 containers
  Create Interval: 10s
  Task Batch:      2 batches x 5 containers
  Task Interval:   5s
  Test Duration:   160s

[Browser Workflow (5 steps = 1 query)]
  Step 1: openclaw browser open [URL] --label [NAME]
  Step 2: openclaw browser focus [TAB_ID]
  Step 3: openclaw browser snapshot --limit 200
  Step 4: openclaw browser click e218 (retry on fail)
  Step 5: openclaw browser screenshot

[Container Status]
  Created (Docker):   10 / 10
  Ports Ready:        10 / 10
  Create Failed:      0
  Port Check Failed:  0
  Offline (runtime):  0

[Container Creation Performance]
  (docker run --cpus --mem elapsed time)
  Min:  1.5s
  Max:  8.2s
  Avg:  2.1s
  P50:  1.8s
  P95:  5.3s
  P99:  7.6s

[Port Check Wait Performance]
  (Waiting for [18789, 11436] ports)
  Min:  5.0s
  Max:  45.0s
  Avg:  12.3s
  P50:  10.0s
  P95:  35.0s
  P99:  42.0s

[Total Startup Performance]
  (Container creation + port wait)
  Min:  6.5s
  Max:  53.2s
  Avg:  14.4s
  P50:  11.8s
  P95:  40.3s
  P99:  49.6s

[Browser Query Statistics]
  (5-step workflow = 1 query)
  Total Queries: 1250
  Success:       1180
  Failed:        70 (timeout: 25)
  Success Rate:  94.4%
  Avg Latency:   2345.6ms
  P99 Latency:   5678.2ms

[Overall QPS]
  Total QPS:     7.38 queries/sec
  (Success queries / Test duration)

[Per-Step Latency Analysis]
  Open        : avg=856.2ms
  Focus       : avg=123.4ms
  Snapshot    : avg=567.8ms
  Click       : avg=234.5ms
  Screenshot  : avg=563.7ms

================================================================================
```

## 相关文档

- [E2B Bench 使用指南](e2b-bench-usage-zh.md) - E2B 沙箱性能测试
- [设计文档](../dockerfile_build/README.md) - Docker 镜像构建