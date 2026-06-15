# 多VM Agent自动化测试系统设计文档

[**English Version**](design-en.md)

**日期**: 2026-06-02
**作者**: Jack

---

## 1. 概述

### 1.1 目标

构建一个完整的自动化测试系统，实现从VM创建、内存迁移、预热、测试到监控的全流程自动化，支持批量测试多组参数组合并生成对比报告。

### 1.2 前置条件

- 人工手动创建大页内存（如200GB）
- OpenStack环境已配置（~/.admin-openrc）
- 已有工具：create_server.py、vm_bench_lite.py、qemu_monitor.py、smap_tool
- 配置网络桥接：`ip addr add 192.168.110.10/24 dev brqxxx`
- 启动预热Web服务器：`cd web_content/en.wikipedia.org/wiki && python3 -m http.server 8080`

### 1.3 测试流程概览

```
删除旧VM → 确认删除完成 → 创建新VM(n个) → 启动smap_tool → 等待就绪 → 预热 → 测试+监控 → 收集结果 → 清理
```

---

## 2. 系统架构

### 2.1 三层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        批量调度层                                  │
│  batch_test_scheduler.py                                        │
│  - 定义测试参数矩阵 (VM数量、借用比例、活跃百分比)                    │
│  - 循环调用核心测试脚本                                            │
│  - 管理测试队列和进度                                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        核心测试层                                  │
│  auto_vm_test.py                                                │
│  - 执行单次完整测试流程                                            │
│  - 删除旧VM → 创建新VM → 启动smap_tool → 等待就绪                  │
│    → 预热 → 测试 → 监控 → 清理                                     │
│  - 接收配置文件路径作为参数                                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        工具执行层                                  │
│  - create_server.py (创建VM)                                    │
│  - smap_tool (内存迁移)                                          │
│  - vm_bench_lite.py (预热+测试)                                  │
│  - qemu_monitor.py (监控)                                       │
│  - getfre (核心频率采集)                                         │
│  - openstack CLI / virsh (删除VM)                               │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 组件职责

| 组件 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `batch_test_scheduler.py` | 批量调度管理 | 测试参数矩阵配置 | 测试结果汇总报告 |
| `auto_vm_test.py` | 单次完整测试执行 | 配置文件路径 | 测试结果目录 |
| `test_config_template.yaml` | 配置模板 | - | - |
| 临时配置文件 | 具体测试参数 | 模板+参数动态值 | 保存到结果目录 |

---

## 3. 配置文件设计

### 3.1 配置模板结构

文件: `test_config_template.yaml`

```yaml
# ========================================
# 测试基础配置
# ========================================

# OpenStack 配置
openstack:
  openrc_path: "~/.admin-openrc"
  network_id: "cc56708a-c0c0-4d75-a87e-ed1b1a8af844"
  flavor: "2U_4G_30G_4K"
  image: "ubuntu-24.04"
  az: "nova_zone:controller"
  subnet_prefix: "192.168.110."

# VM 配置（动态参数）
vm:
  count: "{{VM_COUNT}}"
  start_ip: "{{START_IP}}"
  username: "root"
  password: "openEuler12#$"

# 内存迁移工具配置
smap_tool:
  path: "/home/l30038718/vm/smap_tool"
  swap_size_gb: "{{SWAP_SIZE_GB}}"      # 大页量(GB)，转换为MB: swap_size_gb * 1024
  ratio: "{{RATIO}}"                    # 借用比例，如 0.15

# 测试配置
test:
  duration: 160                         # 测试持续时间(秒)
  active_percent: "{{ACTIVE_PERCENT}}"  # 活跃VM百分比，如 0.5
  batch_size: 10                        # 批次大小
  batch_interval: 5                     # 批次间隔(秒)
  browser_interval_min: 5               # 浏览器任务最小间隔(秒)
  browser_interval_max: 15              # 浏览器任务最大间隔(秒)
  browser_url: "http://192.168.110.10:8080/Weibo.html"

# 预热配置
warmup:
  urls:
    - "http://192.168.110.10:8080/China.html"
    - "http://192.168.110.10:8080/Earth.html"
    - "http://192.168.110.10:8080/Galaxy.html"
    - "http://192.168.110.10:8080/Hubble_Space_Telescope.html"
    - "http://192.168.110.10:8080/Human.html"
    - "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html"
    - "http://192.168.110.10:8080/Solar_System.html"
    - "http://192.168.110.10:8080/United_States.html"
    - "http://192.168.110.10:8080/World_War_II.html"
  loops: 1                              # 预热循环次数
  delay: 2                              # 页面间延迟(秒)
  batch_size: 20                        # 预热批次大小
  batch_interval: 5                     # 预热批次间隔(秒)

# 监控配置
monitor:
  interval: 2                           # 监控采样间隔(秒)
  numa_nodes: [0, 1]                    # NUMA节点列表
  enable_capture: true                  # 是否启用日志收集(devkit/ksys/ub_watch/smap_bw/getfre)
  # 注意：工具路径使用.env文件配置，getfre使用getfre_config.yaml配置

# 等待配置
wait:
  ssh_timeout: 300                      # SSH连接超时(秒)
  service_timeout: 300                  # 服务启动超时(秒)
  cpu_threshold: 5                      # CPU利用率阈值(%)
  check_interval: 10                    # 检查间隔(秒)

# 结果配置
result:
  base_dir: "results"
```

