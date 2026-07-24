# E2B Sandbox Bench - 使用指南 (中文)

E2B沙箱批量性能测试工具，用于测试E2B沙箱的启动性能和浏览器任务执行性能，支持内存迁移压力测试。

## 完整测试流程

从环境搭建到运行压测的完整端到端流程：

```text
download_page.sh → docker build → push_to_harbor.sh → build_e2b.py → http.server
     ↓                 ↓              ↓                 ↓            ↓
  网页页面       Docker镜像     Harbor仓库        E2B模板       Web服务器
                                                                     ↓
                            config/e2b_bench.yaml (模板名 + URL)
                                     ↓
                            --create-only → --detect → 压测 → delete_sandbox.sh
```

### 第1步：下载网页

下载维基百科页面（含图片）用于浏览器预热和压测任务：

```bash
# 下载全部10个维基百科页面（China、Earth、Galaxy、Hubble等）
bash download_page.sh

# 或下载指定页面
bash download_page.sh -p Weibo,China
```

页面保存到 `web_content/en.wikipedia.org/wiki/`，每个页面包含HTML和图片，图片链接已修复为本地路径。

### 第2步：构建Docker镜像

构建包含openclaw、agent-browser、Chromium、llama-server和supervisor的基础Docker镜像：

```bash
cd dockerfile_build

# 构建ARM64镜像（默认）
docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 .

# 构建x86镜像
docker build -f Dockerfile.x86 -t ubuntu-openclaw-chromium:24.04-linuxx86 .
```

Dockerfile安装内容：
- Ubuntu 24.04 基础系统
- Node24 + openclaw@2026.6.6 + agent-browser
- Chromium（通过xtradeb PPA）
- llama-server + BGE嵌入模型
- supervisor（管理llama-server + openclaw-gateway）

### 第3步：推送镜像到Harbor仓库

推送构建好的镜像到Harbor仓库（E2B模板构建需要）：

```bash
cd dockerfile_build

# 设置Harbor IP为你的E2B/Harbor服务器地址
HARBOR_IP=71.14.96.192 bash push_to_harbor.sh
```

脚本执行流程：
1. 检查基础镜像是否存在
2. 启动临时容器，安装systemd + openssh-server + websocat
3. 导出容器为新镜像 `ubuntu-openclaw-chromium:custom`
4. 标记并推送到 `HARBOR_IP:2900/e2b-orchestration/ubuntu-openclaw-chromium:custom`

Harbor访问地址：`http://HARBOR_IP:2900/`（admin/Harbor12345）

### 第4步：构建E2B模板

从Harbor镜像构建E2B模板，创建用于sandbox.create()的Firecracker microVM模板：

```bash
cd dockerfile_build

# 构建模板（alias = 配置中使用的模板名称）
python3 build_e2b.py --server-ip 71.14.96.192 --alias openclaw-browser-v1

# 自定义Harbor IP和模板参数
python3 build_e2b.py \
    --server-ip 71.14.96.192 \
    --harbor-ip 71.14.96.192 \
    --alias openclaw-browser-v1 \
    --cpu 2 \
    --memory 4096
```

**前置条件：** `~/.e2b/config.json` 必须存在，包含：
```json
{
  "teamId": "...",
  "accessToken": "sk_e2b_...",
  "teamApiKey": "e2b_..."
}
```

脚本读取配置、设置E2B环境变量、从Harbor镜像构建模板、创建测试沙箱。

### 第5步：启动Web服务器

启动本地HTTP服务器提供下载的页面，绑定到指定NUMA节点实现内存隔离：

```bash
cd web_content/en.wikipedia.org/wiki

# 绑定到NUMA 2,3（与沙箱NUMA一致，确保本地访问）
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

可用页面：`http://本机IP:8080/China.html`、`http://本机IP:8080/Hubble_Space_Telescope.html` 等。

### 第6步：修改配置

编辑 `config/e2b_bench.yaml` 使其匹配你的环境：

