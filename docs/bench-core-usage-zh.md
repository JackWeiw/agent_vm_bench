# bench-core 使用指南（src 内核）

> 主机无关的压测内核,通过 `EnvironmentProvider` 抽象驱动 e2b / docker / fake。
> 与冻结的 legacy `e2b_bench/`、`docker_bench/` 并存,互不影响(共享零代码)。
> 架构原理见设计文档 `docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md`。

## 概述

`bench_core` 把压测流程从沙箱实现里解耦:内核只通过一个 `exec()` 原语下发命令,
沙箱后端(e2b / docker / 未来的 kata / agentenv)只负责把命令送进沙箱并返回结果。
因此切换后端只需 `--provider`,**同一份压测配置在任一后端上跑同一套压力曲线**。

- `src/bench_core/` — 内核:`run_benchmark` 脊柱、stats/round_robin/task_runner、KernelConfig
- `src/env_provider/` — 契约(`EnvironmentProvider` ABC + `SandboxInstance`)+ e2b/docker/fake 实现
- `config/common/` — 后端无关的工作流配置(每个文件同时带 `e2b:` 和 `docker:` 块)

---

## 1. 安装

editable 安装后,`bench-core` 和 `python -m bench_core` 都可直接用,**无需 `PYTHONPATH=src`**:

> 需要 **Python 3.10+**(CI 跑 3.13;见 `pyproject.toml` 的 `requires-python`)。

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

`pip install -e .` 会按 `pyproject.toml` 自动拉取核心依赖(`psutil`、`paramiko`、
`flask`、`PyYAML`、`pandas`、`openpyxl` 等),无需再单独 `pip install -r requirements.txt`。

可选后端 SDK(用到哪个装哪个;不用可不装):

```bash
pip install e2b       # --provider e2b
pip install docker     # --provider docker
```

> 验证安装:`bench-core --provider fake --config config/common/browser.yaml --create-only -n 1`
> 能跑通即内核 + CLI + 配置解析全部就绪(fake 不需要任何 SDK)。

---

## 2. 配置

### 2.1 配置文件结构

`config/common/` 下每个 YAML = 一个工作流,同时携带两个后端块:

```yaml
workflow_type: browser        # browser | coding | document

e2b:                          # --provider e2b 读这一块
  template: "openclaw-browser-v1"
  numa_bind: 2
  sandbox_ids_file: "sandboxs.txt"
  env: { ... }                # 凭据占位符会自动回退 ~/.e2b/config.json

docker:                       # --provider docker 读这一块
  image: "ubuntu-openclaw-chromium:24.04-arm64"
  container_prefix: "oc-bench"
  cpu_limit: 2.0
  memory_limit: "2g"

# === 共享压力段(两个后端都读,→ KernelConfig)===
sandbox:      { total_count: 100, ... }
create_batch: { size: 20, interval: 3 }
task_batch:   { size: 10, interval: 5 }
browser:      { urls: [...], warmup_urls: [...], ... }
test:         { duration: 160, benchmark_mode: "round_robin", ... }
report:       { output_dir: "results/browser", filename_prefix: "browser_bench" }
```

内核只读共享段(`KernelConfig.from_raw` 是唯一读者);后端块由各自 `Config.from_raw` 读。
`--provider` 决定走哪个后端块,**同一份压力曲线跑在任一后端**。

### 2.2 工作流配置清单

| 配置 | workflow | 说明 |
|------|----------|------|
| `browser.yaml` | browser | 浏览器压测,round_robin tab-switch,100 沙箱 |
| `coding-ts.yaml` | coding | TypeScript(vuejs/core),`npx tsx` verify,verify_repeat=3 |
| `coding-go.yaml` | coding | Go(gohugoio/hugo),`go run` 冷编译 verify |
| `coding-python.yaml` | coding | Python(django/django),`python3` import 峰值 verify |
| `docker.yaml` | browser | docker 专用的小压力档(10 容器,单 Weibo url) |

### 2.3 coding 配置为何这么薄

三个 coding 配置都**省略了 `source_files`**——内核 `KernelConfig.__post_init__` 会按
`language` 自动填入 canonical 替换对(各 6 对,取自真实 swe_bench_multilingual 实例):
ts→vuejs/core、go→gohugoio/hugo、python→django/django。内容集中在
`src/bench_core/coding_payload.py`,配置只声明 `language` + `verify_cmd` + `verify_repeat`。

### 2.4 凭据:占位符自动回退

YAML 里 `your_e2b_access_token_here` / `your_e2b_api_key_here` 是占位符。
内核会把它视为"未设置",自动回退读 `~/.e2b/config.json`(E2B CLI 配置),
所以**直接复制模板即可,不必在 YAML 里填密钥**。也可用 `E2B_CONFIG` 环境变量指向别的路径。

### 2.5 就绪检查是 provider 透明的

