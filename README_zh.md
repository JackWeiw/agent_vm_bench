# Agent VM Bench - OpenStack VM 内存超分配性能测试

[English Documentation](README.md)

本目录包含用于测试 OpenStack VM 内存超分配场景下性能的工具。

## 文件说明

- `create_server.py` - 通过 OpenStack 创建 VM，用于运行 openclaw agent 负载
- `qemu_monitor.py` - 监控 QEMU 进程资源使用
- `stress_tool.cpp` - VM 内存/CPU 消耗压测工具
- `vm_bench_lite.py` - 基准测试脚本（支持浏览器和 QA 模式，测试 openclaw agent 性能）
- `download_page.sh` - 下载 Wikipedia 预热页面

## 快速开始

### 1. 终端环境设置（首先执行）

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. 配置宿主机网桥

为了让 VM 能够访问宿主机上的网页，需要在 OpenStack 网桥接口上添加 IP 地址：

```bash
# 查找网桥接口名称
ip a | grep brq
```

示例输出：

```text
10: brqb3fa561d-67: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue state UP group default qlen 1000
11: tap8eee944d-02@if2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UP group default qlen 1000
12: vxlan-667: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
13: tap5cbb0361-f6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
14: tapafbc3810-87: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
```

为网桥接口添加 IP 地址（使用上面输出的网桥名称）：

```bash
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

配置完成后，VM 可以通过 `http://192.168.110.10:8080/Weibo.html` 访问宿主机上的静态网页。

### 3. 下载预热页面

运行此脚本下载 Wikipedia 页面和图片用于浏览器预热：

```bash
bash download_page.sh
```

这将创建 `web_content` 目录，包含：

- `web_content/en.wikipedia.org/wiki/` - HTML 页面
- `web_content/upload.wikimedia.org/` - 图片

### 4. 启动预热 Web 服务器

启动本地 HTTP 服务器来提供预热页面：

```bash
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

服务器运行在端口 8080，访问地址为 `http://<host_ip>:8080/<page>.html`

可用的预热页面：

- China.html
- World_War_II.html
- United_States.html
- Hubble_Space_Telescope.html
- Solar_System.html
- Earth.html
- Human.html
- List_of_paintings_by_Vincent_van_Gogh.html
- Galaxy.html
- Weibo.html

### 5. 创建 VM（运行 openclaw agent）

通过 OpenStack 创建用于运行 openclaw agent 负载的 VM：

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

### 6. 资源监控

创建 VM 后，等待 QEMU 进程 CPU 使用率稳定在约 1% 后再开始基准测试：

```bash
python3 qemu_monitor.py -t 300 -i 2
```

### 7. 运行基准测试

```bash
python vm_bench_lite.py -n 10 --start-ip 192.168.110.11 --stress-percent 0 --batch-size 10 --batch-interval 5 -t 160 --browser-mode --browser-url https://192.168.110.10:8080 --browser-interval-min 5 --browser-interval-max 15 \
--warmup \
    --warmup-url "http://192.168.110.10:8080/China.html" \
    --warmup-url "http://192.168.110.10:8080/Earth.html" \
    --warmup-url "http://192.168.110.10:8080/Galaxy.html" \
    --warmup-url "http://192.168.110.10:8080/Hubble_Space_Telescope.html" \
    --warmup-url "http://192.168.110.10:8080/Human.html" \
    --warmup-url "http://192.168.110.10:8080/List_of_paintings_by_Vincent_van_Gogh.html" \
    --warmup-url "http://192.168.110.10:8080/Solar_System.html" \
    --warmup-url "http://192.168.110.10:8080/United_States.html" \
    --warmup-url "http://192.168.110.10:8080/World_War_II.html" \
    --warmup-loops 2 \
    --warmup-delay 2 \
    --browser-url "http://192.168.110.10:8080/Weibo.html"
```

**注意：** 在不使用 LLM 的浏览器模式下（`--browser-mode` 但未指定 `--browser-use-llm`），基准测试会在每次请求的延迟上额外增加 10 秒，以模拟 LLM 响应延迟。这样可以获得与真实 Agent 工作流相当的 realistic 延时数据。

### 8. 删除 VM

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all  # 检查删除是否完成
```