```yaml
e2b_env:
  E2B_API_URL: "http://71.14.96.192:3000"  # 你的E2B API服务器

sandbox:
  template: "openclaw-browser-v1"  # 第4步创建的模板别名
  total_count: 100
  numa_bind: 2

browser:
  urls:
    - "http://本机IP:8080/Hubble_Space_Telescope.html"  # 你的Web服务器
  warmup_urls:
    - "http://本机IP:8080/China.html"
    - "http://本机IP:8080/Earth.html"
    - "http://本机IP:8080/Galaxy.html"
    - "http://本机IP:8080/Hubble_Space_Telescope.html"
    - "http://本机IP:8080/Human.html"
    - "http://本机IP:8080/List_of_paintings_by_Vincent_van_Gogh.html"
    - "http://本机IP:8080/Solar_System.html"
    - "http://本机IP:8080/United_States.html"
    - "http://本机IP:8080/World_War_II.html"

test:
  benchmark_mode: "round_robin"
  round_size: 5
  round_count: 5
  round_interval: 5
```

### 第7步：创建沙箱

仅创建沙箱不执行任务（Phase 0），沙箱保持运行供后续压测使用：

```bash
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# 保存沙箱ID供跨会话复用
python -m e2b_bench --config config/e2b_bench.yaml --create-only --sandbox-ids-file sandboxs.txt
```

### 第8步：运行压测

检测已有沙箱并运行压测：

```bash
# 固定模式（所有沙箱并发执行任务）
python -m e2b_bench --config config/e2b_bench.yaml --detect

# 轮转模式（分组轮换，用于内存迁移压力测试）
python -m e2b_bench --config config/e2b_bench.yaml --detect -bm round_robin -rs 5 -rc 5

# 多阶段方式：先预热，再压测
python -m e2b_bench --config config/e2b_bench.yaml --detect --warmup-only  # 预热阶段
python -m e2b_bench --config config/e2b_bench.yaml --detect                # 压测阶段

# 使用沙箱ID文件
python -m e2b_bench --config config/e2b_bench.yaml --detect --sandbox-ids-file sandboxs.txt
```

### 第9步：删除沙箱

测试完成后删除所有运行中的沙箱：

```bash
cd e2b_bench/scripts

# 配置环境
cp .env.example .env
# 编辑 .env: E2B_API_URL=http://71.14.96.192:3000

# 删除所有沙箱
bash delete_sandbox.sh

# 指定自定义.env路径
bash delete_sandbox.sh path/to/.env
```

脚本从 `.env` 读取 `E2B_API_URL`，从 `~/.e2b/config.json` 读取API凭证，通过E2B API删除所有沙箱。

## 功能特性

- **批量沙箱创建** - 支持分批创建或全并发创建
- **端口检查** - 自动检查18789 (openclaw-gateway) 和 11436 (llama-server) 端口就绪
- **NUMA绑定** - 绑定沙箱创建到指定NUMA节点，控制内存位置
- **浏览器预热阶段** - 通过agent-browser多标签页方式预热内存
- **两种压测模式** - 固定模式（子集百分比）和轮转模式（分组轮换+新标签操作）
- **步骤级计时** - 分离记录open_tab、page_load、snapshot、click、screenshot各步骤耗时
- **尾部延迟分析** - P99/P50比率及严重程度分类（最小/中等/显著）
- **轮次对比** - 每轮统计表格，含成功率与延迟分析
- **错误分类** - 自动错误类型分类（D-Bus、Gateway、Timeout等）
- **smap_tool集成** - 内存迁移监控，可配置swap大小、迁移比例和NUMA节点
- **vm_monitor集成** - 性能监控，通过stress-file同步机制检测压测阶段
- **沙箱ID持久化** - 跨会话保存/加载沙箱ID，支持复用
- **实时统计** - 实时显示创建时间、端口等待时间、任务延迟
- **性能报告** - 生成详细TXT报告（P50/P95/P99延迟、步骤计时、错误详情）
- **批量测试模式** - 基于矩阵配置的批量测试，同组内复用沙箱和smap_tool
- **离线摘要** - 从已有测试结果生成聚合Excel摘要
- **CLI > YAML > 默认值优先级** - 所有字段统一的配置覆盖链
- **四种运行模式** - 完整流程、仅创建、检测已有、仅预热

## 架构设计

