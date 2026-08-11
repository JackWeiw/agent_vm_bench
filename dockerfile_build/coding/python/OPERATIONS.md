# Python (django) 变体 · 完整构建与测试操作手册

本手册覆盖 Python/django 变体的完整链路：构建 Docker 镜像 → 推送 Harbor → 构建 E2B 模板 → 手动测试 → 正式跑基准 → 查看结果。

> 当前镜像已固定到 SWE-bench `django__django-15781` 的 base_commit
> `8d160f154f0240a423e83ffe0690e472f837373c`（复刻该实例轨迹的源码状态）。

## 链路总览

```
① 构建 Docker 镜像  →  ② 推送 Harbor  →  ③ 构建 E2B 模板+建沙箱
                →  ④ 手动测试(沙箱内 bench_helper.sh)
                →  ⑤ 正式跑基准(e2b_bench yaml)  →  ⑥ 看结果
```

| 环节 | 文件 |
|------|------|
| Dockerfile 系列 | `dockerfile_build/coding/python/Dockerfile{,.x86,.openeuler,.openeuler.x86}` |
| 推送 Harbor | `dockerfile_build/coding/python/push_to_harbor.sh` |
| 沙箱内手动测试 | `dockerfile_build/coding/python/bench_helper.sh` |
| E2B 模板构建 | `dockerfile_build/build_e2b.py` |
| 正式基准配置 | `config/e2b/coding_python_bench.yaml` |

---

## 0. 前置条件

- 本机已 `docker login <harbor_ip>:2900`（Harbor）
- 有 E2B 凭证：`~/.e2b/config.json`（含 `accessToken` / `teamApiKey`），或准备填到 yaml 的 `e2b_env` 里
- 本机可访问代理 `http://90.255.211.160:8888`（`PROXY` 默认值，脚本装系统包/websocat 时使用）

---

## 1. 构建 Docker 镜像

```bash
cd dockerfile_build/coding/python

# ubuntu arm64（默认）
docker build -t ubuntu-coding-python-bench:24.04-linuxarm64 -f Dockerfile .
# ubuntu x86_64
docker build -t ubuntu-coding-python-bench:24.04-x86_64 -f Dockerfile.x86 .
# openEuler arm64 / x86_64
docker build -t openeuler-coding-python-bench:24.03-lts-sp3-linuxarm64 -f Dockerfile.openeuler .
docker build -t openeuler-coding-python-bench:24.03-lts-sp3-x86_64 -f Dockerfile.openeuler.x86 .
```

> 注意：镜像现在 `git clone`（全量）+ `git checkout 8d160f154f0240a423e83ffe0690e472f837373c`，
> 固定到 django-15781 的 base_commit。构建会拉全量 django 历史，比原浅克隆慢，
> 但能精确保到该实例的源码状态。构建成功会打印 `django <version>` 表示 `import django` 校验通过。

---

## 2. 推送 Harbor

```bash
cd dockerfile_build/coding/python

HARBOR_IP=<你的harbor_ip> bash push_to_harbor.sh                        # ubuntu arm64（默认）
ARCH=x86  HARBOR_IP=<你的harbor_ip> bash push_to_harbor.sh              # ubuntu x86_64
OS=openeuler HARBOR_IP=<你的harbor_ip> bash push_to_harbor.sh           # openEuler arm64
OS=openeuler ARCH=x86 HARBOR_IP=<你的harbor_ip> bash push_to_harbor.sh  # openEuler x86_64
```

脚本流程：从 base 镜像起临时容器 → 装 E2B 系统包（systemd/openssh/websocat）→
`docker export/import` 成 `:custom` → 推送。

**推送目标**：`<harbor_ip>:2900/e2b-orchestration/<ubuntu|openeuler>-coding-python-bench:custom`

> 注意：四个变体共用 `:custom` 标签会互相覆盖，最后推谁就是谁。

---

## 3. 构建 E2B 模板 + 建沙箱

