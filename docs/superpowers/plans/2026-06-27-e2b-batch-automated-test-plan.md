# E2B 批量自动化测试实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 e2b_bench 实现完整的批量自动化测试流程，包括 smap_tool 内存迁移监控、vm_monitor 性能采集、批量测试调度和结果汇总。

**Architecture:** 采用类结构设计，模块化实现。Phase 1 增强单次测试流程（SmapToolManager、VmMonitorManager），Phase 2 实现批量调度框架（BatchTask、TaskGroup、TaskGenerator、GroupRunner、BatchScheduler），Phase 3 实现指标提取和汇总（MetricsExtractor、ReportAggregator）。

**Tech Stack:** Python 3, dataclasses, subprocess, pandas, xlsxwriter, yaml, argparse

---

## Phase 1: 单次测试流程增强

### Task 1: 新增配置字段 (config.py)

**Files:**
- Modify: `e2b_bench/config.py`

- [ ] **Step 1: 在 Config dataclass 中添加 smap_tool 配置字段**

在 `Config` 类中添加以下字段（约在第 45 行后）：

```python
# smap_tool configuration (memory migration monitoring)
smap_tool_enabled: bool = False
smap_tool_path: str = ""
smap_tool_swap_size: int = 81920
smap_tool_ratio: int = 15
smap_tool_src_nid: int = 2
smap_tool_dest_nid: int = 5

# vm_monitor configuration (performance monitoring)
vm_monitor_enabled: bool = False
vm_monitor_vmm_type: str = "firecracker"
vm_monitor_duration: int = 600
vm_monitor_log_dir: str = "results/e2b/vm_monitor"
vm_monitor_stress_file: str = "/dev/shm/e2b_benchmark_lock"
```

- [ ] **Step 2: 在 _from_dict 方法中解析新配置**

在 `_from_dict` 方法中添加解析逻辑（约在第 100 行后）：

```python
# smap_tool and vm_monitor configuration
smap_tool = data.get('smap_tool', {})
vm_monitor = data.get('vm_monitor', {})

return cls(
    # ... existing fields ...

    # smap_tool configuration
    smap_tool_enabled=smap_tool.get('enabled', False),
    smap_tool_path=smap_tool.get('path', ""),
    smap_tool_swap_size=smap_tool.get('swap_size', 81920),
    smap_tool_ratio=smap_tool.get('ratio', 15),
    smap_tool_src_nid=smap_tool.get('src_nid', 2),
    smap_tool_dest_nid=smap_tool.get('dest_nid', 5),

    # vm_monitor configuration
    vm_monitor_enabled=vm_monitor.get('enabled', False),
    vm_monitor_vmm_type=vm_monitor.get('vmm_type', "firecracker"),
    vm_monitor_duration=vm_monitor.get('duration', 600),
    vm_monitor_log_dir=vm_monitor.get('log_dir', "results/e2b/vm_monitor"),
    vm_monitor_stress_file=vm_monitor.get('stress_file', "/dev/shm/e2b_benchmark_lock"),
)
```

- [ ] **Step 3: 在 merge_with_args 和 from_args 中处理新字段**

在 `merge_with_args` 方法中添加（约在第 157 行后）：

```python
# smap_tool and vm_monitor - use yaml values (no CLI override for these)
smap_tool_enabled=yaml_config.smap_tool_enabled,
smap_tool_path=yaml_config.smap_tool_path,
smap_tool_swap_size=yaml_config.smap_tool_swap_size,
smap_tool_ratio=yaml_config.smap_tool_ratio,
smap_tool_src_nid=yaml_config.smap_tool_src_nid,
smap_tool_dest_nid=yaml_config.smap_tool_dest_nid,

vm_monitor_enabled=yaml_config.vm_monitor_enabled,
vm_monitor_vmm_type=yaml_config.vm_monitor_vmm_type,
vm_monitor_duration=yaml_config.vm_monitor_duration,
vm_monitor_log_dir=yaml_config.vm_monitor_log_dir,
vm_monitor_stress_file=yaml_config.vm_monitor_stress_file,
```

在 `from_args` 方法中添加默认值：

```python
smap_tool_enabled=False,
smap_tool_path="",
smap_tool_swap_size=81920,
smap_tool_ratio=15,
smap_tool_src_nid=2,
smap_tool_dest_nid=5,

vm_monitor_enabled=False,
vm_monitor_vmm_type="firecracker",
vm_monitor_duration=600,
vm_monitor_log_dir="results/e2b/vm_monitor",
vm_monitor_stress_file="/dev/shm/e2b_benchmark_lock",
```

- [ ] **Step 4: 验证配置加载正确**

运行测试验证配置字段可以正确解析：

```bash
python -c "from e2b_bench.config import Config; c = Config(); print(f'smap_tool_enabled: {c.smap_tool_enabled}')"
```

Expected: `smap_tool_enabled: False`

- [ ] **Step 5: Commit**

```bash
git add e2b_bench/config.py
git commit -m "feat(e2b_bench): add smap_tool and vm_monitor config fields"
```

---

### Task 2: 实现 SmapToolManager 类 (bench.py)

**Files:**
- Modify: `e2b_bench/bench.py`

- [ ] **Step 1: 在 bench.py 开头添加导入**

在文件开头导入部分添加：

```python
import subprocess
import signal
from pathlib import Path
```

- [ ] **Step 2: 实现 SmapToolManager 类**

在 `bench.py` 中（约在第 20 行后，`print_config` 函数前）添加类定义：

```python
class SmapToolManager:
    """Manage smap_tool process lifecycle for memory migration monitoring"""

    def __init__(self, config):
        self.config = config
        self.process: subprocess.Popen = None
        self.pid: int = None
        self.log_file = None

    def start(self, sandbox_count: int) -> bool:
        """
        Start smap_tool process

        Command format:
        ./smap_tool <count> `pidof firecracker` --swap-size <size> --ratio <ratio> --src-nid <nid> --dest-nid <nid>
        """
        if not self.config.smap_tool_enabled:
            print("[SmapTool] Disabled in config, skipping")
            return True

        if not self.config.smap_tool_path:
            print("[SmapTool] Path not configured, skipping")
            return True

        # Get firecracker PIDs
        try:
            result = subprocess.run(['pidof', 'firecracker'], capture_output=True, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                print("[SmapTool] No firecracker processes found")
                return False
            firecracker_pids = result.stdout.strip()
            print(f"[SmapTool] Found firecracker PIDs: {firecracker_pids}")
        except Exception as e:
            print(f"[SmapTool] Failed to get firecracker PIDs: {e}")
            return False

        # Build command
        smap_dir = Path(self.config.smap_tool_path).parent
        smap_exe = Path(self.config.smap_tool_path).name

        # Clean up existing smap_config
        smap_config_path = Path("/dev/shm/smap_config")
        if smap_config_path.exists():
            smap_config_path.unlink()

        cmd = (
            f"./{smap_exe} {sandbox_count} {firecracker_pids} "
            f"--swap-size {self.config.smap_tool_swap_size} "
            f"--ratio {self.config.smap_tool_ratio} "
            f"--src-nid {self.config.smap_tool_src_nid} "
            f"--dest-nid {self.config.smap_tool_dest_nid}"
        )

        print(f"[SmapTool] Starting: {cmd}")
        print(f"[SmapTool] Working directory: {smap_dir}")

        # Prepare log files
        log_dir = Path(self.config.output_dir) / "smap_tool"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = open(log_dir / f"smap_{timestamp}.log", 'w')

        try:
            self.process = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(smap_dir),
                stdout=self.log_file,
                stderr=self.log_file,
                preexec_fn=os.setpgrp  # Create new process group for clean termination
            )
            self.pid = self.process.pid
            print(f"[SmapTool] Started with PID: {self.pid}")
            return True
        except Exception as e:
            print(f"[SmapTool] Failed to start: {e}")
            return False

    def stop(self) -> None:
        """Stop smap_tool process"""
        if self.process is None:
            return

        print(f"[SmapTool] Stopping process (PID: {self.pid})...")
        try:
            # Send SIGTERM to process group
            os.killpg(os.getpgid(self.pid), signal.SIGTERM)
            self.process.wait(timeout=10)
            print("[SmapTool] Process stopped gracefully")
        except subprocess.TimeoutExpired:
            # Force kill if timeout
            os.killpg(os.getpgid(self.pid), signal.SIGKILL)
            print("[SmapTool] Process killed (timeout)")
        except Exception as e:
            print(f"[SmapTool] Error stopping process: {e}")

        if self.log_file:
            self.log_file.close()

        self.process = None
        self.pid = None

    def is_running(self) -> bool:
        """Check if smap_tool process is still running"""
        if self.process is None:
            return False
        return self.process.poll() is None
```

