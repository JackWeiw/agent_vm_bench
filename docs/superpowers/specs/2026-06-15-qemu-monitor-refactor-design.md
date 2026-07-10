# QEMU Monitor 模块化重构设计文档

**日期**: 2026-06-15
**版本**: 1.0.0
**状态**: 设计待审批

---

## 概述

### 背景
当前 `qemu_monitor.py` 单文件已达 **3300行代码**，从工程化角度存在以下问题：
- 代码量过大，新人不易理解和上手
- 职责混杂（配置管理、日志收集、解析、导出、监控、CLI）
- 修改某个功能需要阅读整个大文件
- 难以单独测试各个模块

### 目标
**主要目标**：提高代码可维护性，让代码更容易理解、修改和上手

**次要目标**：
- 保持向后兼容（所有现有用法无需修改）
- 清晰的模块边界和职责划分
- 符合Python工程化最佳实践

### 成功标准
- ✅ 拆分为5-7个模块，每个模块 < 900行
- ✅ 所有现有命令行用法正常工作
- ✅ 所有导入路径兼容（包导入 + 模块导入）
- ✅ 功能测试100%通过
- ✅ 循环依赖正确处理

---

## 设计方案

### 架构决策：方案1（5模块拆分）

**决策依据**：
- 5模块是平衡点，既满足"工程化"又不过度复杂
- 每个模块职责清晰，符合单一职责原则
- parsers模块集中管理6个解析器，比过度拆分更实用
- 与现有代码结构匹配度高，迁移风险最低

**模块结构**：

```
qemu_monitor/                    # 新建的Python包目录
│
├── __init__.py                  # 包入口，导出所有公开API（~50行）
│
├── config.py                    # 配置管理模块（~280行）
│   ├── load_env_config()
│   ├── save_env_config()
│   ├── validate_and_prompt_missing()
│   ├── calculate_cpu_range_from_numa()
│   ├── numa_to_physical_cores()
│   ├── load_getfre_config()
│   └── 常量: ENV_FILE_PATH, ENV_REQUIRED_KEYS
│
├── log_capture.py               # 并行日志收集模块（~640行）
│   ├── class LogCapture
│   │   ├── __init__()
│   │   ├── start()
│   │   ├── stop()
│   │   ├── wait()
│   │   ├── get_results()
│   │   ├── _start_devkit_mem()
│   │   ├── _start_ksys()
│   │   ├── _start_ub_watch()
│   │   ├── _start_smap_bw()
│   │   ├── _start_getfre()
│   │   └── _getfre_collector_thread()
│   └── DEFAULT_TOOL_TIMEOUTS
│
├── parsers.py                   # 日志解析模块（~720行）
│   ├── parse_devkit_top_down()
│   ├── parse_ksys()
│   ├── parse_devkit_mem()
│   ├── parse_getfre()
│   ├── parse_ub_watch()
│   ├── parse_smap_bw()
│   └── parse_all_logs()
│
├── exporters.py                 # 数据导出模块（~500行）
│   ├── export_to_excel()
│   ├── print_capture_summary()
│   └── openpyxl图表生成逻辑
│
├── monitor.py                   # QEMU监控核心模块（~860行）
│   ├── class QEMUMonitor
│   │   ├── __init__()
│   │   ├── collect_sample()
│   │   ├── get_qemu_vms_realtime()
│   │   ├── display_realtime_table()
│   │   ├── start_monitoring()
│   │   ├── wait_for_stress_and_monitor()
│   │   ├── analyze_and_export()
│   │   ├── get_numa_nodes_memory()
│   │   ├── collect_hugepage_stats()
│   │   ├── collect_numa_cpu()
│   │   ├── collect_host_stats()
│   │   ├── collect_swap_stats()
│   │   └── export/统计方法
│
└── cli.py                       # 命令行入口模块（~100行）
    ├── main()
    └── argparse参数解析

qemu_monitor.py                  # 保留的兼容入口（~15行）
```

---

## 模块依赖设计

### 依赖图

```
config.py (底层，无依赖)
    ↓
parsers.py (依赖config)
    ↓
exporters.py (依赖parsers, monitor类型作为参数)
    ↓
log_capture.py (依赖config)
    ↓
monitor.py (依赖config, exporters延迟导入)
    ↓
cli.py (顶层，依赖所有模块)
```

### 导入设计

#### config.py
```python
# 仅依赖标准库和第三方库
import os
import yaml  # 可选导入
from dotenv import load_dotenv, set_key  # 可选导入
```

#### parsers.py
```python
from .config import numa_to_physical_cores
```

#### exporters.py
```python
from .parsers import parse_all_logs
# monitor类型作为参数传入，避免循环依赖
# 使用TYPE_CHECKING和字符串标注
```

#### log_capture.py
```python
from .config import (
    load_getfre_config,
    numa_to_physical_cores,
    calculate_cpu_range_from_numa
)
```

