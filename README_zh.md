# Agent VM Bench - OpenStack VM 内存超分配性能测试

[English Documentation](README.md)

本目录包含用于测试 OpenStack VM 内存超分配场景下性能的工具。

## 文件说明

- `create_server.py` - 通过 OpenStack 创建 VM，用于运行 openclaw agent 负载
- `qemu_monitor.py` - 监控 QEMU 进程资源使用，支持日志采集和 Excel 报告导出
- `stress_tool.cpp` - VM 内存/CPU 消耗压测工具
- `vm_bench_lite.py` - 基准测试脚本（支持浏览器和 QA 模式，测试 openclaw agent 性能）
- `download_page.sh` - 下载 Wikipedia 预热页面
- `requirements.txt` - Python 依赖

## 依赖安装

安装必要的 Python 包：

```bash
pip install -r requirements.txt
```

核心依赖：

- `psutil` - 系统监控
- `paramiko` - SSH 客户端
- `flask` - Web 框架

可选依赖（用于 Excel 导出和图表）：

- `pandas` - 数据分析
- `openpyxl` - Excel 文件生成
- `python-dotenv` - .env 文件支持

## 快速开始

### 1. 终端环境设置（首先执行）

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. 配置宿主机网桥

为了让 VM 能够访问宿主机上的网页，需要在 OpenStack 网桥接口上添加 IP 地址：

```bash
# 查找网桥接口名称
ip a | grep brq
```

示例输出：

```text
10: brqb3fa561d-67: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue state UP group default qlen 1000
11: tap8eee944d-02@if2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UP group default qlen 1000
12: vxlan-667: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
13: tap5cbb0361-f6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
14: tapafbc3810-87: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
```

为网桥接口添加 IP 地址（使用上面输出的网桥名称）：

```bash
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

配置完成后，VM 可以通过 `http://192.168.110.10:8080/Weibo.html` 访问宿主机上的静态网页。

### 3. 下载预热页面

运行此脚本下载 Wikipedia 页面和图片用于浏览器预热：

```bash
bash download_page.sh
```

这将创建 `web_content` 目录，包含：

- `web_content/en.wikipedia.org/wiki/` - HTML 页面
- `web_content/upload.wikimedia.org/` - 图片

### 4. 启动预热 Web 服务器

启动本地 HTTP 服务器来提供预热页面：

```bash
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

服务器运行在端口 8080，访问地址为 `http://<host_ip>:8080/<page>.html`

可用的预热页面：

- China.html
- World_War_II.html
- United_States.html
- Hubble_Space_Telescope.html
- Solar_System.html
- Earth.html
- Human.html
- List_of_paintings_by_Vincent_van_Gogh.html
- Galaxy.html
- Weibo.html

### 5. 创建 VM（运行 openclaw agent）

通过 OpenStack 创建用于运行 openclaw agent 负载的 VM：

```bash
python3 create_server.py \
  --start_ip 192.168.110.11 \
  --n 10 \
  --subnet-prefix 192.168.110. \
  --network-id cc56708a-c0c0-4d75-a87e-ed1b1a8af844 \
  --az nova_zone:controller \
  --flavor 2U_4G_30G_4K \
  --image ubuntu-24.04
```

### 6. 资源监控

创建 VM 后，等待 QEMU 进程 CPU 使用率稳定在约 1% 后再开始基准测试：

```bash
# 基本监控（默认 60 秒）
python3 qemu_monitor.py -t 300 -i 2

# 启用日志采集（采集 devkit、ksys、ub_watch 日志）
python3 qemu_monitor.py -t 300 -i 2 --enable-capture

# 指定日志输出目录
python3 qemu_monitor.py -t 300 --enable-capture --log-dir /data/test_run_1

# 指定监控的 NUMA 节点
python3 qemu_monitor.py -t 300 --enable-capture --numa 0,1
```

#### 日志采集配置

首次使用 `--enable-capture` 前，需要在 `.env` 文件中配置采集工具路径：

```env
# DevKit 采集工具路径
DEVKIT_PATH=/path/to/devkit

# ksys 采集工具路径和配置文件
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/config.yaml

# ub_watch 采集工具路径
UB_WATCH_PATH=/path/to/ub_watch

# DevKit top-down CPU 核心范围（可选，未配置时根据 -numa 自动计算）
DEVKIT_CPU_RANGE=96-191
```

如果 `.env` 文件不存在或路径无效，工具会交互式提示用户输入路径。

#### 输出文件

启用日志采集后，输出目录包含以下文件：

