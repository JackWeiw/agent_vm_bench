# Agent VM Bench — 主机无关的沙箱性能压测

[English](README.md)

虚拟化场景的性能测试框架。**`bench_core` 内核 + `env_provider` 抽象**是主机无关的核心：通过 `EnvironmentProvider` 抽象驱动 e2b / docker / 未来的 kata / agentenv，切换 `--provider` 即切后端，**同一份 `config/common/*.yaml` 压力曲线跑在任一后端上**。

- **`src/bench_core/`** — 内核：`run_benchmark` 脊柱、stats / round-robin / task runner、`KernelConfig`。主机无关，绝不静态导入后端 SDK。
- **`src/env_provider/`** — `EnvironmentProvider` 契约（ABC + `SandboxInstance`）+ e2b / docker / fake 实现（opt-in 子模块；契约本体不依赖任何 SDK）。
- **`config/common/`** — 后端无关的工作流配置（每个文件同时带 `e2b:` 和 `docker:` 块；`--provider` 决定读哪块）。

冻结的 legacy `e2b_bench/`、`docker_bench/` 与 OpenStack（`vm_bench/`、`auto_vm_test.py`、`batch_test_scheduler.py`）仍保留给现有用户，与内核零代码共享。

## 文档导航

| 文档 | 说明 |
|------|------|
| [bench-core 使用指南](docs/bench-core-usage-zh.md) | **src 内核压测（推荐）：安装→配置→CLI→清理** |
| [设计文档](docs/design.md) | 系统架构与流程设计 |
| [设计文档 (英文)](docs/design-en.md) | 英文版设计文档 |
| [指标参考](docs/metrics-reference.md) | 50+ 指标详细说明 |
| [使用指南](docs/usage-guide.md) | 详细工具使用与配置 |
| [vm_bench 使用指南](docs/vm_bench-usage-guide-zh.md) | 模块化 vm_bench（OpenStack） |
| [E2B Bench 使用指南](docs/e2b-bench-usage-zh.md) | E2B 沙箱批量性能测试 |
| [Docker Bench 使用指南](docs/docker-bench-usage-zh.md) | Docker 容器浏览器自动化性能测试 |

## 贡献与社区

- [贡献指南](CONTRIBUTING.md) — 开发环境搭建、测试与提交流程
- [行为准则](CODE_OF_CONDUCT.md) — 参与标准
- [获取帮助](SUPPORT.md) — 提问、反馈与建议渠道
- [RFC 流程](docs/rfcs/README.md) — 用于重大设计变更
- [Issue 模板](.github/ISSUE_TEMPLATE/) — Bug、Feature 与性能异常表单

---

## 快速开始（bench-core）

### 1. 安装

```bash
python -m pip install -e .
```

editable 安装后注册 `bench-core` 和 `python -m bench_core`，**无需 `PYTHONPATH=src`**。后端 SDK 按需安装（用到哪个装哪个；`fake` 不需要）：

```bash
pip install e2b       # --provider e2b
pip install docker     # --provider docker
```

一条命令验证内核 + CLI + 配置解析全部就绪（无 SDK）：

```bash
bench-core --provider fake --config config/common/browser.yaml --create-only -n 1
```

### 2. 配置

`config/common/*.yaml` —— 一个工作流一个文件，每个文件同时带 `e2b:` 和 `docker:` 块。`--provider` 决定读哪块，所以**同一份压力曲线跑在任一后端**：

```yaml
workflow_type: browser        # browser | coding | document

e2b:                          # --provider e2b 读这一块
  template: "openclaw-browser-v1"
  numa_bind: 2
  sandbox_ids_file: "sandboxs.txt"
  env: { ... }                # 占位符凭据自动回退 ~/.e2b/config.json

docker:                       # --provider docker 读这一块
  image: "ubuntu-openclaw-chromium:24.04-arm64"
  container_prefix: "oc-bench"
  cpu_limit: 2.0
  memory_limit: "2g"

# === 共享压力段（两个后端都读 → KernelConfig）===
sandbox:      { total_count: 100 }
create_batch: { size: 20, interval: 3 }
test:         { duration: 160, benchmark_mode: "round_robin" }
```

| 配置 | 工作流 | 说明 |
|------|--------|------|
| `browser.yaml` | browser | round-robin tab-switch，100 沙箱 |
| `coding-ts.yaml` | coding | TypeScript（vuejs/core），`npx tsx` verify |
| `coding-go.yaml` | coding | Go（gohugoio/hugo），`go run` verify |
| `coding-python.yaml` | coding | Python（django/django），`python3` verify |
| `docker.yaml` | browser | docker 专用小压力档（10 容器） |

coding 配置很薄：省略 `source_files` → `KernelConfig` 按 `language` 自动填 canonical 替换对。就绪检查**与 provider 透明**（browser = 端口扫描，coding = `uname -a`，document = `document-bench-validate`）——任何 YAML 都不声明端口/时序。

### 3. CLI

```text
bench-core --config <yaml> --provider {fake,e2b,docker} [模式/参数]
```