就绪检查(沙箱创建后等它可用)是**工作流关注点,不是后端配置项**:
- browser → 端口扫描(`ss | grep :18789 :11436`)
- coding → `uname -a` 返回非空
- document → `document-bench-validate` 退出 0

由 `src/env_provider/_ready.py` 的 `ReadyChecker` 统一调度,e2b/docker 走同一逻辑,
**配置里不声明端口/时序**——这些都是内核常量。所以 docker 配置块只有 image/资源,没有 port_check。

---

## 3. CLI

```
bench-core --config <yaml> --provider {fake,e2b,docker} [模式/参数]
```

| 参数 | 说明 |
|------|------|
| `--config` | YAML 配置路径 |
| `--provider` | `fake`(无 SDK)/ `e2b` / `docker` |
| `-n, --total-count` | 覆盖沙箱总数 |
| `--workflow-type` | `browser` / `coding` / `document` |
| `-bm, --benchmark-mode` | `fixed` / `round_robin` |
| `--round-count` / `--round-size` / `--test-duration` / `--benchmark-percent` | 覆盖压测参数 |
| `--create-only` | 只创建+就绪检查+存 ID,然后退出(沙箱保留) |
| `--detect` | 复用已有沙箱(不新建);结束时不清理 |
| `--warmup-only` | 创建/检测 + 预热,然后退出(沙箱保留) |
| `--cleanup` | 列出+销毁所有现有沙箱,然后退出 |
| `-o, --output-dir` | 覆盖报告输出目录 |

> `bench-core` 找不到?见文末排错。等价写法:`python -m bench_core ...`。

---

## 4. 使用流程:阶段阶梯

按"创建 → 复用预热 → 短压测 → 清理"逐级验证,出问题能立刻定位是哪一阶段。
`--create-only` 和 `--detect` 都把沙箱留着不杀,所以最后用 `--cleanup` 收尾。

### Tier 0 — fake(零依赖,验证内核)

无 SDK、无 daemon,秒级。验证 `run_benchmark` 全流程 + report 生成:

```bash
bench-core --provider fake --config config/common/browser.yaml --test-duration 10 -n 3
bench-core --provider fake --config config/common/coding-ts.yaml --test-duration 10 -n 3
```

### Tier 1 — docker(本地 daemon,真实后端)

前置:Docker daemon 可达;镜像已构建且其 openclaw-gateway(18789)+llama-server(11436)会监听
(browser 就绪检查扫这两个端口)。

```bash
# 1) 创建 2 个容器、就绪检查、存 ID
bench-core --provider docker --config config/common/browser.yaml --create-only -n 2

# 2) 检测现有容器 + 预热(docker 的 detect 靠前缀 oc-bench-*)
bench-core --provider docker --config config/common/browser.yaml --detect --warmup-only

# 3) 检测 + 30s 短压测 + 出 report(detect 模式结束不杀容器)
bench-core --provider docker --config config/common/browser.yaml --detect --test-duration 30

# 4) 清理
bench-core --provider docker --config config/common/browser.yaml --cleanup
```

> 想先**只验 provider 接线**(不等 300s 端口):临时用 browser 镜像跑 coding 的 create-only
> ——coding 就绪是 `uname -a`,browser 镜像有 uname,立即通过:
> `bench-core --provider docker --config config/common/coding-ts.yaml --create-only -n 1`
> (临时把 coding-ts.yaml 的 `docker.image` 换成 chromium 镜像)
> 这验证 create/list/exec_probe/cleanup 接线,不依赖 openclaw 服务。

### Tier 2 — e2b(云端 firecracker,真实后端)

前置:`~/.e2b/config.json` 有凭据;e2b dev server(`E2B_API_URL`)可达;模板已构建
(`openclaw-browser-v1` / `openclaw-coding-{ts,go,python}-v1`)。

```bash
# 1) 创建 + 存 ID 到 sandboxs_ts.txt
bench-core --provider e2b --config config/common/coding-ts.yaml --create-only -n 2

# 2) 从 ID 文件 detect + 预热
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --warmup-only

# 3) detect + 30s 短压测 + report
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --test-duration 30

# 4) 清理
bench-core --provider e2b --config config/common/coding-ts.yaml --cleanup
```

browser / coding-go / coding-python 同理,换 `--config` 即可(各自 `sandbox_ids_file` 不同:
`sandboxs.txt` / `sandboxs_ts.txt` / `sandboxs_go.txt` / `sandboxs_python.txt`)。

### 阶段速记

| 命令 | 验证什么 | 沙箱去留 |
|------|---------|---------|
| `--create-only` | 创建 + 就绪 + ID 持久化 | 保留 |
| `--detect --warmup-only` | detect + attach + 预热 | 保留 |
| `--detect --test-duration N` | 全脊柱 + report | 保留(detect 不清理) |
| `--cleanup` | list + 销毁 | 清除 |

---

## 5. 报告