### 3.2 动态参数说明

| 参数 | 描述 | 示例值 |
|------|------|--------|
| `{{VM_COUNT}}` | VM数量 | 100 |
| `{{START_IP}}` | 起始IP | "192.168.110.11" |
| `{{SWAP_SIZE_GB}}` | 大页量(GB) | 200 |
| `{{RATIO}}` | 借用比例 | 0.15 |
| `{{ACTIVE_PERCENT}}` | 活跃VM百分比 | 0.5 |
| `{{DURATION}}` | 测试持续时间(秒) | 160 |

---

## 4. 核心自动化测试脚本设计

### 4.1 脚本入口

文件: `auto_vm_test.py`

```bash
python auto_vm_test.py --config test_config.yaml
```

### 4.2 执行流程详解

```
步骤1: 初始化
  ├─ 解析配置文件
  ├─ 创建结果目录: results/vm{n}_ratio{ratio}_active{percent}_时间戳/
  ├─ 保存配置文件副本到结果目录
  ├─ 初始化日志系统
  ├─ 清理旧的锁文件（防止误判）
  │   rm -f /tmp/vm_benchmark_running.lock
  └─ 设置OpenStack环境变量
      source ~/.admin-openrc
      unset http_proxy
      unset https_proxy

步骤2: 清理环境（删除已存在的VM）
  ├─ 执行删除命令
  │   openstack server list -c ID -f value | xargs openstack server delete --force
  ├─ 等待删除完成（轮询检查）
  │   while True:
  │     virsh list --all
  │     if 没有running的VM:
  │       break
  │     sleep 10
  ├─ 确认删除完成
  │   openstack server list -c ID -f value  # 应返回空
  │   virsh list --all                      # 应只显示shut off或空
  └─ 记录删除耗时

步骤3: 创建VM
  ├─ 调用 create_server.py 创建指定数量VM
  │   python3 create_server.py \
  │     --start_ip {start_ip} \
  │     --n {count} \
  │     --subnet-prefix {subnet_prefix} \
  │     --network-id {network_id} \
  │     --az {az} \
  │     --flavor {flavor} \
  │     --image {image}
  ├─ 检查创建结果，记录成功/失败的VM
  ├─ 输出创建统计信息
  └─ 失败处理：创建失败超过阈值 → 终止测试

步骤4: 启动内存迁移工具
  ├─ 清理旧的smap配置
  │   rm -rf /dev/shm/smap_config
  ├─ 获取所有qemu-kvm进程PID
  │   pidof qemu-kvm
  ├─ 计算swap_size_mb = swap_size_gb * 1024
  ├─ 计算ratio_percent = int(ratio * 100)  # 如0.15转为15
  ├─ 启动smap_tool
  │   cd /home/l30038718/vm
  │   ./smap_tool {vm_count} `pidof qemu-kvm` --swap-size {swap_size_mb} --ratio {ratio_percent}
  │   # 示例: ./smap_tool 100 `pidof qemu-kvm` --swap-size 204800 --ratio 10
  ├─ 记录smap_tool进程PID（测试完成后需要kill）
  ├─ 验证smap_tool启动成功（检查进程是否存在）
  └─ 失败处理：启动失败 → 终止测试

步骤5: 等待VM就绪
  ├─ 循环检查每个VM（并行检查）:
  │   ├─ SSH连接是否成功
  │   │   ssh -o ConnectTimeout=10 root@{ip} "echo connected"
  │   ├─ openclaw gateway服务是否运行（SSH到VM内部检查）
  │   │   # 检查进程
  │   │   pgrep -f openclaw
  │   │   # 检查端口18789是否监听
  │   │   ss -tln | grep 18789
  │   ├─ qemu-kvm进程CPU利用率 < {cpu_threshold}%（在宿主机上检测）
  │   │   top -b -n 1 -p {pid} | grep qemu-kvm
  │   │   # 或使用psutil获取CPU利用率
  │   └─ 所有条件满足后标记VM就绪
  ├─ 记录等待时间和就绪VM列表
  ├─ 超时处理：未就绪VM记录并跳过
  └─ 统计就绪VM数量，输出就绪状态

步骤6: 浏览器模式预热
  ├─ 构建预热命令（参考README）
  │   python vm_bench_lite.py -n {count} --start-ip {start_ip} --browser-mode \
  │     -wp \
  │     --batch-size {warmup.batch_size} --batch-interval {warmup.batch_interval} \
  │     --warmup-url "{url1}" \
  │     --warmup-url "{url2}" \
  │     ... (所有warmup urls) \
  │     --warmup-loops {loops} \
  │     --warmup-delay {delay}
  ├─ 执行预热命令
  ├─ 等待预热完成
  ├─ 收集预热结果
  └─ 记录预热耗时和成功/失败VM数量

步骤7: 启动监控（stress-file + duration同步）
  ├─ 构建监控命令
  │   python qemu_monitor.py -t {duration} -i {interval} \
  │     --enable-capture \
  │     --log-dir {result_dir}/qemu_monitor \
  │     --numa {numa_nodes} \
  │     --stress-file /tmp/vm_benchmark_running.lock
  ├─ 后台启动监控进程
  │   subprocess.Popen(monitor_cmd)
  ├─ 记录监控进程PID
  ├─ 监控等待锁文件出现，暂不采样（不空跑）
  └─ duration秒后自然停止并生成Excel

步骤8: 浏览器模式测试
  ├─ 创建锁文件通知监控开始采样
  │   touch /tmp/vm_benchmark_running.lock
  ├─ 构建测试命令（参考README）
  │   python vm_bench_lite.py -n {count} --start-ip {start_ip} --browser-mode \
  │     -bsp {active_percent} \
  │     --batch-size {batch_size} --batch-interval {batch_interval} \
  │     --browser-url "{browser_url}" \
  │     --browser-interval-min {browser_interval_min} \
  │     --browser-interval-max {browser_interval_max} \
  │     -t {duration}
  ├─ 执行测试命令
  ├─ 等待测试完成（duration秒）
  ├─ 不删除锁文件，监控用duration自然结束
  ├─ 收集测试报告
  └─ 记录测试统计（成功率、延迟等）

步骤9: 等待监控自然结束并收集结果
  ├─ 等待监控进程自然结束（duration秒后）
  │   # 监控需要额外2-5分钟处理日志、生成Excel
  ├─ 等待最多5分钟确保Excel生成完成
  ├─ 验证监控日志文件完整性
  │   - qemu_monitor.csv
  │   - summary.csv
  │   - analysis_report.xlsx
  │   - devkit_mem.log / devkit_top_down.log
  │   - ksys.log / ub_watch.log
  ├─ 清理锁文件
  │   rm /tmp/vm_benchmark_running.lock
  ├─ 移动vm_bench_lite报告到结果目录
  ├─ 解析监控日志提取关键指标
  └─ 生成综合测试报告

步骤10: 清理环境
  ├─ 停止smap_tool进程（使用步骤4记录的PID）
  │   kill {smap_tool_pid}
  │   # 或 pkill -f smap_tool
  ├─ 删除测试VM
  │   openstack server list -c ID -f value | xargs openstack server delete --force
  ├─ 确认删除完成
  │   virsh list --all  # 检查是否还有running的VM
  │   openstack server list -c ID -f value  # 应返回空
  └─ 输出测试完成信息

结束: 返回测试结果路径
```

