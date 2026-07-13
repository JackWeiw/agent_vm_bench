# VM Monitor 模块重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 qemu_monitor 模块重构为 vm_monitor，支持 QEMU 和 Firecracker 监控，保持向后兼容。

**Architecture:** 继承式架构 - VMMonitorBase 抽象基类包含系统级监控，QEMUMonitor 和 FirecrackerMonitor 子类实现进程发现和 ID 提取。

**Tech Stack:** Python, abc (抽象基类), psutil (进程监控), re (正则表达式)

---

## 文件结构

```
vm_monitor/                      # 新包（创建）
    __init__.py                  # 导出所有公共类
    base.py                      # VMMonitorBase 抽象基类
    qemu.py                      # QEMUMonitor 子类
    firecracker.py               # FirecrackerMonitor 子类
    cli.py                       # CLI 入口（迁移自 qemu_monitor/cli.py）
    config.py                    # 配置管理（迁移自 qemu_monitor/config.py）
    exporters.py                 # 导出工具（迁移自 qemu_monitor/exporters.py）
    parsers.py                   # 日志解析（迁移自 qemu_monitor/parsers.py）
    log_capture.py               # 日志采集（迁移自 qemu_monitor/log_capture.py）

qemu_monitor/                    # 向后兼容层（修改）
    __init__.py                  # 废弃警告 + 别名转发
    monitor.py                   # 废弃警告 + 别名转发

qemu_monitor.py                  # 废弃警告 + 转发（修改）
vm_monitor.py                    # 新入口脚本（创建）
```

---

## Task 1: 创建 vm_monitor 包目录结构

**Files:**
- Create: `vm_monitor/` 目录

- [ ] **Step 1: 创建 vm_monitor 目录**

```bash
mkdir -p vm_monitor
```

Expected: 目录创建成功

- [ ] **Step 2: 创建空的 __init__.py 占位**

```python
# vm_monitor/__init__.py (占位，后续更新)
"""VM Monitor Package - placeholder"""
```

Expected: 文件创建成功

---

## Task 2: 创建 VMMonitorBase 抽象基类

**Files:**
- Create: `vm_monitor/base.py`

- [ ] **Step 1: 创建 base.py 文件头和导入**

```python
# vm_monitor/base.py
"""
VMMonitorBase - Abstract Base Class for VM Monitor

Provides system-level monitoring infrastructure independent of VMM type:
- NUMA memory statistics
- Hugepage memory tracking
- NUMA CPU usage
- Host CPU/memory statistics
- Swap usage
- Process memory via numastat
- Sampling loop framework
- Export/analysis infrastructure
"""

from abc import ABC, abstractmethod
import psutil
import time
import signal
import subprocess
import csv
import os
import re
import sys
from datetime import datetime, timedelta
from collections import defaultdict
import threading
from typing import Dict, List, Optional, Tuple
```

- [ ] **Step 2: 创建 VMMonitorBase 类定义和 __init__**

```python
class VMMonitorBase(ABC):
    """Abstract base class for Virtual Machine Monitor implementations

    Provides:
    - System-level monitoring (NUMA, hugepage, host CPU/mem, swap)
    - Process cache management
    - Generic sampling loop and display infrastructure
    - Export/analysis infrastructure

    Subclasses must implement:
    - get_vms_realtime(): VMM-specific process discovery
    - get_process_names(): Return process names to match
    - extract_vm_id(): Extract VM/Sandbox ID from cmdline
    - get_monitor_title(): Return title for display
    - get_no_vm_message(): Return message when no VMs detected
    - get_csv_filename_prefix(): Return prefix for CSV filenames
    """

    def __init__(self):
        """Initialize monitor with empty data containers"""
        # Core state
        self.running = False
        self.data = []
        self.stop_event = threading.Event()
        self.process_cache = {}

        # NUMA memory tracking
        self.numa_memory_history = []

        # VM tracking peaks
        self.peak_total_memory_mb = 0.0
        self.peak_total_cpu = 0.0

        # Hugepage tracking
        self.hugepage_total_mb = 0.0
        self.hugepage_free_mb = 0.0
        self.hugepage_used_mb = 0.0
        self.hugepage_used_history = []
        self.peak_hugepage_used_mb = 0.0
        self.hugepage_per_numa = {}
        self.hugepage_per_numa_history = []

        # Host machine tracking
        self.host_cpu_history = []
        self.host_mem_history = []
        self.peak_host_cpu = 0.0
        self.peak_host_mem_mb = 0.0

        # Swap tracking
        self.swap_history = []
        self.peak_swap_used_mb = 0.0

        # NUMA CPU tracking
        self.target_numa_nodes = [0]
        self.numa_cpu_history = defaultdict(list)
        self.numa_cpu_peak = defaultdict(float)
        self.available_numa_nodes = self.get_available_numa_nodes()
        self.last_vm_count = 0
```

