# vm_monitor 使用指南

`vm_monitor` 是 Agent VM Bench 的宿主机级资源监控工具。它采样 VM/VMM 进程及宿主机 `/proc`、`/sys` 计数器，并可选地并行驱动外部性能采集工具（devkit、ksys、ub_watch、smap_bw、getfre）。输出：CSV、深色主题 SVG 时序曲线，以及汇总的 `resource_report.xlsx`。

- 入口：`vm-monitor`（`vm_monitor.cli:main`），由 `pip install -e .` 注册
- 模块直调：`python3 -m vm_monitor`（legacy 布局下 `python3 vm_monitor.py`）
- 需要 **Linux**（读取 `/proc`、`/sys/devices/system/node`、`/sys/block`）；**以 root 运行**才能读取其他进程的内存映射

## 前置条件

```bash
# 强烈建议 root —— 读取其他进程的 numastat / numa_maps 需要
sudo -i

# 安装（注册 vm-monitor 命令行 + 拉取核心依赖）
python -m pip install -e .
```

`vm_monitor` 本身依赖 `psutil`、`pandas`、`openpyxl`、`PyYAML`、`python-dotenv`（在 `pyproject.toml` 中声明）。外部采集工具（devkit、ksys 等）**不是** pip 包——它们是厂商/内核工具，需自行安装并通过 `.env` 指定路径。

## 运行模式

三种模式（在时序/同步轴上互斥）：

### 模式 1 —— 压测同步监控（等待 benchmark 起来后再采样）

被 `bench-core` 自动 vm-monitor 和批量调度器使用。vm_monitor 空转等待标记出现，然后采样 `-t` 秒。

```bash
# 文件锁（bench-core 默认的同步机制）
sudo vm-monitor --stress-file /tmp/vm_benchmark_running.lock --vmm firecracker -t 300 -i 2

# 或按进程名检测压测进程
sudo vm-monitor --stress-process auto_vm_test --vmm qemu -t 300 -i 2
```

### 模式 2 —— 计时监控（立即采样 N 秒）

```bash
sudo vm-monitor -t 60 -i 2 --vmm qemu
```

### 模式 3 —— 计时 + 并行日志采集（`--enable-capture`）

将 devkit/ksys/ub_watch/smap_bw/getfre 作为并行子进程与监控一起运行：

```bash
sudo vm-monitor -t 300 -i 2 --vmm firecracker --enable-capture --auto-skip \
    --numa 2,3 --log-dir /data/test_run_1
```

`--auto-skip` 让缺失的工具被静默禁用（无提示）——适用于自动化运行。交互使用时去掉 `--auto-skip`，vm_monitor 会逐个提示输入缺失的 `.env` 路径。

## 命令行参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--vmm` | `qemu` | VMM 类型：`qemu` 或 `firecracker` |
| `-t, --time` | `60` | 采样时长（秒） |
| `-i, --interval` | `2` | 采样间隔（秒） |
| `--stress-process` | — | 等待该进程名出现后开始采样 |
| `--stress-file` | — | 等待该锁文件出现后开始采样 |
| `-o, --output` | — | 输出文件名前缀（默认 `{qemu\|firecracker}_monitor`） |
| `--log-dir` | `logs_<ts>/` | 输出目录 |
| `--numa` | `all` | 关注的 NUMA 节点：`all` 或 `0,1` |
| `--remote-numa` | `5` | 跨 socket"远程借阅"节点，用于空闲内存监控；负值禁用 |
| `--disks` | `all` | 监控 I/O 的块设备：`all`（自动发现物理设备）或 `sda,nvme0n1` |
| `--enable-capture` | off | 并行运行 devkit/ksys/ub_watch/smap_bw/getfre |
| `--auto-skip` | off | 静默禁用缺失的采集工具（非交互） |
| `--ksys-parse-timeout` | `600` | ksys 解析阶段超时（秒） |
| `--no-svg` | off | 跳过 SVG 时序曲线 |
| `--no-charts` | off | 跳过 xlsx 图表阶段（表仍写入；超大运行更快） |
| `--no-{collector}` | off | 跳过某个 `/proc` 采集器或某个 devkit 子工具（见下） |

## VMM 类型