- [ ] **Step 3: 验证类定义正确**

运行语法检查：

```bash
python -c "from e2b_bench.bench import SmapToolManager; print('SmapToolManager imported successfully')"
```

Expected: `SmapToolManager imported successfully`

- [ ] **Step 4: Commit**

```bash
git add e2b_bench/bench.py
git commit -m "feat(e2b_bench): implement SmapToolManager class"
```

---

### Task 3: 实现 VmMonitorManager 类 (bench.py)

**Files:**
- Modify: `e2b_bench/bench.py`

- [ ] **Step 1: 在 bench.py 开头添加 datetime 导入**

确保 datetime 已导入（如果未导入则添加）：

```python
from datetime import datetime
```

- [ ] **Step 2: 实现 VmMonitorManager 类**

在 `SmapToolManager` 类后添加 `VmMonitorManager` 类：

```python
class VmMonitorManager:
    """Manage vm_monitor process lifecycle for performance monitoring"""

    def __init__(self, config):
        self.config = config
        self.process: subprocess.Popen = None
        self.analysis_file: str = None

    def start(self, task_id: str = "") -> bool:
        """
        Start vm_monitor process with stress-file sync

        Command format:
        python3 vm_monitor/cli.py --vmm firecracker -t <duration> --stress-file <file> --log-dir <dir>
        """
        if not self.config.vm_monitor_enabled:
            print("[VmMonitor] Disabled in config, skipping")
            return True

        # Prepare log directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir_name = f"vm_monitor_{task_id}_{timestamp}" if task_id else f"vm_monitor_{timestamp}"
        log_dir = Path(self.config.vm_monitor_log_dir) / log_dir_name
        log_dir.mkdir(parents=True, exist_ok=True)

        # Clean up existing stress file
        stress_file = Path(self.config.vm_monitor_stress_file)
        if stress_file.exists():
            stress_file.unlink()

        # Build command
        # vm_monitor is in the project root directory
        project_root = Path(__file__).parent.parent
        vm_monitor_cli = project_root / "vm_monitor" / "cli.py"

        cmd = [
            "python3", str(vm_monitor_cli),
            "--vmm", self.config.vm_monitor_vmm_type,
            "-t", str(self.config.vm_monitor_duration),
            "--stress-file", str(stress_file),
            "--log-dir", str(log_dir),
            "--auto-skip"  # Auto skip missing tools for batch testing
        ]

        print(f"[VmMonitor] Starting: {' '.join(cmd)}")
        print(f"[VmMonitor] Log directory: {log_dir}")

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            print(f"[VmMonitor] Started with PID: {self.process.pid}")
            print(f"[VmMonitor] Waiting for stress file: {stress_file}")

            # Store expected analysis file path
            self.analysis_file = str(log_dir / "analysis_report.xlsx")
            return True
        except Exception as e:
            print(f"[VmMonitor] Failed to start: {e}")
            return False

    def trigger_sampling(self) -> None:
        """Create stress file to trigger vm_monitor sampling"""
        stress_file = Path(self.config.vm_monitor_stress_file)
        stress_file.touch()
        print(f"[VmMonitor] Stress file created: {stress_file}")

    def stop_sampling(self) -> None:
        """Remove stress file to stop vm_monitor sampling"""
        stress_file = Path(self.config.vm_monitor_stress_file)
        if stress_file.exists():
            stress_file.unlink()
            print(f"[VmMonitor] Stress file removed: {stress_file}")

    def wait_for_report(self, timeout: int = 300) -> str:
        """
        Wait for analysis_report.xlsx to be generated

        Returns file path if found, None if timeout
        """
        if not self.analysis_file:
            return None

        analysis_path = Path(self.analysis_file)
        print(f"[VmMonitor] Waiting for report: {analysis_path}")

        start_time = time.time()
        while time.time() - start_time < timeout:
            if analysis_path.exists() and analysis_path.stat().st_size > 0:
                print(f"[VmMonitor] Report generated: {analysis_path}")
                return str(analysis_path)
            time.sleep(5)

        print(f"[VmMonitor] Report not found after {timeout}s timeout")
        return None

    def stop(self) -> None:
        """Stop vm_monitor process"""
        if self.process is None:
            return

        print(f"[VmMonitor] Stopping process (PID: {self.process.pid})...")
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
            print("[VmMonitor] Process stopped gracefully")
        except subprocess.TimeoutExpired:
            self.process.kill()
            print("[VmMonitor] Process killed (timeout)")
        except Exception as e:
            print(f"[VmMonitor] Error stopping process: {e}")

        self.process = None
```

- [ ] **Step 3: 验证类定义正确**

运行语法检查：

```bash
python -c "from e2b_bench.bench import VmMonitorManager; print('VmMonitorManager imported successfully')"
```

Expected: `VmMonitorManager imported successfully`

- [ ] **Step 4: Commit**

```bash
git add e2b_bench/bench.py
git commit -m "feat(e2b_bench): implement VmMonitorManager class"
```

---

### Task 4: 创建批量测试配置模板文件

**Files:**
- Create: `config/e2b_batch_template.yaml`
- Create: `config/e2b_batch_matrix.yaml`

- [ ] **Step 1: 创建批量测试模板配置文件**

创建 `config/e2b_batch_template.yaml`：

```yaml
# E2B Batch Test Template Configuration
# Fixed parameters for batch testing (variable parameters in e2b_batch_matrix.yaml)

e2b_env:
  E2B_ACCESS_TOKEN: "your_e2b_access_token_here"
  E2B_API_KEY: "your_e2b_api_key_here"
  E2B_DOMAIN: "e2b.app"
  E2B_API_URL: "http://localhost:3000"
  E2B_HTTP_SSL: "false"

sandbox:
  template: "openclaw-browser-v1"
  create_timeout: 86400
  # total_count will be overridden by test matrix
  detect_existing: false
  create_only: false

create_batch:
  size: 20
  interval: 3

task_batch:
  size: 10
  interval: 5

browser:
  urls:
    - "http://192.168.110.10:8080/Weibo.html"
  task_timeout: 200
  interval_min: 5
  interval_max: 15
  warmup_urls:
    - "http://192.168.110.10:8080/page1.html"
  warmup_loops: 1
  warmup_delay: 2
  warmup_only: false

test:
  duration: 600
  stats_interval: 10
  # benchmark_percent will be overridden by test matrix

# smap_tool configuration (ratio will be overridden by test matrix)
smap_tool:
  enabled: true
  path: "/path/to/smap_tool"  # Change to actual path
  swap_size: 81920
  src_nid: 2
  dest_nid: 5

# vm_monitor configuration
vm_monitor:
  enabled: true
  vmm_type: "firecracker"
  duration: 600
  log_dir: "results/e2b/vm_monitor"
  stress_file: "/dev/shm/e2b_benchmark_lock"

report:
  output_dir: "results/e2b/batch"
  filename_prefix: "e2b_batch"
```