#### monitor.py
```python
from .config import ...  # 如果需要
# exporters延迟导入（在analyze_and_export方法内）
```

#### cli.py
```python
from .config import load_env_config, validate_and_prompt_missing
from .log_capture import LogCapture
from .monitor import QEMUMonitor
from .exporters import export_to_excel, print_capture_summary
```

### 循环依赖处理

**潜在风险**：`monitor.py` ↔ `exporters.py`

**解决方案**：
1. **延迟导入**：monitor在方法内部导入exporters
2. **类型标注**：exporters使用字符串标注（`'QEMUMonitor'`）或`typing.Any`
3. **依赖注入**：exporters的函数接受monitor实例作为参数

---

## 向后兼容设计

### 兼容入口文件

```python
# qemu_monitor.py（根目录）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QEMU Virtual Machine Real-time Monitoring Tool
向后兼容入口 - 所有功能已迁移至 qemu_monitor/ 包

用法保持不变:
  python qemu_monitor.py -t 600 -i 2
  python qemu_monitor.py --stress-file /tmp/bench_running.lock
"""

from qemu_monitor.cli import main

if __name__ == '__main__':
    main()
```

### 导入路径兼容

**支持三种导入方式**：

```python
# 方式1：从包导入（推荐）
from qemu_monitor import QEMUMonitor, LogCapture

# 方式2：从模块导入（兼容）
from qemu_monitor.monitor import QEMUMonitor
from qemu_monitor.parsers import parse_devkit_top_down

# 方式3：运行根文件（完全向后兼容）
python qemu_monitor.py -t 60  # 依然有效
```

### __init__.py 导出API

```python
from .monitor import QEMUMonitor
from .log_capture import LogCapture
from .config import (
    load_env_config,
    save_env_config,
    validate_and_prompt_missing,
    load_getfre_config,
)
from .parsers import (
    parse_devkit_top_down,
    parse_ksys,
    parse_devkit_mem,
    parse_getfre,
    parse_ub_watch,
    parse_smap_bw,
    parse_all_logs,
)
from .exporters import (
    export_to_excel,
    print_capture_summary,
)

__all__ = [
    'QEMUMonitor',
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

## 工程化最佳实践

### 类型标注（Type Hints）

所有公开函数添加完整类型标注：

```python
def numa_to_physical_cores(
    numa_nodes: List[int],
    core_interval: int = 1
) -> Dict[int, List[int]]:
    """将NUMA节点ID转换为物理核心列表"""
    ...

def export_to_excel(
    monitor: 'QEMUMonitor',  # 字符串标注避免循环依赖
    log_dir: str,
    numa_nodes: Optional[List[int]] = None,
    output_file: Optional[str] = None,
    capture_results: Optional[Dict] = None
) -> Optional[str]:
    """导出监控数据到Excel"""
    ...
```

### 模块文档标准

每个模块顶部包含：
- 模块描述和主要功能
- 使用示例
- 依赖模块说明
- 作者和版本信息

每个类和函数包含：
- 功能描述
- 参数说明（Args）
- 返回值说明（Returns）
- 异常说明（Raises）
- 使用示例（Example）

### 错误处理分级

- **Level 1（致命）**：导入失败、配置缺失 → 抛出异常或退出
- **Level 2（警告）**：单个工具启动失败、解析失败 → 记录日志继续
- **Level 3（信息）**：正常操作 → INFO级别日志

### 可选导入处理

```python
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    # 不影响核心功能，仅Excel导出降级

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

try:
    from dotenv import load_dotenv, set_key
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False
```

---

## 测试验证计划

### 功能测试矩阵

| 测试场景 | 命令 | 验证模块 |
|----------|------|----------|
| 基础监控 | `python qemu_monitor.py -t 10 -i 2` | monitor.py |
| 压测同步 | `python qemu_monitor.py --stress-file /tmp/test.lock -t 30` | monitor.py |
| 日志收集 | `python qemu_monitor.py -t 30 --enable-capture` | log_capture.py |
| Excel导出 | 检查生成 `analysis_report.xlsx` | exporters.py |
| 解析器独立 | 单独测试各解析函数 | parsers.py |
| 包导入 | `python -c "from qemu_monitor import QEMUMonitor"` | __init__.py |
| 模块导入 | `python -c "from qemu_monitor.monitor import QEMUMonitor"` | 相对导入 |

### 向后兼容性测试

```python
#!/usr/bin/env python3
"""测试向后兼容性"""

# 测试1：根文件作为入口
import subprocess
result = subprocess.run(['python', 'qemu_monitor.py', '-t', '5', '-i', '1'],
                       capture_output=True, text=True)
assert result.returncode == 0, "根文件入口失败"

# 测试2：包导入
from qemu_monitor import QEMUMonitor, LogCapture
assert QEMUMonitor is not None, "包导入QEMUMonitor失败"
assert LogCapture is not None, "包导入LogCapture失败"