```text
e2b_bench/
├── __init__.py            # 包初始化
├── __main__.py            # 模块入口（--batch或单次模式）
├── bench.py               # 主入口 - 测试流程、SmapToolManager、VmMonitorManager
├── config.py              # 配置管理（YAML + CLI + 默认值）
├── sandbox_manager.py     # 沙箱生命周期（创建、端口检查、NUMA绑定、关闭）
├── task_runner.py         # 任务执行：WarmupRunner、BrowserTaskRunner、TabOperationRunner
├── round_robin.py         # 轮转任务管理器（分组轮换、循环）
├── task_generator.py      # 批量任务生成（从矩阵配置）
├── stats_collector.py     # 统计收集、ErrorClassifier、ReportFormatter
├── schemas.py             # 数据结构（BrowserMetrics含步骤级计时）
├── metrics_extractor.py   # 从vm_monitor和浏览器报告提取指标
├── report_aggregator.py   # 聚合批量结果为带样式的Excel
├── utils.py               # calc_percentiles、calc_tail_ratio、classify_tail_latency
├── tests/                 # 单元测试
├── .env.example           # 环境变量模板
└── requirements.txt       # 依赖说明

config/
├── e2b_bench.yaml         # 单次测试配置
├── e2b_batch_matrix.yaml  # 批量测试矩阵（total_counts、ratios、percentages）
└── e2b_batch_template.yaml # 批量测试模板配置
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
  detect_existing: false
  create_only: false
  numa_bind: 2  # 绑定沙箱创建到NUMA节点2（null/省略=不绑定）
  sandbox_ids_file: "sandboxs.txt"  # 保存/加载沙箱ID（空=禁用）

# 创建批量控制（保护E2B API不被过载）
create_batch:
  size: 20      # 每批创建的沙箱数量
  interval: 30  # 创建批次间隔（秒）

# 压测批量控制（保护被压测服务器不被过载）
task_batch:
  size: 10      # 每批开始压测的沙箱数
  interval: 5   # 压测批次间隔（秒）

# 浏览器任务配置
browser:
  urls:
    - "http://192.168.110.10:8080/Hubble_Space_Telescope.html"
  task_timeout: 200
  interval_min: 5
  interval_max: 15
  # 预热配置（通过agent-browser多标签页方式）
  warmup_urls:
    - "http://192.168.110.10:8080/China.html"
    - "http://192.168.110.10:8080/Earth.html"
    - "http://192.168.110.10:8080/Galaxy.html"
    - "http://192.168.110.10:8080/Hubble_Space_Telescope.html"
    - "http://192.168.110.10:8080/Human.html"
    - "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html"
    - "http://192.168.110.10:8080/Solar_System.html"
    - "http://192.168.110.10:8080/United_States.html"
    - "http://192.168.110.10:8080/World_War_II.html"
  warmup_loops: 1      # 循环次数（注意：标签页模式下每个URL只打开一次）
  warmup_delay: 5      # 预热页面间延迟（秒）
  warmup_only: false   # 仅运行预热后退出

# 测试运行配置
test:
  duration: 160
  stats_interval: 10
  benchmark_percent: 1.0    # 压测沙箱百分比（仅固定模式）

  # 轮转模式配置
  benchmark_mode: "round_robin"   # "fixed"（默认）或 "round_robin"
  round_size: 5    # 每轮沙箱数（组数 = ceil(总数 / round_size))
  round_count: 5   # 最大轮次数（终止条件，达到时或超过duration时停止）
  round_interval: 5  # 轮次间隔（秒）

# smap_tool配置（内存迁移监控）
smap_tool:
  enabled: false
  path: ""            # smap_tool可执行文件路径
  swap_size: 81920    # Swap大小（MB）
  ratio: 15           # 迁移比例
  src_nid: 2          # 源NUMA节点
  dest_nid: 5         # 目标NUMA节点

# vm_monitor配置（性能监控）
vm_monitor:
  enabled: false
  vmm_type: "firecracker"
  duration: 600
  numa: "1"           # 监控NUMA节点（逗号分隔）
  log_dir: "results/e2b/vm_monitor"
  stress_file: "/dev/shm/e2b_benchmark_lock"

# 报告配置
report:
  output_dir: "results/e2b"
  filename_prefix: "e2b_bench"
```

### 3. 运行测试

#### 完整流程模式（固定压测）

创建沙箱、预热、对子集执行压测、生成报告：

```bash
# 使用配置文件
python -m e2b_bench --config config/e2b_bench.yaml

# 命令行参数覆盖
python -m e2b_bench --config config/e2b_bench.yaml --total 50 --duration 300 -bp 0.5

# 完全命令行模式（无配置文件）
python -m e2b_bench \
    --template openclaw-browser-v1 \
    --e2b-access-token your_token \
    --total 100 \
    --duration 600
```

