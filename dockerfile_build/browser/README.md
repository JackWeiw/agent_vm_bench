# Docker Image Build Guide

This directory contains all files needed to build the `ubuntu-openclaw-chromium:24.04` base image for E2B templates.

## File List

| File | Description |
|------|-------------|
| `Dockerfile` | Docker build file (ARM64) |
| `Dockerfile.x86` | Docker build file (x86_64) |
| `openclaw.json` | Openclaw configuration file |
| `llama_openclaw.conf` | Supervisor configuration (manages llama-server and openclaw-gateway) |
| `push_to_harbor.sh` | Script to prepare image and push to Harbor |
| `build_e2b.py` | Script to build E2B template and create sandbox |

## Build Steps

### Step 1: Build Base Image

**ARM64:**

```bash
cd dockerfile_build
docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 .
```

**x86_64:**

```bash
cd dockerfile_build
docker build -f Dockerfile.x86 -t ubuntu-openclaw-chromium:24.04-x86_64 .
```

**Custom Proxy (optional):**

If your server requires a proxy to access external network, use `--build-arg`:

```bash
# ARM64 with proxy
docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 \
  --build-arg HTTP_PROXY=http://your-proxy:port \
  --build-arg HTTPS_PROXY=http://your-proxy:port .

# x86_64 with proxy
docker build -f Dockerfile.x86 -t ubuntu-openclaw-chromium:24.04-x86_64 \
  --build-arg HTTP_PROXY=http://your-proxy:port \
  --build-arg HTTPS_PROXY=http://your-proxy:port .
```

> **Note:** Proxy is disabled by default. Only add `--build-arg` if your network requires proxy.

### Step 2: Push to Harbor Registry

```bash
# Set proxy and Harbor IP (modify as needed)
export PROXY=http://90.255.211.160:8888
export HARBOR_IP=192.168.1.100

# Run the push script
chmod +x push_to_harbor.sh
./push_to_harbor.sh
```

Or run directly with parameters:
```bash
PROXY=http://your-proxy:8888 HARBOR_IP=192.168.1.100 ./push_to_harbor.sh
```

### Step 3: Build E2B Template

```bash
# Install dependencies
pip install e2b

# Build template with server IP and Harbor IP
python build_e2b.py --server-ip 141.61.17.196 --harbor-ip 141.61.17.196

# Optional: customize template settings
python build_e2b.py --server-ip 141.61.17.196 --harbor-ip 141.61.17.196 \
    --alias my-template --cpu 4 --memory 4096 \
    --image e2b-orchestration/ubuntu-openclaw-chromium:custom
```

## Configuration

### Proxy Configuration

The Dockerfiles include proxy settings for servers that require proxy to access external network:

- **Default proxy:** `http://90.255.211.160:8888`
- **Docker build:** Uses `HTTP_PROXY` and `HTTPS_PROXY` build args
- **push_to_harbor.sh:** Uses `PROXY` environment variable

**To customize proxy during build:**

```bash
docker build --build-arg HTTP_PROXY=http://your-proxy:port --build-arg HTTPS_PROXY=http://your-proxy:port .
```

### Harbor Registry
- Set `HARBOR_IP` environment variable for push_to_harbor.sh
- Harbor URL: `http://{HARBOR_IP}:2900/`
- Default credentials: `admin` / `Harbor12345`
- Harbor nginx reverse proxy port: `30443` (for E2B template build)

### E2B API Server
- `--server-ip` parameter specifies the E2B orchestration API server IP
- E2B API runs on port 3000: `http://{server_ip}:3000`
- `--harbor-ip` parameter specifies Harbor registry IP (nginx port 30443)

### E2B Config File
- Required: `/root/.e2b/config.json`
- Must contain `accessToken` and `teamApiKey` fields

## Notes

1. The image supports both ARM64 and x86_64 architectures
2. Supervisor auto-starts llama-server (port 11436) and openclaw gateway (port 18789)
3. Non-Snap Chromium is installed via xtradeb PPA
4. Websocat bridges SSH to port 8081 for E2B connectivity