# 测试3：模块导入
from qemu_monitor.monitor import QEMUMonitor as Monitor2
from qemu_monitor.parsers import parse_devkit_top_down
assert Monitor2 is not None, "模块导入失败"
assert parse_devkit_top_down is not None, "解析器导入失败"

print("✅ 所有向后兼容性测试通过")
```

---

## 迁移检查清单

| 检查项 | 原代码位置 | 新模块位置 | 迁移动作 |
|--------|------------|------------|----------|
| 全局常量 | 行46-49 | config.py顶部 | 复制常量定义 |
| 可选导入 | 行24-43 | 各模块顶部 | 分散到使用模块 |
| LogCapture类 | 行331-969 | log_capture.py | 完整迁移，添加相对导入 |
| 6个解析函数 | 行973-1684 | parsers.py | 完整迁移，调整导入 |
| export_to_excel | 行1688-2186 | exporters.py | 完整迁移，类型标注改为字符串 |
| QEMUMonitor类 | 行2352-3207 | monitor.py | 完整迁移，延迟导入exporters |
| main()函数 | 行3209-3310 | cli.py | 完整迁移，导入改为相对导入 |

---

## 文件变更清单

### 新建文件

1. `qemu_monitor/__init__.py` - 包入口
2. `qemu_monitor/config.py` - 配置管理
3. `qemu_monitor/log_capture.py` - 日志收集
4. `qemu_monitor/parsers.py` - 日志解析
5. `qemu_monitor/exporters.py` - 数据导出
6. `qemu_monitor/monitor.py` - 监控核心
7. `qemu_monitor/cli.py` - 命令行入口

### 修改文件

1. `qemu_monitor.py` - 缩减为15行兼容入口（保留向后兼容）
2. `requirements.txt` - 补充可选依赖标注（可选）

### 保留文件（不修改）

1. `.env` - 配置文件
2. `getfre_config.yaml` - getfre配置
3. 其他项目文件

---

## 风险与对策

### 风险1：循环依赖导致启动失败
**对策**：延迟导入 + 类型字符串标注 + 依赖注入

### 风险2：相对导入在Python 3.7以下版本失败
**对策**：项目要求Python >= 3.7（setup.py标注）

### 风险3：模块拆分后功能遗漏
**对策**：
- 逐模块迁移，每个模块完成后运行测试矩阵
- 完整的功能测试矩阵覆盖所有场景
- 向后兼容性测试确保导入路径正确

### 风险4：可选导入缺失导致功能降级不明显
**对策**：
- 每个模块独立处理可选导入
- 功能降级时打印警告信息
- Excel导出失败时明确提示需要pandas

---

## 实施计划

### Phase 1：基础结构搭建（预估30分钟）
- 创建 `qemu_monitor/` 目录
- 创建 `__init__.py`（空文件，后续填充）
- 创建各模块空文件

### Phase 2：底层模块迁移（预估1小时）
- 迁移 `config.py`（无依赖，可独立测试）
- 迁移 `parsers.py`（仅依赖config）
- 验证解析器独立功能

### Phase 3：中间层模块迁移（预估1小时）
- 迁移 `log_capture.py`
- 迁移 `exporters.py`（处理循环依赖）
- 验证导出功能

### Phase 4：核心模块迁移（预估1小时）
- 迁移 `monitor.py`（延迟导入exporters）
- 迁移 `cli.py`
- 填充 `__init__.py` 导出API

### Phase 5：兼容入口与测试（预估30分钟）
- 修改根目录 `qemu_monitor.py` 为兼容入口
- 运行完整测试矩阵
- 向后兼容性测试

### Phase 6：工程化完善（预估30分钟）
- 补充类型标注
- 补充模块文档
- 补充 `requirements.txt`
- （可选）添加 `setup.py`

---

## 附录：requirements.txt 补充建议

```txt
# 核心依赖（必需）
psutil>=5.8.0

# 配置管理（可选）
python-dotenv>=0.19.0  # .env文件支持
PyYAML>=5.4.0          # getfre_config.yaml支持

# 数据导出（可选）
pandas>=1.3.0          # Excel导出
openpyxl>=3.0.0        # Excel图表

# 测试依赖（开发）
pytest>=6.0            # 单元测试
```

---

## 审批检查清单

- [ ] 模块结构合理（5-7模块，每个 < 900行）
- [ ] 模块依赖清晰（无循环依赖或已处理）
- [ ] 向后兼容设计完整（三种导入方式）
- [ ] 测试验证矩阵覆盖关键场景
- [ ] 工程化实践符合标准（类型标注、文档）
- [ ] 风险对策合理可行
- [ ] 实施计划时间预估合理

---

**设计状态**: 待用户审批
**下一步**: 用户审批后调用 `writing-plans` 技能创建详细实现计划
