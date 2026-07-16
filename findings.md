# 研究发现 - 容器场景浏览器测试 Bench

## E2B Bench 架构分析

### 核心组件

1. **bench.py** - 主入口
   - 测试流程编排：创建 → 端口检查 → 预热 → 任务 → 统计
   - 支持多种模式：完整流程、仅创建、检测已有、仅预热
   - CLI 参数解析和配置合并

2. **config.py** - 配置管理
   - YAML 配置加载
   - CLI 参数覆盖
   - 批量控制参数（create_batch, task_batch）
   - E2B 环境变量设置

3. **sandbox_manager.py** - 沙箱生命周期
   - 批量创建：分批或全并发
   - 端口检查：18789 (openclaw-gateway) + 11436 (llama-server)
   - 检测已有沙箱
   - 关闭沙箱

4. **task_runner.py** - 任务执行
   - WarmupRunner：预热阶段
   - BrowserTaskRunner：浏览器任务循环
   - TaskManager：批量任务启动控制
   - 错误处理和连续失败检测

5. **stats_collector.py** - 统计收集
   - 实时快照收集
   - 性能报告生成
   - P50/P95/P99 延迟统计
   - 错误分类

6. **schemas.py** - 数据结构
   - SandboxStatus：状态枚举
   - CreationMetrics：创建性能指标
   - BrowserMetrics：任务指标
   - SandboxState：完整状态

### 关键设计特点

1. **批量控制分离**
   - create_batch：保护 E2B API
   - task_batch：保护目标服务器

2. **预热阶段**
   - 预加载浏览器组件
   - 稳定内存占用
   - 减少冷启动延迟

3. **状态机**
   ```text
   PENDING → CREATING → CREATED → PORT_READY → (ACTIVE) → KILLED
                        ↓
                     FAILED
                        ↓
                  PORT_FAILED
                        ↓
                     OFFLINE
   ```

4. **实时监控**
   - 定期快照
   - 终端输出
   - 最终报告

## 容器场景差异点

### Docker vs E2B Sandbox

| 方面 | E2B | Docker |
|------|-----|--------|
| SDK | `e2b` Python SDK | `docker` Python SDK 或 CLI |
| 创建 | `Sandbox.create(template)` | `docker run --name --cpus -m image` |
| 命令 | `sandbox.commands.run(cmd)` | `docker exec container cmd` |
| 删除 | `sandbox.kill()` | `docker rm -f container` |
| 列表 | `Sandbox.list()` | `docker ps` |
| 连接 | `Sandbox.connect(id)` | `docker exec` (已有容器) |

### Docker SDK 关键 API

```python
import docker

client = docker.from_env()

# 创建容器
container = client.containers.run(
    image='ubuntu-openclaw-chromium:arm64',
    name='oc-test-1',
    cpus=2.0,
    mem_limit='2g',
    detach=True,
    remove=False
)

# 执行命令
result = container.exec_run(cmd, user='root')
exit_code = result.exit_code
output = result.output

# 列出容器
containers = client.containers.list(all=True)

# 删除容器
container.remove(force=True)
```

### 端口检查策略

Docker 容器内的端口检查方式与 E2B 相同：
- 使用 `docker exec` 执行 `ss -tlnp` 或 `netstat`
- 等待 18789 + 11436 端口就绪

## OpenClaw 浏览器操作流程

### 用户提供的测试步骤

```bash
# 前置：启动 OpenClaw 后台（热启动）
openclaw browser status && start

# Step 1: 打开页面
openclaw browser open [URL] --label [NAME]

# Step 2: 标签聚焦
openclaw browser focus [TAB_ID]

# Step 3: DOM快照
openclaw browser snapshot --limit 200

# Step 4: 元素点击（失败重试）
openclaw browser click e218

# Step 5: 视觉截图
openclaw browser screenshot

# 后置：擦除缓存
rm -rf /root/.openclaw/browser/openclaw/user-data
```

### 任务执行策略

1. **热启动模式**
   - 测试前执行一次 `openclaw browser status && start`
   - 所有容器共享同一后台进程

2. **完整流程执行**
   - 每次任务执行 5 步流程
   - 记录每步延迟
   - 计算总体 QPS

3. **错误处理**
   - Step 4 失败自动重试一次
   - 连续失败 3 次标记容器离线

## 性能指标设计

### 核心指标

1. **QPS（每秒成功请求数）**
   - 成功完成的完整流程数 / 总时间

2. **容器启动时间**
   - `docker run` 到端口就绪的时间

3. **任务延迟**
   - 单次完整流程的耗时
   - P50/P95/P99 分布

4. **成功率**
   - 成功任务数 / 总任务数

### 统计维度

- 容器创建性能
- 端口等待性能
- 浏览器任务性能
- 错误类型分布

## 建议实现方案

### 推荐架构

完全参考 e2b_bench，替换 E2B SDK 为 Docker SDK：

```text
docker_bench/
├── bench.py            # 主入口（复制 e2b_bench 结构）
├── config.py           # 配置（替换 e2b_env 为 docker 参数）
├── container_manager.py # 容器管理（替换 sandbox_manager）
├── task_runner.py      # 任务执行（修改命令执行方式）
├── stats_collector.py  # 统计（可复用大部分逻辑）
├── schemas.py          # 数据结构（ContainerState 替换 SandboxState）
└── utils.py            # 工具函数（复用）
```

### 关键修改点

1. **container_manager.py**
   - 使用 `docker.from_env()` 创建客户端
   - `docker run` 创建容器（支持 --cpus, -m 参数）
   - `docker exec` 执行命令
   - `docker ps --filter` 检测已有容器
   - `docker rm -f` 清理容器

2. **config.py**
   - 移除 E2B 环境变量
   - 添加 Docker 配置（镜像名、CPU/Mem 规格）
   - 保留批量控制参数

3. **task_runner.py**
   - 修改命令执行为 `container.exec_run()`
   - 保留预热、批量控制逻辑

4. **schemas.py**
   - ContainerStatus 枚举（可复用 SandboxStatus）
   - ContainerState 替换 SandboxState
   - 保留 Metrics 结构