### 4.3 关键命令参考（来自README.md）

#### 4.3.1 终端设置
```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

#### 4.3.2 删除VM并确认
```bash
# 删除所有VM
openstack server list -c ID -f value | xargs openstack server delete --force

# 确认删除完成
virsh list --all  # 检查是否还有running的VM
openstack server list -c ID -f value  # 应返回空
```

#### 4.3.3 创建VM
```bash
python3 create_server.py \
  --start_ip 192.168.110.11 \
  --n 10 \
  --subnet-prefix 192.168.110. \
  --network-id cc56708a-c0c0-4d75-a87e-ed1b1a8af844 \
  --az nova_zone:controller \
  --flavor 2U_4G_30G_4K \
  --image ubuntu-24.04
```

#### 4.3.4 启动smap_tool
```bash
cd /home/l30038718/vm
rm -rf /dev/shm/smap_config
# 参数说明：
# - 第一个参数：VM数量
# - pid_list：通过 `pidof qemu-kvm` 获取
# --swap-size：大页量(MB) = 大页GB × 1024
# --ratio：借用比例（整数百分比，如10表示10%）
./smap_tool 100 `pidof qemu-kvm` --swap-size 204800 --ratio 10
# 记录smap_tool的PID，测试完成后需要kill
```

#### 4.3.5 预热阶段
```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
    --batch-size 20 --batch-interval 5 \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-url "http://192.168.110.10:8080/Galaxy.html" \
    --warmup-url "http://192.168.110.10:8080/Hubble_Space_Telescope.html" \
    --warmup-url "http://192.168.110.10:8080/Human.html" \
    --warmup-url "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html" \
    --warmup-url "http://192.168.110.10:8080/Solar_System.html" \
    --warmup-url "http://192.168.110.10:8080/United_States.html" \
    --warmup-url "http://192.168.110.10:8080/World_War_II.html" \
    --warmup-loops 1 \
    --warmup-delay 2