#### 轮转模式（内存迁移压力测试）

沙箱分组轮换执行，每轮打开新标签页触发内存分配和swap：

```bash
# 轮转模式：每轮5个沙箱，共5轮
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -rc 5 -ri 5

# 轮转模式：不限轮数，直到duration结束
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -ri 5 --duration 600

# 轮转模式配合smap_tool内存迁移监控
python -m e2b_bench --config config/e2b_bench.yaml \
    -bm round_robin -rs 5 -rc 10 \
    --warmup-url http://server/page1.html
```

#### 仅创建模式（Phase 0）

只创建沙箱，不执行任务。沙箱保持运行供后续使用：

```bash
python -m e2b_bench --config config/e2b_bench.yaml --create-only

# 配合创建批量控制和NUMA绑定
python -m e2b_bench --config config/e2b_bench.yaml \
    --create-only \
    --create-batch-size 20 \
    --create-batch-interval 30

# 保存沙箱ID供后续复用
python -m e2b_bench --config config/e2b_bench.yaml \
    --create-only --sandbox-ids-file sandboxs.txt
```

#### 检测已有模式

检测当前运行的沙箱（从ID文件或API）并在其上执行压测：

```bash
# 检测所有运行中的沙箱
python -m e2b_bench --config config/e2b_bench.yaml --detect

# 从保存的ID文件检测
python -m e2b_bench --config config/e2b_bench.yaml \
    --detect --sandbox-ids-file sandboxs.txt

# 配合压测批量控制
python -m e2b_bench --config config/e2b_bench.yaml \
    --detect \
    --task-batch-size 10 \
    --task-batch-interval 5
```

#### 仅预热模式

只运行预热阶段预热浏览器内存，然后退出。沙箱保持运行供后续压测：

```bash
python -m e2b_bench --config config/e2b_bench.yaml --warmup-only

# 配合自定义预热页面
python -m e2b_bench --config config/e2b_bench.yaml \
    --warmup-only \
    --warmup-url http://192.168.110.10:8080/page1.html \
    --warmup-url http://192.168.110.10:8080/page2.html \
    --warmup-loops 1 \
    --warmup-delay 5

# 大规模预热（> 100沙箱会分波创建）
python -m e2b_bench --config config/e2b_bench.yaml \
    --warmup-only --total 200
```

#### 完整流程：预热 + 轮转模式

创建沙箱、预热（打开N个标签页），然后轮转压测（每轮打开新标签页）：

```bash
python -m e2b_bench --config config/e2b_bench.yaml \
    --warmup-url http://192.168.110.10:8080/page1.html \
    -bm round_robin -rs 5 -rc 5 --duration 600
```

## 运行模式对比

| 模式 | 参数 | 描述 | 沙箱行为 |
|------|------|------|----------|
| **完整流程（固定）** | (默认) | 创建→端口检查→压测→报告 | 测试后关闭 |
| **完整流程（轮转）** | `-bm round_robin` | 创建→端口检查→轮转压测→报告 | 测试后关闭 |
| **仅创建** | `--create-only` | 创建→端口检查→退出 | 保持运行 |
| **检测已有** | `--detect` | 检测→压测→报告 | 保持运行 |
| **仅预热** | `--warmup-only` | 创建/检测→预热→退出 | 保持运行 |

## 压测模式详解

### 固定模式（默认）

指定百分比的沙箱并发执行浏览器任务，持续到测试结束。由 `benchmark_percent` 控制（如0.5 = 50%的沙箱）。

```yaml
test:
  benchmark_mode: "fixed"
  benchmark_percent: 0.5  # 50%的沙箱执行任务
```

### 轮转模式

沙箱按 `round_size` 分成若干组，每轮激活一组，打开新标签页执行操作。这会持续分配内存，触发swap-out事件用于内存迁移压力测试。

**分组方式：**
- `round_size` 决定组数：`group_count = ceil(total / round_size)`
- 沙箱均匀分配到各组（余数分配到前几组）
- 示例：103个沙箱，`round_size=5` → 21组 (5,5,...,5,3)

