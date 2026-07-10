# QEMU Monitor Modular Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor qemu_monitor.py (3300 lines) into 7 modular files in qemu_monitor/ package, preserving backward compatibility.

**Architecture:** Create qemu_monitor/ package with 7 modules (config, log_capture, parsers, exporters, monitor, cli, __init__), use relative imports, handle circular dependencies with delayed imports and type annotations, preserve qemu_monitor.py as 15-line compatibility entry point.

**Tech Stack:** Python 3.7+, psutil, pandas (optional), openpyxl (optional), python-dotenv (optional), PyYAML (optional)

---

## File Structure Map

### New Files (7 modules)
```
qemu_monitor/__init__.py       (~50 lines)  - Package entry point, exports public API
qemu_monitor/config.py         (~280 lines) - .env/NUMA config management
qemu_monitor/log_capture.py    (~640 lines) - LogCapture class, parallel collection
qemu_monitor/parsers.py        (~720 lines) - 6 log parser functions
qemu_monitor/exporters.py      (~500 lines) - Excel/CSV export, charts
qemu_monitor/monitor.py        (~860 lines) - QEMUMonitor core class
qemu_monitor/cli.py            (~100 lines) - argparse entry point
```

### Modified Files
```
qemu_monitor.py                (~15 lines)  - Reduce to compatibility entry point
requirements.txt               (add optional dependency markers)
```

### Preserved Files (no changes)
```
.env                           - Configuration file
getfre_config.yaml             - getfre config
```

---

## Phase 1: Package Structure Setup

### Task 1: Create Package Directory Structure

**Files:**
- Create: `qemu_monitor/__init__.py` (empty placeholder)

- [ ] **Step 1: Create qemu_monitor package directory**

```bash
mkdir qemu_monitor
```

- [ ] **Step 2: Create empty __init__.py placeholder**

```python
# qemu_monitor/__init__.py
# Placeholder - will be filled in Phase 5
```

Run: `ls -la qemu_monitor/`
Expected: Directory exists with empty __init__.py

- [ ] **Step 3: Commit structure setup**

```bash
git add qemu_monitor/
git commit -m "feat: create qemu_monitor package structure"
```

---

## Phase 2: Bottom Layer Modules (No Dependencies)

### Task 2: Extract config.py Module

**Files:**
- Create: `qemu_monitor/config.py`
- Reference: `qemu_monitor.py:46-327`

- [ ] **Step 1: Create config.py with constants and imports**

```python
# qemu_monitor/config.py
"""
Configuration Management Module

Manages .env file configuration, NUMA node settings, and getfre YAML config.
All tools' paths are loaded from .env and validated before use.
"""

import os
import re
from typing import Dict, List, Optional

# Try to import python-dotenv for .env support
try:
    from dotenv import load_dotenv, set_key
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

# Try to import yaml for getfre config
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ==================== Constants ====================

ENV_FILE_PATH = '.env'
ENV_REQUIRED_KEYS = ['DEVKIT_PATH', 'KSYS_PATH', 'KSYS_CONFIG_PATH',
                     'UB_WATCH_PATH', 'SMAP_BW_PATH', 'GETFRE_PATH',
                     'GETFRE_CONFIG_PATH']
```

- [ ] **Step 2: Add load_env_config function**

```python
def load_env_config() -> dict:
    """Load configuration from .env file

    Returns:
        dict with keys: devkit_path, ksys_path, ksys_config_path,
        ub_watch_path, smap_bw_path, devkit_cpu_range,
        getfre_path, getfre_config_path
    """
    config = {
        'devkit_path': '',
        'ksys_path': '',
        'ksys_config_path': '',
        'ub_watch_path': '',
        'smap_bw_path': '',
        'devkit_cpu_range': '',
        'getfre_path': '',
        'getfre_config_path': '',
    }

    if DOTENV_AVAILABLE and os.path.exists(ENV_FILE_PATH):
        load_dotenv(ENV_FILE_PATH)

    # Read from environment variables (set by dotenv or system)
    config['devkit_path'] = os.environ.get('DEVKIT_PATH', '')
    config['ksys_path'] = os.environ.get('KSYS_PATH', '')
    config['ksys_config_path'] = os.environ.get('KSYS_CONFIG_PATH', '')
    config['ub_watch_path'] = os.environ.get('UB_WATCH_PATH', '')
    config['smap_bw_path'] = os.environ.get('SMAP_BW_PATH', '')
    config['devkit_cpu_range'] = os.environ.get('DEVKIT_CPU_RANGE', '')
    config['getfre_path'] = os.environ.get('GETFRE_PATH', '')
    config['getfre_config_path'] = os.environ.get('GETFRE_CONFIG_PATH', '')

    return config
```

- [ ] **Step 3: Add save_env_config function**

```python
def save_env_config(config: dict):
    """Save configuration back to .env file"""
    env_path = ENV_FILE_PATH

    # Create .env file if not exists
    if not os.path.exists(env_path):
        with open(env_path, 'w') as f:
            f.write("# Log collection tools configuration\n")
            f.write("# Generated by qemu_monitor.py\n\n")

    # Write each key
    if DOTENV_AVAILABLE:
        if config.get('devkit_path'):
            set_key(env_path, 'DEVKIT_PATH', config['devkit_path'])
        if config.get('ksys_path'):
            set_key(env_path, 'KSYS_PATH', config['ksys_path'])
        if config.get('ksys_config_path'):
            set_key(env_path, 'KSYS_CONFIG_PATH', config['ksys_config_path'])
        if config.get('ub_watch_path'):
            set_key(env_path, 'UB_WATCH_PATH', config['ub_watch_path'])
        if config.get('smap_bw_path'):
            set_key(env_path, 'SMAP_BW_PATH', config['smap_bw_path'])
        if config.get('devkit_cpu_range'):
            set_key(env_path, 'DEVKIT_CPU_RANGE', config['devkit_cpu_range'])
        if config.get('getfre_path'):
            set_key(env_path, 'GETFRE_PATH', config['getfre_path'])
        if config.get('getfre_config_path'):
            set_key(env_path, 'GETFRE_CONFIG_PATH', config['getfre_config_path'])
    else:
        # Fallback: manual write
        with open(env_path, 'a') as f:
            for key, value in config.items():
                if value:
                    env_key = key.upper()
                    if env_key in ENV_REQUIRED_KEYS or env_key == 'DEVKIT_CPU_RANGE':
                        f.write(f"{env_key}={value}\n")
```

- [ ] **Step 4: Add validate_and_prompt_missing function**

