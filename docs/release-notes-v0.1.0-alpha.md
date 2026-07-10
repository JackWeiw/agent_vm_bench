## v0.1.0-alpha - vm_bench Modular Refactoring

### 🚀 New Features

**vm_bench Package** - Modular architecture for VM creation and benchmarking:
- `config.py`: YAML + CLI configuration management with priority system (CLI > YAML > defaults)
- `schemas.py`: Type-safe data structures (VMStatus, VMState, Metrics)
- `vm_manager.py`: OpenStack VM creation + SSH connection lifecycle
- `task_runner.py`: QA, Stress, Browser task managers
- `bench.py`: Main orchestration entry point
- `health_checker.py`: VM health monitoring via SSH and OpenStack
- `stats_collector.py`: Real-time statistics and report generation

**Multiple Execution Modes**:
- `--create-only`: Create VMs via OpenStack, no benchmark (Phase 0)
- `--detect`: Connect existing VMs without creation
- `--warmup-only`: Execute warmup phase only
- `--detect --warmup-only`: Connect existing VMs and warmup
- Full workflow: Create → Connect → Warmup → Benchmark

**YAML Configuration**:
- `config/vm_bench.yaml` configuration template
- CLI argument override support
- Default parameters aligned with original scripts

**Python API**:
```python
from vm_bench import Config, VMManager, run_benchmark

config = Config.load_from_yaml('config/vm_bench.yaml')
vm_states = VMManager(config, stop_event).create_all()
result = run_benchmark(config)
```

**Unit Tests**:
- 7 test files covering all modules
- Mocked SSH/OpenStack for isolated testing
- `pytest` support with coverage reporting

### 📚 Documentation

- **English**: [docs/vm_bench-usage-guide.md](docs/vm_bench-usage-guide.md)
- **Chinese**: [docs/vm_bench-usage-guide-zh.md](docs/vm_bench-usage-guide-zh.md)
- Updated README.md and README_zh.md with vm_bench sections

### 🔧 Bug Fixes

- Export VMManager and VMConnection in __init__.py
- Handle missing CLI args with hasattr checks
- Implement detect_existing mode properly
- Relax network_id validation for detect mode
- Remove invalid batch_controller.stop() call

### 🔄 Migration

`auto_vm_test.py` migrated to use vm_bench module internally with backward-compatible fallback functions.

### ⚠️ Deprecation Notice

Legacy scripts are **deprecated** but still available:
- `create_server.py` → use `python -m vm_bench --create-only`
- `vm_bench_lite.py` → use `python -m vm_bench`

Will be **removed** in v2.0.0.

### 📦 Installation

```bash
pip install -r vm_bench/requirements.txt
```

### 🏃 Quick Start

```bash
# Create VMs only
python -m vm_bench --config config/vm_bench.yaml --create-only

# Detect existing VMs and benchmark
python -m vm_bench --config config/vm_bench.yaml --detect -bsp 0.5 -t 300

# Warmup only
python -m vm_bench --config config/vm_bench.yaml --warmup-only

# Full workflow
python -m vm_bench --config config/vm_bench.yaml
```

### 📋 Test Plan

| Feature | Status |
|---------|--------|
| Module imports | ✅ Verified |
| YAML loading | ✅ Verified |
| CLI parsing | ✅ Verified |
| create-only mode | ✅ Verified |
| detect mode | ⏳ Pending integration test |
| warmup-only mode | ⏳ Pending integration test |
| Full benchmark | ⏳ Pending OpenStack testing |
| Unit tests | ✅ All pass |

### 🎯 Next Steps

- v0.2.0-beta: More integration tests, bug fixes
- v1.0.0: First stable release, mark legacy deprecated
- v2.0.0: Remove legacy scripts

---

**Full Changelog**: [CHANGELOG.md](CHANGELOG.md)