- [ ] **Step 3: 复制系统级监控方法（从 monitor.py）**

复制以下方法到 base.py（代码从 monitor.py 提取，保持不变）：
- `get_numa_nodes_memory()` (lines 86-115)
- `collect_hugepage_stats()` (lines 117-151)
- `collect_hugepage_per_numa_stats()` (lines 153-204)
- `get_available_numa_nodes()` (lines 240-248)
- `collect_numa_cpu()` (lines 251-281)
- `collect_host_stats()` (lines 284-300)
- `collect_swap_stats()` (lines 303-317)
- `get_vm_memory_from_numastat()` (lines 319-371)
- `print_numa_real_time()` (lines 206-214)
- `print_final_numa_stats()` (lines 216-237)

- [ ] **Step 4: 定义抽象方法**

```python
    # ==================== Abstract Methods ====================

    @abstractmethod
    def get_vms_realtime(self) -> List[Dict]:
        """Discover and collect stats for running VMs

        Returns:
            List of dicts with keys: pid, name, cpu_percent, memory_mb,
            memory_huge_mb, memory_private_mb, memory_heap_mb,
            memory_per_numa, status
        """
        pass

    @abstractmethod
    def get_process_names(self) -> Tuple[str, ...]:
        """Return tuple of process names to match"""
        pass

    @abstractmethod
    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract VM/Sandbox ID from process info

        Args:
            pid: Process ID
            cmdline: Full command line string

        Returns:
            VM/Sandbox ID string
        """
        pass

    @abstractmethod
    def get_monitor_title(self) -> str:
        """Return title string for monitoring display"""
        pass

    @abstractmethod
    def get_no_vm_message(self) -> str:
        """Return message when no VMs are detected"""
        pass

    @abstractmethod
    def get_csv_filename_prefix(self) -> str:
        """Return prefix for CSV output filenames"""
        pass
```

- [ ] **Step 5: 复制模板方法（collect_sample, display_realtime_table 等）**

复制以下方法，修改为使用抽象方法：
- `collect_sample()` - 修改为调用 `self.get_vms_realtime()`
- `display_realtime_table()` - 修改为使用 `self.get_monitor_title()` 和 `self.get_no_vm_message()`
- `check_stress_process()` (lines 563-574)
- `check_stress_file()` (lines 576-577)
- `wait_for_stress_and_monitor()` (lines 579-642)
- `start_monitoring()` (lines 644-677)

关键修改点：
```python
# collect_sample() 修改
def collect_sample(self):
    """Collect one sample - calls subclass get_vms_realtime()"""
    self.collect_hugepage_stats()
    self.collect_numa_cpu()
    self.collect_host_stats()
    self.collect_swap_stats()
    vms = self.get_vms_realtime()  # 调用子类实现
    self.last_vm_count = len(vms)
    # ... rest unchanged

# display_realtime_table() 修改
def display_realtime_table(self, sample_data, elapsed_time, duration, check_method=""):
    # ...
    print(f"{self.get_monitor_title()} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    # ...
    if not sample_data:
        print(self.get_no_vm_message(), flush=True)
        return
```

- [ ] **Step 6: 复制导出和分析方法**

复制以下方法（保持不变）：
- `export_raw_csv()` (lines 679-689) - 修改为使用 `self.get_csv_filename_prefix()`
- `calculate_vm_stats()` (lines 691-711)
- `calculate_overall_stats()` (lines 713-724)
- `export_summary_csv()` (lines 726-802)
- `print_summary_report()` (lines 804-879)
- `analyze_and_export()` (lines 881-891)