报告路径看各 YAML 的 `report.output_dir` + `filename_prefix`。create-only 模式出的是创建时序报告
(`Sandbox.create` 耗时 / 就绪等待 / 总启动 的 P50/P95/P99);完整压测出性能报告(任务统计 + 快照)。
报告里 `Create Failed` / `Ready Check Failed` 计数用于定位是创建、就绪还是任务阶段挂了。

---

## 6. Python API

```python
from bench_core.bench import run_benchmark, load_config
from bench_core.config import KernelConfig
from env_provider.fake import FakeProvider

# 1) 从 YAML 加载(KernelConfig 读共享段,raw 透传给后端)
config, raw = load_config("config/common/browser.yaml")

# 2) 构造 provider(e2b/docker 的 build_provider 也接受 (config, raw))
provider = FakeProvider(count=config.total_count)

# 3) 跑
result = run_benchmark(config, provider)
print(result["report"])        # 报告文本
print(result["filepath"])      # 报告文件路径(create-only/warmup-only 为 None)
```

---

## 7. 排错

**`bench-core: command not found`**
脚本装在 conda 的 `Scripts/` 下(如 `C:\Users\<user>\miniconda3\Scripts\bench-core.exe`),
该目录只在**激活了 conda** 的终端 PATH 里。解决:激活环境(`conda activate`),或用
`python -m bench_core ...`(不依赖 PATH)。注意命令是 `bench-core`(连字符),不是 `bench_core`。

**就绪检查超时(Ready Check Failed)**
- browser:容器/沙箱里 openclaw-gateway(18789)+llama-server(11436)没起来 → 检查镜像/模板
- coding/document:沙箱没正常启动 → 检查镜像/模板 + 沙箱日志

**e2b 凭据失败**
确认 `~/.e2b/config.json` 存在且有 `teamApiKey`/`accessToken`;或用 `E2B_CONFIG` 指向别处。
YAML 里保留占位符即可(会自动回退),别把真密钥写进 YAML。

**docker coding 镜像不存在**
coding 配置里的 `ubuntu-openclaw-coding-{ts,go,python}:24.04-arm64` 是占位符,需先构建
(含对应语言工具链 + 项目仓库),再用 `--provider docker` 跑 coding。browser 镜像现成。

---

## 8. Replay 工作流

`workflow_type: replay` 把录制的 SWE-bench agent 轨迹(有序 shell + `str_replace_editor`
动作,带 per-step `delay_time`)通过 `provider.exec()` 原样回放。同一份压力曲线可跑在
aenv(lifecycle pause/resume)或 e2b/docker(exec_only)上。`config/common/replay.yaml` 是
aenv lifecycle 内存超卖压测的 1:1 基线配置。

### 8.1 三种 mode

三种模式的根本区别是**沙箱生命周期**,不是"有没有限流":

| mode | 沙箱生命周期 | 每 step 做什么 | 并发/超卖控制 | 何时用 |
|------|------------|--------------|--------------|--------|
| `exec_only` | 预创建后**长驻**,整轮不创不杀 | 仅 exec | 无 | 测纯轨迹回放(exec)开销基线;后端无 lifecycle/ephemeral 能力(e2b/docker/fake) |
| `lifecycle` | 预创建后**长驻**,整轮不杀 | acquire slot→resume→exec→pause→release | pause 打快照释放内存,k×N 沙箱塞进 N slot(**内存超卖**) | 测 pause/resume 快照开销 + 内存 overcommit;需 LifecycleCapable(aenv) |
| `trajectory` | **临时**,每条轨迹 create→…→kill | acquire slot(整条轨迹持有)→resume→exec→pause→release | M slot 限并发轨迹数,未开始的推迟 create(**排队限流,非内存复用**) | 测频繁建删沙箱的 create/kill 开销 + 启动节流;需 EphemeralCapable(aenv) |

**exec_only vs trajectory 的区别不在"限流"**:

- exec_only 沙箱**长驻**,整轮压测用同一批预创建的沙箱,只 exec,**不创不杀、不 pause/resume**。
- trajectory 沙箱**临时**,每条轨迹单独 `create_one` → 跑完 → `kill_one`。
- `launch_interval_sec` 只是 trajectory 因为**频繁 create 才需要**的启动节流;exec_only 预创建
  一次、整轮复用,用不到启动节流。所以"trajectory 多了个限流"只是表象,本质是沙箱生命周期不同
  (长驻复用 vs 临时建删)。

**lifecycle vs trajectory 的超卖机制不同**:

- lifecycle 超卖 = **快照内存复用**。沙箱长驻,pause 释放物理内存,所以 `total_count = k×N`
  个沙箱能放进 `running_concurrency = N` 个 slot 的 RAM。running slot 按 **step 粒度**获取/释放
  (一条命令一轮 acquire/release)。
- trajectory 超卖 = **排队限流**。running slot 按**整条轨迹**持有(create 前 acquire、kill 后
  release);M slot → 同时 M 条轨迹在跑,其余排队等开始,**不会"暂停腾内存"**,沙箱用完即杀。