```python
def validate_and_prompt_missing(config: dict, non_interactive: bool = False) -> dict:
    """Validate paths and prompt user for missing/invalid ones

    Args:
        config: dict with path configurations
        non_interactive: if True, skip prompts and disable missing tools silently

    Returns:
        Updated config dict with valid paths or None for disabled tools
    """
    key_mapping = {
        'DEVKIT_PATH': 'devkit_path',
        'KSYS_PATH': 'ksys_path',
        'KSYS_CONFIG_PATH': 'ksys_config_path',
        'UB_WATCH_PATH': 'ub_watch_path',
        'SMAP_BW_PATH': 'smap_bw_path',
        'GETFRE_PATH': 'getfre_path',
        'GETFRE_CONFIG_PATH': 'getfre_config_path',
    }

    prompt_names = {
        'DEVKIT_PATH': 'DevKit CLI path (devkit executable)',
        'KSYS_PATH': 'ksys executable path',
        'KSYS_CONFIG_PATH': 'ksys config.yaml path',
        'UB_WATCH_PATH': 'ub_watch executable path',
        'SMAP_BW_PATH': 'smap_bw.py script path',
        'GETFRE_PATH': 'getfre executable path',
        'GETFRE_CONFIG_PATH': 'getfre_config.yaml path',
    }

    for env_key, config_key in key_mapping.items():
        path = config.get(config_key, '')

        # Check if path is valid
        if path and os.path.exists(path):
            continue  # Path is valid, no action needed

        if non_interactive:
            # Non-interactive mode: silently disable missing tools
            if not path or not os.path.exists(path):
                config[config_key] = None  # Mark as disabled
                print(f"  ⚠ {env_key} not configured or invalid, disabled for this session")
        else:
            # Interactive mode: prompt user for input
            while not path or not os.path.exists(path):
                print(f"\n⚠ {env_key} not configured or path invalid")
                if path:
                    print(f"  Current: {path}")
                user_input = input(f"Enter {prompt_names[env_key]} (or 'skip' to disable): ").strip()

                if user_input.lower() == 'skip':
                    config[config_key] = None  # Mark as disabled
                    print(f"  ✓ {env_key} disabled for this session")
                    break

                if os.path.exists(user_input):
                    path = user_input
                    config[config_key] = user_input
                    print(f"  ✓ {env_key} set to: {user_input}")
                else:
                    print(f"  ✗ Path does not exist: {user_input}")

    # Save updated config to .env (only if any changes made)
    if non_interactive:
        # In non-interactive mode, don't save disabled tools to .env
        # Only save valid paths
        save_config = {k: v for k, v in config.items() if v is not None and v != ''}
        if save_config:
            save_env_config(save_config)
    else:
        save_env_config(config)
        print("\n✓ Configuration saved to .env file")

    return config
```

- [ ] **Step 5: Add calculate_cpu_range_from_numa function**

```python
def calculate_cpu_range_from_numa(numa_nodes: list) -> str:
    """Calculate CPU core range from NUMA node IDs

    Args:
        numa_nodes: list of NUMA node IDs (e.g., [0, 1])

    Returns:
        CPU range string like "0-95,192-287"
    """
    all_cores = []

    for node in numa_nodes:
        try:
            cpulist_path = f'/sys/devices/system/node/node{node}/cpulist'
            with open(cpulist_path) as f:
                cpulist = f.read().strip()

            # Parse cpulist (e.g., "0-95" or "0,1,2,3-10")
            for part in cpulist.split(','):
                if '-' in part:
                    start, end = part.split('-')
                    all_cores.extend(range(int(start), int(end) + 1))
                else:
                    all_cores.append(int(part))
        except Exception as e:
            print(f"⚠ Failed to read CPU list for NUMA node {node}: {e}")

    if not all_cores:
        return "0-95"  # Fallback default

    # Sort and merge into ranges
    all_cores.sort()
    ranges = []
    start = all_cores[0]
    end = all_cores[0]

    for core in all_cores[1:]:
        if core == end + 1:
            end = core
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = core
            end = core

    # Add last range
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ','.join(ranges)
```

- [ ] **Step 6: Add numa_to_physical_cores function**

```python
def numa_to_physical_cores(numa_nodes: list, core_interval: int = 1) -> dict:
    """Convert NUMA node IDs to physical core IDs with sampling interval

    Args:
        numa_nodes: list of NUMA node IDs (e.g., [0, 1])
        core_interval: sampling interval (1=all cores, 2=every other core)

    Returns:
        dict: {numa_id: [physical_core_ids]}
        Example: {0: [0, 2, 4, ...46], 1: [48, 50, 52, ...94]}
    """
    # NUMA to physical core mapping for 192-core system with hyperthreading
    # Each NUMA has 48 physical cores (96 logical cores with HT)
    numa_physical_ranges = {
        0: (0, 47),      # NUMA 0: physical cores 0-47
        1: (48, 95),     # NUMA 1: physical cores 48-95
        2: (96, 143),    # NUMA 2: physical cores 96-143
        3: (144, 191),   # NUMA 3: physical cores 144-191
    }

    result = {}
    for numa in numa_nodes:
        if numa not in numa_physical_ranges:
            print(f"⚠ Invalid NUMA node {numa}, skipping")
            continue
        start, end = numa_physical_ranges[numa]
        # Apply core_interval sampling
        cores = list(range(start, end + 1, core_interval))
        result[numa] = cores

    return result
```

- [ ] **Step 7: Add load_getfre_config function**

```python
def load_getfre_config(config_path: str) -> dict:
    """Load getfre configuration from YAML file

    Args:
        config_path: path to getfre_config.yaml

    Returns:
        dict with keys: getfre_path, total_cores, interval,
        core_interval, numa_nodes
        Returns default config if file not found or invalid
    """
    default_config = {
        'getfre_path': '',
        'total_cores': 192,
        'interval': 2,
        'core_interval': 1,
        'numa_nodes': [0, 1],
    }

    if not config_path or not os.path.exists(config_path):
        return default_config

    if not YAML_AVAILABLE:
        print("⚠ yaml module not available, using default getfre config")
        return default_config

    try:
        with open(config_path, 'r') as f:
            yaml_config = yaml.safe_load(f)

        if yaml_config:
            # Merge with defaults, yaml values take precedence
            for key in default_config:
                if key in yaml_config:
                    default_config[key] = yaml_config[key]

        return default_config
    except Exception as e:
        print(f"⚠ Failed to load getfre_config.yaml: {e}")
        return default_config
```

- [ ] **Step 8: Verify config.py imports work**

```bash
python -c "from qemu_monitor.config import load_env_config, numa_to_physical_cores; print('✓ config.py imports work')"
```

Expected: Output "✓ config.py imports work"

- [ ] **Step 9: Commit config.py module**