修改 export_raw_csv:
```python
def export_raw_csv(self, filename=None):
    if not filename:
        filename = f"{self.get_csv_filename_prefix()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    # ... rest unchanged
```

---

## Task 3: 创建 QEMUMonitor 子类

**Files:**
- Create: `vm_monitor/qemu.py`

- [ ] **Step 1: 创建 qemu.py 文件头和导入**

```python
# vm_monitor/qemu.py
"""
QEMUMonitor - QEMU Virtual Machine Monitor

Monitors qemu-kvm and qemu-system processes.
"""

import re
import psutil
from typing import Dict, List, Tuple
from .base import VMMonitorBase
```

- [ ] **Step 2: 创建 QEMUMonitor 类和进程发现逻辑**

```python
class QEMUMonitor(VMMonitorBase):
    """QEMU Virtual Machine Monitor

    Monitors qemu-kvm and qemu-system processes.
    """

    # Process names to match
    PROCESS_NAMES = ('qemu-kvm', 'qemu-system')

    def get_process_names(self) -> Tuple[str, ...]:
        """Return QEMU process names to match"""
        return self.PROCESS_NAMES

    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract VM name from -name parameter

        Args:
            pid: Process ID
            cmdline: Full command line string

        Returns:
            VM name string (e.g., 'vm-123' or parsed name)
        """
        name_match = re.search(r'-name\s+([^,\s]+)', cmdline)
        if name_match:
            return name_match.group(1)
        return f"vm-{pid}"
```

- [ ] **Step 3: 实现 get_vms_realtime 方法**

从 monitor.py 的 `get_qemu_vms_realtime()` (lines 373-459) 复制逻辑，修改为使用抽象方法：

```python
def get_vms_realtime(self) -> List[Dict]:
    """Discover QEMU VMs and collect stats"""
    vms = []
    current_pids = set()
    current_total_mem = 0.0
    current_total_cpu = 0.0

    process_names = self.get_process_names()

    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
        try:
            proc_name = proc.info['name'] or ''
            if not any(name in proc_name for name in process_names):
                continue

            pid = proc.info['pid']
            current_pids.add(pid)
            cmdline = ' '.join(proc.info['cmdline'] or [])

            # Extract VM ID using subclass method
            vm_name = self.extract_vm_id(pid, cmdline)

            # Memory via numastat
            numastat_mem = self.get_vm_memory_from_numastat(pid)
            memory_mb = numastat_mem.get('total_mb', 0.0)
            memory_huge_mb = numastat_mem.get('huge_mb', 0.0)
            memory_private_mb = numastat_mem.get('private_mb', 0.0)
            memory_heap_mb = numastat_mem.get('heap_mb', 0.0)
            memory_per_numa = numastat_mem.get('per_node', {})

            # Fallback to psutil if numastat fails
            if memory_mb <= 0:
                try:
                    mem_info = proc.memory_info()
                    memory_mb = round(mem_info.pss / 1024 / 1024, 2)
                except:
                    mem_info = proc.memory_info()
                    memory_mb = round(mem_info.rss / 1024 / 1024, 2)

            current_total_mem += memory_mb

            # CPU stats with caching
            if pid not in self.process_cache:
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent()
                    self.process_cache[pid] = p
                    cpu = 0.0
                except:
                    cpu = 0.0
            else:
                try:
                    p = self.process_cache[pid]
                    cpu = p.cpu_percent()
                except psutil.NoSuchProcess:
                    self.process_cache.pop(pid, None)
                    cpu = 0.0

            cpu = round(max(0, min(cpu, 10000)), 2)
            current_total_cpu += cpu

            vms.append({
                'pid': pid,
                'name': vm_name,
                'cpu_percent': cpu,
                'memory_mb': memory_mb,
                'memory_huge_mb': memory_huge_mb,
                'memory_private_mb': memory_private_mb,
                'memory_heap_mb': memory_heap_mb,
                'memory_per_numa': memory_per_numa,
                'status': proc.info['status']
            })
        except:
            continue

    # Update peaks
    if current_total_mem > self.peak_total_memory_mb:
        self.peak_total_memory_mb = current_total_mem
    if current_total_cpu > self.peak_total_cpu:
        self.peak_total_cpu = current_total_cpu

    # Clean dead processes from cache
    dead_pids = [p for p in self.process_cache if p not in current_pids]
    for p in dead_pids:
        self.process_cache.pop(p, None)

    return vms
```

