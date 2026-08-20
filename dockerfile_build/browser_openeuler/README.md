# Docker Image Build Guide (openEuler)

This directory contains all files needed to build the
`openeuler-agent-browser:24.03-lts-sp3` base image for E2B templates. Unlike
the Ubuntu browser image, this is a **minimal** image: agent-browser + Playwright
Chromium only (no openclaw / llama-server / supervisor).

## File List

| File | Description |
|------|-------------|
| `Dockerfile` | Docker build file (ARM64) |
| `Dockerfile.x86` | Docker build file (x86_64) |
| `push_to_harbor.sh` | Script to prepare image and push to Harbor |

> `build_e2b.py` (the shared E2B template builder) lives at
> `dockerfile_build/build_e2b.py` and is shared with the browser/coding flows.

## Build Steps

### Step 0: Load the openEuler base tar (required)

openEuler is not on a pull-able registry. Load the base tar before building.

**ARM64:**

```bash
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/aarch64/openEuler-docker.aarch64.tar.xz
xz -d openEuler-docker.aarch64.tar.xz
docker load -i openEuler-docker.aarch64.tar
```

**x86_64:**

```bash
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/x86_64/openEuler-docker.x86_64.tar.xz
xz -d openEuler-docker.x86_64.tar.xz
docker load -i openEuler-docker.x86_64.tar
```

Both load to image name `openeuler-24.03-lts-sp3:latest`.

### Step 1: Build Base Image

**ARM64:**

```bash
# From the repo root. ARM uses dockerfile_build/ as context so the shared
# _bench_looper package is reachable for COPY (the image vendors it).
docker build -f dockerfile_build/browser_openeuler/Dockerfile \
  -t openeuler-agent-browser:24.03-lts-sp3-linuxarm64 dockerfile_build/
```

**x86_64:**

```bash
cd dockerfile_build/browser_openeuler
docker build -f Dockerfile.x86 -t openeuler-agent-browser:24.03-lts-sp3-x86_64 .
```

**Custom Proxy (optional):**

```bash
# ARM64 with proxy
docker build -f dockerfile_build/browser_openeuler/Dockerfile \
  -t openeuler-agent-browser:24.03-lts-sp3-linuxarm64 \
  --build-arg HTTP_PROXY=http://your-proxy:port \
  --build-arg HTTPS_PROXY=http://your-proxy:port dockerfile_build/

# x86_64 with proxy
docker build -f Dockerfile.x86 -t openeuler-agent-browser:24.03-lts-sp3-x86_64 \
  --build-arg HTTP_PROXY=http://your-proxy:port \
  --build-arg HTTPS_PROXY=http://your-proxy:port .
```

> Proxy is disabled by default. Only add `--build-arg` if your network requires proxy.

### Step 2: Push to Harbor Registry

```bash
export PROXY=http://your-proxy:8888
export HARBOR_IP=192.168.1.100
chmod +x push_to_harbor.sh
./push_to_harbor.sh
```

For x86_64, set `ARCH=x86` (the default is `arm`):

```bash
ARCH=x86 HARBOR_IP=192.168.1.100 ./push_to_harbor.sh
```

### Step 3: Build E2B Template

The shared `build_e2b.py` lives one directory up (shared with the browser/coding flows):

```bash
pip install e2b
python ../build_e2b.py --server-ip <your-e2b-server-ip> --harbor-ip <your-harbor-ip> \
    --image e2b-orchestration/openeuler-agent-browser:custom \
    --alias openeuler-browser-v1
```

## In-image bench looper (openEuler ARM)

The ARM image vendors the shared `bench_looper` package and a `browser-bench`
entry point at `/usr/local/bin`. Default CMD is `sleep infinity`
(long-running container for slicing); the entry point runs the browser
scenario end-to-end (open_tab -> page_load -> snapshot -> click -> screenshot)
and writes JSON results. The Go and TS images expose `coding-bench-go` and
`coding-bench-ts` the same way. The browser fetches pages from an external
http.server on the LAN, so run with host/bridge networking (not `--network none`).

One-shot end-to-end:

```bash
docker run --rm --network host --cpus=2 --memory=4g \
  -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-agent-browser:24.03-lts-sp3-linuxarm64 \
  browser-bench --loops 100
```

Long-running container driven via `docker exec`:

```bash
docker run -d --name b1 --network host --cpus=2 --memory=4g \
  -v "$PWD/results:/results" -e BENCH_RESULTS_DIR=/results \
  openeuler-agent-browser:24.03-lts-sp3-linuxarm64
docker exec b1 browser-bench --loops 100
```

Results land in `/results/browser/<run-id>/{iterations.jsonl,summary.json}`.

## Configuration

### Proxy Configuration

- **Default proxy:** `http://your-proxy:8888`
- **Docker build:** Uses `HTTP_PROXY` and `HTTPS_PROXY` build args
- **push_to_harbor.sh:** Uses `PROXY` environment variable

### Harbor Registry

- Set `HARBOR_IP` environment variable for push_to_harbor.sh
- Harbor URL: `http://{HARBOR_IP}:2900/`
- Default credentials: `admin` / `Harbor12345`
- Harbor nginx reverse proxy port: `30443` (for E2B template build)

### E2B API Server

- `--server-ip` specifies the E2B orchestration API server IP (port 3000)
- `--harbor-ip` specifies Harbor registry IP (nginx port 30443)

### E2B Config File

- Required: `/root/.e2b/config.json`
- Must contain `accessToken` and `teamApiKey` fields

## Notes

1. The image supports both ARM64 and x86_64 architectures.
2. Minimal image: agent-browser + Playwright Chromium only (no openclaw/llama/supervisor).
3. Playwright's bundled Chromium is installed (`npx playwright install chromium`), not an apt/dnf chromium package.
4. Websocat bridges SSH to port 8081 for E2B connectivity.
