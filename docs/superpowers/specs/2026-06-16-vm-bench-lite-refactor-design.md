# VM Bench Lite Modular Refactoring Design

**Date:** 2026-06-16
**Author:** Claude
**Status:** Draft - Pending User Review

## Overview

Refactor `vm_bench_lite.py` (1585 lines) into a modular package structure following domain-driven organization principles, similar to the successful `qemu_monitor` refactoring. The goal is to improve maintainability, testability, and code clarity while preserving 100% backward compatibility.

## Current State Analysis

### Existing Structure (Monolithic)
- Single 1585-line file containing all functionality
- Mixed responsibilities: SSH management, task execution, health monitoring, batch control, statistics, CLI
- All classes and functions in one namespace
- Difficult to test individual components
- Hard to modify specific features without affecting others

### Refactoring Goals
1. **Separation of Concerns**: Each module handles one domain responsibility
2. **Testability**: Enable unit testing of individual modules
3. **Maintainability**: Easy to modify specific features without touching unrelated code
4. **Backward Compatibility**: Preserve existing CLI usage and functionality 100%
5. **Clear Dependencies**: Explicit module relationships

## Proposed Architecture

### Package Structure

```
vm_bench_lite/
├── __init__.py              # Package exports
├── cli.py                   # CLI entry point (argparse + main)
├── config.py                # Configuration classes (Config + SSH settings)
├── models.py                # Data models (Metrics, VMState, Enums)
├── connection.py            # SSH connection management (VMConnection)
├── tasks/                   # Task execution subpackage
│   ├── __init__.py          # Export all task managers
│   ├── qa.py                # QA task management (QATaskManager)
│   ├── stress.py            # Stress task management (StressTaskManager)
│   └── browser.py           # Browser task management (BrowserTaskManager)
├── monitoring/              # Monitoring components subpackage
│   ├── __init__.py          # Export monitoring components
│   ├── health.py            # Health checking (HealthChecker)
│   ├── batch.py             # Batch control (BatchController)
│   ├── openstack.py         # OpenStack integration (OpenStackVMChecker)
│   └── stats.py             # Statistics collection (StatsCollector)
├── runner.py                # VM task runner thread (VMTaskRunner)
└── coordinator.py           # Main coordinator (run_benchmark logic)
```

### Backward Compatibility Entry Point

```
vm_bench_lite.py             # Preserve as entry point
                             # Content: from vm_bench_lite.cli import main
                             # Usage unchanged: python vm_bench_lite.py -n 80 ...
```

## Module Responsibilities

### Core Modules

#### `config.py`
- **Responsibility**: Configuration management
- **Contents**:
  - `Config` dataclass (stress test configuration)
  - SSH configuration constants
  - Configuration validation
- **Dependencies**: None (standalone)
- **Size estimate**: ~130 lines (lines 64-138 from original)

#### `models.py`
- **Responsibility**: Data models and metrics
- **Contents**:
  - `OOMType` enum
  - `QAMetrics` dataclass
  - `BrowserMetrics` dataclass
  - `StressMetrics` dataclass
  - `VMHealth` dataclass
  - `VMState` dataclass
  - `TestSnapshot` dataclass
  - Task constants (QA_MEMORY_TEXT, QA_QUESTIONS, BROWSER_TASKS)
- **Dependencies**: None (standalone)
- **Size estimate**: ~220 lines (lines 66-298 + 966-982)

#### `connection.py`
- **Responsibility**: SSH connection management
- **Contents**:
  - `VMConnection` class (connect, execute, is_alive, close)
  - Connection retry logic
  - Paramiko integration
- **Dependencies**: paramiko, threading
- **Size estimate**: ~75 lines (lines 299-376)

### Task Execution Subpackage (`tasks/`)

#### `tasks/qa.py`
- **Responsibility**: QA task execution
- **Contents**:
  - `QATaskManager` class
  - HTTP query execution
  - CLI query execution
  - Memory initialization
  - Round-robin query logic
- **Dependencies**: config, models, connection
- **Size estimate**: ~130 lines (lines 378-508)

#### `tasks/stress.py`
- **Responsibility**: Stress task execution
- **Contents**:
  - `StressTaskManager` class
  - Start stress process
  - Check and restart logic
  - Process verification
  - OOM diagnosis
- **Dependencies**: config, models, connection
- **Size estimate**: ~120 lines (lines 510-629)

#### `tasks/browser.py`
- **Responsibility**: Browser task execution
- **Contents**:
  - `BrowserTaskManager` class
  - HTTP browser execution
  - CLI browser execution
  - Direct browser execution
  - Warmup phase logic
