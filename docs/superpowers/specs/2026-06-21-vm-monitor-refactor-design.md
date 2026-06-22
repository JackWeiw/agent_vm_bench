# VM Monitor 模块重构设计

## 背景

将现有的 `qemu_monitor` 模块重构为支持多种 VMM（虚拟机监视器）的通用监控架构，使其能够同时监控 QEMU 和 Firecracker 进程。Firecracker 是 E2B 沙箱使用的 microVM 后端，复用现有的监控逻辑可以快速实现对 E2B 资源的监控。

## 目标

1. 创建清晰的继承式架构，分离系统级监控和进程级监控
2. `QEMUMonitor` 和 `FirecrackerMonitor` 作为子类实现
3. 易于扩展未来其他 VMM 类型
4. 文件和变量命名逻辑清晰
5. 保持向后兼容（旧导入路径继续工作，有废弃警告）

---

## 目录结构

```
vm_monitor/                      # 新包名（重命名自 qemu_monitor）
    __init__.py                  # 包入口，导出所有公共类
    base.py                      # VMMonitorBase 抽象基类
    qemu.py                      # QEMUMonitor 子类
    firecracker.py               # FirecrackerMonitor 子类
    cli.py                       # CLI 入口，支持 --vmm 参数
    config.py                    # 配置管理（从 qemu_monitor 迁移）
    exporters.py                 # 导出工具（从 qemu_monitor 迁移）
    parsers.py                   # 日志解析（从 qemu_monitor 迁移）
    log_capture.py               # 日志采集（从 qemu_monitor 迁移）

qemu_monitor/                    # 向后兼容层（保留旧包名）
    __init__.py                  # 仅导出别名，指向 vm_monitor（含废弃警告）
    monitor.py                   # 废弃警告 + 别名指向 vm_monitor.qemu
```

---

## 类继承关系

```python
VMMonitorBase (base.py)          # 抽象基类
    │
    ├── QEMUMonitor (qemu.py)    # QEMU 实现
    │
    └── FirecrackerMonitor (firecracker.py)  # Firecracker 实现
```

---

## VMMonitorBase 基类职责

### 系统级监控方法（通用，保留在基类）

| 方法名 | 功能 |
|--------|------|
| `get_numa_nodes_memory()` | NUMA 内存统计 |
| `collect_hugepage_stats()` | 大页统计 |
| `collect_hugepage_per_numa_stats()` | 每 NUMA 大页统计 |
| `collect_numa_cpu()` | NUMA CPU 使用率 |
| `collect_host_stats()` | 主机 CPU/内存统计 |
| `collect_swap_stats()` | Swap 使用统计 |
| `get_vm_memory_from_numastat(pid)` | 进程内存统计（通过 numastat） |
| `get_available_numa_nodes()` | 获取可用 NUMA 节点 |
| `print_numa_real_time()` | NUMA 内存实时显示 |
| `print_final_numa_stats()` | NUMA 内存汇总显示 |

### 基础设施方法（通用）

| 方法名 | 功能 |
|--------|------|
| `collect_sample()` | 采样框架，调用子类 `get_vms_realtime()` |
| `display_realtime_table()` | 实时显示框架 |
| `start_monitoring()` | 定时监控循环 |
| `wait_for_stress_and_monitor()` | 压力同步监控循环 |
| `export_raw_csv()` | 原始数据 CSV 导出 |
| `export_summary_csv()` | 汇总数据 CSV 导出 |
| `calculate_vm_stats()` | VM 统计计算 |
| `calculate_overall_stats()` | 总体统计计算 |
| `print_summary_report()` | 汇报报告打印 |
| `analyze_and_export()` | 分析和导出完整流程 |
| `check_stress_process()` | 进程型压力检测 |
| `check_stress_file()` | 文件型压力检测 |

### 抽象方法（子类必须实现）

```python
@abstractmethod
def get_vms_realtime(self) -> List[Dict]:
    """发现并收集 VM 进程统计

    Returns:
        List[Dict]: 每个 VM 的统计信息，包含:
            - pid: 进程 ID
            - name: VM/Sandbox ID
            - cpu_percent: CPU 使用率
            - memory_mb: 内存使用 (MB)
            - memory_huge_mb: 大页内存 (MB)
            - memory_private_mb: 私有内存 (MB)
            - memory_heap_mb: 堆内存 (MB)
            - memory_per_numa: 每 NUMA 内存分布
            - status: 进程状态
    """
    pass

@abstractmethod
def get_process_names(self) -> Tuple[str, ...]:
    """返回要匹配的进程名列表"""
    pass

@abstractmethod
def extract_vm_id(self, pid: int, cmdline: str) -> str:
    """从进程信息提取 VM/Sandbox ID"""
    pass

@abstractmethod
def get_monitor_title(self) -> str:
    """返回监控显示标题"""
    pass

@abstractmethod
def get_no_vm_message(self) -> str:
    """返回无 VM 时的提示消息"""
    pass

@abstractmethod
def get_csv_filename_prefix(self) -> str:
    """返回 CSV 文件名前缀"""
    pass
```

---

## QEMUMonitor 子类实现

### 进程发现

