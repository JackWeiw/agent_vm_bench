# Agent VM Bench - Project Guide for AI Assistants

This file provides project-specific conventions, architecture overview, and working guidelines for AI assistants working on this codebase.

## Project Overview

**Agent VM Bench** is a performance testing framework for virtualization scenarios:

- **OpenStack VM Memory Overcommit** - QEMU/KVM VMs with smap_tool memory migration
- **E2B Sandbox** - Firecracker microVMs via E2B API
- **Docker Containers** - Browser automation in containerized environments

The framework collects **50+ performance metrics** from hardware counters, kernel metrics, and application-level measurements.

## Architecture

### Three-Tier Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Batch Scheduling Layer                                  │
│  - batch_test_scheduler.py (OpenStack)                   │
│  - e2b_bench/batch_scheduler.py (E2B)                    │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Core Test Layer                                         │
│  - auto_vm_test.py (OpenStack single test)               │
│  - e2b_bench/bench.py (E2B single test)                  │
│  - docker_bench/bench.py (Docker single test)            │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Tool Execution Layer                                    │
│  - create_server.py, vm_bench_lite.py, vm_monitor.py     │
│  - sandbox_manager.py, container_manager.py              │
│  - External: smap_tool, devkit, ksys, ub_watch, getfre   │
└─────────────────────────────────────────────────────────┘
```

### Key Packages

| Package | Purpose | Key Files |
|---------|---------|-----------|
| `vm_monitor/` | VMM monitoring (QEMU/Firecracker) | `base.py`, `qemu.py`, `firecracker.py` |
| `qemu_monitor/` | Legacy QEMU monitoring (deprecated) | Use `vm_monitor` instead |
| `e2b_bench/` | E2B sandbox testing | `bench.py`, `sandbox_manager.py`, `batch_scheduler.py` |
| `docker_bench/` | Docker container testing | `bench.py`, `container_manager.py` |
| `vm_bench_lite/` | Browser/QA benchmark execution | Browser warmup + benchmark phases |

## Code Conventions

### Python Style

- Follow PEP 8 conventions
- Use type hints for public APIs
- Maximum line length: 120 characters
- Use f-strings for string formatting
- Prefer `pathlib.Path` over `os.path`

### Configuration

- All configs in YAML format under `config/`
- Template configs use `{{PLACEHOLDER}}` for dynamic values
- Environment variables loaded from `.env` files
- Never hardcode paths - use config or `.env`

### Logging

- Use Python logging module, not print statements
- Log levels: DEBUG (verbose), INFO (progress), WARNING (issues), ERROR (failures)
- Streaming logs to files use `buffering=1` (line buffering)
- Include timestamps in log messages

### Entry Points

| Script | Purpose | CLI |
|--------|---------|-----|
| `vm_monitor.py` | VMM monitoring | `-t`, `-i`, `--vmm`, `--enable-capture` |
| `vm_bench_lite.py` | Browser/QA benchmark | `-n`, `-wp`, `-bsp`, `-t` |
| `auto_vm_test.py` | Single OpenStack test | `--config` |
| `batch_test_scheduler.py` | Batch OpenStack tests | `--config`, `--dry-run`, `--offline` |
| `e2b_bench/__main__.py` | E2B testing | `--config`, `--batch`, `--detect` |
| `docker_bench/__main__.py` | Docker testing | `--config`, `--create-only`, `--detect` |

## External Tool Dependencies

### Required Tools (config via `.env`)

| Tool | Purpose | Config Key |
|------|---------|------------|
| `smap_tool` | Memory migration | Hardcoded in design - should move to config |
| `devkit_top_down` | CPU top-down analysis | `DEVKIT_PATH` |
| `devkit_mem` | Cache/memory metrics | `DEVKIT_PATH` |
| `ksys` | Kernel metrics | `KSYS_PATH`, `KSYS_CONFIG_PATH` |
| `ub_watch` | NUMA interconnect | `UB_WATCH_PATH` |
| `smap_bw` | SMAP bandwidth | `SMAP_BW_PATH` |
| `getfre` | Core frequency | `GETFRE_PATH`, `GETFRE_CONFIG_PATH` |

### Tool Output Parsing

Each tool produces logs parsed by `vm_monitor/parsers.py`:

- `devkit_top_down.log` → DevKit_TopDown sheet (13 metrics)
- `devkit_mem.log` → DevKit_Memory, NUMA_Bandwidth sheets
- `ksys.log` → KSys sheet (11 metrics)
- `ub_watch.log` → UBWatch_Latency sheet (7 metrics)
- `smap_bw.log` → SMAPBW_Summary sheet (5 metrics)
- `getfre_NUMA*.log` → Getfre_Summary sheet

## Testing Workflow Patterns

### OpenStack VM Test (auto_vm_test.py)

```
1. Delete old VMs → Confirm deletion
2. Create new VMs (n) → Wait for SSH ready
3. Start smap_tool (memory migration)
4. Warmup phase (browser warmup on all VMs)
5. Start vm_monitor (background)
6. Benchmark phase (active_percent VMs)
7. Collect results → Generate Excel
8. Cleanup (stop smap_tool, delete VMs)
```

### E2B Batch Test (batch_scheduler.py)

```
1. Group tasks by (total_count, ratio)
2. For each group:
   - Create sandboxes (shared within group)
   - Start smap_tool (shared logs)
   - Warmup (shared)
   - For each benchmark_percent:
     - Start vm_monitor (task-specific)
     - Run benchmark
     - Stop vm_monitor
     - Save task results
   - Cleanup group