| `--vmm` | 匹配的进程名 | 用途 |
|---------|-------------|------|
| `qemu` | `qemu-kvm`, `qemu-system` | QEMU/KVM 虚拟机（OpenStack） |
| `firecracker` | `firecracker` | Firecracker microVM（E2B / AENV 沙箱） |

每个子类继承 `VMMonitorBase`，提供 `get_process_names()` / `extract_vm_id()` / `get_vms_realtime()`。新增 VMM 时继承 `VMMonitorBase` 并在 `vm_monitor/__init__.py` + `vm_monitor/cli.py` 中注册。

## `.env` 配置

由 `vm_monitor/config.py::load_env_config` 加载。在启动 `vm-monitor` 的 cwd 下放置 `.env`：

```env
# DevKit CLI（devkit_top_down 与 devkit_mem 共用）
DEVKIT_PATH=/path/to/devkit

# ksys —— 两者均必填，任一缺失则 ksys 被跳过
KSYS_PATH=/path/to/ksys
KSYS_CONFIG_PATH=/path/to/ksys_config.yaml

# ub_watch
UB_WATCH_PATH=/path/to/ub_watch

# smap_bw（经 sudo 运行以访问 dmesg）
SMAP_BW_PATH=/path/to/smap_bw.py

# getfre 及其 YAML 配置
GETFRE_PATH=/path/to/getfre
GETFRE_CONFIG_PATH=/path/to/getfre_config.yaml

# DevKit top-down 的 CPU 核范围（可选；缺省由 --numa 的 sysfs cpulist 自动计算）
DEVKIT_CPU_RANGE=96-191
```

### key → 工具映射

| `.env` key | 驱动 | 必需用于 | 备注 |
|------------|------|----------|------|
| `DEVKIT_PATH` | `devkit_top_down`, `devkit_mem` | DevKit sheets | 共用二进制；用 `--no-devkit-*` 拆分 |
| `DEVKIT_CPU_RANGE` | `devkit_top_down` | top-down CPU 范围 | 可选；由 `--numa` 自动算 |
| `KSYS_PATH` + `KSYS_CONFIG_PATH` | `ksys` | KSys sheet | **两者均必需** |
| `UB_WATCH_PATH` | `ub_watch` | UBWatch sheets | 可选 |
| `SMAP_BW_PATH` | `smap_bw` | SMAPBW sheets | 可选；sudo |
| `GETFRE_PATH` + `GETFRE_CONFIG_PATH` | `getfre` | Getfre sheets | 可选；配置可从宿主机拓扑自动填充 |

缺失路径的行为：
- **`--auto-skip` / bench-core 自动 vm-monitor**：该工具被静默禁用，打印一条 `[WARN]`，运行继续。缺失工具的 sheet 在 `resource_report.xlsx` 中直接不存在——绝不会硬失败。
- **交互模式（无 `--auto-skip`）**：vm_monitor 逐个提示输入缺失路径，`skip` 禁用，有效路径经 `python-dotenv` 写回 `.env`。

### getfre 配置（`getfre_config.yaml`）

仓库内置模板 [config/tools/getfre_config.yaml](../../config/tools/getfre_config.yaml)：

```yaml
getfre_path: /path/to/getfre    # 可执行文件（也可由 .env 的 GETFRE_PATH 覆盖）
total_cores: 192                # 物理核数（省略则由 sysfs 拓扑自动探测）
interval: 2                     # 采样间隔（秒）
core_interval: 1                # 1=每个物理核，2=隔一个
numa_nodes: [0, 1]              # 省略则自动发现
```

`ksys_config.yaml` 与 devkit/ub_watch 二进制是厂商工具——其配置格式由这些工具自身定义，不在本仓库。将 `KSYS_CONFIG_PATH` 指向你的 ksys 发行版所附带的配置即可。

## 日志采集工具 —— 精确调用

`LogCapture`（[vm_monitor/log_capture.py](../../vm_monitor/log_capture.py)）将每个工具作为子进程启动（stdout+stderr → 各自的日志文件）。时长 `-d` 与间隔 `-i` 镜像 vm_monitor 的 `-t` / `-i`。