- [ ] **Step 4: 实现显示字符串方法**

```python
def get_monitor_title(self) -> str:
    """Return QEMU monitoring title"""
    return "QEMU VM Real-time Monitoring"

def get_no_vm_message(self) -> str:
    """Return message when no QEMU VMs detected"""
    return "No running QEMU virtual machines detected"

def get_csv_filename_prefix(self) -> str:
    """Return CSV filename prefix"""
    return "qemu_monitor"
```

---

## Task 4: 创建 FirecrackerMonitor 子类

**Files:**
- Create: `vm_monitor/firecracker.py`

- [ ] **Step 1: 创建 firecracker.py 文件头和导入**

```python
# vm_monitor/firecracker.py
"""
FirecrackerMonitor - Firecracker microVM Monitor

Monitors firecracker processes (used in E2B, containerd environments).
"""

import psutil
from typing import Dict, List, Tuple
from .base import VMMonitorBase
```

- [ ] **Step 2: 创建 FirecrackerMonitor 类和进程发现逻辑**

```python
class FirecrackerMonitor(VMMonitorBase):
    """Firecracker microVM Monitor

    Monitors firecracker processes.
    """

    # Process names to match
    PROCESS_NAMES = ('firecracker',)

    def get_process_names(self) -> Tuple[str, ...]:
        """Return Firecracker process names to match"""
        return self.PROCESS_NAMES

    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract Sandbox ID from Firecracker process

        Simple implementation: use PID as fallback.
        Can be extended to parse --id or --api-sock path.

        Args:
            pid: Process ID
            cmdline: Full command line string

        Returns:
            Sandbox ID string (e.g., 'fc-123')
        """
        # TODO: Can extend to parse --id parameter or socket path
        return f"fc-{pid}"
```

- [ ] **Step 3: 实现 get_vms_realtime 方法**

复制 QEMUMonitor 的逻辑，修改进程名匹配：

```python
def get_vms_realtime(self) -> List[Dict]:
    """Discover Firecracker microVMs and collect stats"""
    vms = []
    current_pids = set()
    current_total_mem = 0.0
    current_total_cpu = 0.0

    process_names = self.get_process_names()

    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
        try:
            proc_name = proc.info['name'] or ''
            if not any(name in proc_name for name in process_names):
                continue

            pid = proc.info['pid']
            current_pids.add(pid)
            cmdline = ' '.join(proc.info['cmdline'] or [])

            # Extract Sandbox ID
            sandbox_id = self.extract_vm_id(pid, cmdline)

            # Memory via numastat
            numastat_mem = self.get_vm_memory_from_numastat(pid)
            memory_mb = numastat_mem.get('total_mb', 0.0)
            memory_huge_mb = numastat_mem.get('huge_mb', 0.0)
            memory_private_mb = numastat_mem.get('private_mb', 0.0)
            memory_heap_mb = numastat_mem.get('heap_mb', 0.0)
            memory_per_numa = numastat_mem.get('per_node', {})

            # Fallback to psutil
            if memory_mb <= 0:
                try:
                    mem_info = proc.memory_info()
                    memory_mb = round(mem_info.pss / 1024 / 1024, 2)
                except:
                    mem_info = proc.memory_info()
                    memory_mb = round(mem_info.rss / 1024 / 1024, 2)

            current_total_mem += memory_mb

            # CPU stats with caching
            if pid not in self.process_cache:
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent()
                    self.process_cache[pid] = p
                    cpu = 0.0
                except:
                    cpu = 0.0
            else:
                try:
                    p = self.process_cache[pid]
                    cpu = p.cpu_percent()
                except psutil.NoSuchProcess:
                    self.process_cache.pop(pid, None)
                    cpu = 0.0

            cpu = round(max(0, min(cpu, 10000)), 2)
            current_total_cpu += cpu

            vms.append({
                'pid': pid,
                'name': sandbox_id,
                'cpu_percent': cpu,
                'memory_mb': memory_mb,
                'memory_huge_mb': memory_huge_mb,
                'memory_private_mb': memory_private_mb,
                'memory_heap_mb': memory_heap_mb,
                'memory_per_numa': memory_per_numa,
                'status': proc.info['status']
            })
        except:
            continue

    # Update peaks
    if current_total_mem > self.peak_total_memory_mb:
        self.peak_total_memory_mb = current_total_mem
    if current_total_cpu > self.peak_total_cpu:
        self.peak_total_cpu = current_total_cpu

    # Clean cache
    dead_pids = [p for p in self.process_cache if p not in current_pids]
    for p in dead_pids:
        self.process_cache.pop(p, None)

    return vms
```

