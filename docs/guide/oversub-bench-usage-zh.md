# oversub-bench / oversub-compare 使用指南

`oversub-bench` 驱动 replay 内核扫过一组内存/CPU 超卖比,产出退化曲线;`oversub-compare` 对比
**已经产出**的扫描结果。基础 replay 工作流(mode、ratio 配置、结果怎么看)在
[bench-core-usage-zh.md](bench-core-usage-zh.md) §8.1–8.4。

## 是什么

driver(`src/bench_core/oversub.py`)把 replay 内核跑过一组超卖比:`running_concurrency`(N)是
ratio 基准,`total_count = k×N` 每个 trial 缩放。lifecycle 时 running fleet 是**全部 k×N**(所有
沙箱同时 pause/resume);admission-free 的 exec mode 本来就在 k×N 全并发。每个 trial 一次
`bench-core` 调用(开启进程分片时每 shard
一次);driver 读每个 trial 的机器可读 `run_summary.json`,聚合 per-trial + per-ratio 退化曲线。

**分层:kernel 出原始事实,driver 算 valid。** driver 不导入 kernel 数据路径内部(lifecycle /
admission / stats / observability)——只用 CLI、YAML schema、`run_summary.json` schema 和共享的
`setup_logging` 助手。kernel 不知道自己是"扫描的第 k 个 ratio",只输出发生了什么,driver 判定
trial 是否跑完。

## 1. 快速开始

```bash
# fake 冒烟 —— 秒级跑通整条产物链。必须钉住 --modes exec_ephemeral:
# FakeProvider 只支持 EphemeralCapable(非 lifecycle),不钉的话 lifecycle trial 全判 invalid。
oversub-bench --config config/common/replay-exec-only.yaml --provider fake --modes exec_ephemeral \
  --ratios 1,2,3 --test-duration 5 --output-root results/fake_sweep

oversub-bench --sweep-config config/oversub/lifecycle-1to3.yaml          # 一个现成扫描
oversub-bench --sweep-config config/oversub/template.yaml --ratios 4     # 覆盖单个旋钮
oversub-bench --config config/common/replay.yaml --provider aenv --ratios 1,2,3   # 无 sweep-config
```

优先级:**CLI 旗标 > sweep-config > 内置默认**。`--sweep-config` 承载所有旋钮 + `base_config`;
`--config` 仅在 sweep-config 没设 `base_config` 时必填。完整旗标集见 `oversub-bench --help`
(`--running-concurrency`、`--modes`、`--repeats`、`--test-duration`、`--multiplier`、`--shards`、
`--failure-tolerance`、`--cooldown-sec`、`--cleanup-between-trials`、`--trial-timeout-sec`、
`--output-root`、`--reuse`、`--stop-on-failure`、`--dry-run`、`--no-vm-monitor`、`--bench-core-bin`)。

## 2. 写一个 sweep-config

`config/oversub/template.yaml` 是带注释模板;`lifecycle-1to3.yaml` / `exec-ephemeral-1to3.yaml` /
`lifecycle-1to6.yaml` 是现成扫描,复制其一再改。未知键**会被拒绝**(`repeat:` 笔误成 `repeats`
直接报错)。

