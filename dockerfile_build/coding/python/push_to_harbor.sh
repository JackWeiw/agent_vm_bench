#!/bin/bash
# Script to prepare and push the coding benchmark image for E2B template
# Based on push_to_harbor.sh (ts/go) but using coding-python-bench image name
#
# Usage: HARBOR_IP=X bash push_to_harbor.sh                              # ubuntu arm (default)
#        ARCH=x86 HARBOR_IP=X bash push_to_harbor.sh                     # ubuntu x86_64
#        OS=openeuler HARBOR_IP=X bash push_to_harbor.sh                # openEuler arm
#        OS=openeuler ARCH=x86 HARBOR_IP=X bash push_to_harbor.sh       # openEuler x86_64

set -e

# Configuration
PROXY="${PROXY:-http://your-proxy:8888}"
HARBOR_IP="${HARBOR_IP:-localhost}"

# Architecture: "arm" (default) builds the linuxarm64 tag;
#               "x86"   builds the x86_64 tag.
# OS:           "ubuntu" (default) or "openeuler".
ARCH="${ARCH:-arm}"
case "${ARCH}" in
    arm)
        TAG_SUFFIX="linuxarm64"
        DOCKERFILE_ARCH=""
        WEBSOCAT_ASSET="websocat.aarch64-unknown-linux-musl" ;;
    x86)
        TAG_SUFFIX="x86_64"
        DOCKERFILE_ARCH=".x86"
        WEBSOCAT_ASSET="websocat.x86_64-unknown-linux-musl" ;;
    *)
        echo "ERROR: ARCH must be 'arm' or 'x86', got: ${ARCH}" >&2
        exit 1 ;;
esac

OS="${OS:-ubuntu}"
case "${OS}" in
    ubuntu)
        OS_TAG="24.04"
        IMAGE_NAME="ubuntu-coding-python-bench"
        DOCKERFILE_STEM="Dockerfile" ;;
    openeuler)
        OS_TAG="24.03-lts-sp3"
        IMAGE_NAME="openeuler-coding-python-bench"
        DOCKERFILE_STEM="Dockerfile.openeuler" ;;
    *)
        echo "ERROR: OS must be 'ubuntu' or 'openeuler', got: ${OS}" >&2
        exit 1 ;;
esac

# Dockerfile = OS stem + arch suffix (Dockerfile / Dockerfile.x86 /
#              Dockerfile.openeuler / Dockerfile.openeuler.x86)
DOCKERFILE="${DOCKERFILE_STEM}${DOCKERFILE_ARCH}"

# Image names (the Harbor-side tag is arch/OS-neutral — overwritten per build)
BASE_IMAGE="${IMAGE_NAME}:${OS_TAG}-${TAG_SUFFIX}"
CUSTOM_IMAGE="${IMAGE_NAME}:custom"
HARBOR_IMAGE="e2b-orchestration/${IMAGE_NAME}:custom"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if base image exists
check_base_image() {
    if ! docker images "${BASE_IMAGE}" --format "{{.Repository}}" | grep -q "${IMAGE_NAME}"; then
        log_error "Base image ${BASE_IMAGE} not found!"
        log_info "Please build it first: cd dockerfile_build/coding/python && docker build -t ${BASE_IMAGE} -f ${DOCKERFILE} ."
        exit 1
    fi
    log_info "Base image found: ${BASE_IMAGE}"
}

# Clean up
cleanup_temp_container() {
    log_info "Cleaning up any existing temp-coding-python-image container..."
    docker rm -f temp-coding-python-image 2>/dev/null || true
}

# Start temporary container
start_temp_container() {
    log_info "Starting temporary container..."
    docker run -d --name temp-coding-python-image "${BASE_IMAGE}"
    log_info "Container started successfully"
}

# Install E2B-required system packages
install_components() {
    log_info "Installing E2B-required system packages (systemd, openssh-server, websocat, etc.)..."
    log_info "Using proxy: ${PROXY}"

    case "${OS}" in
        ubuntu)
            docker exec temp-coding-python-image bash -c \
                "export http_proxy=${PROXY}; \
                 export https_proxy=\$http_proxy; \
                 apt-get update && \
                 apt-get install -y wget systemd systemd-sysv openssh-server sudo chrony socat curl iputils-ping dnsutils iproute2 netcat-openbsd tcpdump passwd && \
                 apt-get clean && \
                 rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*" ;;
        openeuler)
            # openEuler package names: iputils (not iputils-ping),
            # bind-utils (not dnsutils), nmap-ncat (not netcat-openbsd),
            # iproute (not iproute2).
            docker exec temp-coding-python-image bash -c \
                "export http_proxy=${PROXY}; \
                 export https_proxy=\$http_proxy; \
                 dnf install -y wget systemd systemd-sysv openssh-server sudo chrony socat curl iputils bind-utils iproute nmap-ncat tcpdump passwd && \
                 dnf clean all && \
                 rm -rf /var/cache/dnf /var/tmp/* /tmp/*" ;;
    esac

    if [ $? -eq 0 ]; then
        log_info "System packages installed successfully"
    else
        log_error "Failed to install system packages"
        exit 1
    fi

    # Install websocat (required by E2B for WebSocket access)
    log_info "Installing websocat..."
    docker exec temp-coding-python-image bash -c \
        "export http_proxy=${PROXY}; \
         export https_proxy=\$http_proxy; \
         wget --no-check-certificate -O /usr/local/bin/websocat \
         http://github.com/vi/websocat/releases/latest/download/${WEBSOCAT_ASSET} && \
         chmod a+x /usr/local/bin/websocat && \
         /usr/local/bin/websocat --version"

    if [ $? -eq 0 ]; then
        log_info "websocat installed successfully"
    else
        log_warn "websocat installation may have failed, continuing..."
    fi
}

# Stop and export container
export_container() {
    log_info "Stopping and exporting container..."
    docker stop temp-coding-python-image

    log_info "Importing as new image ${CUSTOM_IMAGE}..."
    docker export temp-coding-python-image | docker import - "${CUSTOM_IMAGE}"

    log_info "Cleaning up temporary container..."
    docker rm -f temp-coding-python-image
}

# Push to Harbor registry
push_to_harbor() {
    log_info "Tagging and pushing to Harbor registry..."
    log_info "Harbor IP: ${HARBOR_IP}"

    IMAGE_NAME="${HARBOR_IP}:2900/${HARBOR_IMAGE}"

    docker tag "${CUSTOM_IMAGE}" "${IMAGE_NAME}"

    log_info "Pushing image to Harbor: ${IMAGE_NAME}"
    docker push "${IMAGE_NAME}"

    if [ $? -eq 0 ]; then
        log_info "Image pushed successfully!"
        log_info "Harbor URL: http://${HARBOR_IP}:2900/"
    else
        log_error "Failed to push image to Harbor"
        exit 1
    fi
}

# Main
main() {
    log_info "=== Starting E2B coding-python-bench image preparation ==="
    log_info "Proxy: ${PROXY}"
    log_info "Harbor IP: ${HARBOR_IP}"

    check_base_image
    cleanup_temp_container
    start_temp_container
    install_components
    export_container
    push_to_harbor

    log_info "=== Process completed successfully ==="
    log_info "Next step: python3 build_e2b.py --server-ip X --harbor-ip X --alias openclaw-coding-python-v1 --image ${HARBOR_IMAGE}"
}

main
