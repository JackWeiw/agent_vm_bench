# E2B Batch Test Scheduler Usage Guide

[中文文档](e2b-batch-usage.md)

## Quick Start

### 1. Prepare Configuration Files

#### Test Matrix Configuration (`config/e2b_batch_matrix.yaml`)

Define variable parameter dimensions and result configuration:

```yaml
test_matrix:
  total_counts: [10, 20, 50]       # Sandbox count
  benchmark_percentages: [0.5, 0.75, 1.0]  # Benchmark ratio
  ratios: [10, 20]                 # Memory migration ratio

reuse_strategy:
  reuse_sandbox: true              # Reuse sandbox within same group
  reuse_smap_tool: true            # Reuse smap_tool within same group

# Result Configuration
result:
  template_path: "config/e2b_batch_template.yaml"  # Template config path
  output_dir: "results/e2b/batch"                  # Output directory
```

#### Template Configuration (`config/e2b_batch_template.yaml`)

Define fixed parameters (E2B credentials, browser URLs, smap_tool/vm_monitor configs):

```yaml
e2b_env:
  E2B_ACCESS_TOKEN: "your_token"
  E2B_API_KEY: "your_key"
  ...

test:
  duration: 600  # Test duration, vm_monitor sampling window auto-synced

smap_tool:
  enabled: true
  path: "/path/to/smap_tool"  # smap_tool executable path
  swap_size: 81920
  src_nid: 2
  dest_nid: 5

vm_monitor:
  enabled: true
  vmm_type: "firecracker"
  numa: "1"  # NUMA nodes to monitor, comma-separated (e.g., "0,1")
  # duration auto-synced with test.duration
  # --enable-capture and --auto-skip are added by default
```

### 2. Run Batch Tests

```bash
# Single benchmark test
python -m e2b_bench --config config/e2b_bench.yaml

# Batch test (all configs from matrix file)
python -m e2b_bench --batch --matrix config/e2b_batch_matrix.yaml

# Batch test (continue on failure)
python -m e2b_bench --batch \
    --matrix config/e2b_batch_matrix.yaml \
    --continue-on-failure

# Offline summary (generate report from existing results)
python -m e2b_bench --batch --offline --result-dir results/e2b/batch
```

### 3. View Results

After batch test completion, results are generated in `output_dir`:

```
results/e2b/batch/                              # output_dir
├── tc10_ratio10_20260629_143052/               # Group directory
│   ├── smap_tool/                              # smap_tool logs (shared within group)
│   │   ├── smap_stdout.log
│   │   └── smap_stderr.log
│   ├── tc10_ratio10_bp0.5_20260629_143100/     # Task subdirectory
│   │   ├── config_tc10_ratio10_bp0.5.yaml      # Test config file
│   │   ├── test_log.txt                        # Test execution log
│   │   ├── bench_report.txt                    # Benchmark report
│   │   └── vm_monitor/                         # vm_monitor results
│   │       ├── monitor_stdout.log
│   │       ├── monitor_stderr.log
│   │       └── analysis_report.xlsx            # Performance metrics report
│   ├── tc10_ratio10_bp0.75_20260629_143200/
│   │   └── ...
├── tc10_ratio20_20260629_144000/               # Different ratio group
│   ├── smap_tool/
│   │   └── smap_stdout.log
│   │   └── smap_stderr.log
│   ├── tc10_ratio20_bp0.5_20260629_144100/
│   │   └── ...
├── e2b_batch_summary_20260629_145000.xlsx      # Summary report
├── batch_log_20260629_143000.txt               # Batch test log
```

**Directory Structure:**

- **Group directory** (`tc10_ratio10_20260629_143052/`): Grouped by `(total_count, ratio)`:
  - `smap_tool/`: Shared smap_tool logs within group
  - Multiple Task subdirectories (different benchmark_percent)

- **Task directory** (`tc10_ratio10_bp0.5_20260629_143100/`): Single test results:
  - `config_xxx.yaml`: Full test configuration
  - `test_log.txt`: Test execution log (streaming write, view in real-time)
  - `bench_report.txt`: Browser benchmark report
  - `vm_monitor/`: vm_monitor collection results

## Configuration File Details

### Test Config File (per Task directory)

Each test task saves config file `config_<task_id>.yaml`:

```yaml
task_id: tc10_ratio10_bp0.5
total_count: 10
benchmark_percent: 0.5
ratio: 10
smap_tool_enabled: true
smap_tool_ratio: 10
test_duration: 600
browser_urls:
  - "http://192.168.110.10:8080/Weibo.html"
warmup_urls:
  - "http://192.168.110.10:8080/page1.html"
```

Easy to compare parameter differences across tests.

## Sandbox Reuse Strategy

Batch tests are grouped by `(total_count, ratio)`:

- Sandbox and smap_tool reused within same group
- Different `benchmark_percent` tests run sequentially on same sandbox batch

Example grouping:

```
Group tc10_ratio10: [bp0.5, bp0.75, bp1.0]  ← Reuse 10 sandboxes
Group tc10_ratio20: [bp0.5, bp0.75, bp1.0]  ← Create 10 new sandboxes
Group tc20_ratio10: [bp0.5, bp0.75, bp1.0]  ← Create 20 new sandboxes
```

## Memory Migration Comparison Test

smap_tool is optional, allowing comparison between with/without memory migration:

```yaml
# With memory migration
smap_tool:
  enabled: true
  ratio: 10

# Without memory migration
smap_tool:
  enabled: false
```

Set different ratio values in test matrix (including 0) to compare memory migration impact.

## Test Flow

Each group test flow:

```
1. Create sandboxes (total_count)
2. Start smap_tool (ratio param, if enabled)
3. Warmup (warmup_urls)
4. Iterate benchmark_percent:
   - Start vm_monitor (--enable-capture --auto-skip --stress-file sync)
   - Benchmark
   - Stop vm_monitor
   - Save test config and results
5. Stop smap_tool
6. Destroy sandboxes
```

## Metrics Extraction

Metrics extracted from `analysis_report.xlsx`:

| Data Source | Sheet | Metrics Count |
|-------------|-------|---------------|
| VM CPU | Summary | 2 |
| DevKit TopDown | DevKit_TopDown | 13 |
| DevKit Memory | DevKit_Memory | 6+ |
| NUMA Bandwidth | NUMA_Bandwidth | per-node |
| KSys | KSys | 11 |
| UBWatch Latency | UBWatch_Latency | 7 |
| UBWatch Bandwidth | UBWatch_Bandwidth | per-chip+port |
| SMAPBW | SMAPBW_Summary | 5 |
| Getfre | Getfre_Summary | per-NUMA |

Summary report `e2b_batch_summary_*.xlsx`:

- Auto-excludes empty columns (tools not enabled)
- Row 0: Data source labels (color-coded)
- Row 1: Column headers

## Offline Summary

Generate summary report from existing test results:

```bash
# Basic usage
python -m e2b_bench --batch --offline --result-dir results/e2b/batch

# Specify output path
python -m e2b_bench --batch --offline --result-dir results/e2b/batch --output custom_summary.xlsx
```

Offline mode scans result directory, extracts metrics from all tests, and generates summary Excel.