> `launch_interval_sec`(浮点秒,per-sandbox create 节流)只在 trajectory 模式生效。lifecycle
> 模式预创建用 `create_batch.size`/`interval`(且 `interval` 是整数秒),做不了 sub-second 级
> per-sandbox 节流——这是 lifecycle 的已知限制,需精细启动节流请用 trajectory。

### 8.2 lifecycle 内存超卖:ratio 配置法

整机内存固定,按"基线 VM 数 = 整机内存 / 单 VM 内存"算:

- 例:1.5 TiB 整机、单 VM 4 GiB → 基线 = 1536 / 4 = **384** 个 VM。
- `running_concurrency` 恒等于基线 VM 数(N 个 running slot);
  `total_count` 随超卖比 `1:k` 放大到 `k × 基线`。

| 超卖比 | `total_count` | `running_concurrency` | 含义 |
|--------|--------------|----------------------|------|
| 1:1(基线) | 384 | 384 | 无超卖,384 沙箱全跑 |
| 1:2 | 768 | 384 | 2x overcommit,768 沙箱在 384 slot 上多路复用 |
| 1:3 | 1152 | 384 | 3x overcommit |

`config/common/replay.yaml` 是 1:1 基线。测别的 ratio 时改三处(或用 `-n` 覆盖 `total_count`,
但 `running_concurrency` / `round_size` 在 YAML 里,需一起改):

```yaml
sandbox:
  total_count: 768        # k × 基线
test:
  round_size: 768         # 跟 total_count 一致 -> 单组=全部 -> 全并发
  # running_concurrency: 384   保持不变(N slot)
```

```bash
bench-core --provider aenv --config config/common/replay.yaml -n 768
```

> 扫描多个 ratio 测退化曲线时,写个脚本循环改 `total_count` + `round_size` 跑即可。

### 8.3 轨迹格式与 template_manifest

- bench-core 的 loader 期望每个轨迹 JSON 为
  `{instance_id, environment, trajectory:[{action, delay_time}, ...]}`,在首个
  `submit/finish/done` 处截断(见 `src/bench_core/replay_payload.py`)。若你的轨迹是别的
  字段名(如 sweagent 原始格式),需先转成 `.replay.json`。
- `template_manifest` 是 `{trajectory相对路径: template}` 的 side JSON。多模板时,非 trajectory
  模式按 template 亲和路由(孤儿模板跳过计数);trajectory 模式 `create_one(template=)` 逐条带。

### 8.4 可观测性工作簿 (`*_replay_obs_report.xlsx`)

`report.format: xlsx|both` 时,除了文本报告 + JSONL lifecycle series,还会产出
`<output_dir>/<prefix>_replay_obs_report.xlsx`(8 张表,openpyxl 渲染;Overview 为合并汇总看板——原 Admission & QPS / Throughput & overcommit / Retry impact 三张标量表已并入,数据名称栏填色加粗)。所有时长列**统一为秒(s)**,
与参考实现的 `step-detail.csv` 单位一致;`Per-step timings` / `Lifecycle overhead`
两张表的内嵌折图为可读性用毫秒(ms),表头会标注。无 lifecycle_series 文件时(如
minimal install),依赖 series 的表只输出表头,不报错。

| Sheet | 行粒度 | 内容 |
|-------|--------|------|
| Overview | 标量(合并汇总,分组着色) | **单表汇总**(原 Admission & QPS / Throughput & overcommit / Retry impact 三张标量表已并入此表):Run(mode/total_count/running_concurrency/test_duration/wall_sec/steps/success/failed/overcommit_ratio)+ Throughput(steps_per_sec/effective_parallelism/exec_wall_utilization/concurrency)+ Admission & QPS(running_slot 的 maximum/active/peak_active/granted/avg_queue_wait/waiting + QPS 限流 qps/inflight_cap/in_flight/dispatched/avg_wait/max_wait + per-operation 分发/等待子表)+ Retry(retry_count/time_lost_to_retry_sec/retries_per_slice_p95 + per-operation retry_queued)。数据名称栏(A 列)填色加粗,分组用 banner 行分隔 |
| Per-step timings | 池化百分位 | 全 fleet `latency`(=纯 exec 耗时)的 n/min/max/avg/p50/p95/p99,按 `action_type` 分桶;附 per-step 折图(ms) |
| Lifecycle overhead | 池化百分位 | `resume` / `pause` / `slice_total` / `slot_held` / `interaction` 五段的百分位;附 per-step 折图(ms)。仅 lifecycle/trajectory 模式 |
| Trajectory summary | **每 trajectory 一行** | n_steps + 各段 sum(slice_total/exec/resume/pause/interaction_total/slot_contention_wait/resume_rate_pacing_wait/pause_rate_pacing_wait/running_slot_held)+ avg_slice(秒)。按 trajectory_id 升序;trajectory 模式额外附 create_sec/kill_sec 百分位 |
| Step detail | **每 step 事件一行** | 见下表;含成功与 `slice_failed` 合成行,按 (trajectory, sandbox, step) 排序,冻结首行 + autofilter |
| Concurrency states | 每秒一行 | 每秒各 sandbox 的主导状态计数(pausing/paused/resuming/exec/active)+ 折图 |
| Gantt | 图 | 每 sandbox 的 phase 时间线(resume/exec/pause),内嵌 PNG;大 fleet 自动缩小行高 |
| Snapshot sizes | 每 pause 一行 | logical/disk/inherited/cumulative MiB + generations/files;附折图。仅 `SnapshotSizeCapable`(aenv) |

