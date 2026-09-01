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
