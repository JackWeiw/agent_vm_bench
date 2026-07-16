# 任务规划 - 容器场景浏览器测试 Bench

## 任务概述

新增容器场景的浏览器测试方案，参考 e2b_bench 设计，用于验证 OpenClaw 浏览器自动化能力在 Docker 容器化部署环境下的并发性能与稳定性。

## 目标

- 基于 `ubuntu-openclaw-chromium` 镜像，在宿主机上批量创建容器实例
- 每个容器独立运行 Chromium 浏览器并执行 OpenClaw CLI 操作流程
- 支持多容器并发压测模拟真实业务负载
- 以总体 QPS（每秒成功请求数）作为核心指标评估吞吐能力

## 测试流程设计

```text
前置: openclaw browser status && start（热启动，复用后台进程）
Step 1: openclaw browser open [URL] --label [NAME]  → 页面打开
Step 2: openclaw browser focus [TAB_ID]             → 标签聚焦
Step 3: openclaw browser snapshot --limit 200       → DOM快照
Step 4: openclaw browser click e218                 → 元素点击（失败重试）
Step 5: openclaw browser screenshot                 → 视觉截图
后置: rm -rf /root/.openclaw/browser/openclaw/user-data（擦除缓存）
```

## 架构设计（参考 e2b_bench）

```text
docker_bench/
├── __init__.py         # 包初始化
├── __main__.py         # 模块入口
├── bench.py            # 主入口 - 测试流程控制
├── config.py           # 配置管理
├── container_manager.py # 容器生命周期（创建、端口检查、清理）
├── task_runner.py      # 浏览器任务执行（支持批量控制）
├── stats_collector.py  # 统计收集与报告生成
├── schemas.py          # 数据结构定义
├── utils.py            # 工具函数
├── .env.example        # 环境变量模板
└── requirements.txt    # 依赖说明

config/
└── docker_bench.yaml   # 配置文件模板
```

## 关键差异（容器 vs E2B Sandbox）

| 特性 | E2B Sandbox | Docker Container |
|------|-------------|------------------|
| 生命周期管理 | E2B SDK (`sandbox.create/kill`) | Docker CLI/SDK (`docker run/exec/rm`) |
| 命令执行 | `sandbox.commands.run()` | `docker exec` |
| 端口检查 | 18789 + 11436 | 可配置（同端口或不同） |
| 资源限制 | E2B template CPU/Mem | `--cpus`, `-m` 参数 |
| 网络模式 | E2B 内部网络 | Docker bridge/host |
| 标识 | `sandbox_id` | `container_name` |

## 设计决策

### 待确认
1. 容器创建方式：使用 Docker SDK 还是 CLI？
2. 容器命名规则：统一前缀 + 序号？
3. 测试 URL：使用本地服务器还是远程 URL？
4. 是否需要预热阶段？
5. 是否需要端口检查等待？

### 已确定
1. 架构参考 e2b_bench，保持相似的设计风格
2. 支持批量创建和批量任务控制
3. 支持多种运行模式（完整流程、仅创建、检测已有、仅预热）
4. 容器规格：2vCPU/2G（可配置）
5. 使用 `ubuntu-openclaw-chromium:arm64` 镜像（可配置）

## 实施阶段

### 阶段 1: 基础架构搭建（设计）
- 创建 docker_bench 目录结构
- 设计数据模型（ContainerState, ContainerStatus 等）
- 设计配置结构

### 阶段 2: 核心组件实现
- container_manager.py - 容器生命周期管理
- task_runner.py - 浏览器任务执行
- stats_collector.py - 统计收集

### 阶段 3: 整合与测试
- bench.py - 主入口整合
- config.py - 配置管理
- 测试脚本和文档

### 阶段 4: 文档与集成
- 使用文档（中文/英文）
- README 更新
- 配置模板

## 当前状态

- **阶段**: ✅ 实现完成
- **完成时间**: 2026-06-25

## 已创建文件

### docker_bench 模块
| 文件 | 描述 | 状态 |
|------|------|------|
| `__init__.py` | 包初始化 | ✅ |
| `__main__.py` | 模块入口 | ✅ |
| `bench.py` | 主入口 - 测试流程控制 | ✅ |
| `config.py` | 配置管理 | ✅ |
| `container_manager.py` | 容器生命周期管理 | ✅ |
| `task_runner.py` | 浏览器任务执行（5步流程） | ✅ |
| `stats_collector.py` | 统计收集与报告生成 | ✅ |
| `schemas.py` | 数据结构定义 | ✅ |
| `utils.py` | 工具函数 | ✅ |
| `requirements.txt` | 依赖说明 | ✅ |

### 配置和文档
| 文件 | 描述 | 状态 |
|------|------|------|
| `config/docker_bench.yaml` | 配置模板 | ✅ |
| `docs/docker-bench-usage-zh.md` | 使用指南（中文） | ✅ |
| `README.md` | 项目文档（已更新） | ✅ |