| 键 | 默认 | 含义 |
|-----|------|------|
| `base_config` | —(必填) | base replay 压力 profile;每 trial 深拷贝 |
| `provider` | `aenv` | `{aenv, e2b, docker, fake}` |
| `running_concurrency` | base `replay.running_concurrency` | 基线 N;lifecycle 时每 trial 放大到 k×N(全量并发),exec mode 保持 N(ratio 基准) |
| `ratios` | `1,2,3` | k 值(list 或 `"1,2,3"`);`total_count = k×N` |
| `modes` | `exec_ephemeral,lifecycle` | 要扫的 replay mode(每条曲线;默认 exec_ephemeral 纯 COW 在前,lifecycle 其次);driver 自动套用默认时 WARNING |
| `multiplier` | `2` | per-sandbox 轨迹数 M(仅 exec_ephemeral + fixed;每沙箱跑满 M 条自停;不设 = 每沙箱 2 条;`--multiplier 0` 关闭 M,沿用 base 的 `benchmark_mode`(仅当 base 为 `round_robin`,如 `replay.yaml`/template,才是 round 模型)) |
| `repeats` | `1` | 每 `(mode, ratio)` 重复次数(跨 repeat 取中位) |
| `test_duration` | base `test.duration`(600) | 每 trial 硬上限(秒) |
| `failure_tolerance` | `0.0` | trial 判 valid 允许的最大 `failure_rate` |
| `cooldown_sec` | `30` | trial 间沉淀时间 |
| `cleanup_between_trials` | `on` | 每 trial 前跑 `bench-core --cleanup` 拆残留 |
| `trial_timeout_sec` | `21600`(6h) | 外层墙钟防御背板(防 hung kernel 卡死整个 sweep);`0` = 关 |
| `output_root` | `results/oversub/oversub-N{N}-{ts}/` | 扫描输出目录 |
| `reuse` | `false` | 跳过已 valid 的 `(mode, ratio, repeat)`(重启安全) |
| `stop_on_failure` | `false` | 遇首个 invalid trial 即停 |
| `no_vm_monitor` | `false` | 透传 `--no-vm-monitor` 给 bench-core |
| `bench_core_bin` | `[bench-core]` | kernel 子进程命令(测试指向 stub) |
| `auto_compare` | `true` | sweep 结束后自动对本 sweep 目录跑单 series 的 `oversub-compare`,产物在 `<output_root>/comparison/`(分位数退化 + 同轨迹跨 ratio 表);`--no-auto-compare` 关闭 |

### 每 trial 的 config 覆盖

driver 深拷贝 `base_config` 只改写超卖字段,其余透传:

| 字段 | 改成 | 原因 |
|-------|------|------|
| `sandbox.total_count` | `k×N` | 超卖目标 |
| `replay.running_concurrency` | `k×N`(lifecycle)/ `N`(exec mode) | lifecycle 全量并发——所有 k×N 沙箱同时 pause/resume,无比并发上限(rc == total_count);admission-free 的 exec mode 保持 N(ratio 基准) |
| `replay.mode` | 扫描的 mode | lifecycle / exec_only / exec_ephemeral |
| `test.round_size` | `k×N` | 所有 k×N 沙箱一组并发;不设则 k≥2 按 N 串行多组,破坏超卖动态 |
| `test.duration` | `test_duration` | 时间上限 |
| `report.output_dir` / `report.filename_prefix` | 每 trial 目录/前缀 | 隔离各 trial 产物 |