| 工具 | vm_monitor 运行的命令 | 输出日志 | xlsx sheet |
|------|---------------------|----------|------------|
| **devkit_top_down** | `$DEVKIT_PATH tuner top-down -d <t> -i 3 -c <cpu_range>` | `devkit_top_down.log` | DevKit_TopDown（13 指标） |
| **devkit_mem** | `$DEVKIT_PATH tuner memory -d <t> -i 3` | `devkit_mem.log` | DevKit_Memory, NUMA_Bandwidth |
| **ksys** | `$KSYS_PATH collect -d <t> -i 3 -c $KSYS_CONFIG_PATH` | `ksys.log` | KSys（11 指标） |
| **ub_watch** | `$UB_WATCH_PATH -t <t> -i 3` | `ub_watch.log` | UBWatch_Latency, UBWatch_Bandwidth |
| **smap_bw** | `sudo python3 $SMAP_BW_PATH --clear --duration <t> --timeout <t+10>` | `smap_bw.log` | SMAPBW_Summary, SMAPBW_Cycles |
| **getfre** | 按 NUMA 节点线程化：每核 `$GETFRE_PATH <total_cores> <core_id>` | `getfre_NUMA{N}.log` | Getfre_Summary, Getfre_NUMA{N}, Getfre_Timeline_NUMA{N} |

`<cpu_range>` = `DEVKIT_CPU_RANGE`，或由 `--numa` 经 `/sys/devices/system/node/node{N}/cpulist` 自动计算。两者都无解时 devkit_top_down 被跳过（绝不以错误范围启动）。

### ksys 两阶段生命周期

ksys 是采集后还有长尾的唯一工具：

1. **采集阶段** —— 运行 `-d <duration>` 秒。
2. **解析阶段** —— 解析累计数据；大样本时可能耗时数分钟。

vm_monitor 通过扫描 `ksys.log` 标记来跟踪解析进度：

| `ksys.log` 中的标记 | 状态 |
|---------------------|------|
| `Starting to collect data` | 采集中 |
| `Starting to parse data` | 解析中 |
| `Starting to process and print data` / `CPU Metrics` / `Data saved successfully` | 已完成 |

若解析超过 `--ksys-parse-timeout`（默认 600 秒），ksys 被强制终止并打印告警及建议：增大 `--ksys-parse-timeout`、检查 `ksys.log` 大小、降低采样间隔。进度每 30 秒记录一次，并在 50/75/90% 阈值告警。其他工具均用简单的 `duration + 60s` 超时。

## 选择性采集器（`--no-X`）

所有采集器默认**开启**。禁用某个会使其历史为空，对应的 sheet/SVG 被省略——优雅降级，下游无需额外判断。

`/proc` 采集器（target `base`）：

| 参数 | 跳过 | 受影响 sheet |
|------|------|--------------|
| `--no-hugepage` | 大页统计 | Summary 中的 Hugepage 行 |
| `--no-numa-cpu` | 每 NUMA CPU% | NUMA CPU 行 |
| `--no-host-stats` | 宿主 CPU/内存 | Summary 中的 Host 行 |
| `--no-swap` | swap 用量 + swap-in/out | Swap_Timeline |
| `--no-host-mem-detail` | cached/dirty/writeback | Host_Mem_Timeline |
| `--no-pressure` | 页缓存压力 + iowait | Host_Pressure_Timeline |
| `--no-numa-memory` | 每 NUMA meminfo | NUMA_Memory_Timeline |
| `--no-vm-total` | 聚合 VM 内存 | VM_Total_Memory_Timeline |
| `--no-disk` | 每设备磁盘 I/O（1s 子采样） | Disk_IO_Timeline |
| `--no-ublk` | ublk 设备数 | Disk_IO_Timeline ublk 列 |

devkit 拆分（target `devkit` —— 两个子工具共用 `DEVKIT_PATH`）：

| 参数 | 跳过 |
|------|------|
| `--no-devkit-mem` | devkit tuner memory |
| `--no-devkit-topdown` | devkit tuner top-down |

`bench-core` 将 `monitor.skip` 的 YAML token 透传为 `--no-{stem}`。

## 输出

```
logs_<timestamp>/
├── qemu_monitor.csv         # 原始每采样 VM 记录（或 firecracker_monitor.csv）
├── summary.csv              # 峰值/均值汇总
├── resource_report.xlsx     # 汇总 Excel（下列所有 sheet）
├── disk_io.svg              # 深色主题 SVG 时序曲线
├── host_resources.svg
├── swap.svg
├── numa.svg
├── vm_total.svg
├── devkit_top_down.log      # 原始工具日志（带 --enable-capture）
├── devkit_mem.log
├── ksys.log
├── ub_watch.log
├── smap_bw.log
└── getfre_NUMA{N}.log
```

