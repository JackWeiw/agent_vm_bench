# Shared PDF/XLSX document image

This directory builds one offline runtime image for both document cases. The
host-side benchmark runner selects PDF or XLSX; the image only contains the
seed data, LibreOffice/Poppler, skills, helpers, verifiers, and the lightweight
`document-bench-validate` ready-check.

## Build base images

ARM64:

```bash
docker build \
  --build-arg HTTP_PROXY="${PROXY:-}" \
  --build-arg HTTPS_PROXY="${PROXY:-}" \
  --build-arg NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}" \
  -t ubuntu-document-bench:24.04-linuxarm64 -f Dockerfile .
```

x86_64:

```bash
docker build \
  --build-arg HTTP_PROXY="${PROXY:-}" \
  --build-arg HTTPS_PROXY="${PROXY:-}" \
  --build-arg NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}" \
  -t ubuntu-document-bench:24.04-linuxamd64 -f Dockerfile.x86 .
```

Local assets under `assets/` are used first. If the PDF or finished XLSX is
missing, the asset-builder downloads the pinned public source or constructs
the workbook. Missing pinned wheels are downloaded only for the selected
architecture. TLS certificate verification is disabled for the target network.

## Add E2B components and push

Set `HARBOR_IP` to the Harbor address reachable from the current deployment
server. Set `PROXY` to that environment's complete proxy URL (including its
actual port), or leave it empty when the server does not require a proxy.

```bash
# With a proxy
HARBOR_IP=YOUR_HARBOR_IP PROXY=YOUR_PROXY_URL bash push_to_harbor.sh

# Without a proxy
HARBOR_IP=YOUR_HARBOR_IP PROXY= bash push_to_harbor.sh
```

The script selects the current host architecture and pushes `custom-arm64` or
`custom-amd64`. After the push completes, create the E2B Template with alias
`openclaw-document-v1` using the target server's template workflow.

Before downloading websocat, the script checks the architecture-specific local
cache below. A non-empty cached binary is copied into the temporary container
and kept when `websocat --version` reports `WEBSOCAT_VERSION` (default `1.14.0`);
otherwise the pinned GitHub release asset is downloaded as before.

```text
assets/runtime/arm64/websocat
assets/runtime/amd64/websocat
```