```python
def get_process_names(self) -> Tuple[str, ...]:
    return ('qemu-kvm', 'qemu-system')
```

### VM ID 提取

```python
def extract_vm_id(self, pid: int, cmdline: str) -> str:
    # 从 -name 参数提取
    name_match = re.search(r'-name\s+([^,\s]+)', cmdline)
    if name_match:
        return name_match.group(1)
    # 回退到 PID
    return f"vm-{pid}"
```

### 显示字符串

| 方法 | 返回值 |
|------|--------|
| `get_monitor_title()` | "QEMU VM Real-time Monitoring" |
| `get_no_vm_message()` | "No running QEMU virtual machines detected" |
| `get_csv_filename_prefix()` | "qemu_monitor" |

---

## FirecrackerMonitor 子类实现

### 进程发现

```python
def get_process_names(self) -> Tuple[str, ...]:
    return ('firecracker',)
```

### VM ID 提取（简单实现）

```python
def extract_vm_id(self, pid: int, cmdline: str) -> str:
    # 第一阶段：简单实现，使用 PID
    # 后续可扩展：从 --id 参数、--api-sock 路径、E2B API 获取
    return f"fc-{pid}"
```

### 显示字符串

| 方法 | 返回值 |
|------|--------|
| `get_monitor_title()` | "Firecracker VM Real-time Monitoring" |
| `get_no_vm_message()` | "No running Firecracker microVMs detected" |
| `get_csv_filename_prefix()` | "firecracker_monitor" |

---

## 向后兼容策略

### 兼容层实现

```python
# qemu_monitor/__init__.py
"""
DEPRECATED: This package is renamed to 'vm_monitor'.
Please use: from vm_monitor import QEMUMonitor, FirecrackerMonitor

This module will be removed in a future version.
"""

import warnings
warnings.warn(
    "qemu_monitor is deprecated, use vm_monitor instead",
    DeprecationWarning,
    stacklevel=2
)

from vm_monitor import (
    QEMUMonitor,
    FirecrackerMonitor,
    VMMonitorBase,
    LogCapture,
    ...
)

__all__ = ['QEMUMonitor', 'FirecrackerMonitor', 'VMMonitorBase', ...]
```

### CLI 兼容

保留 `qemu_monitor.py` 根目录脚本，转发到 `vm_monitor.cli`:

```python
# qemu_monitor.py（根目录）
"""DEPRECATED: Use vm_monitor.py instead"""
import warnings
warnings.warn("Use vm_monitor.py instead", DeprecationWarning)
from vm_monitor.cli import main
if __name__ == '__main__':
    main()
```

### 导入路径兼容表

| 旧导入 | 新导入 | 状态 |
|--------|--------|------|
| `from qemu_monitor import QEMUMonitor` | `from vm_monitor import QEMUMonitor` | ✓ 有废弃警告 |
| `from qemu_monitor.monitor import QEMUMonitor` | `from vm_monitor.qemu import QEMUMonitor` | ✓ 有废弃警告 |

---

## CLI 设计

### 参数

```python
parser.add_argument(
    '--vmm',
    type=str,
    choices=['qemu', 'firecracker'],
    default='qemu',
    help='VMM type to monitor (default: qemu)'
)
```

### 使用示例

```bash
# QEMU 监控（默认）
python vm_monitor.py -t 60 -i 3

# Firecracker 监控
python vm_monitor.py --vmm firecracker -t 60 -i 3

# 向后兼容
python qemu_monitor.py -t 60 -i 3  # 自动使用 QEMU，有废弃警告
```

---

## 实现步骤

1. **创建 vm_monitor 包目录结构**
2. **创建 base.py - VMMonitorBase 抽象基类**
   - 从 monitor.py 提取系统级监控方法
   - 定义抽象方法
3. **创建 qemu.py - QEMUMonitor 子类**
   - 继承 VMMonitorBase
   - 实现抽象方法
4. **创建 firecracker.py - FirecrackerMonitor 子类**
   - 继承 VMMonitorBase
   - 实现抽象方法
5. **迁移其他模块**
   - config.py, exporters.py, parsers.py, log_capture.py 迁移到 vm_monitor
6. **更新 cli.py**
   - 添加 --vmm 参数
   - 根据参数选择对应的 Monitor 类
7. **创建向后兼容层**
   - 修改 qemu_monitor/__init__.py 为别名转发
   - 修改 qemu_monitor/monitor.py 为废弃别名
8. **更新根目录入口脚本**
   - 创建 vm_monitor.py
   - 修改 qemu_monitor.py 为废弃转发

---

## 扩展性设计

添加新的 VMM 类型只需：

1. 创建新文件 `vm_monitor/<vmm_name>.py`
2. 继承 `VMMonitorBase`
3. 实现抽象方法（进程名、ID 提取、显示字符串）
4. 在 `cli.py` 的 `--vmm choices` 中添加选项
5. 在 `__init__.py` 中导出新类

示例（Cloud-Hypervisor）:

```python
# vm_monitor/cloud_hypervisor.py
class CloudHypervisorMonitor(VMMonitorBase):
    def get_process_names(self) -> Tuple[str, ...]:
        return ('cloud-hypervisor',)

    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        return f"clh-{pid}"

    # ... 其他抽象方法
```