`resource_report.xlsx` 的 sheet：`Summary`、`NUMA_Overview`、`VM_Stats`、`DevKit_TopDown`、`DevKit_Memory`、`NUMA_Bandwidth`、`KSys`、`UBWatch_Latency`、`UBWatch_Bandwidth`、`SMAPBW_Summary`、`SMAPBW_Cycles`、`Getfre_Summary`、`Getfre_NUMA{N}`、`Getfre_Timeline_NUMA{N}`、`Raw_VM_Data`、`Swap_Timeline`、`NUMA_Memory_Timeline`、`VM_Total_Memory_Timeline`、`Disk_IO_Timeline`、`Host_Mem_Timeline`、`Host_Pressure_Timeline`、`Host_CPU_Timeline`。

> xlsx 被**最后**写入是有意为之——`bench-core` 的 `MonitorController` 以 `resource_report.xlsx` 作为"全部产物已写入"的信号，在其出现的瞬间收割子进程。SVG 先于 xlsx 运行，以免被该收割切断。

## Python API

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

# QEMU
mon = QEMUMonitor()
mon.target_numa_nodes = [0, 1]
mon.start_monitoring(duration_seconds=60, interval_seconds=3)
mon.analyze_and_export("qemu.csv", "summary.csv")

# Firecracker
fc = FirecrackerMonitor()
fc.start_monitoring(duration_seconds=60, interval_seconds=3)

# 自定义 VMM
class MyMonitor(VMMonitorBase):
    def get_process_names(self): return ("my-vmm",)
    def extract_vm_id(self, pid, cmdline): return f"myvm-{pid}"
    def get_monitor_title(self): return "My VMM Monitoring"
    def get_no_vm_message(self): return "No running instances"
    def get_csv_filename_prefix(self): return "my_vmm"
    def get_vms_realtime(self): ...
```

## bench-core 集成（自动 vm-monitor）

`bench-core` 通过 `bench_core.monitor.MonitorController` 包装 vm_monitor。当选中的 provider 声明了 `vmm_type`（e2b/aenv → firecracker；docker/fake → 跳过）时**自动开启**。监控以本地子进程运行，通过同步 stress-file 包裹活跃压测阶段，输出到 `<report.output_dir>/vm_monitor/`。

相关 `monitor:` YAML 块（与 `report:` 同级）：

```yaml
monitor:
  enabled: auto          # auto | true | false（auto = 当 provider 有 vmm_type 时开）
  merge_report: false    # false = vm_monitor 的 resource_report.xlsx 独立成文
                          # true  = 将宿主 sheets 拷入 replay 的 obs workbook
  skip: []               # 透传为 --no-{stem}：["disk", "devkit-topdown", ...]
  skip_charts: false     # 透传为 --no-charts
```

CLI 的 `--no-vm-monitor` 会短路关闭。缺失二进制/工具或锁目录不可写时降级为告警 + 跳过——绝不影响 bench 本身。

## 故障排查

| 现象 | 原因 / 修复 |
|------|-------------|
| `[WARN] Recommended to run as root` | 用 `sudo` 重跑；读取其他进程的 numastat/numa_maps 需要 root |
| `ksys parse timeout` | 增大 `--ksys-parse-timeout`；降低 `-i`；检查 `ksys.log` 大小 |
| devkit_top_down 被跳过 | `DEVKIT_CPU_RANGE` 未设且 sysfs cpulist 不可读——在 `.env` 中设置 `DEVKIT_CPU_RANGE` |
| xlsx 缺少某个 sheet | 其采集器被禁用（`--no-X`）或工具路径未配（`--auto-skip` 禁用了它） |
| 未检测到 VM | `--vmm` 选错，或 VM 进程尚未启动（压测同步模式仍在等待） |
| 磁盘 I/O 全为 0 | `--disks` 过滤掉了物理设备，或窗口期内确无 I/O |

完整指标定义（每个 sheet 的列 + `/proc`/`/sys` 来源与公式）见 [Metrics Reference](metrics-reference.md)。