- [ ] **Step 4: 实现显示字符串方法**

```python
def get_monitor_title(self) -> str:
    """Return Firecracker monitoring title"""
    return "Firecracker VM Real-time Monitoring"

def get_no_vm_message(self) -> str:
    """Return message when no Firecracker VMs detected"""
    return "No running Firecracker microVMs detected"

def get_csv_filename_prefix(self) -> str:
    """Return CSV filename prefix"""
    return "firecracker_monitor"
```

---

## Task 5: 迁移其他模块到 vm_monitor

**Files:**
- Copy: `qemu_monitor/config.py` → `vm_monitor/config.py`
- Copy: `qemu_monitor/exporters.py` → `vm_monitor/exporters.py`
- Copy: `qemu_monitor/parsers.py` → `vm_monitor/parsers.py`
- Copy: `qemu_monitor/log_capture.py` → `vm_monitor/log_capture.py`

- [ ] **Step 1: 复制 config.py**

```bash
cp qemu_monitor/config.py vm_monitor/config.py
```

Expected: 文件复制成功

- [ ] **Step 2: 复制 parsers.py**

```bash
cp qemu_monitor/parsers.py vm_monitor/parsers.py
```

Expected: 文件复制成功

- [ ] **Step 3: 复制 log_capture.py**

```bash
cp qemu_monitor/log_capture.py vm_monitor/log_capture.py
```

Expected: 文件复制成功

- [ ] **Step 4: 复制并修改 exporters.py**

复制并更新 TYPE_CHECKING 导入：

```python
# vm_monitor/exporters.py 顶部修改
if TYPE_CHECKING:
    from .base import VMMonitorBase  # 改为导入基类

def export_to_excel(monitor: 'VMMonitorBase', log_dir: str, numa_nodes: list = None,
                    output_file: str = None, capture_results: dict = None) -> str:
    """Export all monitoring and parsed log data to Excel

    Args:
        monitor: VMMonitorBase instance (QEMUMonitor or FirecrackerMonitor)
        ...
    """
```

---

## Task 6: 更新 vm_monitor/__init__.py

**Files:**
- Modify: `vm_monitor/__init__.py`

- [ ] **Step 1: 编写完整的 __init__.py**

```python
# vm_monitor/__init__.py
"""
VM Monitor Package

Real-time monitoring for QEMU and Firecracker virtual machines.
Provides modular components for configuration, log capture, parsing,
data export, and VM monitoring.

Usage:
    # Package-level import (recommended)
    from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

    # Module-level import (for specific functions)
    from vm_monitor.parsers import parse_devkit_top_down

    # CLI entry point
    python -m vm_monitor.cli -t 60 -i 3
    python -m vm_monitor.cli --vmm firecracker -t 60 -i 3
"""

# Core classes
from .base import VMMonitorBase
from .qemu import QEMUMonitor
from .firecracker import FirecrackerMonitor
from .log_capture import LogCapture

# Configuration management
from .config import (
    load_env_config,
    save_env_config,
    validate_and_prompt_missing,
    load_getfre_config,
)

# Parser functions
from .parsers import (
    parse_devkit_top_down,
    parse_ksys,
    parse_devkit_mem,
    parse_getfre,
    parse_ub_watch,
    parse_smap_bw,
    parse_all_logs,
)

# Export utilities
from .exporters import (
    export_to_excel,
    print_capture_summary,
)

# Version marker
__version__ = '2.0.0'

__all__ = [
    'VMMonitorBase',
    'QEMUMonitor',
    'FirecrackerMonitor',
    'LogCapture',
    'load_env_config',
    'save_env_config',
    'validate_and_prompt_missing',
    'load_getfre_config',
    'parse_devkit_top_down',
    'parse_ksys',
    'parse_devkit_mem',
    'parse_getfre',
    'parse_ub_watch',
    'parse_smap_bw',
    'parse_all_logs',
    'export_to_excel',
    'print_capture_summary',
]
```