```

#### 4.3.6 测试阶段
```bash
# 连接50%的VM进行测试
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

#### 4.3.7 监控
```bash
# 基本监控
python3 qemu_monitor.py -t 300 -i 2

# 带日志收集
python3 qemu_monitor.py -t 300 -i 2 --enable-capture --log-dir /data/test_run_1

# 指定NUMA节点
python3 qemu_monitor.py -t 300 --enable-capture --numa 0,1
```

### 4.4 错误处理策略

| 步骤 | 失败处理 | 重试策略 |
|------|----------|----------|
| 步骤2 (删除VM) | 超时后强制终止残留VM | virsh destroy |
| 步骤3 (创建VM) | 创建失败超过30% → 终止测试 | 无重试，记录失败VM |
| 步骤4 (smap_tool) | 启动失败 → 终止测试 | 重试一次 |
| 步骤5 (等待就绪) | 超时 → 继续测试，跳过未就绪VM | SSH重连3次 |
| 步骤7 (监控) | 启动失败 → 记录警告，继续测试 | 无重试 |
| 步骤8 (测试) | 异常 → 记录异常，继续收集数据 | 无重试 |

---

## 5. 批量调度脚本设计

### 5.1 脚本入口

文件: `batch_test_scheduler.py`

```bash
python batch_test_scheduler.py --config batch_config.yaml
```

### 5.2 执行流程