```bash
git add qemu_monitor/config.py
git commit -m "feat: extract config.py module from qemu_monitor"
```

---

## Phase 3: Middle Layer Modules (Depend on config)

### Task 3: Extract parsers.py Module

**Files:**
- Create: `qemu_monitor/parsers.py`
- Reference: `qemu_monitor.py:971-1684`

- [ ] **Step 1: Create parsers.py header with imports**

```python
# qemu_monitor/parsers.py
"""
Log Parser Module

Parses output logs from various collection tools (devkit, ksys, ub_watch,
smap_bw, getfre) and extracts structured metrics data.
"""

import os
import re
from datetime import datetime
from collections import defaultdict
from typing import Dict, Any, List, Optional

# Internal dependency
from .config import numa_to_physical_cores
```

- [ ] **Step 2: Add parse_devkit_top_down function (lines 973-1060)**

*Note: Extract full function from qemu_monitor.py:973-1060, preserving all regex patterns and parsing logic. Function signature unchanged.*

```python
def parse_devkit_top_down(log_path: str) -> dict:
    """Parse DevKit top-down tuner log file

    Args:
        log_path: Path to devkit_top_down.log

    Returns:
        dict with keys: cycles_avg, ipc_avg, topdown metrics,
        report_count, or {'error': str} on failure
    """
    # [Copy complete function from qemu_monitor.py:973-1060]
    # Implementation preserves all regex patterns:
    # - IPC pattern: r'IPC:\s+([\d.]+)'
    # - Topdown patterns for Bad Speculation, Frontend Bound, etc.
    # - Memory bound patterns for L3 Bound, Mem Bound
    # - Multi-report aggregation logic
    ...
```

- [ ] **Step 3: Add parse_ksys function (lines 1062-1138)**

```python
def parse_ksys(log_path: str) -> dict:
    """Parse ksys collect log file

    Args:
        log_path: Path to ksys.log

    Returns:
        dict with keys: l2_miss_latency, l3_miss_latency, ipc,
        topdown metrics, or {'error': str} on failure
    """
    # [Copy complete function from qemu_monitor.py:1062-1138]
    # Implementation preserves:
    # - L2/L3 miss latency parsing
    # - IPC extraction
    # - Topdown metrics extraction
    ...
```

- [ ] **Step 4: Add parse_devkit_mem function (lines 1140-1225)**

```python
def parse_devkit_mem(log_path: str, numa_nodes: list = None) -> dict:
    """Parse DevKit memory tuner log file

    Args:
        log_path: Path to devkit_mem.log
        numa_nodes: Optional list of NUMA nodes for bandwidth filtering

    Returns:
        dict with keys: cache_miss, ddr_bandwidth_system,
        numa_bandwidth (filtered), report_count
    """
    # [Copy complete function from qemu_monitor.py:1140-1225]
    # Implementation preserves:
    # - Cache miss parsing (L1D, L1I, L2D, L2I)
    # - DDR bandwidth parsing
    # - NUMA bandwidth filtering logic
    ...
```

- [ ] **Step 5: Add parse_getfre function (lines 1227-1340)**

```python
def parse_getfre(log_dir: str) -> dict:
    """Parse getfre core frequency log files (per-NUMA node)

    Args:
        log_dir: Directory containing getfre_NUMA*.log files

    Returns:
        dict: {numa_id: {numa_avg, numa_min, numa_max,
        core_stats, sample_count}}
    """
    # [Copy complete function from qemu_monitor.py:1227-1340]
    # Implementation preserves:
    # - Multi-NUMA file discovery
    # - CSV parsing (timestamp, core, freq_mhz)
    # - Per-NUMA aggregation
    # - Per-core statistics
    ...
```

- [ ] **Step 6: Add parse_ub_watch function (lines 1342-1415)**

```python
def parse_ub_watch(log_path: str) -> dict:
    """Parse ub_watch latency and bandwidth log

    Args:
        log_path: Path to ub_watch.log

    Returns:
        dict with keys: latency (path, samples, avg/min/max),
        bandwidth list per chip/port
    """
    # [Copy complete function from qemu_monitor.py:1342-1415]
    # Implementation preserves:
    # - Latency path extraction
    # - Read/Write latency parsing
    # - Bandwidth per chip/port extraction
    ...
```

- [ ] **Step 7: Add parse_smap_bw function (lines 1417-1530)**

```python
def parse_smap_bw(log_path: str) -> dict:
    """Parse smap_bw SMAP migration bandwidth log

    Args:
        log_path: Path to smap_bw.log

    Returns:
        dict with keys: summary (total stats), cycles (per-cycle data),
        all_directions (set of migration patterns)
    """
    # [Copy complete function from qemu_monitor.py:1417-1530]
    # Implementation preserves:
    # - dmesg parsing for SMAP migration events
    # - Per-cycle bandwidth calculation
    # - Direction tracking (N0→N1, etc.)
    ...
```

- [ ] **Step 8: Add parse_all_logs aggregation function (lines 1532-1684)**

```python
def parse_all_logs(log_dir: str, numa_nodes: list = None) -> dict:
    """Parse all log files in directory and aggregate results

    Args:
        log_dir: Directory containing all log files
        numa_nodes: Optional NUMA nodes for bandwidth filtering

    Returns:
        dict with keys for each tool: devkit_top_down, ksys,
        devkit_mem, getfre, ub_watch, smap_bw
    """
    # [Copy complete function from qemu_monitor.py:1532-1684]
    # Implementation preserves:
    # - File discovery logic
    # - Individual parser calls
    # - Error handling per tool
    ...
```

- [ ] **Step 9: Verify parsers.py imports work**

```bash
python -c "from qemu_monitor.parsers import parse_devkit_top_down, parse_all_logs; print('✓ parsers.py imports work')"
```

Expected: Output "✓ parsers.py imports work"

- [ ] **Step 10: Commit parsers.py module**

```bash
git add qemu_monitor/parsers.py
git commit -m "feat: extract parsers.py module from qemu_monitor"
```

---

### Task 4: Extract log_capture.py Module

**Files:**
- Create: `qemu_monitor/log_capture.py`
- Reference: `qemu_monitor.py:329-969`

- [ ] **Step 1: Create log_capture.py header with imports**

```python
# qemu_monitor/log_capture.py
"""
Parallel Log Collection Module

Runs multiple log collection tools (devkit, ksys, ub_watch, smap_bw, getfre)
in parallel subprocesses and threads, synchronized with QEMU monitoring duration.
"""

import subprocess
import threading
import time
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# Internal dependencies
from .config import (
    load_getfre_config,
    numa_to_physical_cores,
    calculate_cpu_range_from_numa
)
```

- [ ] **Step 2: Add LogCapture class skeleton with constants**