---

## Task 7: 创建 vm_monitor/cli.py（带 --vmm 参数）

**Files:**
- Create: `vm_monitor/cli.py`

- [ ] **Step 1: 创建 CLI 文件头和导入**

```python
# vm_monitor/cli.py
"""
Command Line Interface Entry Point

Main entry point for VM monitor tool. Handles argparse parsing,
initialization of monitor and log capture, and coordinates execution.
"""

import argparse
import os
import sys
import time
from datetime import datetime

# Internal dependencies
from .config import load_env_config, validate_and_prompt_missing
from .log_capture import LogCapture
from .qemu import QEMUMonitor
from .firecracker import FirecrackerMonitor
from .exporters import export_to_excel, print_capture_summary

# Try to import pandas for Excel availability check
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
```

- [ ] **Step 2: 创建 main 函数（添加 --vmm 参数）**

```python
def main():
    """Main entry point for VM monitoring tool"""
    parser = argparse.ArgumentParser(
        description='VM Monitoring Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
[Mode 1: Stress Sync Monitoring]
  sudo python3 vm_monitor.py --stress-file /tmp/bench_running.lock
    -> Wait for lock file to appear then start monitoring

[Mode 2: Timer Monitoring]
  sudo python3 vm_monitor.py -t 60 -i 2
    -> Monitor for 60 seconds

[Mode 3: With Log Collection]
  sudo python3 vm_monitor.py -t 60 -i 2 --enable-capture
    -> Monitor for 60 seconds with parallel log collection

[VMM Type Selection]
  sudo python3 vm_monitor.py --vmm qemu -t 60
    -> Monitor QEMU VMs (default)
  sudo python3 vm_monitor.py --vmm firecracker -t 60
    -> Monitor Firecracker microVMs
        """
    )

    # VMM type selection
    parser.add_argument(
        '--vmm',
        type=str,
        choices=['qemu', 'firecracker'],
        default='qemu',
        help='VMM type to monitor (default: qemu)'
    )

    # Other arguments (same as original)
    sync = parser.add_mutually_exclusive_group()
    sync.add_argument('--stress-process', type=str, help='Stress process name')
    sync.add_argument('--stress-file', type=str, help='Stress marker file')
    parser.add_argument('-t','--time', type=int, default=60, help='Timer duration seconds')
    parser.add_argument('-i','--interval', type=int, default=3, help='Sampling interval')
    parser.add_argument('-o','--output', type=str, help='Output prefix')
    parser.add_argument('--numa', type=str, default='1', help='NUMA nodes to monitor')
    parser.add_argument('--log-dir', type=str, help='Log output directory')
    parser.add_argument('--enable-capture', action='store_true',
                        help='Enable parallel log collection')
    parser.add_argument('--auto-skip', action='store_true',
                        help='Auto-skip missing log capture tools')
    parser.add_argument('--ksys-parse-timeout', type=int, default=600,
                        help='Timeout for ksys parsing')
    args = parser.parse_args()

    # Check root permission
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        print("[WARN] Recommended to run as root")
        time.sleep(1)

    # Setup log directory
    log_dir = args.log_dir or f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(log_dir, exist_ok=True)
    print(f"[OK] Log directory: {log_dir}")

    # Select monitor based on VMM type
    if args.vmm == 'firecracker':
        m = FirecrackerMonitor()
        print(f"[OK] Using FirecrackerMonitor")
    else:
        m = QEMUMonitor()
        print(f"[OK] Using QEMUMonitor (default)")

    # Set NUMA nodes
    try:
        m.target_numa_nodes = list(map(int, args.numa.split(',')))
    except:
        m.target_numa_nodes = [0]

    # Load config and start capture (if enabled)
    capture = None
    config = None
    if args.enable_capture:
        print("\nLoading log collection configuration...")
        config = load_env_config()
        config = validate_and_prompt_missing(config, non_interactive=args.auto_skip)

    # Start log capture
    if args.enable_capture:
        print("\nStarting log collection tools...")
        capture = LogCapture(config, args.time, log_dir, m.target_numa_nodes,
                             ksys_parse_timeout=args.ksys_parse_timeout)
        capture.start()
        print(f"[OK] Log collection tools started (duration={args.time}s)")
        sys.stdout.flush()

    # Start monitoring
    if args.stress_process:
        m.wait_for_stress_and_monitor('process', args.stress_process, args.interval, args.time)
    elif args.stress_file:
        m.wait_for_stress_and_monitor('file', args.stress_file, args.interval, args.time)
    else:
        m.start_monitoring(args.time, args.interval)

    # Wait for capture
    if capture:
        print("\nWaiting for log collection tools...")
        capture.wait()
        print("[OK] Log collection complete")

    # Export results
    prefix = m.get_csv_filename_prefix()
    raw = os.path.join(log_dir, f"{args.output}.csv" if args.output else f"{prefix}.csv")
    sumf = os.path.join(log_dir, f"summary_{args.output}.csv" if args.output else "summary.csv")
    m.analyze_and_export(raw, sumf)

    # Capture summary
    capture_results = None
    if capture:
        capture_results = capture.get_results()
        print_capture_summary(capture_results, log_dir, m.target_numa_nodes)

    # Excel export
    if PANDAS_AVAILABLE:
        excel_file = os.path.join(log_dir, 'analysis_report.xlsx')
        export_to_excel(m, log_dir, m.target_numa_nodes, excel_file, capture_results)

    print(f"\nComplete! All outputs saved to: {log_dir}/")
```