```bash
cd dockerfile_build   # build_e2b.py 在这个目录
python3 build_e2b.py \
    --server-ip 141.61.17.196 \        # E2B API 服务 IP（脚本默认就是这个）
    --harbor-ip  141.61.17.196 \       # Harbor 所在 IP
    --alias     openclaw-coding-python-v1 \
    --image     e2b-orchestration/ubuntu-coding-python-bench:custom \
    --cpu 2 \
    --memory 4096
```

- `build_e2b.py` 读 `~/.e2b/config.json` 拿凭证，从 Harbor 拉镜像构建 E2B 模板并创建一个测试沙箱。
- Harbor 仓库通过 `IP:30443`（nginx 反代）访问。
- 返回的沙箱 ID 用于后续手动测试或正式基准。

---

## 4. 手动测试（沙箱内）

进到沙箱 shell 后（E2B SDK / websocat 连接），运行复刻轨迹的 `find → read → edit → verify → diff` 循环：

```bash
bash /opt/coding-bench/bench_helper.sh 0            # Round 0，全流程
bash /opt/coding-bench/bench_helper.sh 1            # Round 1（轮换到下一个编辑目标）
bash /opt/coding-bench/bench_helper.sh --no-verify  # 跳过 verify（只测编辑）
bash /opt/coding-bench/bench_helper.sh --help       # 帮助
```

每轮输出：目标文件、find/replace、`python3 /tmp/bench_verify.py` 的 verify 耗时/退出码、补丁落盘。

手动观察内存峰值（host 侧）：

```bash
numastat -p firecracker      # 空闲基线 ~200-300MB
numastat -p firecracker      # verify 运行期间 → 看瞬态峰值
```

---

## 5. 正式跑基准

配置在 `config/e2b/coding_python_bench.yaml`，关键参数：

- `sandbox.template: openclaw-coding-python-v1`、`total_count: 10`
- `coding.language: python`、`project_dir: /opt/coding-bench`、`verify_cmd: python3 /tmp/bench_verify.py`
- `coding.source_files`：6 组替换对
- `test.benchmark_mode: round_robin`、`duration: 160`

**先填真实 E2B 凭证**（yaml 里目前是占位符 `your_e2b_access_token_here`）。

```bash
# 只创建沙箱（Phase 0）
python -m e2b_bench -c config/e2b/coding_python_bench.yaml --create-only

# 或检测已有沙箱再跑任务
python -m e2b_bench -c config/e2b/coding_python_bench.yaml --detect -bm round_robin
```

---

## 6. 查看结果

- 报告输出目录：`results/e2b/coding_python/`，前缀 `e2b_coding_python_bench`（见 yaml `report` 段）
- N 个沙箱交错的 verify 峰值在 host 侧形成内存 overcommit，由 `vm_monitor` / `smap_tool` 观测
- 批量矩阵测试：`python -m e2b_bench --batch --matrix <matrix>`
- 离线汇总：`python -m e2b_bench --batch --offline --result-dir ...`

---

## 重要提醒（关于 base_commit 固定）

镜像固定到 django-15781 的 base_commit（约 2022 年 4.x 时代），但 `bench_helper.sh` 和 yaml
里的 6 组替换对原本针对最新 django HEAD 编写。固定 commit 后建议先验证这些 find 串仍能命中，
否则某轮会落到"通用 comment-marker"回退：

```bash
# 在容器/沙箱里逐一确认
cd /opt/coding-bench
grep -n 'LANGUAGE_CODE = "en-us"'  django/conf/global_settings.py
grep -n 'class Field(RegisterLookupMixin):' django/db/models/fields/__init__.py
grep -n 'class HttpResponse:' django/http/response.py
grep -n 'def slugify(value, allow_unicode=False):' django/utils/text.py
grep -n 'class Template:' django/template/base.py
grep -n 'class URLResolver:' django/urls/resolvers.py
```

如某个文件在 base_commit 下路径/字符串变化，需同步修改 yaml 与 bench_helper.sh 中对应替换对。