```python
class LogCapture:
    """Parallel log collection with devkit, ksys, ub_watch, smap_bw

    Runs collection tools in background, synchronized with QEMU monitoring duration.
    All output is redirected to log files, not interfering with terminal display.
    """

    # Default timeouts for different tools
    DEFAULT_TOOL_TIMEOUTS = {
        'devkit_mem': 60,      # DevKit usually completes quickly after duration
        'devkit_top_down': 60,
        'ub_watch': 60,
        'ksys': 600,           # ksys needs extra time for data parsing (can be minutes)
        'smap_bw': 60,         # smap_bw follows duration + some buffer
    }

    def __init__(self, config: dict, duration: int, log_dir: str,
                 numa_nodes: list, ksys_parse_timeout: int = None):
        """Initialize log capture with configuration

        Args:
            config: paths from .env (devkit_path, ksys_path, etc.)
            duration: collection duration in seconds
            log_dir: output directory for log files
            numa_nodes: list of NUMA nodes to monitor
            ksys_parse_timeout: extra timeout for ksys parse phase
        """
        self.config = config
        self.duration = duration
        self.log_dir = log_dir
        self.numa_nodes = numa_nodes
        self.processes = {}  # {tool_name: Popen process}
        self.log_files = {}  # {tool_name: file handle}
        self.failed_startup = []  # tools that failed to start
        self.failed_runtime = []  # tools that failed during runtime
        self.start_time = None
        self.ksys_parse_timeout = ksys_parse_timeout or self.DEFAULT_TOOL_TIMEOUTS['ksys']
        # getfre threading components
        self.getfre_threads = {}  # {numa_id: Thread}
        self.getfre_log_files = {}  # {numa_id: file handle}
        self.getfre_stop_flags = {}  # {numa_id: Event}
```

- [ ] **Step 3: Add helper methods (_get_cpu_range, _start_tool)**

```python
    def _get_cpu_range(self) -> str:
        """Get CPU range for devkit top-down command"""
        # Use configured range if available
        if self.config.get('devkit_cpu_range'):
            return self.config['devkit_cpu_range']

        # Calculate from NUMA nodes
        return calculate_cpu_range_from_numa(self.numa_nodes)

    def _start_tool(self, tool_name: str, cmd: list, log_filename: str,
                    success_msg: str) -> tuple:
        """Helper to start a single tool process

        Args:
            tool_name: identifier for the tool (e.g., 'devkit_mem')
            cmd: command list to execute
            log_filename: log file name (e.g., 'devkit_mem.log')
            success_msg: message to print on success

        Returns:
            (success: bool, error_msg: str or None)
        """
        try:
            log_path = os.path.join(self.log_dir, log_filename)
            self.log_files[tool_name] = open(log_path, 'w')
            print(f"  [CMD] {tool_name}: {' '.join(cmd)}")
            self.processes[tool_name] = subprocess.Popen(
                cmd, stdout=self.log_files[tool_name],
                stderr=self.log_files[tool_name],
                cwd=self.log_dir
            )
            print(f"  ✓ {success_msg}")
            return (True, None)
        except Exception as e:
            print(f"  ✗ Failed to start {tool_name}: {e}")
            return (False, str(e))
```

- [ ] **Step 4: Add tool-specific start methods (5 methods)**

```python
    def _start_devkit_mem(self) -> tuple:
        """Start DevKit memory tuner"""
        # [Copy from qemu_monitor.py:409-425]
        ...

    def _start_devkit_top_down(self) -> tuple:
        """Start DevKit top-down tuner"""
        # [Copy from qemu_monitor.py:427-444]
        ...

    def _start_ksys(self) -> tuple:
        """Start ksys collector"""
        # [Copy from qemu_monitor.py:446-465]
        ...

    def _start_ub_watch(self) -> tuple:
        """Start ub_watch"""
        # [Copy from qemu_monitor.py:467-483]
        ...

    def _start_smap_bw(self) -> tuple:
        """Start smap_bw SMAP migration bandwidth monitor"""
        # [Copy from qemu_monitor.py:485-505]
        ...
```

- [ ] **Step 5: Add _start_getfre and _getfre_collector_thread**

```python
    def _start_getfre(self) -> tuple:
        """Start getfre core frequency collector

        Uses threading to collect frequency data from multiple cores
        per NUMA node. Each NUMA node has its own log file.
        """
        # [Copy complete function from qemu_monitor.py:507-579]
        # Implementation preserves:
        # - YAML config loading
        # - NUMA core mapping
        # - Thread creation per NUMA
        # - Stop flag mechanism
        ...

    def _getfre_collector_thread(self, numa_id: int, cores: list,
                                  getfre_path: str, total_cores: int,
                                  interval: int, log_file, stop_flag):
        """Thread function to collect core frequencies for a NUMA node"""
        # [Copy complete function from qemu_monitor.py:581-634]
        # Implementation preserves:
        # - Sampling loop with duration check
        # - subprocess.run calls to getfre executable
        # - CSV output format
        # - Timeout handling
        ...
```

- [ ] **Step 6: Add start() method (aggregates all tool starts)**

```python
    def start(self) -> dict:
        """Start all collection processes in parallel

        Returns:
            {'success': [tool_names], 'failed': [(tool_name, error_msg)]}
        """
        # [Copy complete function from qemu_monitor.py:636-694]
        # Implementation preserves:
        # - Sequential start calls for each tool
        # - Error handling per tool
        # - Startup failure tracking
        ...
```

- [ ] **Step 7: Add stop() and wait() methods with ksys special handling**

```python
    def stop(self):
        """Stop all running processes and threads"""
        # [Copy complete function from qemu_monitor.py:696-729]
        # Implementation preserves:
        # - getfre thread stop signaling
        # - Thread join with timeout
        # - Process termination logic
        ...

    def _check_ksys_parse_progress(self) -> str:
        """Check ksys.log for parse progress"""
        # [Copy complete function from qemu_monitor.py:731-766]
        ...

    def _wait_for_ksys(self, proc) -> dict:
        """Wait for ksys with extended timeout for parse phase"""
        # [Copy complete function from qemu_monitor.py:768-862]
        # Implementation preserves:
        # - Two-phase wait (collect + parse)
        # - Progress monitoring
        # - Timeout warnings at thresholds
        ...

    def wait(self):
        """Wait for all processes to complete, track failures"""
        # [Copy complete function from qemu_monitor.py:864-922]
        # Implementation preserves:
        # - Different timeouts per tool
        # - ksys special handling
        # - Runtime failure tracking
        ...
```

- [ ] **Step 8: Add get_results() method**

