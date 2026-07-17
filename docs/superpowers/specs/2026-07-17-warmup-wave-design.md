# E2B Bench Warmup-Only 模式增强设计

> **Status:** ✅ Implemented

## 概述

对 `e2b_bench` 的 warmup-only 模式进行三项增强：
1. 分波创建预热 - 支持大规模沙箱（>100）分波执行
2. 沙箱ID追加写入 - 每波完成后立即保存，而非最后一次性写入
3. 性能测试URL合并 - 使用 warmup_urls + browser_urls 循环访问

## 需求详情

### 1. 分波创建预热

**触发条件**：warmup-only 模式且 `total_count > 100`

**流程**：
```
第1波: 创建100个沙箱 → 端口检查 → 预热 → 写入ID
第2波: 创建100个沙箱 → 端口检查 → 预热 → 写入ID
...
直到达到目标总数
```

**波次大小**：硬编码常量 `WARMUP_WAVE_SIZE = 100`

**执行方式**：串行波次，每波完整执行完再进入下一波

### 2. 沙箱ID追加写入

**写入时机**：每波沙箱端口检查通过后，预热完成后立即追加写入

**写入模式**：追加模式（`"a"`），而非覆盖模式（`"w"`）

**配置项**：复用现有 `sandbox_ids_file` 配置项

### 3. 性能测试URL合并

**合并规则**：`all_urls = warmup_urls + browser_urls`

**访问方式**：Round-robin 循环访问合并后的列表

## 技术设计

### 文件改动

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `e2b_bench/bench.py` | 修改 | 实现分波创建预热逻辑 |
| `e2b_bench/task_runner.py` | 修改 | URL合并逻辑 |

### 详细设计

#### bench.py

**新增常量**：
```python
WARMUP_WAVE_SIZE = 100  # 每波最大沙箱数
```

**新增函数**：
```python
def append_sandbox_ids(config: Config, sandbox_states: Dict[int, SandboxState]) -> None:
    """追加写入沙箱ID到文件

    Args:
        config: 配置对象
        sandbox_states: 沙箱状态字典
    """
    if not config.sandbox_ids_file:
        return

    successful_ids = [
        s.sandbox_obj.sandbox_id
        for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.PORT_READY and s.sandbox_obj is not None
    ]

    if successful_ids:
        with open(config.sandbox_ids_file, "a") as f:  # 追加模式
            for sid in successful_ids:
                f.write(f"{sid}\n")
        print(f"Appended {len(successful_ids)} sandbox IDs to: {config.sandbox_ids_file}")
```

**修改 run_benchmark() 函数**：

在 warmup-only 分支中，替换现有的单次创建预热逻辑：

```python
# 现有逻辑（单次）：
# sandbox_states = sandbox_manager.create_all()
# task_manager.start_warmup()
# task_manager.wait_warmup()

# 新逻辑（分波）：
if config.total_count <= WARMUP_WAVE_SIZE:
    # 单波次：保持现有逻辑
    sandbox_states = sandbox_manager.create_all()
    if config.warmup_urls:
        task_manager = TaskManager(config, sandbox_states, stop_event)
        task_manager.start_warmup()
        task_manager.wait_warmup(timeout=300)
    append_sandbox_ids(config, sandbox_states)
else:
    # 多波次
    sandbox_states = {}
    remaining = config.total_count
    wave_id = 0

    while remaining > 0:
        current_wave_size = min(WARMUP_WAVE_SIZE, remaining)
        start_idx = wave_id * WARMUP_WAVE_SIZE

        print(f"\n[Wave {wave_id + 1}] Creating {current_wave_size} sandboxes...")

        # 创建当前波次配置
        wave_config = create_wave_config(config, current_wave_size)
        wave_manager = SandboxManager(wave_config, stop_event)

        # 创建沙箱
        wave_states = wave_manager.create_all()
        sandbox_states.update(wave_states)

        # 预热
        if config.warmup_urls:
            task_manager = TaskManager(wave_config, wave_states, stop_event)
            task_manager.start_warmup()
            task_manager.wait_warmup(timeout=300)

        # 追加写入ID
        append_sandbox_ids(config, wave_states)

        remaining -= current_wave_size
        wave_id += 1
```

#### task_runner.py

**修改 BrowserTaskRunner._run_single_task()**：

```python
def _run_single_task(self) -> Tuple[bool, float]:
    """Execute single browser task

    Use state.sandbox_obj handle to execute command

    Returns: (success, latency_seconds)
    """
    sbx = self.state.sandbox_obj
    if not sbx:
        return False, 0.0

    e2b_sandbox_id = sbx.sandbox_id if hasattr(sbx, "sandbox_id") else "N/A"

    # 合并 warmup_urls 和 browser_urls
    all_urls = self.config.warmup_urls + self.config.browser_urls

    if not all_urls:
        return False, 0.0

    # Get current URL (round-robin)
    url_idx = self.state.browser_metrics.total_tasks % len(all_urls)
    url = all_urls[url_idx]

    # Build browser command
    cmd = f"openclaw browser --browser-profile openclaw open '{url}'"

    # ... 后续逻辑不变
```

## 边界情况处理

1. **total_count <= 100**：保持现有单波次逻辑，无行为变化

2. **warmup_urls 为空**：跳过预热步骤，但仍创建沙箱并写入ID

3. **sandbox_ids_file 未配置**：跳过写入步骤，打印警告

4. **波次中途失败**：已写入的ID保留，继续下一波或退出（取决于失败类型）

5. **warmup_urls + browser_urls 合并后为空**：跳过任务执行，记录警告

## 测试要点

1. **单波次验证**：`total_count=50`，验证行为与现有逻辑一致

2. **多波次验证**：`total_count=250`，验证3波次执行（100+100+50）

3. **ID追加写入验证**：检查文件内容，确认每波后追加而非覆盖

4. **URL合并验证**：配置 `warmup_urls=["url1"]`, `browser_urls=["url2"]`，验证任务循环访问两个URL

5. **边界情况**：空URL列表、未配置sandbox_ids_file等

## 向后兼容性

- 非warmup-only模式：无影响
- warmup-only模式 + total_count <= 100：行为不变
- warmup-only模式 + total_count > 100：新增分波逻辑
- sandbox_ids_file写入：从最后一次性写入改为每波追加写入（行为变化，但功能增强）