- [ ] **Step 2: 创建测试矩阵配置文件**

创建 `config/e2b_batch_matrix.yaml`：

```yaml
# E2B Batch Test Matrix Configuration
# Variable parameters for batch testing

test_matrix:
  total_counts: [10, 20, 50]
  benchmark_percentages: [0.5, 0.75, 1.0]
  ratios: [10, 20]

# Reuse strategy for sandbox
reuse_strategy:
  reuse_sandbox: true      # Reuse sandbox within same (total_count, ratio) group
  reuse_smap_tool: true    # Reuse smap_tool within same group
```

- [ ] **Step 3: 验证配置文件格式正确**

```bash
python -c "import yaml; yaml.safe_load(open('config/e2b_batch_template.yaml')); yaml.safe_load(open('config/e2b_batch_matrix.yaml')); print('Config files valid')"
```

Expected: `Config files valid`

- [ ] **Step 4: Commit**

```bash
git add config/e2b_batch_template.yaml config/e2b_batch_matrix.yaml
git commit -m "feat(e2b_bench): add batch test configuration templates"
```

---

## Phase 2: 批量调度框架

### Task 5: 新增 BatchTask 和 TaskGroup 数据结构 (schemas.py)

**Files:**
- Modify: `e2b_bench/schemas.py`

- [ ] **Step 1: 在 schemas.py 开头添加导入**

确保 `Optional` 已导入：

```python
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum
import statistics
```

- [ ] **Step 2: 添加 BatchTask 数据类**

在 `TestSnapshot` 类后添加：

```python
@dataclass
class BatchTask:
    """Single batch test task parameters"""
    task_id: str              # Unique ID, e.g. "tc10_ratio10_bp0.5"
    total_count: int          # Sandbox count
    benchmark_percent: float  # Percentage of sandboxes for benchmark
    ratio: int                # Memory migration ratio (%)

    # Runtime state (filled after execution)
    result_dir: Optional[str] = None      # Result directory path
    report_file: Optional[str] = None     # bench_report.txt path
    analysis_file: Optional[str] = None   # analysis_report.xlsx path
    browser_metrics: Optional[Dict[str, Any]] = None  # Extracted browser metrics
    vm_metrics: Optional[Dict[str, Any]] = None       # Extracted vm_monitor metrics
    success: bool = False
    error_msg: Optional[str] = None
```

- [ ] **Step 3: 添加 TaskGroup 数据类**

在 `BatchTask` 后添加：

```python
@dataclass
class TaskGroup:
    """Group of tasks that can reuse the same sandbox set"""
    group_id: str             # Group ID, e.g. "tc10_ratio10"
    total_count: int          # Shared by all tasks in group
    ratio: int                # Shared by all tasks in group
    tasks: List[BatchTask]    # Tasks with different benchmark_percent

    # Runtime state
    sandbox_states: Optional[Dict[int, Any]] = None  # Shared sandbox states
    smap_tool_manager: Optional[Any] = None          # Shared SmapToolManager
```

- [ ] **Step 4: 验证数据类定义正确**

```bash
python -c "from e2b_bench.schemas import BatchTask, TaskGroup; t = BatchTask('test', 10, 0.5, 15); print(f'BatchTask created: {t.task_id}')"
```

Expected: `BatchTask created: test`

- [ ] **Step 5: Commit**

```bash
git add e2b_bench/schemas.py
git commit -m "feat(e2b_bench): add BatchTask and TaskGroup data structures"
```

---

### Task 6: 实现 TaskGenerator 类 (task_generator.py)

**Files:**
- Create: `e2b_bench/task_generator.py`

- [ ] **Step 1: 创建 task_generator.py 文件**

```python
"""
Task Generator Module

Generates TaskGroups and BatchTasks from test matrix configuration.
Groups tasks by (total_count, ratio) for sandbox reuse.
"""

from typing import List, Dict, Any
from .schemas import BatchTask, TaskGroup


class TaskGenerator:
    """Generate test tasks from parameter matrix"""

    def __init__(self, matrix_config: Dict[str, Any]):
        """
        Initialize TaskGenerator with matrix configuration

        Args:
            matrix_config: Dict containing test_matrix and reuse_strategy
        """
        self.matrix = matrix_config.get('test_matrix', {})
        self.reuse_strategy = matrix_config.get('reuse_strategy', {})

        self.total_counts = self.matrix.get('total_counts', [10])
        self.benchmark_percentages = self.matrix.get('benchmark_percentages', [1.0])
        self.ratios = self.matrix.get('ratios', [15])

    def generate_groups(self) -> List[TaskGroup]:
        """
        Generate TaskGroups grouped by (total_count, ratio)

        Tasks in same group can reuse sandbox and smap_tool.

        Returns:
            List of TaskGroup objects
        """
        groups = []

        for total_count in self.total_counts:
            for ratio in self.ratios:
                group_id = f"tc{total_count}_ratio{ratio}"

                # Generate all tasks for this group
                tasks = []
                for bp in self.benchmark_percentages:
                    task_id = f"{group_id}_bp{bp}"
                    task = BatchTask(
                        task_id=task_id,
                        total_count=total_count,
                        benchmark_percent=bp,
                        ratio=ratio
                    )
                    tasks.append(task)

                group = TaskGroup(
                    group_id=group_id,
                    total_count=total_count,
                    ratio=ratio,
                    tasks=tasks
                )
                groups.append(group)

        return groups

    def get_total_task_count(self) -> int:
        """Calculate total number of tasks across all groups"""
        return len(self.total_counts) * len(self.ratios) * len(self.benchmark_percentages)

    def get_group_count(self) -> int:
        """Calculate number of groups"""
        return len(self.total_counts) * len(self.ratios)


def load_matrix_config(path: str) -> Dict[str, Any]:
    """Load matrix configuration from YAML file"""
    import yaml
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)
```

- [ ] **Step 2: 验证 TaskGenerator 功能正确**

```bash
python -c "
from e2b_bench.task_generator import TaskGenerator, load_matrix_config
matrix = {'test_matrix': {'total_counts': [10, 20], 'benchmark_percentages': [0.5, 1.0], 'ratios': [10, 20]}}
tg = TaskGenerator(matrix)
groups = tg.generate_groups()
print(f'Groups: {len(groups)}, Tasks: {tg.get_total_task_count()}')
for g in groups[:2]:
    print(f'  Group {g.group_id}: {len(g.tasks)} tasks')
"
```

Expected:
```
Groups: 4, Tasks: 8
  Group tc10_ratio10: 2 tasks
  Group tc10_ratio20: 2 tasks
```

- [ ] **Step 3: Commit**

```bash
git add e2b_bench/task_generator.py
git commit -m "feat(e2b_bench): implement TaskGenerator class"
```

---

### Task 7: 实现 MetricsExtractor 类 (metrics_extractor.py)

**Files:**
- Create: `e2b_bench/metrics_extractor.py`

- [ ] **Step 1: 创建 metrics_extractor.py 文件**

参考现有 `batch_test_scheduler.py` 的提取逻辑，创建模块化的指标提取器：

