# Agent VM Bench - OpenStack VM Memory Overcommit Performance Testing

[中文说明](README_zh.md)

This directory contains tools for testing performance under OpenStack VM memory overcommit scenarios.

## Files

- `create_server.py` - Create OpenStack VMs
- `qemu_monitor.py` - Monitor QEMU process resources
- `stress_tool.cpp` - Stress tool for VM memory/CPU consumption
- `vm_bench_lite.py` - Benchmark script with browser and QA modes
- `download_page.sh` - Download Wikipedia warmup pages

## Quick Start

### 1. Terminal Setup (Execute First)

```bash
source ~/.admin-openrc
unset http_proxy
unset https_proxy
```

### 2. Configure Host Network Bridge

To allow VMs to access web pages on the host machine, add an IP address to the OpenStack bridge interface:

```bash
# Find the bridge interface name
ip a | grep brq
```

Example output:

```text
10: brqb3fa561d-67: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue state UP group default qlen 1000
11: tap8eee944d-02@if2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UP group default qlen 1000
12: vxlan-667: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
13: tap5cbb0361-f6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
14: tapafbc3810-87: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue master brqb3fa561d-67 state UNKNOWN group default qlen 1000
```

Add an IP address to the bridge interface (use the bridge name from above):

```bash
ip addr add 192.168.110.10/24 dev brqb3fa561d-67
```

After this configuration, VMs can access static web pages on the host at `http://192.168.110.10:8080/Weibo.html`.

### 3. Download Warmup Pages

Run this script to download Wikipedia pages and images for browser warmup:

```bash
bash download_page.sh
```

This creates a `web_content` directory with:

- `web_content/en.wikipedia.org/wiki/` - HTML pages
- `web_content/upload.wikimedia.org/` - Images

### 4. Start Warmup Web Server

Start a local HTTP server to serve the warmup pages:

```bash
cd web_content/en.wikipedia.org/wiki
numactl --cpunodebind=2,3 --membind=2,3 python3 -m http.server 8080
```

The server runs on port 8080. Access pages at `http://<host_ip>:8080/<page>.html`

Available warmup pages:

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

### 5. Create VMs

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

### 6. Resource Monitoring

After VM creation, wait for QEMU process CPU usage to stabilize around 1% before starting benchmark:

```bash
python3 qemu_monitor.py -t 300 -i 2
```

### 7. Run Benchmark (Two-Phase Execution)

Browser mode uses a two-phase execution: **warmup phase** (all VMs) then **benchmark phase** (partial VMs).

#### Phase 1: Warmup Phase (`-wp`)

Warmup phase connects all VMs to execute warmup tasks (visiting warmup pages to load browser memory), then exits:

```bash
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -wp \
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

#### Phase 2: Benchmark Phase (`-bsp`)

Benchmark phase connects only a portion of VMs (controlled by `-bsp` parameter) to run browser tests:

```bash
# Connect 50% of VMs (50 VMs out of 100) for benchmark
python vm_bench_lite.py -n 100 --start-ip 192.168.110.11 --browser-mode \
    -bsp 0.5 \
    --batch-size 10 --batch-interval 5 \
    --browser-url "http://192.168.110.10:8080/Weibo.html" \
    --browser-interval-min 5 --browser-interval-max 15 \
    -t 160
```

#### Parameter Reference

| Parameter | Description |
| --------- | ----------- |
| `-wp` / `--warmup-phase` | Run warmup phase only (all VMs execute warmup tasks then exit) |
| `-bsp` / `--browser-stress-percent` | Percentage of VMs to connect in benchmark phase (default 100%) |
| `--warmup-url` | Warmup page URL (can be specified multiple times) |
| `--warmup-loops` | Warmup loop count (default 1) |
| `--warmup-delay` | Delay between warmup pages in seconds (default 2) |

**Note:** In browser mode without LLM (`--browser-mode` without `--browser-use-llm`), the benchmark adds 10 seconds to each request latency to simulate LLM response delay. This provides realistic timing comparable to actual agent workflows.

### 8. Delete VMs

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all  # Check if deletion is complete
```