```
步骤1: 定义测试参数矩阵
  ├─ VM数量列表: [50, 100, 150]
  ├─ 借用比例列表: [0.10, 0.15, 0.20]
  ├─ 活跃百分比列表: [0.5, 0.8, 1.0]
  └─ 计算总测试次数: len(vm_counts) × len(ratios) × len(active_percentages)

步骤2: 生成测试任务列表
  ├─ 遍历所有参数组合
  │   for vm_count in vm_counts:
  │     for ratio in ratios:
  │       for active_percent in active_percentages:
  │         task = {
  │           'vm_count': vm_count,
  │           'ratio': ratio,
  │           'active_percent': active_percent,
  │           'task_id': f"vm{vm_count}_ratio{ratio}_active{active_percent}"
  │         }
  │         tasks.append(task)
  └─ 输出任务列表摘要

步骤3: 循环执行测试
  ├─ for i, task in enumerate(tasks):
  │   ├─ 打印当前任务信息
  │   │   print(f"[{i+1}/{len(tasks)}] Starting: {task['task_id']}")
  │   │
  │   ├─ 根据模板生成临时配置文件
  │   │   config_file = generate_config(
  │   │     template="test_config_template.yaml",
  │   │     output=f"temp_configs/config_{task['task_id']}.yaml",
  │   │     params=task
  │   │   )
  │   │
  │   ├─ 调用核心测试脚本
  │   │   result = subprocess.run([
  │   │     "python", "auto_vm_test.py",
  │   │     "--config", config_file
  │   │   ], capture_output=True)
  │   │
  │   ├─ 等待测试完成
  │   │   监控进程状态，捕获输出和错误
  │   │
  │   ├─ 收集结果
  │   │   - 保存临时配置文件到结果目录
  │   │   - 记录测试状态 (成功/失败)
  │   │   - 记录结果路径
  │   │
  │   ├─ 错误处理
  │   │   if 测试失败 and continue_on_failure:
  │   │     记录失败原因，继续下一轮
  │   │   elif 测试失败 and not continue_on_failure:
  │   │     终止批量测试
  │   │
  │   ├─ 清理临时配置文件
  │   │   os.remove(config_file)  # 已移动到结果目录
  │   │
  │   └─ 打印进度
  │     print(f"Completed: {result_dir}")
  │
  └─ 所有任务完成

步骤4: 生成汇总报告
  ├─ 遍历所有结果目录
  ├─ 解析每个测试的关键指标:
  │   - 浏览器任务成功率、平均延迟、P99延迟
  │   - QEMU CPU利用率平均值
  │   - 内存带宽、IPC等
  ├─ 生成对比表格 (Excel格式):
  │   | VM数 | 借用比例 | 活跃% | 成功率 | Avg延迟 | P99延迟 | CPU% | 内存带宽 |
  │   |------|----------|-------|--------|---------|---------|------|----------|
  │   | 50   | 0.10     | 0.8   | 98%    | 1.2s    | 3.5s    | 15%  | 2.5GB/s  |
  ├─ 保存汇总报告: results/batch_summary_时间戳.xlsx
  └─ 输出汇总信息

结束: 所有测试完成，输出汇总报告路径
```

### 5.3 批量配置文件

文件: `batch_config.yaml`

```yaml
# 测试参数矩阵
test_matrix:
  vm_counts: [50, 100, 150]
  ratios: [0.10, 0.15, 0.20]
  active_percentages: [0.5, 0.8, 1.0]

# 固定参数
fixed_params:
  start_ip: "192.168.110.11"
  swap_size_gb: 200
  duration: 160

# 调度配置
scheduler:
  continue_on_failure: true    # 失败后继续执行下一轮
  cleanup_between_tests: true  # 每轮测试间清理VM

# 结果配置
result:
  template_path: "test_config_template.yaml"
  base_dir: "results"
```

---

## 6. 结果组织结构

### 6.1 目录结构

