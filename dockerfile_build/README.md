# Docker Image Build Guide

This directory contains all files needed to build the `ubuntu-openclaw-chromium:24.04-linuxarm64` base image for E2B templates.

## File List

| File | Description |
|------|-------------|
| `Dockerfile` | Docker build file |
| `openclaw.json` | Openclaw configuration file |
| `llama_openclaw.conf` | Supervisor configuration (manages llama-server and openclaw-gateway) |
| `push_to_harbor.sh` | Script to prepare image and push to Harbor |
| `build_e2b.py` | Script to build E2B template and create sandbox |

## Build Steps

### Step 1: Build Base Image

```bash
cd dockerfile_build
docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 .
```

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

Before building the E2B template, ensure Harbor hostname is configured:

```bash
# Add to /etc/hosts
echo "127.0.0.1 harbor" >> /etc/hosts
```

Then run the build script:

```bash
# Install dependencies
pip install e2b

# Build template with E2B API server IP
python build_e2b.py --server-ip 192.168.1.100

# Optional: customize template settings
python build_e2b.py --server-ip 192.168.1.100 --alias my-template --cpu 4 --memory 4096
```

## Configuration

### Proxy Configuration
- Set `PROXY` environment variable before running `push_to_harbor.sh`
- Default: `http://90.255.211.160:8888`

### Harbor Registry
- Set `HARBOR_IP` environment variable for push_to_harbor.sh
- Harbor URL: `http://{HARBOR_IP}:2900/`
- Default credentials: `admin` / `Harbor12345`
- **For E2B template build**: Configure `/etc/hosts` with `127.0.0.1 harbor`

### E2B API Server
- `--server-ip` parameter specifies the E2B orchestration API server IP
- E2B API runs on port 3000: `http://{server_ip}:3000`
- Harbor and E2B API are typically deployed on the same server

### E2B Config File
- Required: `/root/.e2b/config.json`
- Must contain `accessToken` and `teamApiKey` fields

## Notes

1. The image is designed for ARM64 architecture (Ubuntu 24.04)
2. Supervisor auto-starts llama-server (port 11436) and openclaw gateway (port 18789)
3. Non-Snap Chromium is installed via xtradeb PPA
4. Websocat bridges SSH to port 8081 for E2B connectivity