```python
"""
Metrics Extractor Module

Extracts performance metrics from vm_monitor analysis_report.xlsx.
Supports all sheet types: Summary, DevKit_TopDown, DevKit_Memory,
NUMA_Bandwidth, KSys, UBWatch_Latency, UBWatch_Bandwidth, SMAPBW, Getfre.
"""

import os
import re
from typing import Dict, Any, Optional
from pathlib import Path


class MetricsExtractor:
    """Extract metrics from vm_monitor analysis_report.xlsx"""

    def __init__(self):
        pass

    def extract(self, analysis_file: str) -> Dict[str, Any]:
        """
        Extract all metrics from analysis_report.xlsx

        Args:
            analysis_file: Path to analysis_report.xlsx

        Returns:
            Dict containing all extracted metrics with prefixed keys
        """
        if not os.path.exists(analysis_file):
            print(f"[MetricsExtractor] File not found: {analysis_file}")
            return {}

        metrics = {}

        try:
            import pandas as pd
            xls = pd.ExcelFile(analysis_file)

            # Extract from each sheet
            metrics.update(self._extract_summary(xls))
            metrics.update(self._extract_devkit_topdown(xls))
            metrics.update(self._extract_devkit_memory(xls))
            metrics.update(self._extract_numa_bandwidth(xls))
            metrics.update(self._extract_ksys(xls))
            metrics.update(self._extract_ubwatch_latency(xls))
            metrics.update(self._extract_ubwatch_bandwidth(xls))
            metrics.update(self._extract_smapbw_summary(xls))
            metrics.update(self._extract_smapbw_cycles(xls))
            metrics.update(self._extract_getfre(xls))

            print(f"[MetricsExtractor] Extracted {len(metrics)} metrics from {analysis_file}")

        except Exception as e:
            print(f"[MetricsExtractor] Error extracting metrics: {e}")

        return metrics

    def _extract_summary(self, xls) -> Dict[str, Any]:
        """Extract Summary sheet metrics (VM CPU)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="Summary")
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric == "VM Avg CPU":
                    metrics["VM_CPU_Mean"] = self._to_float(value)
                elif metric == "VM Peak Total CPU":
                    metrics["VM_CPU_Max"] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_devkit_topdown(self, xls) -> Dict[str, Any]:
        """Extract DevKit_TopDown sheet (13 metrics)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="DevKit_TopDown")
            key_map = {
                "Cycles Avg": "DevKit_TopDown_Cycles_Avg",
                "Instructions Avg": "DevKit_TopDown_Instructions_Avg",
                "IPC Avg": "DevKit_TopDown_IPC_Avg",
                "IPC Max": "DevKit_TopDown_IPC_Max",
                "IPC Min": "DevKit_TopDown_IPC_Min",
                "Bad Speculation (%)": "DevKit_TopDown_Bad_Speculation",
                "Frontend Bound (%)": "DevKit_TopDown_Frontend_Bound",
                "Retiring (%)": "DevKit_TopDown_Retiring",
                "Backend Bound (%)": "DevKit_TopDown_Backend_Bound",
                "L3 Bound (%)": "DevKit_TopDown_L3_Bound",
                "Mem Bound (%)": "DevKit_TopDown_Mem_Bound",
                "Latency Bound (%)": "DevKit_TopDown_Latency_Bound",
                "Bandwidth Bound (%)": "DevKit_TopDown_Bandwidth_Bound",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_devkit_memory(self, xls) -> Dict[str, Any]:
        """Extract DevKit_Memory sheet (6 metrics + per-NUMA L3 hit rate)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="DevKit_Memory")
            key_map = {
                "L1D Miss (%)": "DevKit_Memory_L1D_Miss",
                "L1I Miss (%)": "DevKit_Memory_L1I_Miss",
                "L2D Miss (%)": "DevKit_Memory_L2D_Miss",
                "L2I Miss (%)": "DevKit_Memory_L2I_Miss",
                "DDR Read (MB/s)": "DevKit_Memory_DDR_Read",
                "DDR Write (MB/s)": "DevKit_Memory_DDR_Write",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
                elif "L3 Hit Rate" in metric:
                    numa_match = re.match(r"NUMA(\d+)\s+L3 Hit Rate", metric)
                    if numa_match:
                        node_id = numa_match.group(1)
                        metrics[f"DevKit_Memory_NUMA{node_id}_L3_Hit_Rate"] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_numa_bandwidth(self, xls) -> Dict[str, Any]:
        """Extract NUMA_Bandwidth sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="NUMA_Bandwidth")
            total_read = 0.0
            total_write = 0.0
            for idx, row in df.iterrows():
                node = str(row.get("NUMA Node", "")).strip()
                read = self._to_float(row.get("Read (MB/s)", 0))
                write = self._to_float(row.get("Write (MB/s)", 0))
                if node:
                    metrics[f"NUMA_Bandwidth_{node}_Read"] = read
                    metrics[f"NUMA_Bandwidth_{node}_Write"] = write
                    total_read += read
                    total_write += write
            metrics["NUMA_Bandwidth_Total_Read"] = total_read
            metrics["NUMA_Bandwidth_Total_Write"] = total_write
        except Exception:
            pass
        return metrics

    def _extract_ksys(self, xls) -> Dict[str, Any]:
        """Extract KSys sheet (11 metrics)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="KSys")
            key_map = {
                "L2 Miss Latency Max": "KSys_L2_Miss_Latency_Max",
                "L2 Miss Latency Min": "KSys_L2_Miss_Latency_Min",
                "L2 Miss Latency Avg": "KSys_L2_Miss_Latency_Avg",
                "L3 Miss Latency Max": "KSys_L3_Miss_Latency_Max",
                "L3 Miss Latency Min": "KSys_L3_Miss_Latency_Min",
                "L3 Miss Latency Avg": "KSys_L3_Miss_Latency_Avg",
                "IPC": "KSys_IPC",
                "Retiring (%)": "KSys_Retiring",
                "Frontend Bound (%)": "KSys_Frontend_Bound",
                "Bad Speculation (%)": "KSys_Bad_Speculation",
                "Backend Bound (%)": "KSys_Backend_Bound",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_ubwatch_latency(self, xls) -> Dict[str, Any]:
        """Extract UBWatch_Latency sheet (7 metrics)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="UBWatch_Latency")
            key_map = {
                "Samples": "UBWatch_Latency_Samples",
                "Avg Read (ns)": "UBWatch_Latency_Avg_Read_ns",
                "Avg Write (ns)": "UBWatch_Latency_Avg_Write_ns",
                "Min Read (ns)": "UBWatch_Latency_Min_Read_ns",
                "Min Write (ns)": "UBWatch_Latency_Min_Write_ns",
                "Max Read (ns)": "UBWatch_Latency_Max_Read_ns",
                "Max Write (ns)": "UBWatch_Latency_Max_Write_ns",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    try:
                        metrics[key_map[metric]] = float(value) if value is not None else 0
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
        return metrics

    def _extract_ubwatch_bandwidth(self, xls) -> Dict[str, Any]:
        """Extract UBWatch_Bandwidth sheet (dynamic per-chip per-port)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="UBWatch_Bandwidth")
            total_avg_wr = 0.0
            total_avg_rd = 0.0
            total_avg_sum = 0.0

            for idx, row in df.iterrows():
                chip = int(row.get("Chip", -1)) if row.get("Chip") is not None else None
                ports = str(row.get("Ports", "")).strip()
                if chip is not None and chip >= 0 and ports:
                    port_key = "p" + ports.replace("&", "")

                    avg_wr = self._to_float(row.get("Avg Write (MB/s)", 0))
                    avg_rd = self._to_float(row.get("Avg Read (MB/s)", 0))
                    avg_sum = self._to_float(row.get("Avg Sum (MB/s)", 0))

                    key_prefix = f"UBWatch_Bandwidth_Chip{chip}_{port_key}"
                    metrics[f"{key_prefix}_Avg_Write"] = avg_wr
                    metrics[f"{key_prefix}_Avg_Read"] = avg_rd
                    metrics[f"{key_prefix}_Avg_Sum"] = avg_sum

                    total_avg_wr += avg_wr
                    total_avg_rd += avg_rd
                    total_avg_sum += avg_sum

            metrics["UBWatch_Bandwidth_Total_Avg_Write"] = total_avg_wr
            metrics["UBWatch_Bandwidth_Total_Avg_Read"] = total_avg_rd
            metrics["UBWatch_Bandwidth_Total_Avg_Sum"] = total_avg_sum
        except Exception:
            pass
        return metrics

    def _extract_smapbw_summary(self, xls) -> Dict[str, Any]:
        """Extract SMAPBW_Summary sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="SMAPBW_Summary")
            key_map = {
                "Total Cycles": "SMAPBW_Total_Cycles",
                "Total Pages": "SMAPBW_Total_Pages",
                "Avg Bandwidth (GB/s)": "SMAPBW_Avg_Bandwidth_GB_s",
                "Min Bandwidth (GB/s)": "SMAPBW_Min_Bandwidth_GB_s",
                "Max Bandwidth (GB/s)": "SMAPBW_Max_Bandwidth_GB_s",
            }
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                if metric in key_map:
                    metrics[key_map[metric]] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_smapbw_cycles(self, xls) -> Dict[str, Any]:
        """Extract SMAPBW_Cycles sheet"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="SMAPBW_Cycles")
            for idx, row in df.iterrows():
                metric = str(row.get("Metric", "")).strip()
                value = row.get("Value")
                # Store all cycle metrics with prefix
                if metric and pd.notna(value):
                    key = f"SMAPBW_Cycles_{metric.replace(' ', '_').replace('(', '').replace(')', '')}"
                    metrics[key] = self._to_float(value)
        except Exception:
            pass
        return metrics

    def _extract_getfre(self, xls) -> Dict[str, Any]:
        """Extract Getfre_Summary sheet (per-NUMA CoreFreq)"""
        metrics = {}
        try:
            import pandas as pd
            df = pd.read_excel(xls, sheet_name="Getfre_Summary")
            for idx, row in df.iterrows():
                numa = str(row.get("NUMA", "")).strip()
                core_freq = self._to_float(row.get("CoreFreq (MHz)", 0))
                if numa:
                    metrics[f"Getfre_{numa}_CoreFreq_MHz"] = core_freq
        except Exception:
            pass
        return metrics

    def _to_float(self, value: Any) -> float:
        """Convert value to float, handling percentage strings"""
        if value is None:
            return 0.0
        try:
            if isinstance(value, str):
                # Handle percentage strings like "45.2%"
                if '%' in value:
                    return float(value.replace('%', '').strip())
                return float(value.strip())
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    def extract_browser_metrics(self, report_file: str) -> Dict[str, Any]:
        """Extract browser metrics from bench_report.txt"""
        metrics = {}
        if not report_file or not os.path.exists(report_file):
            return metrics

        try:
            with open(report_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # Parse metrics using regex
            match = re.search(r"Success Rate:\s+([\d.]+)%", content)
            if match:
                metrics["Browser_Success_Rate"] = float(match.group(1))

            match = re.search(r"Avg Latency:\s+([\d.]+)ms", content)
            if match:
                metrics["Browser_Avg_Latency_ms"] = float(match.group(1))

            match = re.search(r"P99 Latency:\s+([\d.]+)ms", content)
            if match:
                metrics["Browser_P99_Latency_ms"] = float(match.group(1))

            match = re.search(r"Total Tasks:\s+(\d+)", content)
            if match:
                metrics["Browser_Total_Tasks"] = int(match.group(1))

        except Exception as e:
            print(f"[MetricsExtractor] Error extracting browser metrics: {e}")

        return metrics
```