---

## Task 8: 创建 vm_monitor.py 入口脚本

**Files:**
- Create: `vm_monitor.py`

- [ ] **Step 1: 创建入口脚本**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM Monitor - Real-time Monitoring Tool

Entry point for vm_monitor package.

Usage:
    python vm_monitor.py -t 600 -i 2
    python vm_monitor.py --vmm firecracker -t 60 -i 3
    python vm_monitor.py --stress-file /tmp/bench_running.lock
"""

from vm_monitor.cli import main

if __name__ == '__main__':
    main()
```

---

## Task 9: 创建向后兼容层

**Files:**
- Modify: `qemu_monitor/__init__.py`
- Create: `qemu_monitor/monitor.py`（废弃转发）

- [ ] **Step 1: 修改 qemu_monitor/__init__.py 为废弃转发**

```python
# qemu_monitor/__init__.py
"""
DEPRECATED: This package is renamed to 'vm_monitor'.

Please use:
    from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase

This module will be removed in a future version.
Compatibility aliases provided for backward compatibility.
"""

import warnings
warnings.warn(
    "qemu_monitor is deprecated, use vm_monitor instead",
    DeprecationWarning,
    stacklevel=2
)

# Forward all imports from vm_monitor
from vm_monitor import (
    VMMonitorBase,
    QEMUMonitor,
    FirecrackerMonitor,
    LogCapture,
    load_env_config,
    save_env_config,
    validate_and_prompt_missing,
    load_getfre_config,
    parse_devkit_top_down,
    parse_ksys,
    parse_devkit_mem,
    parse_getfre,
    parse_ub_watch,
    parse_smap_bw,
    parse_all_logs,
    export_to_excel,
    print_capture_summary,
)

__version__ = '1.0.0'  # Legacy version

__all__ = [
    'QEMUMonitor',  # Primary export (backward compatible)
    'VMMonitorBase',
    'FirecrackerMonitor',
    'LogCapture',
    'load_env_config',
    'save_env_config',
    'validate_and_prompt_missing',
    'load_getfre_config',
    'parse_devkit_top_down',
    'parse_ksys',
    'parse_devkit_mem',
    'parse_getfre',
    'parse_ub_watch',
    'parse_smap_bw',
    'parse_all_logs',
    'export_to_excel',
    'print_capture_summary',
]
```

- [ ] **Step 2: 创建 qemu_monitor/monitor.py 废弃转发**

```python
# qemu_monitor/monitor.py
"""
DEPRECATED: This module is renamed to vm_monitor.qemu.

