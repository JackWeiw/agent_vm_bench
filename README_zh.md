# Agent VM Bench - OpenStack VM 内存超配性能测试

[English](README.md)

OpenStack VM 内存超配场景下的性能测试框架，提供全面的性能监控。

## 文档导航

| 文档 | 说明 |
|------|------|
| [设计文档](docs/design.md) | 系统架构与流程设计 |
| [设计文档 (英文)](docs/design-en.md) | 英文版设计文档 |
| [指标参考](docs/metrics-reference.md) | 50+ 指标详细说明 |
| [使用指南](docs/usage-guide.md) | 详细工具使用与配置 |
| [bench-core 使用指南](docs/bench-core-usage-zh.md) | **src 内核压测（推荐）：安装→配置→CLI→清理** |
| [vm_bench 使用指南](docs/vm_bench-usage-guide-zh.md) | **模块化 vm_bench 包使用指南（推荐）** |
| [vm_bench Usage (EN)](docs/vm_bench-usage-guide.md) | vm_bench module usage guide |
| [E2B Bench 使用指南](docs/e2b-bench-usage-zh.md) | E2B 沙箱批量性能测试 |
| [E2B Bench 使用指南 (英文)](docs/e2b-batch-usage-en.md) | E2B Sandbox batch testing guide |
| [Docker Bench 使用指南](docs/docker-bench-usage-zh.md) | Docker 容器浏览器自动化性能测试 |
| [Docker Bench 使用指南（英文）](docs/docker-bench-usage.md)  | Docker container browser automation testing |


## 贡献与社区

- [贡献指南](CONTRIBUTING.md) — 开发环境搭建、测试与提交流程
- [行为准则](CODE_OF_CONDUCT.md) — 参与标准
- [获取帮助](SUPPORT.md) — 提问、反馈与建议渠道
- [RFC 流程](docs/rfcs/README.md) — 用于重大设计变更
- [Issue 模板](.github/ISSUE_TEMPLATE/) — Bug、Feature 与性能异常表单

## 依赖安装

```bash
pip install -r requirements.txt
```

核心依赖：`psutil`、`paramiko`、`flask`、`yaml`

可选依赖（Excel）：`pandas`、`openpyxl`

---

## 快速开始

### 1. 终端设置

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. 配置主机网桥

```bash
# 查找网桥接口
ip a | grep brq

# 添加 IP 到网桥
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

### 3. 下载预热页面

```bash
bash download_page.sh
```

### 4. 启动预热 Web 服务器

```bash
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

---

## bench-core（src 内核，主机无关）

`bench_core` + `env_provider` 是新的压测内核：通过 `EnvironmentProvider` 抽象驱动
e2b / docker / 未来的 kata / agentenv，一份压测配置在任一后端上跑同一套压力曲线。
与冻结的 legacy `e2b_bench/`、`docker_bench/` 并存，互不影响。

```bash
# editable 安装后无需 PYTHONPATH
python -m pip install -e .

# fake 烟测（无 SDK）
bench-core --provider fake --config config/common/browser.yaml --create-only -n 1

# 真实后端（e2b/docker，同一份配置）
bench-core --provider e2b    --config config/common/coding-ts.yaml --create-only -n 2
bench-core --provider docker --config config/common/browser.yaml   --create-only -n 2

# 清理（list + 销毁现有沙箱）
bench-core --provider e2b --config config/common/coding-ts.yaml --cleanup
```

阶段阶梯：`--create-only`（建，留）→ `--detect --warmup-only`（复用，预热）→
`--detect --test-duration 30`（短压测 + report）→ `--cleanup`（收尾）。

详见 [bench-core 使用指南](docs/bench-core-usage-zh.md)；架构原理见
[设计文档](docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md)。

---

## vm_bench 模块（模块化）

`vm_bench` 包是**推荐**的模块化方案，用于 VM 创建和压测，替代原有的 `create_server.py` 和 `vm_bench_lite.py`。

### 快速开始

```bash
# 安装依赖
pip install -r vm_bench/requirements.txt

# 仅创建 VM（阶段 0）
python -m vm_bench --config config/openstack/vm_bench.yaml --create-only

# 检测已有 VM 并压测
python -m vm_bench --config config/openstack/vm_bench.yaml --detect -bsp 0.5 -t 300

# 仅预热
python -m vm_bench --config config/openstack/vm_bench.yaml --warmup-only

# 完整流程
python -m vm_bench --config config/openstack/vm_bench.yaml
```

### Python API

```python
from vm_bench import Config, VMManager, run_benchmark

# 创建 VM
config = Config(total_count=20, start_ip="192.168.110.11", ...)
manager = VMManager(config, threading.Event())
vm_states = manager.create_all()

# 运行压测
result = run_benchmark(config)
print(result['report'])
```

### 文件说明

| 文件 | 说明 |
|------|------|
| `vm_bench/config.py` | 配置管理（YAML + CLI） |
| `vm_bench/vm_manager.py` | VM 生命周期（OpenStack + SSH） |
| `vm_bench/task_runner.py` | 任务执行（QA、Stress、Browser） |
| `vm_bench/bench.py` | 主编排入口 |
| `config/openstack/vm_bench.yaml` | 配置模板 |

