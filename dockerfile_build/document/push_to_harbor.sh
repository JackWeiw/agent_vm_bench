#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROXY="${PROXY:-}"
NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,harbor}"
HARBOR_IP="${HARBOR_IP:-localhost}"
WEBSOCAT_VERSION="${WEBSOCAT_VERSION:-1.14.0}"

case "$(uname -m)" in
    aarch64|arm64)
        ARCH="arm64"
        BASE_IMAGE="ubuntu-document-bench:24.04-linuxarm64"
        CUSTOM_IMAGE="ubuntu-document-bench:custom-arm64"
        HARBOR_TAG="custom-arm64"
        WEBSOCAT_ASSET="websocat_max.aarch64-unknown-linux-musl"
        ;;
    x86_64|amd64)
        ARCH="amd64"
        BASE_IMAGE="ubuntu-document-bench:24.04-linuxamd64"
        CUSTOM_IMAGE="ubuntu-document-bench:custom-amd64"
        HARBOR_TAG="custom-amd64"
        WEBSOCAT_ASSET="websocat.x86_64-unknown-linux-musl"
        ;;
    *)
        echo "Unsupported host architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

TEMP_CONTAINER="temp-document-image-${ARCH}"
HARBOR_REPOSITORY="e2b-orchestration/ubuntu-document-bench"
TARGET_IMAGE="${HARBOR_IP}:2900/${HARBOR_REPOSITORY}:${HARBOR_TAG}"
LOCAL_WEBSOCAT="${SCRIPT_DIR}/assets/runtime/${ARCH}/websocat"

cleanup() {
    docker rm -f "${TEMP_CONTAINER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    echo "Base image not found: ${BASE_IMAGE}" >&2
    if [ "${ARCH}" = "arm64" ]; then
        echo "Build it with: docker build --build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=... -t ${BASE_IMAGE} -f Dockerfile ." >&2
    else
        echo "Build it with: docker build --build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=... -t ${BASE_IMAGE} -f Dockerfile.x86 ." >&2
    fi
    exit 1
fi

echo "Validating ${BASE_IMAGE}"
docker run --rm --entrypoint document-bench-validate "${BASE_IMAGE}"

if [ "${HARBOR_IP}" != "localhost" ] && ! docker info 2>/dev/null | grep -Fq "${HARBOR_IP}:2900"; then
    echo "WARNING: verify that Docker trusts ${HARBOR_IP}:2900 as an insecure registry or has its CA installed." >&2
fi

cleanup
docker run -d --network host --name "${TEMP_CONTAINER}" "${BASE_IMAGE}" >/dev/null

if [ -n "${PROXY}" ]; then
    echo "Installing E2B runtime components (proxy enabled)"
else
    echo "Installing E2B runtime components (proxy disabled)"
fi

docker exec \
    -e http_proxy="${PROXY}" -e https_proxy="${PROXY}" \
    -e HTTP_PROXY="${PROXY}" -e HTTPS_PROXY="${PROXY}" \
    -e no_proxy="${NO_PROXY}" -e NO_PROXY="${NO_PROXY}" \
    "${TEMP_CONTAINER}" bash -c '
set -euo pipefail
printf "%s\n" "Acquire::Retries \"5\";" "Acquire::http::Timeout \"60\";" \
  "Acquire::https::Timeout \"60\";" "Acquire::https::Verify-Peer \"false\";" \
  "Acquire::https::Verify-Host \"false\";" > /etc/apt/apt.conf.d/99document-network
if [ -n "${http_proxy}" ]; then
  printf "Acquire::http::Proxy \"%s\";\nAcquire::https::Proxy \"%s\";\n" "${http_proxy}" "${https_proxy}" >> /etc/apt/apt.conf.d/99document-network
fi
apt-get update
apt-get install -y wget systemd systemd-sysv openssh-server sudo chrony socat curl \
  iputils-ping dnsutils iproute2 netcat-openbsd tcpdump passwd
rm -f /etc/apt/apt.conf.d/99document-network
apt-get clean
rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*
command -v sshd >/dev/null
command -v systemd >/dev/null
'

if [ -s "${LOCAL_WEBSOCAT}" ]; then
    echo "Using local websocat cache: ${LOCAL_WEBSOCAT}"
    docker cp "${LOCAL_WEBSOCAT}" "${TEMP_CONTAINER}:/usr/local/bin/websocat"
    docker exec "${TEMP_CONTAINER}" chmod a+x /usr/local/bin/websocat
fi

docker exec \
    -e http_proxy="${PROXY}" -e https_proxy="${PROXY}" \
    -e HTTP_PROXY="${PROXY}" -e HTTPS_PROXY="${PROXY}" \
    -e no_proxy="${NO_PROXY}" -e NO_PROXY="${NO_PROXY}" \
    "${TEMP_CONTAINER}" bash -c "
set -euo pipefail
if ! command -v websocat >/dev/null || ! websocat --version 2>/dev/null | grep -q '${WEBSOCAT_VERSION}'; then
  curl -kL --fail --connect-timeout 30 --retry 5 --retry-delay 3 --retry-all-errors \\
    -o /usr/local/bin/websocat \\
    'https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/${WEBSOCAT_ASSET}'
  chmod a+x /usr/local/bin/websocat
fi
websocat --version | grep -q '${WEBSOCAT_VERSION}'
document-bench-validate
"

docker stop "${TEMP_CONTAINER}" >/dev/null
docker export "${TEMP_CONTAINER}" | docker import \
    --change 'ENV DOCUMENT_BENCH_ROOT=/opt/document-bench' \
    --change 'ENV http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= no_proxy= NO_PROXY=' \
    --change 'CMD ["sleep", "infinity"]' - "${CUSTOM_IMAGE}" >/dev/null

docker run --rm --entrypoint document-bench-validate "${CUSTOM_IMAGE}"
docker run --rm --entrypoint bash "${CUSTOM_IMAGE}" -c \
    'command -v sshd >/dev/null && command -v systemd >/dev/null && websocat --version >/dev/null'

docker tag "${CUSTOM_IMAGE}" "${TARGET_IMAGE}"
docker push "${TARGET_IMAGE}"

echo "Pushed ${TARGET_IMAGE}"
echo "Create the E2B template with the existing server workflow, alias: openclaw-document-v1"