3. Extract metrics → Aggregate summary
```

### Docker Bench (bench.py)

```
1. Create containers (or detect existing)
2. Port check (18789, 11436)
3. Browser tasks (4-step workflow per container)
4. Collect QPS metrics → Generate report
5. Cleanup containers
```

## Metrics Reference

See [docs/metrics-reference.md](docs/metrics-reference.md) for complete metric descriptions.

### Key Metrics to Watch

| Metric | High Value Indicates | Sheet |
|--------|---------------------|-------|
| `td_backend_bound_percent` | CPU/memory stalls | DevKit_TopDown |
| `td_mem_bound_percent` | Memory bottleneck | DevKit_TopDown |
| `mem_l3d_miss_percent` | L3 cache issues | DevKit_Memory |
| `ksys_l3_latency_avg` | L3 latency high | KSys |
| `ub_avg_read_ns` | NUMA latency high | UBWatch_Latency |
| `browser_p99_latency_ms` | Browser performance issue | bench_report |

## Common Modifications

### Adding a New Metric Source

1. Add collection tool path to `.env`
2. Add parser in `vm_monitor/parsers.py`
3. Add exporter in `vm_monitor/exporters.py`
4. Add metric extraction in `batch_test_scheduler.py` or `e2b_bench/metrics_extractor.py`
5. Update `docs/metrics-reference.md`

### Adding a New VMM Type

1. Create class in `vm_monitor/` extending `VMMonitorBase`
2. Implement: `get_process_names()`, `extract_vm_id()`, `get_vms_realtime()`
3. Register in `vm_monitor/__init__.py`
4. Add CLI flag in `vm_monitor/cli.py`
5. Update `docs/usage-guide.md`

### Modifying Test Flow

- OpenStack: Modify `auto_vm_test.py` phases
- E2B: Modify `e2b_bench/bench.py` or `batch_scheduler.py` GroupRunner
- Docker: Modify `docker_bench/bench.py`

## File Locations

### Results Directory Structure

```
results/
├── batch_summary_*.xlsx         # OpenStack batch summary
├── vm{n}_ratio{r}_active{p}_*/  # OpenStack single test
│   ├── config.yaml
│   ├── test_log.txt
│   ├── vm_bench_lite/
│   ├── qemu_monitor/
│   │   └── analysis_report.xlsx
│   └── summary/
│
└── e2b/batch/                   # E2B batch results
    ├── tc{n}_ratio{r}_*/        # Group directory
    │   ├── smap_tool/
    │   ├── tc{n}_ratio{r}_bp{p}_*/  # Task directory
    │   └── vm_monitor/
    └── e2b_batch_summary_*.xlsx
```

### Config Files

```
config/
├── batch_config.yaml            # OpenStack batch test matrix
├── test_config_template.yaml    # OpenStack single test template
├── e2b_bench.yaml               # E2B single test config
├── e2b_batch_matrix.yaml        # E2B batch test matrix
├── e2b_batch_template.yaml      # E2B batch template
├── docker_bench.yaml            # Docker test config
└── getfre_config.yaml           # getfre frequency monitor config
```

## Known Issues / Limitations

1. **smap_tool path hardcoded** - Should move to `.env` or config
2. **Deprecated qemu_monitor.py** - Use `vm_monitor.py` instead
3. **No unit tests** - Core components lack test coverage
4. **Versioned files** - `920x_ddr_latency_v*.py` should be consolidated
5. **Working notes in root** - `findings.md`, `progress.md`, `task_plan.md` should move to `docs/`

## Related Documentation

- [docs/design.md](docs/design.md) - System architecture (Chinese)
- [docs/design-en.md](docs/design-en.md) - System architecture (English)
- [docs/usage-guide.md](docs/usage-guide.md) - Detailed tool usage
- [docs/metrics-reference.md](docs/metrics-reference.md) - All 50+ metrics explained
- [docs/e2b-bench-usage.md](docs/e2b-bench-usage.md) - E2B testing guide
- [docs/docker-bench-usage.md](docs/docker-bench-usage.md) - Docker testing guide