- [ ] **Step 2: 验证 MetricsExtractor 类导入正确**

```bash
python -c "from e2b_bench.metrics_extractor import MetricsExtractor; me = MetricsExtractor(); print('MetricsExtractor imported successfully')"
```

Expected: `MetricsExtractor imported successfully`

- [ ] **Step 3: Commit**

```bash
git add e2b_bench/metrics_extractor.py
git commit -m "feat(e2b_bench): implement MetricsExtractor class"
```

---

### Task 8: 实现 ReportAggregator 类 (report_aggregator.py)

**Files:**
- Create: `e2b_bench/report_aggregator.py`

- [ ] **Step 1: 创建 report_aggregator.py 文件**

```python
"""
Report Aggregator Module

Aggregates metrics from multiple batch test tasks into a single Excel report.
Supports styled output with data source grouping and color coding.
"""

import os
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path


class ReportAggregator:
    """Aggregate batch test results into Excel report"""

    # Column groupings for data source headers
    COLUMN_GROUPS = {
        'Basic': ['task_id', 'total_count', 'ratio', 'benchmark_percent'],
        'Browser': ['Browser_Success_Rate', 'Browser_Avg_Latency_ms', 'Browser_P99_Latency_ms', 'Browser_Total_Tasks'],
        'VM_CPU': ['VM_CPU_Mean', 'VM_CPU_Max'],
        'DevKit_TopDown': [  # 13 metrics
            'DevKit_TopDown_Cycles_Avg', 'DevKit_TopDown_Instructions_Avg',
            'DevKit_TopDown_IPC_Avg', 'DevKit_TopDown_IPC_Max', 'DevKit_TopDown_IPC_Min',
            'DevKit_TopDown_Bad_Speculation', 'DevKit_TopDown_Frontend_Bound',
            'DevKit_TopDown_Retiring', 'DevKit_TopDown_Backend_Bound',
            'DevKit_TopDown_L3_Bound', 'DevKit_TopDown_Mem_Bound',
            'DevKit_TopDown_Latency_Bound', 'DevKit_TopDown_Bandwidth_Bound',
        ],
        'DevKit_Memory': [
            'DevKit_Memory_L1D_Miss', 'DevKit_Memory_L1I_Miss',
            'DevKit_Memory_L2D_Miss', 'DevKit_Memory_L2I_Miss',
            'DevKit_Memory_DDR_Read', 'DevKit_Memory_DDR_Write',
        ],
    }

    # Colors for different data sources
    SOURCE_COLORS = {
        'Basic': '#FFFFFF',      # White
        'Browser': '#E3F2FD',    # Light Blue
        'VM_CPU': '#E8F5E9',     # Light Green
        'DevKit_TopDown': '#FFF3E0',  # Light Orange
        'DevKit_Memory': '#FCE4EC',   # Light Pink
        'NUMA_Bandwidth': '#F3E5F5',  # Light Purple
        'KSys': '#E0F7FA',       # Light Cyan
        'UBWatch_Latency': '#FFF8E1',  # Light Yellow
        'UBWatch_Bandwidth': '#EFEBE9',  # Light Brown
        'SMAPBW': '#E8EAF6',     # Light Indigo
        'Getfre': '#FBE9E7',     # Light Deep Orange
    }

    def __init__(self, output_dir: str = "results/e2b/batch"):
        self.output_dir = output_dir

    def aggregate(self, metrics_data: List[Dict[str, Any]], output_filename: str = None) -> str:
        """
        Aggregate all test metrics into Excel report

        Args:
            metrics_data: List of dicts, each containing task_id and metrics
            output_filename: Optional custom filename

        Returns:
            Path to generated Excel file
        """
        if not metrics_data:
            print("[ReportAggregator] No metrics data to aggregate")
            return ""

        import pandas as pd

        # Build DataFrame
        df = self._build_dataframe(metrics_data)

        # Sort by parameters
        df = df.sort_values(by=['total_count', 'ratio', 'benchmark_percent'])

        # Generate output path
        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = output_filename or f"e2b_batch_summary_{timestamp}.xlsx"
        output_path = os.path.join(self.output_dir, filename)

        # Export with styling
        self._export_excel(df, output_path)

        print(f"[ReportAggregator] Report saved to: {output_path}")
        return output_path

    def _build_dataframe(self, metrics_data: List[Dict[str, Any]]) -> pd.DataFrame:
        """Build DataFrame from metrics list"""
        import pandas as pd

        rows = []
        for m in metrics_data:
            row = {
                # Basic info (required)
                'task_id': m.get('task_id', ''),
                'total_count': m.get('total_count', 0),
                'ratio': m.get('ratio', 0),
                'benchmark_percent': m.get('benchmark_percent', 0),
            }

            # Add all metrics dynamically
            for key, value in m.items():
                if key not in row and key not in ['success', 'error_msg', 'result_dir']:
                    row[key] = value

            rows.append(row)

        return pd.DataFrame(rows)

    def _export_excel(self, df: pd.DataFrame, output_path: str) -> None:
        """Export DataFrame to styled Excel"""
        import pandas as pd

        # Use xlsxwriter for styling
        with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name='Summary', index=False, startrow=1)

            workbook = writer.book
            worksheet = writer.sheets['Summary']

            # Add header row with data source labels
            header_format = workbook.add_format({
                'bold': True,
                'align': 'center',
                'valign': 'vcenter',
                'border': 1
            })

            # Write column headers
            for col_idx, col_name in enumerate(df.columns):
                worksheet.write(0, col_idx + 1, col_name, header_format)

            # Apply colors to columns based on data source
            for source, columns in self.COLUMN_GROUPS.items():
                color = self.SOURCE_COLORS.get(source, '#FFFFFF')
                cell_format = workbook.add_format({'bg_color': color})

                for col_name in columns:
                    if col_name in df.columns:
                        col_idx = df.columns.get_loc(col_name)
                        # Apply format to entire column (rows 1 to end)
                        worksheet.set_column(col_idx + 1, col_idx + 1, None, cell_format)

            # Auto-adjust column widths
            for col_idx, col_name in enumerate(df.columns):
                max_len = max(
                    len(str(col_name)),
                    df[col_name].astype(str).str.len().max() if len(df) > 0 else 0
                )
                worksheet.set_column(col_idx + 1, col_idx + 1, min(max_len + 2, 30))
```