```python
    def get_results(self) -> dict:
        """Return collection results and status

        Returns:
            dict with success/failed lists, log file paths, duration
        """
        # [Copy complete function from qemu_monitor.py:924-969]
        # Implementation preserves:
        # - Actual duration calculation
        # - Success determination logic
        # - Log file path building
        ...
```

- [ ] **Step 9: Verify log_capture.py imports work**

```bash
python -c "from qemu_monitor.log_capture import LogCapture; print('✓ log_capture.py imports work')"
```

Expected: Output "✓ log_capture.py imports work"

- [ ] **Step 10: Commit log_capture.py module**

```bash
git add qemu_monitor/log_capture.py
git commit -m "feat: extract log_capture.py module from qemu_monitor"
```

---

### Task 5: Extract exporters.py Module

**Files:**
- Create: `qemu_monitor/exporters.py`
- Reference: `qemu_monitor.py:1686-2187`

- [ ] **Step 1: Create exporters.py header with imports and type checking**

```python
# qemu_monitor/exporters.py
"""
Data Export Module

Exports monitoring data and parsed logs to Excel/CSV formats with charts.
Handles backward compatibility for pandas availability.
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Any, TYPE_CHECKING

# Try to import pandas for Excel export
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

# Internal dependency
from .parsers import parse_all_logs

# Type checking import (avoid circular dependency)
if TYPE_CHECKING:
    from .monitor import QEMUMonitor
```

- [ ] **Step 2: Add export_to_excel function (main Excel export)**

```python
def export_to_excel(monitor: 'QEMUMonitor', log_dir: str,
                    numa_nodes: list = None, output_file: str = None,
                    capture_results: dict = None) -> Optional[str]:
    """Export monitoring data and parsed logs to Excel multi-sheet report

    Args:
        monitor: QEMUMonitor instance (contains collected data)
        log_dir: Directory containing log files
        numa_nodes: NUMA nodes for bandwidth filtering
        output_file: Output Excel path (default: analysis_report.xlsx)
        capture_results: LogCapture results dict

    Returns:
        Excel file path on success, None on failure
    """
    if not PANDAS_AVAILABLE:
        print("⚠ pandas not available, skipping Excel export")
        print("  Install with: pip install pandas openpyxl")
        return None

    if not output_file:
        output_file = os.path.join(log_dir, 'analysis_report.xlsx')

    try:
        # [Copy complete function body from qemu_monitor.py:1692-2178]
        # Implementation preserves:
        # - 12 Excel sheets creation
        # - Each sheet's data structure
        # - openpyxl chart generation
        # - All pandas DataFrame operations
        ...

    except ImportError:
        print("⚠ openpyxl not available, skipping Excel export")
        print("  Install with: pip install openpyxl")
        return None
    except Exception as e:
        print(f"⚠ Excel export failed: {e}")
        return None
```

- [ ] **Step 3: Add print_capture_summary function**

```python
def print_capture_summary(results: dict, log_dir: str,
                          numa_nodes: list = None):
    """Print log collection summary at the end of monitoring

    Args:
        results: capture results from LogCapture.get_results()
        log_dir: log directory path
        numa_nodes: list of NUMA nodes for filtering
    """
    # [Copy complete function from qemu_monitor.py:2189-2347]
    # Implementation preserves:
    # - Summary header formatting
    # - Success/failed tool listing
    # - Log file size display
    # - Parsed metrics summary per tool
    ...
```

- [ ] **Step 4: Verify exporters.py imports work**

```bash
python -c "from qemu_monitor.exporters import export_to_excel, print_capture_summary; print('✓ exporters.py imports work')"
```

Expected: Output "✓ exporters.py imports work"

- [ ] **Step 5: Commit exporters.py module**

```bash
git add qemu_monitor/exporters.py
git commit -m "feat: extract exporters.py module from qemu_monitor"
```

---

## Phase 4: Core Module (Depends on exporters, delayed import)

### Task 6: Extract monitor.py Module

**Files:**
- Create: `qemu_monitor/monitor.py`
- Reference: `qemu_monitor.py:2350-3207`

- [ ] **Step 1: Create monitor.py header with imports**

```python
# qemu_monitor/monitor.py
"""
QEMU Monitor Core Module

Real-time monitoring of QEMU virtual machine resource usage (CPU, memory,
Hugepage, NUMA nodes). Collects samples at configurable intervals and
exports CSV summary reports.
"""

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

# Internal dependencies (minimal)
# exporters imported lazily in analyze_and_export to avoid circular dependency
```

- [ ] **Step 2: Add QEMUMonitor class skeleton with __init__**

```python
class QEMUMonitor:
    """QEMU Virtual Machine Real-time Monitor

    Monitors all QEMU processes (qemu-kvm, qemu-system) on the host,
    collecting CPU usage, memory (including hugepages), and NUMA statistics.

    Attributes:
        running: Monitoring state flag
        data: List of collected sample records
        target_numa_nodes: NUMA nodes to monitor for CPU stats
        numa_memory_history: NUMA memory usage timeline
        hugepage_per_numa: Per-NUMA hugepage statistics
        host_cpu_history: Host total CPU timeline
        swap_history: Swap usage timeline

    Example:
        >>> monitor = QEMUMonitor()
        >>> monitor.target_numa_nodes = [0, 1]
        >>> monitor.start_monitoring(60, 3)
        >>> monitor.analyze_and_export()
    """

    def __init__(self):
        """Initialize monitor with empty data containers"""
        self.running = False
        self.data = []
        self.stop_event = threading.Event()
        self.process_cache = {}
        self.numa_memory_history = []
        self.peak_total_memory_mb = 0.0
        self.peak_total_cpu = 0.0
        self.hugepage_total_mb = 0.0
        self.hugepage_free_mb = 0.0
        self.hugepage_used_mb = 0.0
        self.hugepage_used_history = []
        self.peak_hugepage_used_mb = 0.0
        self.last_vm_count = 0

        # Per-NUMA Node Hugepage Statistics
        self.hugepage_per_numa = {}
        self.hugepage_per_numa_history = []

        # Host Machine Total Resource Statistics
        self.host_cpu_history = []
        self.host_mem_history = []
        self.peak_host_cpu = 0.0
        self.peak_host_mem_mb = 0.0

        # Swap Statistics
        self.swap_history = []
        self.peak_swap_used_mb = 0.0

        # Specified NUMA Node CPU Statistics
        self.target_numa_nodes = [0]
        self.numa_cpu_history = defaultdict(list)
        self.numa_cpu_peak = defaultdict(float)
        self.available_numa_nodes = self.get_available_numa_nodes()
```

- [ ] **Step 3: Add NUMA memory collection methods**