#### Step detail 列(26 列,秒)

子段紧贴其父总量,使和不变式可在表内直接验证:
`resume_sec == resume_inflight_wait_sec + resume_api_sec + resume_ready_wait_sec`(rate-pacing 属 PRE-lease,被排除),
`pause_sec == pause_rate_pacing_wait_sec + pause_inflight_wait_sec + pause_api_sec`(rate-pacing 属 IN-lease,被计入),
`interaction_total_sec == slice_total_sec + natural_delay_sec + capacity_wait_sec + resume_rate_pacing_wait_sec`(步间 think-delay 计入 `natural_delay_sec`,只算一次,不另设 delay 项)。`natural_delay_sec` 本身 = runner 在 slice 前实际睡掉的 think-delay(`step.delay_time_sec * replay_delay_scale`)+ 调度器残余 `ready_at` park(`replay_pause_duration`),故只要轨迹有步间间隔且 `delay_scale > 0` 即非 0。

| 列 | 含义 |
|----|------|
| `trajectory_id` | 该 step 所属轨迹的 instance_id |
| `sandbox_index` | 执行沙箱在 fleet 内的下标(0..N-1),非后端 sandbox_id |
| `round_id` | round_robin 轮次号;fixed/trajectory 模式为空 |
| `step_index` | 轨迹内 step 序号(0-based) |
| `action_type` | `shell`/`bash`/`str_replace_editor`/`submit`/`finish`/`done` |
| `slice_failed` | runner 合成的失败 slice(异常/stop_on_error);True 时下面时长列全 0 |
| `resume_sec` | resume 总时长 = inflight + api + ready_wait(rate-pacing 被排除,属 pre-lease) |
| `resume_rate_pacing_wait_sec` | QPS 限流器 1/qps 速率等待(resume,PRE-lease:不计入 resume_sec / running_slot_held) |
| `resume_api_sec` | 纯 resume API 调用耗时 |
| `resume_ready_wait_sec` | resume 后的就绪探测等待(lifecycle/trajectory 模式;exec_only 为 0) |
| `exec_sec` | 纯 `provider.exec()` 墙钟耗时(= Per-step timings 的 latency) |
| `pause_sec` | pause 总时长 = rate-pacing + inflight + api(rate-pacing 被计入,属 in-lease) |
| `pause_rate_pacing_wait_sec` | QPS 限流器 1/qps 速率等待(pause,IN-lease:计入 pause_sec / running_slot_held) |
| `pause_api_sec` | 纯 pause API 调用耗时 |
| `slice_total_sec` | resume + exec + pause;失败 slice 为 0(被排除出百分位计算) |
| `interaction_total_sec` | 一次交互的完整预算 = slice_total + natural_delay + capacity_wait + resume_rate_pacing(≥ slice_total;think-delay 计入 natural_delay,只算一次) |
| `slot_contention_wait_sec` | 派生复合量 = natural_delay + capacity_wait(FIFO running-slot 竞争;admission) |
| `natural_delay_sec` | 步间 think-delay——runner 在 slice 前睡掉的 `step.delay_time_sec * replay_delay_scale` + 调度器残余 `ready_at` park(= `replay_pause_duration`);步间间隔节流(slot_contention 的分量) |
| `capacity_wait_sec` | FIFO running-slot 令牌竞争——真正的排队等待(slot_contention 的分量) |
| `rate_pacing_wait_sec` | 单步速率等待合计 = resume_rate_pacing_wait_sec + pause_rate_pacing_wait_sec(1/qps 整形,RATE 控制;非 FIFO 排队,真排队见 capacity_wait_sec) |
| `inflight_wait_sec` | 单步 inflight 熔断阻塞合计 = resume_inflight_wait_sec + pause_inflight_wait_sec(CONCURRENCY 控制) |
| `resume_inflight_wait_sec` | resume 的 inflight 熔断阻塞(resume_sec 的分量) |
| `pause_inflight_wait_sec` | pause 的 inflight 熔断阻塞(pause_sec 的分量) |
| `running_slot_held_sec` | running slot 持有总时长(acquire→release) |
| `exit_code` | `provider.exec()` 退出码 |
| `timed_out` | 是否命中超时退出码 |

#### Trajectory summary 列(21 列,秒,sum-based)