- [ ] **Step 2: 验证 ReportAggregator 类导入正确**

```bash
python -c "from e2b_bench.report_aggregator import ReportAggregator; ra = ReportAggregator(); print('ReportAggregator imported successfully')"
```

Expected: `ReportAggregator imported successfully`

- [ ] **Step 3: Commit**

```bash
git add e2b_bench/report_aggregator.py
git commit -m "feat(e2b_bench): implement ReportAggregator class"
```

---

### Task 9: 实现 BatchScheduler 和 GroupRunner 类 (batch_scheduler.py)

**Files:**
- Create: `e2b_bench/batch_scheduler.py`

- [ ] **Step 1: 创建 batch_scheduler.py 文件**

```python
"""
Batch Test Scheduler Module

Orchestrates batch testing with sandbox reuse strategy.
Groups tasks by (total_count, ratio) and reuses sandbox/smap_tool within groups.
"""

import os
import sys
import time
import argparse
import yaml
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from .config import Config
from .schemas import BatchTask, TaskGroup, SandboxState, SandboxStatus
from .task_generator import TaskGenerator, load_matrix_config
from .metrics_extractor import MetricsExtractor
from .report_aggregator import ReportAggregator
from .sandbox_manager import SandboxManager
from .task_runner import TaskManager
from .stats_collector import StatsCollector
from .bench import SmapToolManager, VmMonitorManager, print_config


class GroupRunner:
    """Run a single TaskGroup with sandbox reuse"""

    def __init__(self, group: TaskGroup, config: Config, batch_log_file: str):
        self.group = group
        self.config = config
        self.batch_log_file = batch_log_file

        # Runtime managers (shared within group)
        self.sandbox_manager: Optional[SandboxManager] = None
        self.smap_tool: Optional[SmapToolManager] = None
        self.sandbox_states: Dict[int, SandboxState] = {}

        # Stop event
        self.stop_event = None

    def run(self) -> List[BatchTask]:
        """
        Execute all tasks in the group

        Flow:
        1. Create sandboxes (shared)
        2. Start smap_tool (shared)
        3. Warmup (shared, once)
        4. For each task:
           - Start vm_monitor
           - Run benchmark
           - Stop vm_monitor
           - Collect results
        5. Cleanup

        Returns:
            List of completed BatchTask objects
        """
        import threading

        results = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._log(f"\n[{timestamp}] Starting group: {self.group.group_id}")
        self._log(f"  Total count: {self.group.total_count}")
        self._log(f"  Ratio: {self.group.ratio}")
        self._log(f"  Tasks: {len(self.group.tasks)}")

        print(f"\n{'='*60}")
        print(f"Group: {self.group.group_id}")
        print(f"{'='*60}")

        try:
            # 1. Create sandboxes
            self.stop_event = threading.Event()
            self.sandbox_manager = SandboxManager(self._get_group_config(), self.stop_event)

            print(f"\n[Phase 1] Creating {self.group.total_count} sandboxes...")
            self.sandbox_states = self.sandbox_manager.create_all()

            ready_count = sum(
                1 for s in self.sandbox_states.values()
                if s.creation_metrics.status == SandboxStatus.PORT_READY
            )
            if ready_count == 0:
                self._log(f"  ERROR: No sandboxes ready")
                for task in self.group.tasks:
                    task.success = False
                    task.error_msg = "No sandboxes ready"
                return self.group.tasks

            self._log(f"  Sandboxes ready: {ready_count}")

            # 2. Start smap_tool
            if self.config.smap_tool_enabled:
                self.smap_tool = SmapToolManager(self._get_group_config())
                success = self.smap_tool.start(ready_count)
                if not success:
                    self._log(f"  WARN: smap_tool failed to start")

            # 3. Warmup (shared, once)
            if self.config.warmup_urls:
                print(f"\n[Phase 2] Running warmup...")
                task_manager = TaskManager(self._get_group_config(), self.sandbox_states, self.stop_event)
                task_manager.start_warmup()
                task_manager.wait_warmup()
                self._log(f"  Warmup completed")

            # 4. Run each task with different benchmark_percent
            for idx, task in enumerate(self.group.tasks):
                print(f"\n[Phase 3.{idx+1}] Task: {task.task_id}")
                self._log(f"\n  Task {idx+1}/{len(self.group.tasks)}: {task.task_id}")
                self._log(f"    benchmark_percent: {task.benchmark_percent}")

                task_result = self._run_single_task(task, idx)
                results.append(task_result)

            # Mark all tasks in group
            for task in self.group.tasks:
                if task not in results:
                    results.append(task)

        except Exception as e:
            self._log(f"  ERROR: Group execution failed: {e}")
            for task in self.group.tasks:
                task.success = False
                task.error_msg = str(e)

        finally:
            # 5. Cleanup
            self._cleanup()

        return results

    def _run_single_task(self, task: BatchTask, task_idx: int) -> BatchTask:
        """Run a single benchmark task"""
        import threading

        # Create result directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_dir = Path(self.config.output_dir) / f"{task.task_id}_{timestamp}"
        result_dir.mkdir(parents=True, exist_ok=True)
        task.result_dir = str(result_dir)

        # Update config for this task
        task_config = self._get_task_config(task)

        # Start vm_monitor
        vm_monitor = None
        if self.config.vm_monitor_enabled:
            vm_monitor = VmMonitorManager(task_config)
            vm_monitor.start(task.task_id)
            time.sleep(2)  # Wait for vm_monitor to initialize

        # Create stop event for this task
        stop_event = threading.Event()

        # Start stats collector
        stats_collector = StatsCollector(task_config, self.sandbox_states)
        stats_collector.start()

        # Start task manager
        task_manager = TaskManager(task_config, self.sandbox_states, stop_event)

        # Trigger vm_monitor sampling
        if vm_monitor:
            vm_monitor.trigger_sampling()

        # Start browser tasks
        print(f"  Starting browser tasks (benchmark_percent={task.benchmark_percent})...")
        task_manager.start_all()

        # Run for duration
        print(f"  Running for {self.config.test_duration}s...")
        time.sleep(self.config.test_duration)

        # Stop
        stop_event.set()
        task_manager.wait_all(timeout=5)
        stats_collector.stop()

        # Stop vm_monitor sampling
        if vm_monitor:
            vm_monitor.stop_sampling()

        # Generate bench report
        report = stats_collector.generate_report()
        report_file = result_dir / "bench_report.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        task.report_file = str(report_file)

        # Wait for vm_monitor analysis report
        if vm_monitor:
            analysis_file = vm_monitor.wait_for_report(timeout=300)
            if analysis_file:
                task.analysis_file = analysis_file
            vm_monitor.stop()

        # Mark success
        task.success = True
        self._log(f"    Task completed successfully")

        return task

    def _get_group_config(self) -> Config:
        """Create Config for group (with group's total_count and ratio)"""
        # Create a copy of config with group's parameters
        config_dict = {
            'sandbox': {
                'total_count': self.group.total_count,
            },
            'smap_tool': {
                'ratio': self.group.ratio,
            },
            'test': {
                'benchmark_percent': 1.0,  # Will be overridden per task
            }
        }

        # Merge with base config
        # For simplicity, modify existing config
        group_config = Config(
            **{k: v for k, v in self.config.__dict__.items()},
        )
        group_config.total_count = self.group.total_count
        group_config.smap_tool_ratio = self.group.ratio

        return group_config

    def _get_task_config(self, task: BatchTask) -> Config:
        """Create Config for specific task"""
        task_config = self._get_group_config()
        task_config.benchmark_percent = task.benchmark_percent
        task_config.vm_monitor_duration = self.config.test_duration
        return task_config

    def _cleanup(self):
        """Cleanup: stop smap_tool, kill sandboxes"""
        print(f"\n[Cleanup] Group: {self.group.group_id}")

        if self.smap_tool:
            self.smap_tool.stop()
            self._log("  smap_tool stopped")

        if self.sandbox_manager:
            self.sandbox_manager.kill_all()
            self._log("  Sandboxes killed")

        if self.stop_event:
            self.stop_event.set()

    def _log(self, message: str):
        """Write to batch log file"""
        with open(self.batch_log_file, 'a', encoding='utf-8') as f:
            f.write(message + '\n')


class BatchScheduler:
    """Main batch test scheduler"""

    def __init__(self, matrix_path: str, template_path: str, output_dir: str = "results/e2b/batch"):
        self.matrix_path = matrix_path
        self.template_path = template_path
        self.output_dir = output_dir

        # Load configurations
        self.matrix_config = load_matrix_config(matrix_path)
        self.template_config = Config.load_from_yaml(template_path)

        # Apply output_dir override
        if output_dir:
            self.template_config.output_dir = output_dir

        # Initialize components
        self.task_generator = TaskGenerator(self.matrix_config)
        self.metrics_extractor = MetricsExtractor()
        self.report_aggregator = ReportAggregator(output_dir)

        # Batch log file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.batch_log_file = os.path.join(output_dir, f"batch_log_{timestamp}.txt")
        os.makedirs(output_dir, exist_ok=True)

    def run(self, continue_on_failure: bool = True) -> str:
        """
        Execute all batch tests

        Args:
            continue_on_failure: Continue testing if a group fails

        Returns:
            Path to summary report Excel file
        """
        print("\n" + "="*80)
        print("E2B Batch Test Scheduler")
        print("="*80)

        # Print configuration
        print(f"\nMatrix: {self.matrix_path}")
        print(f"Template: {self.template_path}")
        print(f"Output: {self.output_dir}")

        groups = self.task_generator.generate_groups()
        print(f"\nGroups: {len(groups)}")
        print(f"Total tasks: {self.task_generator.get_total_task_count()}")

        # Setup E2B environment
        self.template_config.setup_e2b_env()

        # Execute each group
        all_results: List[BatchTask] = []

        for idx, group in enumerate(groups):
            print(f"\n{'='*80}")
            print(f"Group {idx+1}/{len(groups)}: {group.group_id}")
            print(f"{'='*80}")

            runner = GroupRunner(group, self.template_config, self.batch_log_file)
            results = runner.run()
            all_results.extend(results)

            # Check for failures
            failed = [t for t in results if not t.success]
            if failed and not continue_on_failure:
                print(f"\nGroup failed, stopping (continue_on_failure=False)")
                break

        # Extract metrics
        print(f"\n{'='*80}")
        print("Extracting metrics...")
        print(f"{'='*80}")

        metrics_data = []
        for task in all_results:
            metrics = {
                'task_id': task.task_id,
                'total_count': task.total_count,
                'ratio': task.ratio,
                'benchmark_percent': task.benchmark_percent,
                'success': task.success,
                'error_msg': task.error_msg,
            }

            # Extract browser metrics
            if task.report_file:
                browser_metrics = self.metrics_extractor.extract_browser_metrics(task.report_file)
                metrics.update(browser_metrics)
                task.browser_metrics = browser_metrics

            # Extract vm_monitor metrics
            if task.analysis_file:
                vm_metrics = self.metrics_extractor.extract(task.analysis_file)
                metrics.update(vm_metrics)
                task.vm_metrics = vm_metrics

            metrics_data.append(metrics)

        # Aggregate results
        print(f"\n{'='*80}")
        print("Generating summary report...")
        print(f"{'='*80}")

        report_path = self.report_aggregator.aggregate(metrics_data)

        # Print final summary
        success_count = sum(1 for t in all_results if t.success)
        failed_count = len(all_results) - success_count

        print(f"\n{'='*80}")
        print("Batch Test Complete")
        print(f"{'='*80}")
        print(f"  Total tasks: {len(all_results)}")
        print(f"  Successful: {success_count}")
        print(f"  Failed: {failed_count}")
        print(f"  Summary report: {report_path}")

        return report_path


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(
        description='E2B Batch Test Scheduler - Automated batch testing with sandbox reuse'
    )

    parser.add_argument('--matrix', required=True, help='Test matrix YAML config path')
    parser.add_argument('--template', required=True, help='Template YAML config path')
    parser.add_argument('--output-dir', default='results/e2b/batch', help='Output directory')
    parser.add_argument('--continue-on-failure', action='store_true',
                        help='Continue testing if a group fails')

    return parser


def main():
    """CLI entry point"""
    parser = build_arg_parser()
    args = parser.parse_args()

    scheduler = BatchScheduler(
        matrix_path=args.matrix,
        template_path=args.template,
        output_dir=args.output_dir
    )

    report_path = scheduler.run(continue_on_failure=args.continue_on_failure)

    print(f"\nDone. Report: {report_path}")


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 创建 __main__.py 入口**

修改 `e2b_bench/__main__.py` 以支持 batch 模式：

```python
"""
E2B Bench Module Entry Point

Supports both single benchmark and batch test modes.
"""