**轮次执行：**
- 轮0 → 组0，轮1 → 组1，...，轮20 → 组20
- 所有组完成后循环继续：轮21 → 组0
- 每轮：`TabOperationRunner` 打开新标签页 → snapshot → click → screenshot
- 轮次间隔 (`round_interval`) 提供内存迁移时间窗口

**终止条件（共存）：**
- `round_count`：达到N轮后停止（如指定）
- `duration`：达到测试时长后停止
- 任一条件满足即停止

```yaml
test:
  benchmark_mode: "round_robin"
  round_size: 5       # 每轮5个沙箱（决定组数）
  round_count: 10     # 最大10轮（终止条件）
  round_interval: 5   # 轮次间隔5秒
  duration: 600       # 600秒后也停止
```

**轮转 + 预热：**

预热阶段打开多个标签页（每个warmup_url一个），预加载浏览器内存。轮转压测时，每轮打开**新标签页**，增加内存压力，访问被迁移内存时触发swap。

## 浏览器操作详解

### 预热阶段（标签页模式）

预热使用 `agent-browser` 打开多个标签页：

```text
对于每个沙箱：
  检查agent-browser可用性
  对于每个warmup_url：
    agent-browser tab new "{url}"       # 打开标签页
    agent-browser wait --load domcontentloaded --timeout 120000  # 等待页面加载
    agent-browser snapshot -i           # DOM快照（内存分配）
    agent-browser click {element}       # 点击元素（内存分配）
    agent-browser screenshot            # 截图（内存分配）
    等待warmup_delay秒
  标记warmup_done = True
```

**注意：** 标签页模式下 `warmup_loops` 无效 — 每个URL只打开一次作为一个标签页。

### 固定模式浏览器任务

每个沙箱在独立线程中循环执行浏览器任务：

```text
对于每个沙箱（独立线程）：
  while not stop_event:
    openclaw browser --browser-profile openclaw open '{url}'  # 打开URL
    等待随机间隔（interval_min到interval_max）
    若连续3次失败 → 标记为offline
```

### 轮转模式标签页操作（5步骤）

每轮打开新标签页并执行5步操作序列，带详细计时：

```text
步骤1: agent-browser tab new "{url}"                         → open_tab计时
步骤2: agent-browser wait --load networkidle --timeout 60s   → page_load计时
步骤3: agent-browser snapshot -i                             → snapshot计时
步骤4: agent-browser click {element}                         → click计时
步骤5: agent-browser screenshot                              → screenshot计时
```

**步骤计时提供细粒度性能分析：**
- `open_tab`：标签页创建时间（E2B进程开销）
- `page_load`：网络空闲等待时间（页面渲染+资源加载）
- `snapshot`：DOM快照时间（Chrome DevTools协议开销）
- `click`：元素交互时间
- `screenshot`：截图捕获时间

**非致命步骤：** click和screenshot失败仅记录日志，不标记任务为失败。

## 尾部延迟分析

报告包含基于P99/P50比率的尾部延迟分析：

| 尾部比率 | 分类 | 含义 |
|----------|------|------|
| < 1.2x | 最小 | 分布良好，无显著异常值 |
| 1.2x ~ 1.5x | 中等 | 存在一定的长尾异常值 |
| > 1.5x | 显著 | 严重长尾延迟，异常值占主导 |

应用于步骤级计时和轮次对比表格。

## 错误分类

失败任务自动分类为错误类型：

| 错误类型 | 匹配模式 | 典型原因 |
|----------|----------|----------|
| Open tab failed | `open_tab failed` | 标签页创建失败 |
| Page load failed | `page_load failed` | 网络/页面加载超时 |
| Snapshot failed | `snapshot failed` | DOM快照超时 |
| Click failed | `click failed` | 元素点击超时 |
| Screenshot failed | `screenshot failed` | 截图捕获失败 |
| Chrome start failed | `chrome_start`、`failed to start chrome` | Chrome进程崩溃 |
| D-Bus connection error | `d-bus`、`dbus` | D-Bus守护进程不可用 |
| Gateway connection error | `gateway`、`cdp`、`http_unreachable` | CDP网关不可达 |
| Timeout | `timeout`、`timed out` | 命令超时 |
| Other | （兜底） | 未分类错误 |

每个错误含详细诊断信息：exit_code、stderr、stdout（截断显示）。

## 轮次对比报告

