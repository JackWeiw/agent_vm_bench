# VM Bench - OpenStack VM Memory Overcommit Performance Testing

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

### 2. Download Warmup Pages

Run this script to download Wikipedia pages and images for browser warmup:

```bash
bash download_page.sh
```

This creates a `web_content` directory with:

- `web_content/en.wikipedia.org/wiki/` - HTML pages
- `web_content/upload.wikimedia.org/` - Images

### 3. Start Warmup Web Server

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

### 4. Create VMs

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

### 5. Resource Monitoring

After VM creation, wait for QEMU process CPU usage to stabilize around 1% before starting benchmark:

```bash
python3 qemu_monitor.py -t 300 -i 2
```

### 6. Run Benchmark

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

### 7. Delete VMs

```bash
openstack server list -c ID -f value | xargs openstack server delete --force
virsh list --all  # Check if deletion is complete
```