每条轨迹(instance)一行,做**成本归因**——这条轨迹的总墙钟花在哪了(pause vs resume vs exec vs 各等待分量)。用 **sum 而非百分位**:per-instance 的 per-step 分布已在 `Step detail`(按 trajectory_id 筛)和 `Lifecycle overhead`(池化)里,这里只回答"总量分解 + 浪费性等待"。`n_steps` 计所有 step 事件(含 `slice_failed` 失败步,失败步对 sum 贡献 0 但计入尝试数,故 avg_slice 反映 per-attempt 成本)。

| 列 | 含义 |
|----|------|
| `trajectory_id` | 实例 |
| `n_steps` | 该轨迹累计回放的 step 总数(含失败步) |
| `n_failed` | 失败 step 数 |
| `n_timeout` | 命中超时退出码的 step 数 |
| `success_rate` | 成功率(0 step 时为 None) |
| `slice_total_sum_s` | 总活跃墙钟 = resume + exec + pause(和不变式) |
| `exec_sum_s` | 纯命令执行总耗时 |
| `resume_sum_s` | resume 总耗时 |
| `pause_sum_s` | pause 总耗时 |
| `interaction_total_sum_s` | 完整交互预算 = slice_total + natural_delay + capacity_wait + resume_rate_pacing(≥ slice_total;think-delay 计入 natural_delay,不另设 delay 项) |
| `slot_contention_wait_sum_s` | admission slot 竞争等待总耗时(派生 = natural_delay + capacity_wait) |
| `natural_delay_sum_s` | 步间 think-delay + 残余 park 总耗时(slot_contention 的分量) |
| `capacity_wait_sum_s` | FIFO running-slot 令牌竞争总耗时(slot_contention 的分量) |
| `rate_pacing_wait_sum_s` | 速率等待总耗时 = resume_rate_pacing + pause_rate_pacing(1/qps 整形) |
| `inflight_wait_sum_s` | inflight 熔断阻塞总耗时 = resume_inflight + pause_inflight |
| `resume_rate_pacing_wait_sum_s` | resume 的 QPS 限流 1/qps 速率等待总耗时(pre-lease) |
| `pause_rate_pacing_wait_sum_s` | pause 的 QPS 限流 1/qps 速率等待总耗时(in-lease) |
| `resume_inflight_wait_sum_s` | resume 的 inflight 熔断阻塞总耗时(resume 的分量) |
| `pause_inflight_wait_sum_s` | pause 的 inflight 熔断阻塞总耗时(pause 的分量) |
| `running_slot_held_sum_s` | running slot 持有总时长(slot 占用/超卖粒度) |
| `avg_slice_s` | slice_total_sum / n_steps,典型单步成本 |

1:1(无超卖)且 QPS / inflight 熔断旋钮未设时,`capacity_wait`、`rate_pacing`、`inflight` 及 resume/pause 的 rate-pacing/inflight 分量合理地为 0——只在超卖 + 控制面限流时才非 0。`natural_delay`(think-delay)只要轨迹有步间间隔且 `delay_scale > 0` 即非 0;它是 1:1 下的主要非生产性项,故 `interaction_total` ≫ `slice_total`。

> resume/pause 更细的子段(api_sec / ready_wait / inflight_wait / rate_pacing_wait)per-step 值见 `Step detail`;
> per-second 并发状态见 `Concurrency states`;snapshot 内存见 `Snapshot sizes`。
> per-instance 的 per-step 百分位分布不在本表——按 `trajectory_id` 在 `Step detail` 筛即可,
> 池化百分位见 `Lifecycle overhead` / `Per-step timings`。
> host 级系统资源(CPU/内存/NUMA)在独立的 vm_monitor `resource_report.xlsx`(`monitor.merge_report: false` 时)或合并进
> 本工作簿的 `VM_Stats`/`NUMA_Overview`/`DevKit_TopDown` sheet(`merge_report: true` 时)。

### 8.5 超卖扫描 (`oversub-bench`)

`oversub-bench` driver(`src/bench_core/oversub.py`)把 replay kernel 跑过一组内存/CPU 超卖比:`running_concurrency`(N)固定(取自 base config),`total_count = k×N` 每个 trial 缩放。每个 trial 一次 `bench-core` 调用;driver 读每个 trial 的机器可读 `run_summary.json`,聚合 per-trial + per-ratio 退化曲线。

**分层:kernel 出原始事实,driver 算 valid。** driver 不导入任何 kernel 数据路径内部(lifecycle / admission / stats / observability)——只用 CLI、YAML schema、`run_summary.json` schema 和共享的 `setup_logging` 助手。kernel 不知道自己是"扫描的第 k 个 ratio",它只输出发生了什么,由 driver 判定 trial 是否跑完。

#### 调用