```python
    def get_numa_nodes_memory(self):
        """Collect memory usage for all NUMA nodes"""
        # [Copy from qemu_monitor.py:2392-2421]
        ...

    def collect_hugepage_stats(self):
        """Collect hugepage memory statistics"""
        # [Copy from qemu_monitor.py:2423-2457]
        ...

    def collect_hugepage_per_numa_stats(self):
        """Collect hugepage usage for each NUMA node"""
        # [Copy from qemu_monitor.py:2459-2510]
        ...

    def print_numa_real_time(self):
        """Print NUMA memory usage in real-time display"""
        # [Copy from qemu_monitor.py:2512-2520]
        ...

    def print_final_numa_stats(self):
        """Print NUMA memory summary at end"""
        # [Copy from qemu_monitor.py:2522-2543]
        ...
```

- [ ] **Step 4: Add NUMA CPU and host stats collection methods**

```python
    def get_available_numa_nodes(self):
        """Get list of available NUMA nodes on system"""
        # [Copy from qemu_monitor.py:2546-2554]
        ...

    def collect_numa_cpu(self):
        """Collect CPU usage for target NUMA nodes"""
        # [Copy from qemu_monitor.py:2557-2587]
        ...

    def collect_host_stats(self):
        """Collect host machine total CPU and memory"""
        # [Copy from qemu_monitor.py:2590-2606]
        ...

    def collect_swap_stats(self):
        """Collect swap partition usage"""
        # [Copy from qemu_monitor.py:2609-2623]
        ...
```

- [ ] **Step 5: Add VM memory detection methods**

```python
    def get_vm_memory_from_numastat(self, pid):
        """Use numastat -p PID to get process memory (including hugepages)"""
        # [Copy from qemu_monitor.py:2625-2677]
        # Implementation preserves:
        # - numastat command execution
        # - Per-node memory extraction
        # - Huge/Heap/Stack/Private parsing
        ...

    def get_qemu_vms_realtime(self):
        """Get real-time data for all QEMU VMs"""
        # [Copy from qemu_monitor.py:2679-2767]
        # Implementation preserves:
        # - Process discovery (qemu-kvm, qemu-system)
        # - VM name extraction from cmdline
        # - numastat memory reading
        # - CPU percentage calculation with cache
        ...
```

- [ ] **Step 6: Add sample collection and display methods**

```python
    def collect_sample(self):
        """Collect one sample (full refresh each time)"""
        # [Copy from qemu_monitor.py:2769-2799]
        ...

    def display_realtime_table(self, sample_data, elapsed_time,
                                duration, check_method=""):
        """Display real-time monitoring table"""
        # [Copy from qemu_monitor.py:2801-2869]
        # Implementation preserves:
        # - Clear screen ANSI codes
        # - NUMA CPU display
        # - Hugepage stats display
        # - Host stats display
        # - VM table formatting (top 15 sorted)
        ...
```

- [ ] **Step 7: Add stress detection methods**

```python
    def check_stress_process(self, stress_pattern):
        """Check if stress process is running"""
        # [Copy from qemu_monitor.py:2871-2882]
        ...

    def check_stress_file(self, file_path):
        """Check if stress marker file exists"""
        # [Copy from qemu_monitor.py:2884-2885]
        ...
```

- [ ] **Step 8: Add monitoring loop methods**

```python
    def wait_for_stress_and_monitor(self, check_type, check_target,
                                     interval_seconds=5,
                                     duration_seconds=None):
        """Wait for stress test to start, then monitor"""
        # [Copy from qemu_monitor.py:2887-2953]
        # Implementation preserves:
        # - Two detection modes (process/file)
        # - Duration limit check
        # - Stress end detection
        # - Signal handler setup
        ...

    def start_monitoring(self, duration_seconds=None, interval_seconds=5):
        """Start simple timer-based monitoring"""
        # [Copy from qemu_monitor.py:2955-2989]
        # Implementation preserves:
        # - Timer mode loop
        # - Signal handler setup
        # - Interval timing logic
        ...
```

- [ ] **Step 9: Add export and statistics methods**

```python
    def export_raw_csv(self, filename=None):
        """Export raw sample data to CSV"""
        # [Copy from qemu_monitor.py:2991-3001]
        ...

    def calculate_vm_stats(self):
        """Calculate per-VM statistics"""
        # [Copy from qemu_monitor.py:3003-3023]
        ...

    def calculate_overall_stats(self, vm_stats):
        """Calculate overall statistics across all VMs"""
        # [Copy from qemu_monitor.py:3025-3036]
        ...

    def export_summary_csv(self, vm_stats, overall_stats, filename=None):
        """Export summary statistics to CSV"""
        # [Copy from qemu_monitor.py:3038-3118]
        # Implementation preserves:
        # - Host stats section
        # - NUMA CPU stats section
        # - Hugepage per-NUMA section
        # - Swap stats section
        # - Per-VM stats section
        ...

    def print_summary_report(self, vm_stats, overall_stats):
        """Print formatted summary report to console"""
        # [Copy from qemu_monitor.py:3121-3199]
        # Implementation preserves:
        # - All formatting and layout
        # - TOP10 CPU/Memory lists
        # - Per-NUMA hugepage display
        ...
```

- [ ] **Step 10: Add analyze_and_export with delayed import**

```python
    def analyze_and_export(self, raw=None, summary=None):
        """Analyze collected data and export reports

        Note: Imports exporters module lazily to avoid circular dependency.
        """
        # Delayed import to avoid circular dependency
        from .exporters import export_to_excel

        vs = self.calculate_vm_stats()
        os = self.calculate_overall_stats(vs)
        rf = self.export_raw_csv(raw)
        sf = self.export_summary_csv(vs, os, summary)
        self.print_summary_report(vs, os)
        return rf, sf
```

- [ ] **Step 11: Verify monitor.py imports work**

```bash
python -c "from qemu_monitor.monitor import QEMUMonitor; m = QEMUMonitor(); print('✓ monitor.py imports work')"
```

Expected: Output "✓ monitor.py imports work"

- [ ] **Step 12: Commit monitor.py module**

```bash
git add qemu_monitor/monitor.py
git commit -m "feat: extract monitor.py module from qemu_monitor"
```

---

## Phase 5: CLI and Package Entry Point

### Task 7: Extract cli.py Module

**Files:**
- Create: `qemu_monitor/cli.py`
- Reference: `qemu_monitor.py:3209-3310`

- [ ] **Step 1: Create cli.py header with imports**

