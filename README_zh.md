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
| [E2B Bench 使用指南](docs/e2b-batch-usage.md) | E2B沙箱批量性能测试 |
| [E2B Bench 使用指南 (英文)](docs/e2b-batch-usage-en.md) | E2B Sandbox batch testing guide |

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

### 5. 创建 VM

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
python3 batch_test_scheduler.py --config config/batch_config.yaml --dry-run

# 执行批量测试
python3 batch_test_scheduler.py --config config/batch_config.yaml

# 离线汇总（从已有结果）
python3 batch_test_scheduler.py --offline --result-dir results
```

### 单次测试

```bash
python3 auto_vm_test.py --config config/test_config.yaml
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