```
oversub-bench --sweep-config config/oversub/lifecycle-1to3.yaml
oversub-bench --sweep-config config/oversub/lifecycle-1to3.yaml --ratios 4   # 覆盖单个旋钮
oversub-bench --config config/common/replay.yaml --provider aenv --ratios 1,2,3   # 无 sweep-config
```

优先级:**CLI 旗标 > sweep-config > 内置默认**。`--sweep-config` 承载所有旋钮 + `base_config`;`--config` 仅在没有 sweep-config 设 `base_config` 时必填。完整旗标集见 `oversub-bench --help`(`--running-concurrency`、`--modes`、`--repeats`、`--test-duration`、`--failure-tolerance`、`--cooldown-sec`、`--cleanup-between-trials`、`--trial-timeout-sec`、`--output-root`、`--reuse`、`--stop-on-failure`、`--dry-run`、`--no-vm-monitor`、`--bench-core-bin`)。

#### sweep-config 键

`config/oversub/template.yaml` 是带注释的模板;`lifecycle-1to3.yaml` 是现成的 aenv lifecycle 1:1/1:2/1:3 扫描。复制其一再改。未知键**会被拒绝**(像 `repeat:` vs `repeats:` 这种笔误会直接报错)。

| 键 | 默认 | 含义 |
|-----|---------|---------|
| `base_config` | —(必填) | base replay 压力 profile;每个 trial 深拷贝 |
| `provider` | `aenv` | `{aenv, e2b, docker, fake}` |
| `running_concurrency` | base `replay.running_concurrency` | N 个 running slot(跨 ratio 固定) |
| `ratios` | `1,2,3` | k 值(list 或 `"1,2,3"`);`total_count = k×N` |
| `modes` | `lifecycle,exec_only` | 要扫描的 replay mode(每个一条曲线) |
| `repeats` | `1` | 每个 `(mode, ratio)` 的重复次数(取中位) |
| `test_duration` | base `test.duration`(600) | 每个 trial 的硬上限(秒) |
| `failure_tolerance` | `0.0` | trial 判 valid 时允许的最大 `failure_rate` |
| `cooldown_sec` | `30` | trial 之间的沉淀时间 |
| `cleanup_between_trials` | `on` | trial 前跑 `bench-core --cleanup` 拆除残留 |
| `trial_timeout_sec` | `0` | 每个 trial 的外层墙钟;`0` = 关 |
| `output_root` | `results/oversub/oversub-N{N}-{ts}/` | 扫描输出目录 |
| `reuse` | `false` | 跳过已完成的 valid trial(重启安全) |
| `stop_on_failure` | `false` | 遇首个 invalid trial 即停 |
| `no_vm_monitor` | `false` | 透传 `--no-vm-monitor` 给 bench-core |
| `bench_core_bin` | `[bench-core]` | kernel 子进程命令(测试指向 stub) |

#### 每个 trial 的 config 覆盖

driver 深拷贝 `base_config` 并只改写超卖字段;其余透传:

| 字段 | 改成 | 原因 |
|-------|--------|-----|
| `sandbox.total_count` | `k×N` | 超卖目标 |
| `replay.running_concurrency` | `N`(固定) | 基线 slot 数 |
| `replay.mode` | 扫描的 mode | lifecycle / exec_only / trajectory |
| `test.round_size` | `k×N` | 所有 k×N 沙箱一组并发——不设的话,k≥2 会静默地按 N 一组串行多组,破坏超卖动态 |
| `test.duration` | `test_duration` | 时间上限 |
| `report.output_dir` / `report.filename_prefix` | 每 trial 的目录/前缀 | 隔离各 trial 产物 |

`test.round_count`(工作量旋钮:跑几遍整个 fleet)**透传**。trial 在两者先到时结束:`round_count` 轮 OR `test.duration` 秒(kernel 自带的先到为准语义)。`round_count: 1`(base 默认)= 有界单遍 trial;`0` = 持续到 duration 的窗口。

#### `run_summary.json`(kernel → driver 契约)

每个 trial 写 `{prefix}_run_summary.json`——kernel 与 driver 之间唯一的契约。只含原始事实(kernel 绝不为 driver 重算它需要的指标):

| 字段 | 含义 |
|-------|---------|
| `schema_version` | `1` |
| `replay_mode`、`provider` | 哪个 mode / 后端 |
| `started_at`、`completed_at`(+ `_epoch`) | ISO 本地 + epoch 秒 |
| `test_duration`、`wall_sec` | 配置上限 vs 实际墙钟 |
| `total_count`、`running_concurrency`、`overcommit_ratio` | 该 trial 的 k×N / N / k |
| `throughput` | `total, succeeded, failed, total_steps, steps_per_sec, tasks_per_sec`(`total` = 跑过的沙箱数;`succeeded` = 跑完全部 step 的轨迹数) |
| `admission` | `maximum, peak_active, granted, avg_queue_wait_sec, control_qps, control_dispatched`——exec_only 为 `null`(无 admission 控制器) |
| `lifecycle_overhead` | `pause_sec_sum, resume_sec_sum, pct_of_slice_total`——仅 lifecycle/trajectory |
| `paths` | `report, obs_xlsx, lifecycle_series, trajectory_index, vm_monitor_dir` |
| `error` | trial 出错时的错误串 |