- **Dependencies**: config, models, connection
- **Size estimate**: ~125 lines (lines 631-755)

### Monitoring Subpackage (`monitoring/`)

#### `monitoring/health.py`
- **Responsibility**: VM health monitoring
- **Contents**:
  - `HealthChecker` class
  - Health check loop
  - Connection alive verification
  - Offline detection
- **Dependencies**: config, models, connection, monitoring.openstack (optional)
- **Size estimate**: ~55 lines (lines 849-905)

#### `monitoring/batch.py`
- **Responsibility**: Batch startup control
- **Contents**:
  - `BatchController` class
  - Batch allocation logic
  - Control loop
  - Batch ready notification
- **Dependencies**: config, threading
- **Size estimate**: ~55 lines (lines 907-963)

#### `monitoring/openstack.py`
- **Responsibility**: OpenStack integration
- **Contents**:
  - `OpenStackVMChecker` class
  - OpenStack CLI integration
  - VM status query
  - IP-name mapping
  - Offline detection via OpenStack
- **Dependencies**: subprocess, json, re, config (optional)
- **Size estimate**: ~90 lines (lines 757-848)

#### `monitoring/stats.py`
- **Responsibility**: Statistics collection and reporting
- **Contents**:
  - `StatsCollector` class
  - `TestSnapshot` dataclass (moved from models.py to avoid circular dependency)
  - Snapshot collection loop
  - Report generation
  - Real-time statistics output
- **Dependencies**: config, models, threading
- **Size estimate**: ~230 lines (lines 964-1229)

### Execution Modules

#### `runner.py`
- **Responsibility**: VM task runner thread
- **Contents**:
  - `VMTaskRunner` class (threading.Thread subclass)
  - Task execution loop
  - Error handling
  - Stress handling
  - Connection recovery
- **Dependencies**: config, models, connection, tasks.*, monitoring.health, monitoring.batch, threading
- **Size estimate**: ~130 lines (lines 1231-1361)

#### `coordinator.py`
- **Responsibility**: Main benchmark coordination
- **Contents**:
  - `run_benchmark()` function
  - Component initialization
  - Thread coordination
  - Warmup phase handling
  - Benchmark phase handling
  - Graceful shutdown
  - Report saving
- **Dependencies**: All other modules
- **Size estimate**: ~170 lines (lines 1363-1532)

#### `cli.py`
- **Responsibility**: CLI entry point
- **Contents**:
  - Argument parser definition
  - Configuration construction
  - Main entry point
- **Dependencies**: argparse, config, coordinator
- **Size estimate**: ~50 lines (lines 1534-1585)

## Module Dependencies Graph

```
cli.py → coordinator.py → {
    config.py,
    models.py,
    connection.py,
    runner.py → {
        tasks/qa.py,
        tasks/stress.py,
        tasks/browser.py,
        monitoring/health.py → monitoring/openstack.py,
        monitoring/batch.py
    },
    monitoring/stats.py,
    monitoring/health.py,
    monitoring/batch.py,
    monitoring/openstack.py
}
```

**Layer 1 (No dependencies):**
- config.py
- models.py
- connection.py

**Layer 2 (Depends on Layer 1):**
- tasks/qa.py, tasks/stress.py, tasks/browser.py
- monitoring/batch.py, monitoring/openstack.py

**Layer 3 (Depends on Layer 2):**
- runner.py
- monitoring/health.py
- monitoring/stats.py

**Layer 4 (Depends on Layer 3):**
- coordinator.py

**Layer 5 (Depends on Layer 4):**
- cli.py

## Data Flow

### Initialization Flow
```
cli.py: parse_args() → Config object
      ↓
coordinator.py: run_benchmark(config)
      ↓
    Create connections (connection.py)
      ↓
    Create states (models.py)
      ↓
    Initialize managers (tasks/*.py, monitoring/*.py)
      ↓
    Start threads (runner.py, monitoring threads)
```

### Task Execution Flow
```
runner.py (VMTaskRunner thread)
      ↓
    Wait for batch ready (monitoring/batch.py)
      ↓
    Execute task (tasks/qa.py OR tasks/browser.py)
      ↓
    Handle stress (tasks/stress.py, if stress VM)
      ↓
    Update metrics (models.py: VMState)
      ↓
    Check health (monitoring/health.py)
```

### Statistics Flow
```
monitoring/stats.py (StatsCollector thread)
      ↓
    Periodically collect snapshots from VMState
      ↓
    Aggregate metrics (QAMetrics, BrowserMetrics, StressMetrics)
      ↓
    Print real-time stats
      ↓
    Generate final report
```