轮转模式下，报告包含轮次对比表格：

```text
[Round Comparison]
================================================================================

  Summary: 50 tasks across 10 rounds

Round  Tasks  Success%  Avg(s)  P50(s)  P95(s)  P99(s)  Tail
0      5      100.0     3.42    3.30    4.10    5.20    1.58x (significant)
1      5      80.0      4.10    3.80    5.30    7.10    1.87x (significant)
...
```

每轮显示：
- 任务数（基于轮次起止基线的差值）
- 成功率（百分比）
- 延迟百分位数（Avg、P50、P95、P99）
- 尾部比率及严重程度分类

## smap_tool集成

smap_tool在压测期间监控NUMA节点间的内存迁移：

```yaml
smap_tool:
  enabled: true
  path: "/path/to/smap_tool"  # smap_tool可执行文件路径
  swap_size: 81920             # Swap大小（MB）
  ratio: 15                    # 迁移比例（%）
  src_nid: 2                   # 源NUMA节点
  dest_nid: 5                  # 目标NUMA节点
```

**生命周期：**
- 沙箱创建后启动（获取 `pidof firecracker` 作为目标PID）
- 日志保存到结果目录的 `smap_tool/` 子目录
- 清理阶段停止
- 启动前清理 `/dev/shm/smap_config`

## vm_monitor集成

vm_monitor通过stress-file同步机制收集硬件性能指标：

```yaml
vm_monitor:
  enabled: true
  vmm_type: "firecracker"
  duration: 600        # 监控时长（应与测试时长匹配）
  numa: "1"            # 监控NUMA节点（逗号分隔）
  log_dir: "results/e2b/vm_monitor"
  stress_file: "/dev/shm/e2b_benchmark_lock"
```

**stress-file同步机制：**
- vm_monitor后台启动，等待stress文件出现
- 压测阶段开始时创建stress文件（`touch /dev/shm/e2b_benchmark_lock`）
- vm_monitor检测到stress文件后开始采集指标
- 压测结束时删除stress文件
- vm_monitor停止采样并生成 `analysis_report.xlsx`

**传递给vm_monitor的CLI标志：**
- `--vmm firecracker`（来自配置）
- `--enable-capture`（始终启用）
- `--auto-skip`（跳过不可用的工具）

## 批量测试模式

### 概述

批量模式基于矩阵配置运行多个测试场景，同组内复用沙箱和smap_tool。

```bash
# 在线模式：运行批量测试
python -m e2b_bench --batch --matrix config/e2b_batch_matrix.yaml

# 离线模式：从已有结果生成摘要
python -m e2b_bench --batch --offline --result-dir results/e2b/batch
```

### 矩阵配置

编辑 `config/e2b_batch_matrix.yaml`：

```yaml
test_matrix:
  total_counts: [10, 20, 50]
  benchmark_percentages: [0.5, 0.75, 1.0]
  ratios: [10, 20]

reuse_strategy:
  reuse_sandbox: true      # 同组内复用沙箱
  reuse_smap_tool: true    # 同组内复用smap_tool

result:
  template_path: "config/e2b_batch_template.yaml"
  output_dir: "results/e2b/batch"
```

### 批量工作流

```text
1. 按(total_count, ratio)生成任务组
2. 对于每个组：
   a. 创建共享沙箱（组内total_count）
   b. 启动smap_tool（共享，日志保存到组结果目录/smap_tool/）
   c. 预热（共享，一次）
   d. 对于每个benchmark_percent：
      - 启动vm_monitor（每任务独立，带stress-file同步）
      - 运行压测
      - 停止vm_monitor采样
      - 保存bench_report.txt
      - 等待analysis_report.xlsx
   e. 清理：停止smap_tool，关闭沙箱
3. 从所有结果提取指标
4. 生成聚合Excel摘要（带样式和数据源分组）
```

### 结果目录结构

```
results/e2b/batch/
├── batch_log_*.txt                           # 执行日志
├── e2b_batch_summary_*.xlsx                  # 聚合摘要
├── tc10_ratio10_20260629_140636/             # 组目录
│   ├── smap_tool/
│   │   ├── smap_stdout.log
│   │   └── smap_stderr.log
│   ├── tc10_ratio10_bp0.5_20260629_140805/   # 任务目录
│   │   ├── config_tc10_ratio10_bp0.5.yaml
│   │   ├── test_log.txt
│   │   ├── bench_report.txt
│   │   └── vm_monitor/
│   │       ├── analysis_report.xlsx
│   │       ├── monitor_stdout.log
│   │       └── monitor_stderr.log
│   └── tc10_ratio10_bp0.75_.../
│   └── tc10_ratio10_bp1.0_.../
└── tc20_ratio10_.../
```