Please use:
    from vm_monitor import QEMUMonitor
    or
    from vm_monitor.qemu import QEMUMonitor

This file will be removed in a future version.
"""

import warnings
warnings.warn(
    "qemu_monitor.monitor is deprecated, use vm_monitor.qemu instead",
    DeprecationWarning,
    stacklevel=2
)

from vm_monitor.qemu import QEMUMonitor

__all__ = ['QEMUMonitor']
```

- [ ] **Step 3: 修改根目录 qemu_monitor.py 为废弃转发**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DEPRECATED: Use vm_monitor.py instead

This entry point is deprecated and will be removed in a future version.
Please use vm_monitor.py for both QEMU and Firecracker monitoring.

Usage:
    python vm_monitor.py -t 60 -i 3
    python vm_monitor.py --vmm firecracker -t 60
"""

import warnings
warnings.warn(
    "qemu_monitor.py is deprecated, use vm_monitor.py instead",
    DeprecationWarning,
    stacklevel=2
)

from vm_monitor.cli import main

if __name__ == '__main__':
    main()
```

---

## Task 10: 提交 monitor 相关代码

**Files:**
- Commit: 只提交 vm_monitor 相关文件和兼容层修改

- [ ] **Step 1: 添加新文件到 git**

```bash
git add vm_monitor/
git add vm_monitor.py
git add qemu_monitor/__init__.py
git add qemu_monitor/monitor.py
git add qemu_monitor.py
git add docs/superpowers/specs/2026-06-21-vm-monitor-refactor-design.md
```

- [ ] **Step 2: 提交更改**

```bash
git commit -m "feat(vm_monitor): refactor qemu_monitor to support multiple VMMs

- Create vm_monitor package with VMMonitorBase abstract base class
- Add QEMUMonitor subclass for qemu-kvm/qemu-system monitoring
- Add FirecrackerMonitor subclass for firecracker process monitoring
- Add --vmm CLI parameter to select VMM type (qemu/firecracker)
- Maintain backward compatibility via qemu_monitor aliases with deprecation warnings
- All system-level monitoring (NUMA, hugepage, host stats) in base class

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Expected: 提交成功

---

## 验证

**测试步骤：**

1. 导入测试：
```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor, VMMonitorBase
m = QEMUMonitor()
fc = FirecrackerMonitor()
print(m.get_monitor_title())
print(fc.get_monitor_title())
```

2. CLI 测试：
```bash
python vm_monitor.py --help
python vm_monitor.py --vmm qemu -t 5 -i 1
python vm_monitor.py --vmm firecracker -t 5 -i 1
```

3. 向后兼容测试：
```python
from qemu_monitor import QEMUMonitor  # 应有废弃警告
```

---

## 自审检查

**1. Spec Coverage:**
- 目录结构 ✓ (Task 1, 5)
- VMMonitorBase 基类 ✓ (Task 2)
- QEMUMonitor 子类 ✓ (Task 3)
- FirecrackerMonitor 子类 ✓ (Task 4)
- CLI --vmm 参数 ✓ (Task 7)
- 向后兼容 ✓ (Task 9)

**2. Placeholder Scan:**
- 无 TBD/TODO（除 Firecracker ID 提取的 TODO 注释，这是设计允许的）
- 所有代码步骤都有完整实现

**3. Type Consistency:**
- `get_vms_realtime()` 返回 `List[Dict]` ✓
- `get_process_names()` 返回 `Tuple[str, ...]` ✓
- `extract_vm_id()` 返回 `str` ✓
- 所有抽象方法签名一致
