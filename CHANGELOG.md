# Changelog

All notable changes to this project will be documented in this file.

> **Current Version**: `0.2.0`
> **Version Source**: [`version.py`](version.py)

## [0.2.0] - 2024-07-13

### Deprecated
- `vm_bench_lite.py` - Use `python -m vm_bench` instead
- `create_server.py` - Use `python -m vm_bench --create-only` instead
- `qemu_monitor.py` and `qemu_monitor/` - Use `vm_monitor` instead (deprecated in v0.1.0)

### Note
Deprecated scripts remain functional in this release. Removal timeline will be announced in future versions.

---

## [0.1.0-alpha] - 2024-06-27

### Added
- `vm_bench/` modular package with clean architecture
  - `config.py`: YAML + CLI configuration management
  - `schemas.py`: VMState, Metrics, VMStatus data structures  
  - `vm_manager.py`: OpenStack VM creation + SSH connection
  - `task_runner.py`: QA, Stress, Browser task managers
  - `bench.py`: Main orchestration entry point
- `vm_bench/tests/` comprehensive unit test suite (7 test files)
- `config/vm_bench.yaml` configuration template
- `docs/vm_bench-usage-guide.md` English usage documentation
- `docs/vm_bench-usage-guide-zh.md` Chinese usage documentation
- YAML configuration file support with CLI override priority
- Multiple execution modes: `--create-only`, `--detect`, `--warmup-only`
- Python API: `Config`, `VMManager`, `run_benchmark`

### Changed
- `auto_vm_test.py` migrated to use `vm_bench` module
- Added fallback functions for backward compatibility with legacy scripts

### Fixed
- Export `VMManager` and `VMConnection` in `__init__.py`
- Handle missing CLI args with `hasattr` checks
- Implement `--detect` mode properly
- Relax `network_id` validation for detect mode
- Remove invalid `batch_controller.stop()` call

---

## Versioning Strategy

This project follows [Semantic Versioning](https://semver.org/):

- **MAJOR**: Breaking changes, remove deprecated features
- **MINOR**: New features, backward compatible  
- **PATCH**: Bug fixes, minor improvements

### Release Schedule

| Version | Target | Description |
|---------|--------|-------------|
| `v0.1.0` | Past | Refactoring complete, unit tests added |
| `v0.2.0` | Now | Mark legacy scripts as deprecated |
| `v1.0.0` | Stable | First stable release, mark legacy deprecated |
| `v2.0.0` | Future | Remove legacy scripts (`create_server.py`, `vm_bench_lite.py`) |

### Deprecation Policy

Legacy scripts (`create_server.py`, `vm_bench_lite.py`) are **available but deprecated** in v1.0.0 and will be **removed** in v2.0.0.

Migration path:
```bash
# Legacy (v0.x)
python create_server.py --start_ip ... --n 10
python vm_bench_lite.py -n 100 --browser-mode -wp

# New (v1.x+)
python -m vm_bench --create-only -n 10 --config config/vm_bench.yaml
python -m vm_bench --warmup-only -n 100 --config config/vm_bench.yaml
```

---

## Version History

### [Unreleased]

#### Added
- Additional integration tests for OpenStack workflow
- Performance benchmarks for large-scale VM creation (100+ VMs)

#### Changed
- Documentation improvements based on user feedback

---

[Keep a Changelog]: https://keepachangelog.com/
[Semantic Versioning]: https://semver.org/
