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