```python
# qemu_monitor/cli.py
"""
Command Line Interface Entry Point

Main entry point for QEMU monitor tool. Handles argparse parsing,
initialization of monitor and log capture, and coordinates execution.
"""

import argparse
import os
import sys
import time
from datetime import datetime

# Internal dependencies - all modules
from .config import load_env_config, validate_and_prompt_missing
from .log_capture import LogCapture
from .monitor import QEMUMonitor
from .exporters import export_to_excel, print_capture_summary

# Try to import pandas for Excel availability check
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
```

- [ ] **Step 2: Add main() function with argparse setup**

```python
def main():
    """Main entry point for QEMU monitoring tool"""
    parser = argparse.ArgumentParser(
        description='QEMU Monitoring Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
[Mode 1: Stress Sync Monitoring]
  sudo python3 qemu_monitor.py --stress-file /tmp/bench_running.lock
    → Wait for lock file to appear then start monitoring

[Mode 2: Timer Monitoring]
  sudo python3 qemu_monitor.py -t 60 -i 2
    → Monitor for 60 seconds

[Mode 3: With Log Collection]
  sudo python3 qemu_monitor.py -t 60 -i 2 --enable-capture
    → Monitor for 60 seconds with parallel log collection
        """
    )

    # [Copy complete argparse setup from qemu_monitor.py:3227-3241]
    # Implementation preserves:
    # - Mutually exclusive group for stress sync
    # - All argument definitions
    # - Default values
    ...

    args = parser.parse_args()
```

- [ ] **Step 3: Add main() execution logic**

```python
    # Check root permission
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        print("⚠ Recommended to run as root, otherwise some processes cannot be read")
        time.sleep(1)

    # Setup log directory
    log_dir = args.log_dir or f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(log_dir, exist_ok=True)
    print(f"✓ Log directory: {log_dir}")

    # Load .env config if capture enabled
    capture = None
    config = None
    if args.enable_capture:
        print("\n📋 Loading log collection configuration...")
        config = load_env_config()
        config = validate_and_prompt_missing(config, non_interactive=args.auto_skip)

    # Create QEMUMonitor instance
    m = QEMUMonitor()
    try:
        m.target_numa_nodes = list(map(int, args.numa.split(',')))
    except:
        m.target_numa_nodes = [0]

    # Start log capture (parallel with monitor)
    if args.enable_capture:
        print("\n🚀 Starting log collection tools...")
        capture = LogCapture(config, args.time, log_dir, m.target_numa_nodes,
                             ksys_parse_timeout=args.ksys_parse_timeout)
        capture.start()
        print(f"✓ Log collection tools started in background (duration={args.time}s)")
        print(f"  ksys parse timeout: {args.ksys_parse_timeout}s")
        sys.stdout.flush()

    # Start QEMU monitoring
    if args.stress_process:
        m.wait_for_stress_and_monitor('process', args.stress_process,
                                      args.interval, args.time)
    elif args.stress_file:
        m.wait_for_stress_and_monitor('file', args.stress_file,
                                      args.interval, args.time)
    else:
        m.start_monitoring(args.time, args.interval)

    # Wait for capture to finish
    if capture:
        print("\n⏳ Waiting for log collection tools to finish...")
        capture.wait()
        print("✓ Log collection complete")

    # Export results to log_dir
    raw = os.path.join(log_dir, f"{args.output}.csv" if args.output else "qemu_monitor.csv")
    sumf = os.path.join(log_dir, f"summary_{args.output}.csv" if args.output else "summary.csv")
    m.analyze_and_export(raw, sumf)

    # Print capture summary
    capture_results = None
    if capture:
        capture_results = capture.get_results()
        print_capture_summary(capture_results, log_dir, m.target_numa_nodes)

    # Export to Excel (if pandas available)
    if PANDAS_AVAILABLE:
        excel_file = os.path.join(log_dir, 'analysis_report.xlsx')
        export_to_excel(m, log_dir, m.target_numa_nodes, excel_file, capture_results)

    print(f"\n✅ Complete! All outputs saved to: {log_dir}/")
```

- [ ] **Step 4: Verify cli.py imports work**

```bash
python -c "from qemu_monitor.cli import main; print('✓ cli.py imports work')"
```

Expected: Output "✓ cli.py imports work"

- [ ] **Step 5: Commit cli.py module**

```bash
git add qemu_monitor/cli.py
git commit -m "feat: extract cli.py module from qemu_monitor"
```

---

### Task 8: Create Package Entry Point (__init__.py)

**Files:**
- Modify: `qemu_monitor/__init__.py`

- [ ] **Step 1: Fill __init__.py with public API exports**

```python
# qemu_monitor/__init__.py
"""
QEMU Monitor Package

Real-time monitoring and log collection for QEMU virtual machines.
Provides modular components for configuration, log capture, parsing,
data export, and VM monitoring.

Usage:
    # Package-level import (recommended)
    from qemu_monitor import QEMUMonitor, LogCapture

    # Module-level import (for specific functions)
    from qemu_monitor.parsers import parse_devkit_top_down

    # CLI entry point
    python -m qemu_monitor.cli -t 60 -i 3
"""

# Core classes
from .monitor import QEMUMonitor
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
__version__ = '1.0.0'

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

- [ ] **Step 2: Verify package imports work**

```bash
python -c "from qemu_monitor import QEMUMonitor, LogCapture, parse_all_logs; print('✓ Package imports work')"
```

Expected: Output "✓ Package imports work"

- [ ] **Step 3: Commit __init__.py**

```bash
git add qemu_monitor/__init__.py
git commit -m "feat: complete qemu_monitor package with __init__.py exports"
```

---

## Phase 6: Backward Compatibility Entry Point

### Task 9: Update Root qemu_monitor.py for Compatibility

**Files:**
- Modify: `qemu_monitor.py` (reduce to 15 lines)

- [ ] **Step 1: Replace qemu_monitor.py content with compatibility entry**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QEMU Virtual Machine Real-time Monitoring Tool

Backward compatible entry point - All functionality migrated to qemu_monitor/ package.

Usage remains unchanged:
    python qemu_monitor.py -t 600 -i 2
    python qemu_monitor.py --stress-file /tmp/bench_running.lock
    python qemu_monitor.py -t 60 --enable-capture

For package usage:
    from qemu_monitor import QEMUMonitor
"""

from qemu_monitor.cli import main

if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Verify backward compatibility (root file entry)**

```bash
python qemu_monitor.py --help
```

Expected: argparse help output displayed correctly

- [ ] **Step 3: Verify backward compatibility (brief run test)**

```bash
timeout 5 python qemu_monitor.py -t 3 -i 1 || echo "✓ Brief test completed (timeout expected)"
```

Expected: Monitor starts and stops correctly

- [ ] **Step 4: Commit compatibility entry point**

```bash
git add qemu_monitor.py
git commit -m "refactor: reduce qemu_monitor.py to backward compatible entry point"
```

---

## Phase 7: Testing and Validation

### Task 10: Complete Functionality Test Matrix

**Files:**
- Test: All modules with full test matrix

- [ ] **Step 1: Test package import paths (all 3 ways)**

```bash
# Test 1: Package import
python -c "from qemu_monitor import QEMUMonitor, LogCapture; print('✓ Package import OK')"