## 配置优先级

配置遵循严格的优先级链：

**CLI参数 > YAML配置 > 内置默认值**

这意味着：
- 如果CLI参数被显式提供，它覆盖YAML配置
- 如果CLI参数未提供，使用YAML配置值
- 如果CLI和YAML都未指定，使用内置默认值
- CLI默认值（如`None`）不会错误覆盖YAML值

## 沙箱ID持久化

跨会话保存和加载沙箱ID，支持复用：

```yaml
sandbox:
  sandbox_ids_file: "sandboxs.txt"  # 每行一个ID
```

**行为：**
- **仅创建模式**：创建后写入成功的沙箱ID到文件
- **仅预热模式**：每个预热波次后追加ID
- **检测模式**：从文件加载ID而非查询API

支持多阶段测试工作流：
1. `--create-only --sandbox-ids-file ids.txt` → 创建沙箱，保存ID
2. `--warmup-only --detect --sandbox-ids-file ids.txt` → 检测沙箱并预热
3. `--detect --sandbox-ids-file ids.txt -bm round_robin` → 在预热后的沙箱上轮转压测

## 分波预热

仅预热模式下创建 > 100 个沙箱时，创建过程分为每波100个的分批处理：

```text
波1：创建100个沙箱 → 预热 → 追加ID
波2：创建剩余 → 预热 → 追加ID
...
```

避免过多并发沙箱创建导致E2B API过载。

## CLI参数

### 单次测试CLI

```bash
python -m e2b_bench --help

选项：
  -c, --config              YAML配置文件路径

  # E2B环境
  --e2b-access-token        E2B访问令牌
  --e2b-api-key             E2B API密钥
  --e2b-domain              E2B域名
  --e2b-api-url             E2B API URL
  --e2b-http-ssl            E2B HTTP SSL设置

  # 沙箱配置
  -t, --template            E2B模板名称
  -n, --total               沙箱总数
  --create-timeout          沙箱创建超时
  -d, --detect              检测已有沙箱模式
  --create-only             仅创建模式（Phase 0）
  --sandbox-ids-file        沙箱ID保存/加载文件路径

  # 创建批量控制
  --create-batch-size       创建批次大小（不设置则全并发）
  --create-batch-interval   创建批次间隔秒数

  # 压测批量控制
  --task-batch-size         压测批次大小（不设置则全并发）
  --task-batch-interval     压测批次间隔秒数

  # 浏览器任务
  --browser-url             浏览器URL（可多次指定）
  --browser-timeout         浏览器任务超时
  --browser-interval-min    任务间隔最小值
  --browser-interval-max    任务间隔最大值

  # 预热阶段
  -w, --warmup-url          预热页面URL（可多次指定）
  --warmup-loops            预热循环次数（默认：2）
  --warmup-delay            预热页面间延迟（默认：10）
  -wp, --warmup-only        仅运行预热后退出

  # 压测控制
  -bp, --benchmark-percent  压测沙箱百分比（固定模式，如0.5=50%）

  # 轮转模式控制
  -bm, --benchmark-mode     压测模式：'fixed'（默认）或 'round_robin'
  -rc, --round-count        最大轮次数（终止条件）
  -rs, --round-size         每轮沙箱数（默认5，决定组数）
  -ri, --round-interval     轮次间隔秒数（默认5）

  # 测试运行
  --duration                测试持续时间秒数
  --stats-interval          统计快照间隔

  # 报告
  -o, --output-dir          报告输出目录
  --filename-prefix         报告文件名前缀
```

### 批量模式CLI

```bash
python -m e2b_bench --batch --help

选项：
  --matrix                  测试矩阵YAML配置路径（在线模式必填）
  --offline                 从已有结果生成摘要
  --result-dir              结果目录路径（离线模式必填）
  --output                  输出Excel路径
  --continue-on-failure     组失败时继续测试
```

## 测试流程

### 单次测试（固定模式）