```text
logs_20240601_143052/
├── qemu_monitor.csv          # VM 原始数据
├── summary.csv               # 统计摘要
├── analysis_report.xlsx      # 综合分析报告（含图表）
├── devkit_mem.log            # DevKit 内存调优输出
├── devkit_top_down.log       # DevKit top-down 分析输出
├── ksys.log                  # ksys 采集输出
├── ub_watch.log              # ub_watch 输出
└── *_report.json             # ksys 生成的报告
```

#### Excel 报告内容

`analysis_report.xlsx` 包含多个工作表：

- **Summary** - 测试概览（主机 CPU/内存、大页、Swap、VM 统计）
- **NUMA_CPU** - 各 NUMA 节点 CPU 统计
- **NUMA_Memory** - 各 NUMA 节点内存统计
- **Hugepage_Per_NUMA** - 各 NUMA 节点大页统计
- **VM_Stats** - 各 VM 统计信息
- **DevKit_TopDown** - CPU top-down 分析（含饼图）
- **TopDown_Timeline** - Top-down 指标时间序列（含折线图）
- **DevKit_Memory** - Cache Miss 和 DDR 带宽（含柱状图）
- **Memory_Timeline** - 内存指标时间序列（含折线图）
- **NUMA_Bandwidth** - 各 NUMA 节点带宽统计
- **KSys** - Miss Latency 和 IPC 数据
- **UBWatch_Latency/Bandwidth** - NUMA 互联延迟和带宽
- **Raw_VM_Data** - VM 时间序列原始数据

### 7. 运行基准测试（两阶段执行）

浏览器模式采用两阶段执行：**预热阶段**（所有 VM）然后 **压测阶段**（部分 VM）。

#### 阶段 1：预热阶段 (`-wp`)

预热阶段连接所有 VM 执行预热任务（访问预热页面以加载浏览器内存），完成后退出：

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
    --batch-size 20 --batch-interval 5 \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-url "http://192.168.110.10:8080/Galaxy.html" \
    --warmup-url "http://192.168.110.10:8080/Hubble_Space_Telescope.html" \
    --warmup-url "http://192.168.110.10:8080/Human.html" \
    --warmup-url "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html" \
    --warmup-url "http://192.168.110.10:8080/Solar_System.html" \
    --warmup-url "http://192.168.110.10:8080/United_States.html" \
    --warmup-url "http://192.168.110.10:8080/World_War_II.html" \
    --warmup-loops 1 \
    --warmup-delay 2
```

#### 阶段 2：压测阶段 (`-bsp`)

压测阶段只连接部分 VM（由 `-bsp` 参数控制）执行浏览器测试：

```bash
# 连接 50% 的 VM（100 个 VM 中连接 50 个）进行压测
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

#### 参数说明

| 参数 | 说明 |
| ---- | ---- |
| `-wp` / `--warmup-phase` | 仅运行预热阶段（所有 VM 执行预热任务后退出） |
| `-bsp` / `--browser-stress-percent` | 压测阶段连接的 VM 百分比（默认 100%） |
| `--warmup-url` | 预热页面 URL（可多次指定） |
| `--warmup-loops` | 预热循环次数（默认 1） |
| `--warmup-delay` | 预热页面间隔秒数（默认 2） |

**注意：** 在不使用 LLM 的浏览器模式下（`--browser-mode` 但未指定 `--browser-use-llm`），基准测试会在每次请求的延迟上额外增加 10 秒，以模拟 LLM 响应延迟。这样可以获得与真实 Agent 工作流相当的延时数据。

### 8. 删除 VM

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all  # 检查删除是否完成
```

---

## 自动化批量测试

自动化测试系统支持多组参数组合的批量测试，无需人工干预。

### 系统概述

自动化系统由以下组件构成：

| 文件 | 说明 |
| ---- | ---- |
| `auto_vm_test.py` | 核心自动化脚本 - 执行单次完整测试流程 |
| `batch_test_scheduler.py` | 批量调度器 - 协调多组参数的批量测试 |
| `test_config_template.yaml` | 配置模板 - 包含动态参数占位符 |
| `batch_config.yaml` | 批量配置 - 定义测试参数矩阵 |

### 测试流程

每次自动化测试执行以下步骤：

1. **删除已存在的 VM** → 通过 virsh 确认删除完成
2. **创建新 VM（n 个）** → 调用 create_server.py
3. **启动 smap_tool**（内存迁移工具）
4. **等待 VM 就绪** → SSH 连接、openclaw gateway 服务、CPU < 5%
5. **预热阶段** → 所有 VM 执行浏览器预热
6. **启动监控** → qemu_monitor.py 配合 stress-file 同步机制
7. **压测阶段** → 指定百分比的 VM 执行浏览器测试
8. **收集结果** → 等待 Excel 报告生成完成
9. **清理环境** → 停止 smap_tool、删除 VM

### 快速开始

#### 前置条件

1. 手动创建大页内存（如 200GB）
2. 启动预热 Web 服务器（见第 4 节）
3. 配置 `.env` 文件设置日志采集工具路径

#### 运行批量测试

```bash
# 预览任务列表（不执行）
python3 batch_test_scheduler.py --config batch_config.yaml --dry-run