```
results/
├── batch_summary_20260602_143052.xlsx          # 批量测试汇总报告
├── batch_log_20260602_143052.txt               # 批量调度日志
│
├── vm50_ratio0.10_active0.5_20260602_143052/   # 单次测试结果目录
│   ├── config.yaml                             # 该测试的配置文件副本
│   ├── test_log.txt                            # 测试执行日志
│   │
│   ├── vm_bench_lite/                          # vm_bench_lite 输出
│   │   ├── bench_report_20260602_143520.txt    # 测试报告
│   │   └── warmup_summary_20260602_143450.txt  # 预热摘要
│   │
│   ├── qemu_monitor/                           # qemu_monitor 输出
│   │   ├── qemu_monitor.csv                    # QEMU监控数据
│   │   ├── summary.csv                         # 汇总统计
│   │   ├── analysis_report.xlsx                # 分析报告(Excel)
│   │   ├── devkit_mem.log                      # devkit内存日志
│   │   ├── devkit_top_down.log                 # devkit top-down日志
│   │   ├── ksys.log                            # ksys日志
│   │   ├── ub_watch.log                        # ub_watch日志
│   │   ├── smap_bw.log                         # smap_bw日志
│   │   ├── getfre_NUMA0.log                    # getfre NUMA0频率数据
│   │   ├── getfre_NUMA1.log                    # getfre NUMA1频率数据
│   │   └── *_report.json                       # ksys报告
│   │
│   └───── summary/                             # 综合分析摘要
│       ├── test_summary.txt                    # 测试摘要
│       ├── metrics_summary.json                # 关键指标JSON
│       └── comparison_chart.png                # 对比图表(可选)
│
├── vm50_ratio0.10_active0.8_20260602_150123/
│   └── ... (相同结构)
│
└── ... (其他测试结果)
```

### 6.2 关键指标摘要格式

文件: `metrics_summary.json`

```json
{
  "test_id": "vm50_ratio0.10_active0.5",
  "test_time": "2026-06-02 14:30:52",
  "parameters": {
    "vm_count": 50,
    "ratio": 0.10,
    "active_percent": 0.5,
    "active_vm_count": 25,
    "duration": 160
  },
  "browser_metrics": {
    "total_tasks": 500,
    "success_count": 495,
    "success_rate": 99.0,
    "avg_latency": 1.25,
    "p99_latency": 3.5
  },
  "qemu_metrics": {
    "avg_cpu_percent": 15.2,
    "max_cpu_percent": 45.0,
    "avg_memory_mb": 2048,
    "max_memory_mb": 2560
  },
  "performance_metrics": {
    "ipc_avg": 0.85,
    "l3_miss_latency_avg": 120,
    "ddr_bandwidth_read_avg": 2.5,
    "ddr_bandwidth_write_avg": 1.2
  }
}
```

### 6.3 批量汇总报告格式

文件: `batch_summary_xxx.xlsx` 包含以下列：

| 列名 | 描述 |
|------|------|
| test_id | 测试标识 |
| vm_count | VM总数 |
| ratio | 借用比例 |
| active_percent | 活跃VM百分比 |
| active_vm_count | 实际活跃VM数 |
| success_rate | 浏览器任务成功率 |
| avg_latency | 平均延迟 |
| p99_latency | P99延迟 |
| avg_cpu | 平均CPU利用率 |
| max_cpu | 最大CPU利用率 |
| ipc | IPC平均值 |
| ddr_read | DDR读取带宽 |
| ddr_write | DDR写入带宽 |

---

## 7. 实现计划

### 7.1 开发顺序

1. **配置文件模板** (`test_config_template.yaml`)
   - 定义完整配置结构
   - 标记动态参数位置

2. **核心自动化脚本** (`auto_vm_test.py`)
   - 实现单次测试完整流程
   - 错误处理和日志记录
   - 结果收集和汇总

3. **批量调度脚本** (`batch_test_scheduler.py`)
   - 参数矩阵生成
   - 配置文件动态生成
   - 批量执行和汇总报告

4. **辅助工具优化**
   - 优化现有工具的参数接口
   - 确保工具间协调工作

### 7.2 测试验证

1. 单次测试流程验证（小规模：n=10）
2. 批量调度验证（小规模参数组合：3个测试）
3. 完整测试验证（全参数矩阵）

---

## 8. 风险和约束

### 8.1 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| VM创建失败 | 测试中断 | 设置失败阈值(30%)，超阈值终止 |
| smap_tool启动失败 | 内存迁移失效 | 验证启动成功，失败则终止 |
| 网络不稳定 | SSH连接断开 | 重连机制(3次)，OpenStack状态检查 |
| 删除VM卡住 | 测试无法开始 | virsh destroy强制终止 |
| 监控工具启动失败 | 数据缺失 | 记录警告，继续测试 |

### 8.2 约束条件

- 前置条件依赖人工创建大页
- 测试期间需要稳定的OpenStack环境
- 监控工具(devkit/ksys)需要正确配置路径
- 预热Web服务器需要提前启动
- 网络桥接需要配置IP地址
---