详见 [vm_bench 使用指南](docs/vm_bench-usage-guide-zh.md)。

---

## 旧版脚本

### 6. 资源监控

```bash
# 基础监控（QEMU，默认）
python3 vm_monitor.py -t 300 -i 2

# Firecracker 监控
python3 vm_monitor.py --vmm firecracker -t 300 -i 2

# 带日志采集
python3 vm_monitor.py -t 300 -i 2 --enable-capture

# 自定义输出目录
python3 vm_monitor.py -t 300 --enable-capture --log-dir /data/test_run_1

# 指定 NUMA 节点
python3 vm_monitor.py -t 300 --enable-capture --numa 0,1

# 向后兼容（已废弃）
python3 qemu_monitor.py -t 300 -i 2
```

### 7. 运行压测

#### 预热阶段（所有 VM）

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
    --batch-size 20 --batch-interval 5 \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-loops 1 --warmup-delay 2
```

#### 压测阶段（部分 VM）

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

### 8. 删除 VM

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all
```

---

## 自动化批量测试

### 运行批量测试

```bash
# 预览任务
python3 batch_test_scheduler.py --config config/openstack/batch_config.yaml --dry-run

# 执行批量测试
python3 batch_test_scheduler.py --config config/openstack/batch_config.yaml

# 离线汇总（从已有结果）
python3 batch_test_scheduler.py --offline --result-dir results
```

### 单次测试

```bash
python3 auto_vm_test.py --config config/openstack/test_config_template.yaml
```

### 结果目录结构

```text
results/
├── batch_summary_*.xlsx           # 批量汇总（50+ 指标）
├── batch_log_*.txt                # 执行日志
│
└── vm{n}_ratio{r}_active{p}_*/    # 单次测试结果
    ├── config.yaml                # 测试配置
    ├── test_log.txt               # 执行日志
    ├── vm_bench_lite/             # 压测报告
    ├── qemu_monitor/              # 监控数据 + Excel
    └── summary/                   # 指标 JSON
```

---

## 文件说明

| 文件 | 说明 |
|------|------|
| `create_server.py` | 创建 OpenStack VM |
| `vm_monitor.py` | 监控 VM 资源（QEMU/Firecracker）+ 日志采集 |
| `qemu_monitor.py` | （已废弃）QEMU 监控旧入口 |
| `vm_bench_lite.py` | 浏览器/QA 压测 |
| `auto_vm_test.py` | 单次测试自动化 |
| `batch_test_scheduler.py` | 批量测试调度 |
| `stress_tool.cpp` | VM 压测工具 |
| `download_page.sh` | 下载预热页面 |

---

## vm_monitor 包

`vm_monitor` 包提供统一的监控框架，支持多种 VMM 类型：

| VMM 类型 | 进程名 | CLI 参数 |
|----------|--------|----------|
| QEMU | `qemu-kvm`, `qemu-system` | `--vmm qemu`（默认） |
| Firecracker | `firecracker` | `--vmm firecracker` |

**Python API：**

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

# QEMU 监控
qemu_monitor = QEMUMonitor()
qemu_monitor.start_monitoring(duration_seconds=60, interval_seconds=3)

# Firecracker 监控
fc_monitor = FirecrackerMonitor()
fc_monitor.start_monitoring(duration_seconds=60, interval_seconds=3)
```

## PDF/XLSX 文档场景

两个文档场景共用 `openclaw-document-v1` E2B Template，以及
`dockerfile_build/document/` 下的同一份镜像：

`config/e2b/pdf_bench.yaml` 和 `config/e2b/xlsx_bench.yaml` 中的 token、
API key 和 `http://localhost:3000` 是占位值。连接远程 E2B 服务器时，
可以修改本地 YAML，或使用下面的 CLI 覆盖方式（凭据不会显示在终端）：

```bash
read -rsp "E2B access token: " DOCUMENT_E2B_ACCESS_TOKEN
echo
read -rsp "E2B API key: " DOCUMENT_E2B_API_KEY
echo
read -rp "E2B API URL（例如 http://SERVER_IP:3000）: " DOCUMENT_E2B_API_URL

DOCUMENT_E2B_ARGS=(
  --e2b-access-token "${DOCUMENT_E2B_ACCESS_TOKEN}"
  --e2b-api-key "${DOCUMENT_E2B_API_KEY}"
  --e2b-api-url "${DOCUMENT_E2B_API_URL}"
  --e2b-http-ssl false
  --e2b-domain e2b.app
)

# 根据需要运行其中一个或两个场景
python -m e2b_bench -c config/e2b/pdf_bench.yaml "${DOCUMENT_E2B_ARGS[@]}"
python -m e2b_bench -c config/e2b/xlsx_bench.yaml "${DOCUMENT_E2B_ARGS[@]}"

unset DOCUMENT_E2B_ACCESS_TOKEN DOCUMENT_E2B_API_KEY DOCUMENT_E2B_API_URL DOCUMENT_E2B_ARGS
```