import sys
import argparse


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='E2B Bench Module')

    # Mode selection
    parser.add_argument('--batch', action='store_true', help='Run batch test scheduler')

    # Parse known args to get mode
    args, remaining = parser.parse_known_args()

    if args.batch:
        # Batch mode: delegate to batch_scheduler
        from .batch_scheduler import build_arg_parser, BatchScheduler

        batch_parser = build_arg_parser()
        batch_args = batch_parser.parse_args(remaining)

        scheduler = BatchScheduler(
            matrix_path=batch_args.matrix,
            template_path=batch_args.template,
            output_dir=batch_args.output_dir
        )

        report_path = scheduler.run(continue_on_failure=batch_args.continue_on_failure)
        print(f"\nDone. Report: {report_path}")
    else:
        # Single benchmark mode
        from .bench import main as bench_main
        bench_main()


if __name__ == '__main__':
    main()
```

- [ ] **Step 3: 验证批量调度器导入正确**

```bash
python -c "from e2b_bench.batch_scheduler import BatchScheduler, GroupRunner; print('BatchScheduler imported successfully')"
```

Expected: `BatchScheduler imported successfully`

- [ ] **Step 4: Commit**

```bash
git add e2b_bench/batch_scheduler.py e2b_bench/__main__.py
git commit -m "feat(e2b_bench): implement BatchScheduler and GroupRunner classes"
```

---

## Phase 3: 测试与文档

### Task 10: 添加模块导出

**Files:**
- Modify: `e2b_bench/__init__.py`

- [ ] **Step 1: 更新 __init__.py 导出**

```python
"""
E2B Bench - E2B Sandbox Batch Performance Testing Tool

Provides:
- Single sandbox benchmark (bench.py)
- Batch test scheduler with sandbox reuse (batch_scheduler.py)
- Metrics extraction from vm_monitor (metrics_extractor.py)
- Report aggregation (report_aggregator.py)
"""