`test.round_count`(工作量旋钮:跑几遍整个 fleet)透传。trial 在两者先到时结束:`round_count` 轮
OR `test.duration` 秒(`1` = 有界单遍;`0` = 持续到 duration 的窗口)。ratio 数学本身(基线 VM 数、
`total_count = k×384`):[bench-core-usage-zh.md §8.2](bench-core-usage-zh.md#82-lifecycle-内存超卖ratio-配置法)。

## 3. 进程分片(`shards`)

高 ratio 下单进程客户端先于主机触顶:thread-per-sandbox kernel 的 dispatch 被单个 GIL 封顶
(N=384 实测 ~50–55 steps/s,ratio≥7 后加压只涨客户端排队、不涨主机负载,r9/r10 数据为
client-bound)。`shards` 把一个 trial 的 k×N slot 拆成 P 个 `bench-core` 子进程(`auto` = P=k,
每进程 ~N slot——把每个进程的负载钉在 1:1 已验证的水平,GIL 天花板抬到 ~P×)。机制在
`src/bench_core/oversub_shards.py`,driver 只做组合:

- **只有 admission-free mode 可分片**(`exec_ephemeral` / `exec_only`)。`lifecycle` 始终单进程:
  它的全量 fleet(全部 k×N 沙箱在一个进程里 pause/resume)暂不分片(显式 P>1 会 WARNING 降级,
  不报错——一份 sweep config 可跨 mode)。
- **P≤ratio 时按 group 对齐切分**:每个 shard 的 slot 数是 N 的整数倍,合并后每条轨迹的 run 数
  与单进程 trial 完全一致;P>ratio 退化为 slot 级切分并 WARNING——总量保留,但单 shard 小于
  轨迹池,per-trajectory 分布会重排。
- host 采集器由 **driver 独占**(不再挂在某个 shard 上):每个 shard 各自写自己的 stress 标记
  (`shard-XX/stress.lock`,`monitor.external_collector: true` 表示"只发标记、不 spawn 采集器"),
  driver 起**一个** `vm-monitor` 监听这些标记的**并集**(第一个 shard 进入 stress → 最后一个
  shard 退出 stress),产物固定落在 **`trial_dir/vm_monitor/`**(与 `merged/`、`shard-*/` 同级),
  主机数据不随分片数变化、某个 shard 崩溃或拖慢也不再连累主机报告。shard 0 只保留**控制面
  fleet 计数**(`monitor.api_state: true` → `raw_data/api_state_raw.csv`):shard 是 kernel 进程、
  持有 provider handle,而 driver 只 shell 出去、没有 handle,轮询不了 API,只能让 shard 0 在它
  的采集窗口里顺带打计数;其余 shard 强制不发计数,避免 P 个进程重复打控制面。
- 每个 shard 是完整独立的 kernel run:`trial_dir/shard-{i:02d}/` 下有 `shard.yaml`(total_count/
  round_size = 份额、`report.output_dir` = 本 shard 目录、docker `container_prefix` 加 `-s{i}`
  后缀防容器重名、pacing 旋钮 `launch_interval_sec` / `create_batch.interval` /
  `task_batch.interval` ×P 保持全局创建/派发速率)+ `driver.log` + kernel 产物;`trial.yaml`
  保留为未分片的逻辑 provenance。
- driver 把 P 份产物合并回契约形状:`trial_dir/merged/{prefix}_run_summary.json` +
  `merged/trajectories/index.json` + `merged/{prefix}_lifecycle_series.jsonl`(计数求和、wall 取
  **包络**(max completed − min started,与 kernel 自身 `wall_sec == completed_epoch −
  started_epoch` 语义一致)、速率按包络 wall 重算、`sandbox_index` 按 shard slot 偏移重键保证
  run key 唯一;merged index 的 entry 级聚合按 kernel 规则对**全部 shard 的 runs** 重算——
  success_rate 取 per-run 均值、elapsed_sec 取成功 run 的 p50(全失败回退全量 p50 +
  `all_failed`)、error type 跨 runs first-seen、deep `file` 留在各 shard 置 None;lifecycle
  series 按 slot 偏移重键 `sandbox_index` 后拼接成**全队**时间线,这样 inflight 并发分析看到的
  是全部 k×N slot 而不是单 shard 的 N——只读单个 shard 的 series 会把观测峰值封顶在 N,把分片
  误判成并发回归;`shard_count` / `shard_start_skew_sec` / `shard_return_codes` 等 provenance
  走附加字段)——valid 判定、CSV、compare、`--reuse` 全部无感知。崩溃的 shard **绝不回填 0
  冒充成功测量**:缺失 shard + rc 记入 merged `error`,trial 判 invalid。
- `shards: 1`(或 ratio=1 的 `auto`)也走分片机制:单 shard 跑一遍 `shard-00/` + `merged/` +
  driver 独占的 `trial_dir/vm_monitor/`,布局与 P≥2 完全一致——agent 对比不同超卖比时只读一个
  固定结构,host 采集器在任意 ratio 都由 driver 持有。只有 `lifecycle`(不可分片)保留原单进程
  路径。
- **全队一份 obs workbook**:每个 shard 各自写 `_obs.xlsx`(只覆盖自己那 N 个 slot,不能当 trial
  结果读),driver 另出 `trial_dir/merged/{prefix}_obs.xlsx`,8 个 sheet 齐全。它不是把 P 份
  workbook 拼接——三个由数据模型驱动的 sheet(Overview / Per-step timings / Lifecycle overhead)
  需要 `ReplayMetrics` 累加器,而分片进程的累加器随进程消失,所以用
  `lifecycle_reconstruct.replay_states_from_events` 把 merged series 的事件按 kernel 自己的
  `add` / `record_retry_event` / `append_retries_per_slice` 重放一遍重建(没有第二套聚合规则),
  其余五个 sheet 与 kernel 一样直接读同一份 merged series。Overview 是 trial 级:`total_count`
  = k×N、`running_concurrency` = N、`overcommit_ratio` = k,`wall_sec` 用包络,因此与 merged
  `run_summary.json` 的速率自洽;Admission 区块按构造为空(可分片 mode 无准入)。`report.format:
  txt` 时不生成(沿用 kernel 的 gate);渲染异常只 WARNING 降级、绝不让报告拖垮已跑完的 trial
  (各 shard workbook 与 merged JSON 仍在)。merged summary 的 `paths.obs_xlsx` 指向它。

## 4. `run_summary.json` 字段(kernel → driver 契约)

每个 trial 写 `{prefix}_run_summary.json`——kernel 与 driver 间唯一契约,只含原始事实(kernel
绝不为 driver 重算指标;字段表见下):

| 字段 | 含义 |
|-------|------|
| `schema_version` | 契约版本(`2`);信息性字段——driver 直接读 throughput 字段、从不校验此值(用新 driver 重打分 schema-1 旧文件行为不变:同门槛、同字段) |
| `replay_mode` / `provider` | 哪个 mode / 后端;区分曲线与后端 |
| `started_at` / `completed_at`(+ `_epoch`) | 起止时间;epoch 用于与 vm_monitor 对齐 join |
| `test_duration` / `wall_sec` | 配置上限 vs 实际墙钟;wall ≪ duration = 提前停/卡死 |
| `total_count` / `running_concurrency` / `overcommit_ratio` | `total_count` = k×N;`running_concurrency` =实际并发数(lifecycle 全量为 k×N,exec mode 为 N);`overcommit_ratio` = total÷running(lifecycle 全量为 1.0,exec mode 为 k——扫描横轴 `ratio=k` 在 driver 列,见下) |
| `throughput` | `total`(轨迹 attempt 数,schema 2:slot 跨 ephemeral create/kill 或 fixed multiplier 复用时按轨迹计数,不再按沙箱槽位)/ `succeeded`(跑完全部 step 的轨迹数)/ `failed`(= total − succeeded,恒 ≥ 0)/ `total_steps` / `steps_per_sec` / `tasks_per_sec`;非 M trial 的 valid 门槛读 `total`,M(multiplier)trial 读 `succeeded` |
| `admission` | `maximum, peak_active, granted, avg_queue_wait_sec, control_qps, control_dispatched`;`peak_active≤maximum` 是 valid 门槛(lifecycle 全量时 maximum = k×N);exec_only/exec_ephemeral 为 `null` |
| `lifecycle_overhead` | `pause_sec_sum, resume_sec_sum, pct_of_slice_total`(开销比率 %,与 Overview `lifecycle_overhead_aggregate_pct` 同源);仅 lifecycle |
| `paths` | `report, obs_xlsx, lifecycle_series, trajectory_index, vm_monitor_dir` 各产物路径 |
| `error` | 出错时的错误串;非空 = `return_code≠0` 的根因线索 |

## 5. 有效性(`compute_valid`)

一个 trial(跑一遍 k×N fleet,`round_count=1`)在"跑满大多数 running slot 且未过量准入"时判
**valid**:

- `return_code == 0`;
- `throughput.total >= 0.9 * N`(跑满整个 running fleet 的大多数——崩溃/早退在此失败;`test.duration`
  上限会盖住卡住的 run,随后也在此失败)。mode 无关:单轮 trial 每条轨迹 attempt 一次,
  lifecycle/exec_ephemeral `total` ≈ k×N、exec_only `total` ≈ N,健康 run 都 ≥ 0.9×N;设
  `multiplier`(M)时门槛改读 `succeeded`(完成数)≥ 0.9×M×k×N;
- `admission.peak_active <= maximum`(从不超过 summary 记录的并发上限——lifecycle 全量为整个 k×N fleet,受限模型为 N——exec_only/exec_ephemeral 跳过);
- `failure_rate <= failure_tolerance`。

`valid` 是"粗失败 / 过量准入"门槛,**不是**"每条轨迹都成功"——逐轨迹完成度在
`trajectory-detail.csv` 看(`trial-summary.csv` 里 `total` 对 `target_count`)。

## 6. 输出

每个 trial 后写入(被杀的 driver 也留下完整部分结果)到 `--output-root`:

| 文件 | 粒度 | 关键列 |
|------|------|--------|
| `trial-summary.csv` | 每 `(mode, ratio, repeat)` trial 一行 | `total, succeeded, failed, failure_rate, peak_active, wall_sec, tasks_per_sec, mean_stress_sec, stress_tasks_per_sec, steps_per_sec, lifecycle_overhead_pct, return_code, valid, target_count, test_duration` |
| `ratio-summary.csv` | 每 `(mode, ratio)` 一行(跨 repeat 取中位) | `attempted, successful, median_wall_sec, median_tasks_per_sec, median_stress_sec, median_stress_tasks_per_sec, avg_cmd_exec_sec, median_cmd_exec_sec, cmd_exec_degradation_vs_1_1_pct, time_degradation_vs_1_1_pct, throughput_gain_vs_1_1_pct, stress_throughput_gain_vs_1_1_pct` |
| `trajectory-detail.csv` | 每 trial 每条轨迹一行(取自 `trajectories/index.json`) | `trajectory_id, sandbox_index, n_steps, n_failed, success_rate, elapsed_sec` + 18 个 `*_sec` 分解列。该轨迹执行的命令序列(`action_series`)**不**在 CSV 里(多行单元格会把 Excel 每行撑得很高)——逐轨迹看 `replay_result.json` / `index.json`,或机器可读的 `benchmark-report.json` 的 `trajectory_details`。 |
| `benchmark-report.json` | 完整机器可读报告 | `configuration, trials, ratio_summary, trajectory_details`(每个 `trajectory_details` 行带 `action_series`) |

每个 trial 还有自己的子目录 `<output_root>/<mode>/ratio-XX/repeat-XX/`,里面是 bench-core 跑该
trial 的原生产物:`trial.yaml`(该 trial 的覆盖配置)、`driver.log`(子进程日志)、
`*_run_summary.json`、`*_obs.xlsx`(8 张表,见 [bench-core-usage-zh.md §8.4](bench-core-usage-zh.md#84-可观测性工作簿-_obsxlsx))、
`*_lifecycle_series.jsonl`、`trajectories/index.json`、`vm_monitor/`(若未 `--no-vm-monitor`)、
文本报告。要钻到单 step / 单 sandbox 级别就进这个目录开 obs workbook。

退化在**一个 mode 内**对该 mode 的 `k=1` 基线计算,所以每个被扫 mode 各得一条曲线。某 mode
无 `k=1` trial 时退化列默认 `0.0`。

### 怎么看结果(阅读顺序)

跑完一轮扫描,按下面顺序读,从"整体曲线"一路下钻到"单步瓶颈":

1. **`ratio-summary.csv` —— 入口,整条退化曲线。** 每行一个 `(mode, ratio)`,跨 repeat 取中位。
   重点两列:`time_degradation_vs_1_1_pct`(相对 k=1 的墙钟退化 %,正数=变慢)、
   `stress_throughput_gain_vs_1_1_pct`(剔除 create/kill 后的吞吐增益 %,评分口径)。oversub 的
   核心问题就是"超卖换来的吞吐增益是否盖过单轨迹的时延退化"——两条曲线放一起看。
   `throughput_gain_vs_1_1_pct` / `median_tasks_per_sec` 是含 create/kill 的端到端对照;
   `median_stress_sec` / `median_stress_tasks_per_sec` 只看 stress 窗口。每-cmd 平均执行时长看
   `avg_cmd_exec_sec`(sum(exec)/sum(steps),剔除含失败步的 run;`median_cmd_exec_sec` 是
   per-run 均值的中位)及其 `cmd_exec_degradation_vs_1_1_pct`(相对 k=1 的每-cmd 劣化 %)。
   `median_wall_sec` 是绝对值,`all_repeats_successful` 指该 ratio 所有 repeat 都 valid。
2. **`trial-summary.csv` —— 成败与 valid 门槛。** 先按 `valid` 列筛掉无效 trial;再比 `total`
   对 `target_count`(跑满没有)、`return_code`(非 0 的查 `error` 列;`130` 是被中断,见下节)。
   `peak_active` 应 ≤ `running_concurrency`(lifecycle 为 `k×N`,整个 fleet 都在跑;exec mode 为
   `N`),超过=过量准入。`lifecycle_overhead_pct` 看
   pause/resume 占比是否随 k 走高。
3. **`trajectory-detail.csv` —— 哪条轨迹在哪个 ratio 拖后腿。** 按 `trajectory_id` + `ratio`
   pivot,看 `elapsed_sec` 谁最大,再读 18 个 `*_sec` 分解列定位瓶颈是 exec 慢、resume/pause 慢
   (生命周期开销)、`capacity_wait_sec`(排队=slot 不够)、还是 `natural_delay_sec`(步间思考)。
   `create_error_type`/`kill_error_type` 非空说明该轨迹起/止就失败了。
4. **单 trial 子目录 —— 单 step/sandbox 级。** 进 `<mode>/ratio-XX/repeat-XX/` 开 `*_obs.xlsx`:
   Step detail 筛 `sandbox_index` 看某个沙箱的逐步时延,Gantt 看时间线,Snapshot sizes 看快照
   体积膨胀,Lifecycle overhead 看 pause/resume 分位。
5. **`benchmark-report.json` —— 喂给下游脚本。** 上面三张 CSV 的全量 JSON,`configuration` 段
   记录扫描参数(便于复现)。

> 一句话:`ratio-summary` 看"整体退化没、吞吐涨没",`trial-summary` 看"哪些 trial 有效",
> `trajectory-detail` 看"哪条轨迹为啥慢",obs workbook 看"哪个沙箱哪一步卡住"。

## 7. trial 顺序、reuse、cooldown、中断

- **顺序**:`for mode, for ratio, for repeat`——k 在一个 mode 内递增,自然退化曲线。
- **`reuse`**:跳过其既有 `run_summary.json` 已 valid 的 `(mode, ratio, repeat)`——Ctrl-C 后重启
  从上一个 good trial 继续。
- **`cooldown_sec`**:trial 间沉淀时间(首个 trial 和 `--dry-run` 时跳过)。
- **`cleanup_between_trials: on`**:每 trial 前跑 `bench-core --cleanup` 拆残留,免得上 ratio 的
  幸存者污染下一个。
- **`--dry-run`**:打印每个 `trial.yaml` + bench-core 命令并写空输出,不跑子进程。

**中断 vs 超时的 trial** — 被 Ctrl-C / driver 发起的 SIGTERM 中途打断的 trial **不丢弃**——
kernel 的 SIGTERM 协作 `finally` flush 出一份**部分** `run_summary.json` +
`trajectories/index.json`(产物经 temp-file + `os.replace` 原子写,flush 中途再来一次 SIGTERM 已
写文件仍完好),driver 捕获为一行,**`return_code == 130`** 且 `valid == False`。其 `total` /
`wall_sec` 只反映打断前跑掉的部分——这本身就是退化信号(某 ratio 在停滞窗口卡在 200/1152 条
轨迹,正是超卖崩溃点)。被打断的 trial **结束整个扫描**(driver 以 exit 130 停);**超时**的
trial(非零 `return_code` ≠ 130)**不结束**——扫描继续下一个 ratio,除非设了
`--stop-on-failure`。

> `_interrupted` 是内部行哨兵(非 CSV 列——`DictWriter` 丢弃),`main()` 读它区分"停扫描"(
> 用户中断)还是"继续"(trial 超时)。下游分析脚本应按落盘的 `return_code == 130` 列判断,而非
> 内存哨兵。

## 8. `oversub-compare` — 跨架构/配置对比

对**已经产出的** `oversub-bench` sweep 结果做跨架构/跨配置的对比与可视化。它**不跑沙箱、不产生
benchmark 数据**——只消费 driver 已写好的 CSV 契约(`trajectory-detail.csv` + `trial-summary.
csv`),把 N 个 sweep 目录按 `(mode, ratio, trajectory_id)` join,产出 delta CSV + xlsx。跟
`oversub-bench` 是兄弟,同样不导入 kernel 数据路径内部。

**典型场景:** ARM 基线 vs 若干 x86 频率/L3 配置,ratio 1:1–1:6,看各轨迹在不同超分比下的端到端
时延、exec/resume/pause/wait 分量、总体退化。host 级 freq/L3 cap 在 benchmark 外设(BIOS /
cpufreq / l3cat),工具**不嗅探**——你在 manifest 里声明每个目录代表什么。

**单 mode 一次比较** — 一次比较**同一个 mode 的一组 series**(如 `arm-life1 / arm-life2 /
x86-life`,或 `x86-exec1 / x86-exec2 / arm-exec`)。要对比两种 mode,**分两次跑**,各产一个
workbook 摆一起看。工具不假设两个 mode 混进一个 manifest(`mode` 列每目录恒定,单 mode 干净)。

### manifest

`config/oversub/compare-manifest.yaml` 是带注释模板:

```yaml
baseline_series: arm-ampere
output_dir: results/oversub/comparison-2026-09-19/   # null = comparison-<ts>/
series:
  - {label: arm-ampere, arch: arm, dir: results/oversub/arm/,       caps: {l3_mb: 256}}
  - {label: x86-base,   arch: x86, dir: results/oversub/x86-base/,   caps: {freq_ghz: 3.0, l3_mb: 64}}
  - {label: x86-freq2,  arch: x86, dir: results/oversub/x86-freq2/,  caps: {freq_ghz: 2.0, l3_mb: 64}}
```

`baseline_series` 是所有 delta 的参照(任一 series 都行)。`caps` 是**信息性**——在 Overview +
series 标签里露出,不被解析。相对 `dir` 路径相对 manifest 所在目录解析(可移植)。未知键被拒
(`serie:` 笔误直接报错)。

### 调用方式

```bash
oversub-compare --manifest config/oversub/compare-manifest.yaml
oversub-compare --arm results/oversub/arm/ --x86 results/oversub/x86/   # 双目录简写(arm 为基线)
oversub-compare --manifest ... --allow-mismatched-n   # N 不一致也硬跑(默认拒绝)
```

### 输入(每个 series 目录,driver 既有契约)

- `trajectory-detail.csv` — 每 `(mode, ratio, repeat, trajectory_id)` 一行 + `elapsed_sec` 与
  18 个 `*_sec` 分解列。
- `trial-summary.csv` — 每 trial 的 `wall_sec / tasks_per_sec / mean_stress_sec /
  stress_tasks_per_sec / lifecycle_overhead_pct / avg_queue_wait_sec` 等。
- `benchmark-report.json` — `configuration`(N-parity 校验用)。

缺文件直接报错并指路径。

### 输出(`<output_dir>/`)

| 文件 | 粒度 | 内容 |
|------|------|------|
| `comparison-tidy.csv` | 长表 | `series, arch, <caps…>, mode, ratio, repeat, trajectory_id, metric, value`(concat+melt,最灵活,pandas 直接 group;metric = 20 个 CSV 列 + 派生 `cmd_exec_sec` = `exec_sec/n_steps`,含失败步的 run 为 NaN) |
| `comparison-ratio-summary.csv` | 每 `(mode, ratio, series, metric)` | p50/p90/p99/avg 统计包(`median_sec/p90_sec/p99_sec/avg_sec`)+ delta vs 基线 + pct + 每个统计量各一列对该 series 自己 k=1 的退化%(`degradation_vs_ratio1_pct`=p50、`deg_p90/p99/avg_vs_ratio1_pct`);trial 级指标只有 median;无 k=1 的 series 退化为空(不崩) |
| `comparison-trajectory-delta.csv` | 按 `(mode, ratio, trajectory_id)` join | 基线 `elapsed_sec` + 各 series 值 + delta;缺席侧留空,orphan 计数上 Overview |
| `comparison-cross-ratio-trajectory.csv` | 每 `(series, mode, trajectory_id, ratio)` 一行 | 该轨迹在该 ratio 的 median `elapsed_sec` + 相对**同轨迹** ratio-1 median 的退化%(driver 已把轨迹池钉死跨 ratio 可 join);k=1 缺席的 tid 留空 |
| `comparison.xlsx` | 5 sheet | 见下 |

> 怎么看:先开 `comparison.xlsx` 的 **Overview** 看 N-parity 与 valid 计数,再到 **Per-ratio**
> 看曲线(谁更快/谁退化狠/lifecycle 开销),**Component heatmaps** 看哪个分量(exec/resume/
> pause/排队)在哪档 ratio 最烫,**Per-trajectory delta** 找具体哪条轨迹拉开差距,
> **Cross-ratio per-traj** 看同一条轨迹随 ratio 的退化。要脚本化分析用 tidy CSV。

### `comparison.xlsx` 五个 sheet

1. **Overview** — manifest(series/arch/caps/基线)、N-parity 校验结果、每 series valid/invalid
   trial 计数、median e2e 头表。
2. **Per-ratio** — LineChart(各一轴,非双轴):① 绝对 median e2e(谁更快);② 对各自 k=1 的退化%
   (谁超分退化更狠);③ `lifecycle_overhead_pct`(pause/resume 开销占比);④ 每 series 一块任务
   时长(`elapsed_sec`)退化表+图,p50/p90/p99/avg 各一条线(vs k=1)。exec_only 比较下 ③ 自动
   省略——driver 对 exec_only 写空 cell,pandas 读 NaN,子集为空就跳过,不留平零线。
3. **Component heatmaps** — 一个 component 一张 grid(rows=series,cols=ratio,单色 ColorScale 由浅
   到深):`exec / resume / pause / slot_contention_wait`。`create / kill` **不在内**——
   lifecycle/exec_only 下每步为 0(只有临时模式 exec_ephemeral 才有 per-trajectory
   create_one/kill_one),热力图只会平零;两列仍在 tidy CSV 里供 drill-down。
4. **Per-trajectory delta** — 按 trajectory join 的 e2e delta;freeze panes 在 D2。
5. **Cross-ratio per-traj** — 同轨迹跨 ratio 表(`comparison-cross-ratio-trajectory.csv` 的渲染):
   每条轨迹在各 ratio 的 median `elapsed_sec` + 相对自己 ratio-1 的退化%;freeze panes 在 D2。

### N-parity 守卫

`running_concurrency` 跨 series 必须一致——不同 N 意味着 ratio 1:k 映射到不同绝对沙箱数,跨
series 比较就是 apples-to-oranges。不一致默认拒绝,`--allow-mismatched-n` 硬跑。invalid trial
**留在 median 里**(中位对单个 flaky repeat 鲁棒),Overview 露出每 series 的 valid/invalid 计数,
由你决定要不要 drop + 重跑。

### Runbook

1. 主机 A 上:`oversub-bench --sweep-config config/oversub/lifecycle-1to6.yaml --output-root results/oversub/arm/`
2. 每个 x86 配置(主机间改 freq/L3):`oversub-bench ... --output-root results/oversub/x86-<config>/`
3. 编辑 `config/oversub/compare-manifest.yaml` 指向各目录 + 声明 caps。
4. `oversub-compare --manifest config/oversub/compare-manifest.yaml`
5. 打开 `results/oversub/comparison-<ts>/comparison.xlsx`。

对比两种 mode:重复 1–5,各自一个 manifest(一个 mode 一组 series),两个 workbook 摆一起看。

## 9. 排错

- **trial 判 invalid 但日志没崩** — 按阅读顺序:(1) `trial-summary.csv` 看 `total` vs
  `target_count`(跑满没有)、(2) `return_code`(`130` = 被中断)、(3) `admission.peak_active`
  是否 ≤ N(过量准入)。
- **高 ratio 下 client-bound(GIL 封顶)** — 单进程 dispatch ~50–55 steps/s 就到顶,加压只涨客户端
  排队。设 `shards`(auto = P=ratio)把负载拆到多个 kernel 进程;注意 lifecycle(全局准入)不可分片。