## Backward Compatibility Strategy

### Entry Point Preservation
```python
# vm_bench_lite.py (original file, now entry point only)
#!/usr/bin/env python3
"""
VM Bench Lite - VM Batch Stress Testing Tool
Backward compatible entry point - All functionality migrated to vm_bench_lite/ package.

Usage remains unchanged:
    python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode -t 180
"""

from vm_bench_lite.cli import main

if __name__ == '__main__':
    main()
```

### Import Compatibility
```python
# vm_bench_lite/__init__.py
"""
VM Bench Lite Package
Modular VM batch stress testing tool.

For backward compatibility with direct imports:
    from vm_bench_lite import Config, VMState
"""

# Export commonly used classes for backward compatibility
from .config import Config
from .models import (
    OOMType,
    QAMetrics,
    BrowserMetrics,
    StressMetrics,
    VMHealth,
    VMState,
)

# Package version
__version__ = '2.0.0'

__all__ = [
    'Config',
    'OOMType',
    'QAMetrics',
    'BrowserMetrics',
    'StressMetrics',
    'VMHealth',
    'VMState',
]
```

### CLI Usage Preservation
**Before refactoring:**
```bash
python vm_bench_lite.py -n 80 --start-ip 192.168.110.11 --browser-mode -t 180
```

**After refactoring (unchanged):**
```bash
python vm_bench_lite.py -n 80 --start-ip 192.168.110.11 --browser-mode -t 180
```

### Package-level Import (New capability)
```python
# New usage pattern after refactoring
from vm_bench_lite import Config
from vm_bench_lite.tasks import QATaskManager
from vm_bench_lite.monitoring import HealthChecker

config = Config(total_vms=80)
qa_manager = QATaskManager(config)
```

## Migration Strategy

### Phase 1: Package Structure Creation
1. Create `vm_bench_lite/` directory
2. Create all module files with empty structure
3. Create `__init__.py` files for package and subpackages
4. Set up proper imports and exports

### Phase 2: Module Extraction (Bottom-up)
1. Extract Layer 1 modules (config, models, connection)
2. Test each module independently
3. Verify imports work correctly

### Phase 3: Task Subpackage
1. Extract `tasks/qa.py`
2. Extract `tasks/stress.py`
3. Extract `tasks/browser.py`
4. Create `tasks/__init__.py` with exports

### Phase 4: Monitoring Subpackage
1. Extract `monitoring/health.py`
2. Extract `monitoring/batch.py`
3. Extract `monitoring/openstack.py`
4. Extract `monitoring/stats.py`
5. Create `monitoring/__init__.py` with exports

### Phase 5: Execution Modules
1. Extract `runner.py`
2. Extract `coordinator.py`
3. Extract `cli.py`

### Phase 6: Entry Point Migration
1. Update original `vm_bench_lite.py` to be thin entry point
2. Verify backward compatibility
3. Run all existing test scenarios

### Phase 7: Testing and Validation
1. Test QA mode functionality
2. Test Browser mode functionality
3. Test Browser + Stress mode
4. Test two-phase warmup + benchmark
5. Verify all CLI arguments work
6. Verify reports generate correctly

## Testing Strategy

### Unit Testing (Per-module)
- `test_config.py`: Config validation, property calculations
- `test_models.py`: Metrics aggregation, VMState updates
- `test_connection.py`: Mock SSH connection tests
- `test_tasks_qa.py`: QA query logic, memory init
- `test_tasks_stress.py`: Stress start, restart, OOM diagnosis
- `test_tasks_browser.py`: Browser task execution, warmup
- `test_monitoring_health.py`: Health check logic
- `test_monitoring_batch.py`: Batch allocation
- `test_monitoring_stats.py`: Statistics aggregation, report generation

### Integration Testing
- `test_coordinator.py`: Full benchmark run simulation
- `test_cli.py`: CLI argument parsing and execution

### Backward Compatibility Testing
- Run existing CLI commands unchanged
- Verify all command-line arguments preserved
- Verify output reports unchanged

## Error Handling

### Module-level Error Handling
- Each module handles its own exceptions
- `connection.py`: SSH connection errors, timeout handling
- `tasks/*.py`: Task execution errors, timeout detection
- `monitoring/health.py`: Health check failures, offline detection
- `runner.py`: Thread-level error recovery, connection retry
- `coordinator.py`: Component initialization failures, graceful shutdown

### Error Propagation
- Errors bubble up through module hierarchy
- `runner.py` catches task execution errors and updates VMState
- `coordinator.py` handles critical errors and initiates shutdown