from .config import Config
from .schemas import SandboxState, SandboxStatus, BatchTask, TaskGroup
from .bench import run_benchmark, SmapToolManager, VmMonitorManager
from .batch_scheduler import BatchScheduler, GroupRunner
from .task_generator import TaskGenerator, load_matrix_config
from .metrics_extractor import MetricsExtractor
from .report_aggregator import ReportAggregator

__all__ = [
    'Config',
    'SandboxState', 'SandboxStatus', 'BatchTask', 'TaskGroup',
    'run_benchmark', 'SmapToolManager', 'VmMonitorManager',
    'BatchScheduler', 'GroupRunner',
    'TaskGenerator', 'load_matrix_config',
    'MetricsExtractor',
    'ReportAggregator',
]
```

- [ ] **Step 2: 验证所有导出正确**

```bash
python -c "from e2b_bench import BatchScheduler, MetricsExtractor, ReportAggregator; print('All imports successful')"
```

Expected: `All imports successful`

- [ ] **Step 3: Commit**

```bash
git add e2b_bench/__init__.py
git commit -m "feat(e2b_bench): update module exports for batch testing"
```

---

### Task 11: 使用文档

**Files:**
- Create: `docs/e2b-batch-usage.md`

- [ ] **Step 1: 创建使用文档**

```markdown
# E2B Batch Test Scheduler 使用指南

## 快速开始

### 1. 准备配置文件

#### 测试矩阵配置 (`config/e2b_batch_matrix.yaml`)

定义可变参数维度：

```yaml
test_matrix:
  total_counts: [10, 20, 50]       # 沙箱数量
  benchmark_percentages: [0.5, 0.75, 1.0]  # 压测比例
  ratios: [10, 20]                 # 内存迁移比例

reuse_strategy:
  reuse_sandbox: true
  reuse_smap_tool: true
```

#### 模板配置 (`config/e2b_batch_template.yaml`)

定义固定参数（E2B 凭证、浏览器 URL 等）：

```yaml
e2b_env:
  E2B_ACCESS_TOKEN: "your_token"
  ...

smap_tool:
  enabled: true
  path: "/path/to/smap_tool"
  src_nid: 2
  dest_nid: 5

vm_monitor:
  enabled: true
  vmm_type: "firecracker"
```

### 2. 执行批量测试

```bash
# 单次基准测试
python -m e2b_bench --config config/e2b_bench.yaml

# 批量测试
python -m e2b_bench --batch \
    --matrix config/e2b_batch_matrix.yaml \
    --template config/e2b_batch_template.yaml \
    --output-dir results/e2b/batch
```

### 3. 查看结果

批量测试完成后，会在 `results/e2b/batch/` 目录生成：

- `e2b_batch_summary_YYYYMMDD_HHMMSS.xlsx` - 汇总报告
- `batch_log_YYYYMMDD_HHMMSS.txt` - 执行日志
- `<task_id>/` - 每个测试任务的详细结果目录

## 沙箱复用策略

批量测试按 `(total_count, ratio)` 分组：
- 同组内沙箱和 smap_tool 复用
- 不同 `benchmark_percent` 的测试在同一批沙箱上依次执行

示例分组：
```
Group tc10_ratio10: [bp0.5, bp0.75, bp1.0]  ← 复用 10 个沙箱
Group tc10_ratio20: [bp0.5, bp0.75, bp1.0]  ← 新建 10 个沙箱
Group tc20_ratio10: [bp0.5, bp0.75, bp1.0]  ← 新建 20 个沙箱
```

## 测试流程

每组测试流程：
```
1. 创建沙箱 (total_count 个)
2. 启动 smap_tool (ratio 参数)
3. 预热 (warmup_urls)
4. 遍历 benchmark_percent:
   - 启动 vm_monitor (--stress-file 同步)
   - 压测
   - 停止 vm_monitor
5. 停止 smap_tool
6. 销毁沙箱
```

## 指标提取

从 `analysis_report.xlsx` 提取的指标：

| 数据源 | Sheet | 指标数 |
|--------|-------|--------|
| VM CPU | Summary | 2 |
| DevKit TopDown | DevKit_TopDown | 13 |
| DevKit Memory | DevKit_Memory | 6+ |
| NUMA Bandwidth | NUMA_Bandwidth | per-node |
| KSys | KSys | 11 |
| UBWatch Latency | UBWatch_Latency | 7 |
| UBWatch Bandwidth | UBWatch_Bandwidth | per-chip+port |
| SMAPBW | SMAPBW_Summary | 5 |
```

- [ ] **Step 2: Commit**

```bash
git add docs/e2b-batch-usage.md
git commit -m "docs: add e2b batch test usage guide"
```

---

## 实施完成检查

- [ ] **最终验证：运行语法检查**

```bash
python -m py_compile e2b_bench/batch_scheduler.py
python -m py_compile e2b_bench/task_generator.py
python -m py_compile e2b_bench/metrics_extractor.py
python -m py_compile e2b_bench/report_aggregator.py
echo "All files compile successfully"
```

- [ ] **最终 Commit**

```bash
git add -A
git commit -m "feat(e2b_bench): complete batch automated test implementation"
```