```text
阶段1：创建/检测沙箱
    ├── [完整/仅创建] 调用sandbox.create() API（带NUMA绑定）
    ├── [检测] 查询已有或从sandbox_ids_file加载
    ├── 记录create_elapsed时间
    └── 启动端口检查（18789 + 11436）

阶段2：端口检查
    ├── 检查18789 (openclaw-gateway)
    ├── 检查11436 (llama-server)
    ├── 记录port_wait_elapsed时间
    └── 两个端口都就绪后标记PORT_READY

[仅创建模式：在此退出]

阶段3：预热阶段（可选）
    ├── [有warmup_urls] 通过agent-browser打开标签页
    ├── 每个标签页执行snapshot → click → screenshot
    └── 标记warmup_done完成

[仅预热模式：在此退出]

阶段4：启动浏览器任务
    ├── [固定模式] 按benchmark_percent选择子集
    ├── [有task_batch] 分批启动任务
    └── [无配置] 全并发启动

阶段5：运行测试
    └── 持续test_duration秒收集实时统计

阶段6：停止并生成报告
    ├── [创建的沙箱] 关闭所有沙箱
    └── 生成性能报告
```

### 单次测试（轮转模式）

```text
阶段1-3：同固定模式

阶段4：轮转压测
    ├── 按round_size将沙箱分组
    ├── 对于每一轮：
    │   ├── 选择组（带循环）
    │   ├── 每个沙箱创建TabOperationRunner
    │   ├── 执行：open_tab → page_load → snapshot → click → screenshot
    │   ├── 等待所有runner完成
    │   ├── 记录下一轮的基线
    │   ├── 打印轮次摘要（步骤计时分解）
    │   └── 等待round_interval秒
    └── 当round_count或duration达到时停止

阶段5：停止并生成报告
    ├── 生成含轮次对比表格的报告
    └── 生成含尾部分析的步骤级计时表格
```

### 批量测试

```text
阶段1：加载矩阵配置并生成任务组

阶段2：对于每个组：
    ├── 创建共享沙箱
    ├── 启动smap_tool（如启用）
    ├── 预热（如有warmup_urls）
    └── 对于每个任务（不同benchmark_percent）：
        ├── 启动vm_monitor（带stress-file）
        ├── 创建stress文件（开始采样）
        ├── 运行压测
        ├── 删除stress文件（停止采样）
        ├── 等待analysis_report.xlsx
        └── 保存bench_report.txt

阶段3：清理组（停止smap_tool，关闭沙箱）

阶段4：从所有结果提取指标

阶段5：生成聚合Excel摘要
```

## 删除沙箱

### 使用 delete_sandbox.sh

删除所有运行的沙箱：

```bash
# 设置环境
cp e2b_bench/.env.example e2b_bench/.env
# 编辑.env填入你的E2B_API_URL和E2B_API_KEY

# 运行删除脚本
cd e2b_bench
./delete_sandbox.sh

# 或指定自定义env文件
./delete_sandbox.sh path/to/.env
```

### 环境文件 (.env)

```bash
E2B_API_URL=http://141.61.17.196:3000
E2B_API_KEY=e2b_d8ced731a9db82628c1e7279bec5ca70d6f74a6f
```

## 常见问题

| 错误 | 可能原因 | 解决方案 |
|-----|---------|---------|
| `Response 400` | 模板名称无效 | 检查E2B模板是否存在 |
| `GatewayClient error` | Gateway服务未启动 | 检查18789端口状态 |
| `Port check failed` | 端口检查超时 | 增加PORT_CHECK_MAX_WAIT |
| `Command exit_code=1` | 命令语法错误 | 检查openclaw/agent-browser版本 |
| `SandboxPaginator error` | 迭代方式错误 | 使用paginator.has_next和next_items() |
| `open_tab failed` | 标签页创建超时 | 检查E2B API连接 |
| `page_load failed` | 页面加载超时 | 增加超时或检查URL |
| `D-Bus connection error` | D-Bus不可用 | 检查沙箱D-Bus守护进程 |
| `Gateway connection error` | CDP不可达 | 检查openclaw-gateway状态 |

## 沙箱状态流转

```text
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
- [指标参考](metrics-reference.md) - 50+指标说明
- [vm_monitor使用指南](usage-guide.md) - vm_monitor工具配置