## Implementation Checklist

### File Creation Checklist
- [ ] Create `vm_bench_lite/` directory
- [ ] Create `vm_bench_lite/__init__.py`
- [ ] Create `vm_bench_lite/cli.py`
- [ ] Create `vm_bench_lite/config.py`
- [ ] Create `vm_bench_lite/models.py`
- [ ] Create `vm_bench_lite/connection.py`
- [ ] Create `vm_bench_lite/tasks/__init__.py`
- [ ] Create `vm_bench_lite/tasks/qa.py`
- [ ] Create `vm_bench_lite/tasks/stress.py`
- [ ] Create `vm_bench_lite/tasks/browser.py`
- [ ] Create `vm_bench_lite/monitoring/__init__.py`
- [ ] Create `vm_bench_lite/monitoring/health.py`
- [ ] Create `vm_bench_lite/monitoring/batch.py`
- [ ] Create `vm_bench_lite/monitoring/openstack.py`
- [ ] Create `vm_bench_lite/monitoring/stats.py`
- [ ] Create `vm_bench_lite/runner.py`
- [ ] Create `vm_bench_lite/coordinator.py`
- [ ] Update original `vm_bench_lite.py` to thin entry point

### Code Extraction Checklist
- [ ] Extract Config class (lines 64-138)
- [ ] Extract OOMType enum (lines 66-72)
- [ ] Extract Metrics classes (lines 75-298)
- [ ] Extract VMConnection class (lines 299-376)
- [ ] Extract QATaskManager (lines 378-508)
- [ ] Extract StressTaskManager (lines 510-629)
- [ ] Extract BrowserTaskManager (lines 631-755)
- [ ] Extract OpenStackVMChecker (lines 757-848)
- [ ] Extract HealthChecker (lines 849-905)
- [ ] Extract BatchController (lines 907-963)
- [ ] Extract TestSnapshot (lines 966-982)
- [ ] Extract StatsCollector (lines 984-1229)
- [ ] Extract VMTaskRunner (lines 1231-1361)
- [ ] Extract run_benchmark (lines 1363-1532)
- [ ] Extract CLI/argparse (lines 1534-1585)

### Testing Checklist
- [ ] Test QA mode: `python vm_bench_lite.py -n 80 --start-ip 192.168.110.11 -t 180`
- [ ] Test Browser mode: `python vm_bench_lite.py -n 80 --browser-mode -t 180`
- [ ] Test Browser + LLM: `python vm_bench_lite.py -n 80 --browser-mode --browser-use-llm --mode http`
- [ ] Test Warmup phase: `python vm_bench_lite.py -n 100 --browser-mode -wp`
- [ ] Test Benchmark phase: `python vm_bench_lite.py -n 100 --browser-mode -bsp 0.5 -t 180`
- [ ] Test QA + Stress: `python vm_bench_lite.py -n 80 --stress-percent 0.5`
- [ ] Verify backward compatibility with all existing scripts
- [ ] Verify report generation unchanged

## Success Criteria

1. **All functionality preserved**: Every CLI argument works as before
2. **Reports unchanged**: Output format and content identical
3. **Code modular**: Each module has clear, single responsibility
4. **Testable**: Individual modules can be unit tested
5. **Maintainable**: Easy to modify specific features
6. **Clean imports**: No circular dependencies
7. **Backward compatible**: Existing scripts run without modification

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Circular imports | Design clear layer hierarchy, use lazy imports if needed |
| Breaking backward compatibility | Thorough CLI testing, preserve entry point |
| Missing imports | Careful extraction with import tracking |
| Thread synchronization issues | Keep thread logic in dedicated modules |
| Configuration validation errors | Extract config validation logic intact |
| SSH connection changes | Keep connection module unchanged logically |

## Timeline Estimate

- **Phase 1-2**: 1 hour (package structure + Layer 1 modules)
- **Phase 3-4**: 2 hours (task + monitoring subpackages)
- **Phase 5-6**: 1 hour (execution modules + entry point)
- **Phase 7**: 1 hour (testing and validation)
- **Total**: ~5 hours

## Next Steps

After user approval:
1. Invoke `writing-plans` skill to create detailed implementation plan
2. Execute implementation plan with step-by-step verification
3. Run comprehensive testing after each phase
4. Final validation with all CLI scenarios

---

**Questions for User Review:**

1. Does the module organization match your expectations?
2. Any specific modules you want to adjust?
3. Backward compatibility strategy looks correct?
4. Any additional testing scenarios to include?
5. Ready to proceed with implementation plan?