| 参数 | 说明 |
|------|------|
| `--provider` | `fake`（无 SDK）/ `e2b` / `docker` |
| `-n, --total-count` | 覆盖沙箱总数 |
| `--create-only` | 创建 + 就绪检查 + 存 ID，然后退出（沙箱保留） |
| `--detect` | 复用已有沙箱（不新建）；结束不清理 |
| `--warmup-only` | 创建/检测 + 预热，然后退出（沙箱保留） |
| `--cleanup` | 列出 + 销毁所有现有沙箱，然后退出 |
| `-bm, --benchmark-mode` | `fixed` / `round_robin` |
| `--test-duration` / `--benchmark-percent` / `--round-count` / `--round-size` | 压测参数 |
| `-o, --output-dir` | 报告输出目录 |

> `bench-core: command not found`？脚本装在当前解释器的 `Scripts/` 下（如 conda 的），需激活该环境，或用 `python -m bench_core`。

### 4. 阶段阶梯

逐级验证；`--create-only` 和 `--detect` 都把沙箱留着不杀，最后用 `--cleanup` 收尾。

```bash
# Tier 0 — fake（零依赖，验证内核全流程 + report）
bench-core --provider fake --config config/common/browser.yaml    --test-duration 10 -n 3
bench-core --provider fake --config config/common/coding-ts.yaml  --test-duration 10 -n 3

# Tier 1 — docker（本地 daemon）
bench-core --provider docker --config config/common/browser.yaml --create-only -n 2
bench-core --provider docker --config config/common/browser.yaml --detect --warmup-only
bench-core --provider docker --config config/common/browser.yaml --detect --test-duration 30
bench-core --provider docker --config config/common/browser.yaml --cleanup

# Tier 2 — e2b（云端 firecracker）
bench-core --provider e2b --config config/common/coding-ts.yaml --create-only -n 2
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --warmup-only
bench-core --provider e2b --config config/common/coding-ts.yaml --detect --test-duration 30
bench-core --provider e2b --config config/common/coding-ts.yaml --cleanup
```

| 命令 | 验证什么 | 沙箱去留 |
|------|---------|---------|
| `--create-only` | 创建 + 就绪 + ID 持久化 | 保留 |
| `--detect --warmup-only` | detect + attach + 预热 | 保留 |
| `--detect --test-duration N` | 全脊柱 + report | 保留（detect 不清理） |
| `--cleanup` | list + 销毁 | 清除 |

完整安装→配置→排错见 [bench-core 使用指南](docs/bench-core-usage-zh.md)；架构原理见
[设计文档](docs/superpowers/specs/2026-08-12-environment-provider-bench-core-design.md)。

---

## 旧版后端

`src/` 之前的工具已冻结，与内核零代码共享。仅当依赖其特有行为（batch 调度、smap_tool / vm_monitor 集成）时才用：

| 后端 | 入口 | 指南 |
|------|------|------|
| E2B batch（旧） | `python -m e2b_bench --config config/e2b/bench.yaml` | [E2B Bench 使用指南](docs/e2b-bench-usage-zh.md) |
| Docker（旧） | `python -m docker_bench --config config/docker/docker_bench.yaml` | [Docker Bench 使用指南](docs/docker-bench-usage-zh.md) |
| OpenStack VM | `python -m vm_bench --config config/openstack/vm_bench.yaml` | [vm_bench 使用指南](docs/vm_bench-usage-guide-zh.md) |
| OpenStack batch | `python3 batch_test_scheduler.py --config config/openstack/batch_config.yaml` | — |

### PDF / XLSX 文档场景

两个文档场景共用 `openclaw-document-v1` E2B Template 与 `dockerfile_build/document/` 下的镜像。
`config/e2b/pdf_bench.yaml`、`config/e2b/xlsx_bench.yaml` 中的 token、API key、`http://localhost:3000`
是占位值；连接远程 E2B 服务器时用 CLI 覆盖，凭据不回显终端：

```bash
read -rsp "E2B access token: " DOCUMENT_E2B_ACCESS_TOKEN; echo
read -rsp "E2B API key: " DOCUMENT_E2B_API_KEY; echo
read -rp "E2B API URL（例如 http://SERVER_IP:3000）: " DOCUMENT_E2B_API_URL

DOCUMENT_E2B_ARGS=(
  --e2b-access-token "${DOCUMENT_E2B_ACCESS_TOKEN}"
  --e2b-api-key "${DOCUMENT_E2B_API_KEY}"
  --e2b-api-url "${DOCUMENT_E2B_API_URL}"
  --e2b-http-ssl false --e2b-domain e2b.app
)

python -m e2b_bench -c config/e2b/pdf_bench.yaml  "${DOCUMENT_E2B_ARGS[@]}"
python -m e2b_bench -c config/e2b/xlsx_bench.yaml "${DOCUMENT_E2B_ARGS[@]}"
unset DOCUMENT_E2B_ACCESS_TOKEN DOCUMENT_E2B_API_KEY DOCUMENT_E2B_API_URL DOCUMENT_E2B_ARGS
```

---

## vm_monitor 包

统一监控框架，支持多种 VMM 类型（被旧版 batch 调度器使用；尚未接入 bench-core 内核）：

| VMM 类型 | 进程名 | CLI 参数 |
|----------|--------|----------|
| QEMU | `qemu-kvm`, `qemu-system` | `--vmm qemu`（默认） |
| Firecracker | `firecracker` | `--vmm firecracker` |

```python
from vm_monitor import QEMUMonitor, FirecrackerMonitor

QEMUMonitor().start_monitoring(duration_seconds=60, interval_seconds=3)
FirecrackerMonitor().start_monitoring(duration_seconds=60, interval_seconds=3)
```