#### 有效性(`compute_valid`)

一个 trial(跑一遍 k×N fleet,`round_count=1`)在"跑(接近)满 running fleet 且未过量准入"时判 **valid**:

- `return_code == 0`(kernel 子进程成功);
- `throughput.total >= 0.9 * N`(跑满了 N 个 running slot 的大多数——崩溃/早退在此失败;`test.duration` 上限会盖住一个卡住的 run,随后也在此失败)。mode 无关:lifecycle `total` ≈ k×N、exec_only `total` ≈ N,健康 run 都 ≥ 0.9×N;
- `admission.peak_active <= N`(从不超过 N 并发——exec_only 跳过);
- `failure_rate <= failure_tolerance`(可配置的包装噪声容忍)。

`valid` 是"粗失败 / 过量准入"门槛,**不是**"每条轨迹都成功"——逐轨迹完成度在 `trajectory-detail.csv` 看(`trial-summary.csv` 里 `total` 对 `target_count`)。

#### 输出

每个 trial 后写入(这样被杀的 driver 也留下完整部分结果)到 `--output-root`(默认 `results/oversub/oversub-N{N}-{ts}/`):

| 文件 | 粒度 | 关键列 |
|------|-------------|-------------|
| `trial-summary.csv` | 每个 `(mode, ratio, repeat)` trial 一行 | `total, succeeded, failed, failure_rate, peak_active, wall_sec, tasks_per_sec, steps_per_sec, lifecycle_overhead_pct, return_code, valid, target_count, test_duration` |
| `ratio-summary.csv` | 每个 `(mode, ratio)` 一行(跨 repeat 取中位) | `attempted, successful, median_wall_sec, median_tasks_per_sec, time_degradation_vs_1_1_pct, throughput_gain_vs_1_1_pct` |
| `trajectory-detail.csv` | 每 trial 每条轨迹一行(取自 `trajectories/index.json`) | `trajectory_id, sandbox_index, n_steps, n_failed, success_rate, elapsed_sec` + 18 个 `*_sec` 分解列(exec/resume/pause/requested_delay/create/kill/slice_total/interaction_total/slot_contention_wait/natural_delay/capacity_wait/rate_pacing_wait/inflight_wait/resume_rate_pacing_wait/pause_rate_pacing_wait/resume_inflight_wait/pause_inflight_wait/running_slot_held) |
| `benchmark-report.json` | 完整机器可读报告 | `configuration, trials, ratio_summary, trajectory_details` |

退化在**一个 mode 内**对该 mode 的 `k=1` 基线计算,所以 lifecycle 和 exec_only 各得一条曲线(内存 overcommit 开销 vs CPU 超卖退化)。某 mode 无 `k=1` trial 时,退化列默认 `0.0`。

#### trial 顺序、reuse、cooldown

- **顺序:** `for mode, for ratio, for repeat`——自然的退化曲线(k 在一个 mode 内递增)。
- **`reuse`:** 跳过其既有 `run_summary.json` 已 valid 的 `(mode, ratio, repeat)`——Ctrl-C 后重启从上一个 good trial 继续。
- **`cooldown_sec`:** trial 之间的沉淀时间(首个 trial 和 `--dry-run` 时跳过)。
- **`cleanup_between_trials: on`:** 每 trial 前跑 `bench-core --cleanup`(拆除残留),免得上一 ratio 的幸存者污染下一个。
- **`--dry-run`:** 打印每个 `trial.yaml` + bench-core 命令并写空输出——不跑子进程。

#### 中断 vs 超时的 trial

被 Ctrl-C / driver 发起的 SIGTERM 中途打断的 trial **不丢弃**——kernel 的 SIGTERM 协作 `finally` flush 出一份**部分** `run_summary.json` + `trajectories/index.json`(产物经 temp-file + `os.replace` 原子写,所以 flush 中途再来一次 SIGTERM,已写的文件仍完好),driver 把它捕获为一行,**`return_code == 130`** 且 `valid == False`。其 `total` / `wall_sec` 只反映打断前跑掉的部分——这本身就是退化信号(一个 ratio 在停滞窗口里卡在 200/1152 条轨迹,正是超卖崩溃点)。被打断的 trial **结束整个扫描**(driver 以 exit 130 停);而**超时**的 trial(非零 `return_code` ≠ 130)**不会**——扫描继续下一个 ratio,除非设了 `--stop-on-failure`。

> `_interrupted` 是内部行哨兵(非 CSV 列——`DictWriter` 丢弃它),`main()` 读它来区分"停扫描"(用户中断)还是"继续"(trial 超时)。下游分析脚本应按落盘的 `return_code == 130` 列判断,而非内存哨兵。