# 执行批量测试
python3 batch_test_scheduler.py --config batch_config.yaml
```

### 配置说明

#### 批量配置 (`batch_config.yaml`)

定义测试参数矩阵：

```yaml
# 测试参数矩阵 - 每个组合生成一个测试任务
test_matrix:
  vm_counts: [50, 100]           # 测试的 VM 数量
  ratios: [0.10, 0.15, 0.20]     # 内存借用比例（10%、15%、20%）
  active_percentages: [0.5, 0.8] # 基准测试活跃 VM 百分比

# 固定参数（应用于所有测试）
fixed_params:
  start_ip: "192.168.110.11"     # 起始 IP 地址
  swap_size_gb: 200              # 大页大小（GB）
  duration: 160                  # 测试持续时间（秒）

# 调度配置
scheduler:
  continue_on_failure: true      # 测试失败后继续执行

# 结果配置
result:
  template_path: "test_config_template.yaml"
  base_dir: "results"
```

#### 配置模板 (`test_config_template.yaml`)

包含所有测试参数和动态占位符：

| 占位符 | 说明 | 示例 |
| ------ | ---- | ---- |
| `{{VM_COUNT}}` | VM 数量 | 100 |
| `{{START_IP}}` | 起始 IP | "192.168.110.11" |
| `{{SWAP_SIZE_GB}}` | 大页大小（GB） | 200 |
| `{{RATIO}}` | 内存借用比例 | 0.15 |
| `{{ACTIVE_PERCENT}}` | 活跃 VM 百分比 | 0.5 |
| `{{DURATION}}` | 测试持续时间（秒） | 160 |

### 结果组织结构

每次测试创建独立的结果目录：

```text
results/
├── batch_summary_20260602_143052.xlsx    # 批量测试汇总报告
├── batch_log_20260602_143052.txt         # 批量执行日志
├── temp_configs/                         # 临时配置文件
│   ├── config_vm50_ratio0.10_active0.5.yaml
│   └── ...
│
├── vm50_ratio0.10_active0.5_20260602_143052/  # 单次测试结果
│   ├── config.yaml                       # 测试配置
│   ├── test_log.txt                      # 测试执行日志
│   │
│   ├── vm_bench_lite/                    # 基准测试输出
│   │   ├── bench_report_xxx.txt          # 基准测试报告
│   │   └── warmup_summary_xxx.txt        # 预热摘要
│   │
│   ├── qemu_monitor/                     # 监控输出
│   │   ├── qemu_monitor.csv              # 监控原始数据
│   │   ├── summary.csv                   # 统计摘要
│   │   ├── analysis_report.xlsx          # Excel 报告（含图表）
│   │   ├── devkit_mem.log                # DevKit 内存日志
│   │   ├── devkit_top_down.log           # DevKit top-down 日志
│   │   ├── ksys.log                      # ksys 日志
│   │   ├── ub_watch.log                  # ub_watch 日志
│   │   └── monitor_stdout.log            # 监控标准输出
│   │
│   └── summary/                          # 分析摘要
│       └── metrics_summary.json          # 关键指标 JSON
│
└── ... (其他测试结果)
```

### 监控与压测同步机制

系统采用锁文件机制确保监控与压测时间精确对齐：

1. **监控启动** → 等待 `/tmp/vm_benchmark_running.lock` 出现
2. **压测开始** → 创建锁文件 → 监控开始采样
3. **时间到期** → 监控自然停止 → 生成 Excel 报告
4. **清理** → 删除锁文件

这确保：
- 压测前无空闲采样
- 监控与压测时间精确对齐
- Excel 报告完整生成

### 高级用法

#### 单次测试执行

使用特定配置运行单次测试：

```bash
python3 auto_vm_test.py --config test_config.yaml
```

#### 自定义参数矩阵

修改 `batch_config.yaml` 定义自定义测试场景：

```yaml
test_matrix:
  vm_counts: [10, 20, 50, 100]
  ratios: [0.05, 0.10, 0.15, 0.20, 0.25]
  active_percentages: [0.3, 0.5, 0.7, 1.0]
```

这将生成 4 × 5 × 4 = 80 个测试组合。

#### 修改测试模板

编辑 `test_config_template.yaml` 可调整：
- 预热 URL 列表和参数
- 基准测试批次大小和间隔
- 监控 NUMA 节点和采样间隔
- 等待超时时间和阈值
