# E2B Batch Test Scheduler 使用指南

## 快速开始

### 1. 准备配置文件

#### 测试矩阵配置 (`config/e2b_batch_matrix.yaml`)

定义可变参数维度和结果配置：

```yaml
test_matrix:
  total_counts: [10, 20, 50]       # 沙箱数量
  benchmark_percentages: [0.5, 0.75, 1.0]  # 压测比例
  ratios: [10, 20]                 # 内存迁移比例

reuse_strategy:
  reuse_sandbox: true              # 同组内复用沙箱
  reuse_smap_tool: true            # 同组内复用 smap_tool

# Result Configuration
result:
  template_path: "config/e2b_batch_template.yaml"  # 模板配置文件路径
  output_dir: "results/e2b/batch"                  # 结果输出目录
```

#### 模板配置 (`config/e2b_batch_template.yaml`)

定义固定参数（E2B 凭证、浏览器 URL、smap_tool/vm_monitor 配置等）：

```yaml
e2b_env:
  E2B_ACCESS_TOKEN: "your_token"
  E2B_API_KEY: "your_key"
  ...

test:
  duration: 600  # 测试时长，vm_monitor 采样窗口自动同步

smap_tool:
  enabled: true
  path: "/path/to/smap_tool"  # smap_tool 可执行文件路径
  swap_size: 81920
  src_nid: 2
  dest_nid: 5

vm_monitor:
  enabled: true
  vmm_type: "firecracker"
  numa: "1"  # NUMA nodes to monitor, comma-separated (e.g., "0,1")
  # duration 自动使用 test.duration，无需单独配置
```

### 2. 执行批量测试

```bash
# 单次基准测试
python -m e2b_bench --config config/e2b_bench.yaml

# 批量测试（所有配置从 matrix 文件读取）
python -m e2b_bench --batch --matrix config/e2b_batch_matrix.yaml

# 批量测试（失败后继续执行）
python -m e2b_bench --batch \
    --matrix config/e2b_batch_matrix.yaml \
    --continue-on-failure
```

### 3. 查看结果

批量测试完成后，在 `output_dir` 目录下生成：

```
results/e2b/batch/                          # output_dir
├── tc10_ratio10_bp0.5_20260629_143052/     # task_id + timestamp
│   ├── tc10_ratio10_bp0.5.yaml             # 测试配置文件
│   ├── bench_report.txt                    # 压测报告
│   └── vm_monitor/                         # vm_monitor 结果
│       └── analysis_report.xlsx            # 性能指标报告
├── tc10_ratio10_bp0.75_20260629_143155/
│   ├── tc10_ratio10_bp0.75.yaml
│   └── ...
├── tc10_ratio20_bp0.5_20260629_143210/
│   ├── tc10_ratio20_bp0.5.yaml             # 不同 ratio 对比
│   └── ...
├── e2b_batch_summary_20260629_143500.xlsx  # 汇总报告
├── batch_log_20260629_143000.txt           # 执行日志
```

## 配置文件说明

### 测试配置文件 (每个子测试目录下)

每个测试任务会保存配置文件 `<task_id>.yaml`，包含：

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

方便对比不同测试的参数差异。

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

## 内存迁移对比测试

smap_tool 作为可选配置，可以对比有/无内存迁移的性能差异：

```yaml
# 有内存迁移
smap_tool:
  enabled: true
  ratio: 10

# 无内存迁移
smap_tool:
  enabled: false
```

在测试矩阵中设置不同的 ratio 值（包括设置为 0），即可对比内存迁移对性能的影响。

## 测试流程

每组测试流程：
```
1. 创建沙箱 (total_count 个)
2. 启动 smap_tool (ratio 参数，如果 enabled)
3. 预热 (warmup_urls)
4. 遍历 benchmark_percent:
   - 启动 vm_monitor (--stress-file 同步)
   - 压测
   - 停止 vm_monitor
   - 保存测试配置和结果
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

汇总报告 `e2b_batch_summary_*.xlsx` 会自动排除未启用工具的空数据列。