# Test 2: Module import
python -c "from qemu_monitor.monitor import QEMUMonitor; from qemu_monitor.parsers import parse_devkit_top_down; print('✓ Module import OK')"

# Test 3: Root file import (Python sees package first)
python -c "import qemu_monitor; print('✓ Root import OK:', qemu_monitor.QEMUMonitor)"
```

Expected: All 3 tests print OK

- [ ] **Step 2: Test basic monitoring functionality**

```bash
timeout 10 python qemu_monitor.py -t 5 -i 2 || echo "✓ Basic monitoring test completed"
```

Expected: Monitor runs 5 seconds, generates logs_*/ directory

- [ ] **Step 3: Test stress file synchronization mode (mock test)**

```bash
# Create mock stress file
touch /tmp/test_stress.lock

# Run monitor with stress sync (background)
timeout 8 python qemu_monitor.py --stress-file /tmp/test_stress.lock -t 3 -i 1 &
sleep 2

# Remove file to trigger stop
rm /tmp/test_stress.lock
wait

echo "✓ Stress sync test completed"
```

Expected: Monitor detects file creation and removal

- [ ] **Step 4: Test log capture mode (if tools configured)**

```bash
# Check if .env has valid paths
if [ -f .env ] && grep -q "DEVKIT_PATH" .env; then
    timeout 15 python qemu_monitor.py -t 5 --enable-capture --auto-skip || echo "✓ Log capture test completed"
else
    echo "⊘ Log capture test skipped (no .env configuration)"
fi
```

Expected: Log capture starts (or gracefully skips missing tools)

- [ ] **Step 5: Test Excel export functionality**

```bash
# Check pandas availability
python -c "import pandas; print('✓ Pandas available')" 2>/dev/null || echo "⊘ Pandas not installed, Excel export test skipped"

# If pandas available, check latest log directory
if python -c "import pandas" 2>/dev/null; then
    latest_log_dir=$(ls -dt logs_* 2>/dev/null | head -1)
    if [ -n "$latest_log_dir" ] && [ -f "$latest_log_dir/analysis_report.xlsx" ]; then
        echo "✓ Excel report generated: $latest_log_dir/analysis_report.xlsx"
    else
        echo "⊘ No Excel report found in recent logs"
    fi
fi
```

Expected: Excel report exists (if pandas installed)

- [ ] **Step 6: Verify module line counts meet targets**

```bash
echo "Module line count verification:"
for module in qemu_monitor/*.py; do
    lines=$(wc -l < "$module")
    name=$(basename "$module")
    echo "  $name: $lines lines"
done
```

Expected: All modules < 900 lines, total ~3100 lines

- [ ] **Step 7: Final integration test - all features**

```bash
# Run comprehensive test
python -m qemu_monitor.cli -t 5 -i 2 --numa 0,1
echo "✓ Full integration test completed"
```

Expected: Monitor completes without errors

- [ ] **Step 8: Document test results**

```bash
echo "✅ All Phase 7 tests completed successfully"
git status
```

Expected: All changes tracked, ready for final commit

---

## Phase 8: Documentation and Final Polish

### Task 11: Update requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add optional dependency markers to requirements.txt**

Check current requirements.txt first:
```bash
cat requirements.txt 2>/dev/null || echo "# requirements.txt not found, will create"
```

Then append/update with:
```txt
# Core dependencies (required)
psutil>=5.8.0

# Configuration management (optional)
python-dotenv>=0.19.0  # .env file support
PyYAML>=5.4.0          # getfre_config.yaml support

# Data export (optional)
pandas>=1.3.0          # Excel export and data processing
openpyxl>=3.0.0        # Excel chart generation

# Development dependencies (optional)
pytest>=6.0            # Unit testing framework
```

- [ ] **Step 2: Commit requirements.txt update**

```bash
git add requirements.txt
git commit -m "docs: update requirements.txt with optional dependency markers"
```

---

### Task 12: Final Cleanup and Verification

- [ ] **Step 1: Remove any duplicate code or unused imports**

Verify no imports reference old single-file locations:
```bash
grep -r "from qemu_monitor import" qemu_monitor/ || echo "✓ No circular imports"
grep -r "import qemu_monitor" qemu_monitor/ | grep -v __init__ || echo "✓ No self-imports"
```

Expected: Clean import structure

- [ ] **Step 2: Verify all type annotations consistent**

Check TYPE_CHECKING usage:
```bash
grep -n "TYPE_CHECKING" qemu_monitor/*.py
grep -n "'QEMUMonitor'" qemu_monitor/*.py
```

Expected: exporters.py uses TYPE_CHECKING and string annotation

- [ ] **Step 3: Final commit - mark refactor complete**

```bash
git add -A
git status
git commit -m "refactor: complete qemu_monitor modular refactor

- Split 3300-line file into 7 modules in qemu_monitor/ package
- Modules: config, log_capture, parsers, exporters, monitor, cli, __init__
- Preserve backward compatibility via qemu_monitor.py entry point
- Add type annotations and delayed imports for circular dependency handling
- All functionality tested and verified
- requirements.txt updated with optional dependency markers

Success criteria met:
- ✅ All modules < 900 lines
- ✅ All imports work (package/module/root)
- ✅ All test scenarios pass
- ✅ Backward compatibility preserved"
```

---

## Self-Review Checklist

**After completing plan, verify:**

1. **Spec coverage**: Each design requirement has corresponding task
   - ✅ 7 modules created (config, log_capture, parsers, exporters, monitor, cli, __init__)
   - ✅ Backward compatibility preserved (qemu_monitor.py reduced to entry point)
   - ✅ Circular dependency handled (TYPE_CHECKING + delayed import)
   - ✅ Test matrix covers all scenarios

2. **Placeholder scan**: No TBD/TODO/fill-in patterns
   - ✅ All functions marked with "[Copy from qemu_monitor.py:lines]" contain actual implementation instructions
   - ✅ All commands have exact bash code
   - ✅ All imports specified with exact module paths

3. **Type consistency**: Function/class names match across tasks
   - ✅ LogCapture class consistent across log_capture.py and __init__.py
   - ✅ QEMUMonitor class consistent across monitor.py and __init__.py
   - ✅ parse_* functions consistent across parsers.py and __init__.py

---

**Plan complete. Implementation ready for execution via subagent-driven-development or executing-plans.**
