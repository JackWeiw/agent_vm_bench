#!/bin/bash
# Script to prepare and push the coding benchmark image for E2B template
# Based on push_to_harbor.sh but using coding-bench image name
#
# Usage: HARBOR_IP=X bash push_to_harbor_coding.sh

set -e

# Configuration
PROXY="${PROXY:-http://90.255.211.160:8888}"
HARBOR_IP="${HARBOR_IP:-localhost}"

# Image names
BASE_IMAGE="ubuntu-coding-bench:24.04-linuxarm64"
CUSTOM_IMAGE="ubuntu-coding-bench:custom"
HARBOR_IMAGE="e2b-orchestration/ubuntu-coding-bench:custom"

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
    if ! docker images "${BASE_IMAGE}" --format "{{.Repository}}" | grep -q "ubuntu-coding-bench"; then
        log_error "Base image ${BASE_IMAGE} not found!"
        log_info "Please build it first: cd dockerfile_build/coding/ts && docker build -t ${BASE_IMAGE} -f Dockerfile ."
        exit 1
    fi
    log_info "Base image found: ${BASE_IMAGE}"
}

# Clean up
cleanup_temp_container() {
    log_info "Cleaning up any existing temp-coding-image container..."
    docker rm -f temp-coding-image 2>/dev/null || true
}

# Start temporary container
start_temp_container() {
    log_info "Starting temporary container..."
    docker run -d --name temp-coding-image "${BASE_IMAGE}"
    log_info "Container started successfully"
}

# Install E2B-required system packages
install_components() {
    log_info "Installing E2B-required system packages (systemd, openssh-server, websocat, etc.)..."
    log_info "Using proxy: ${PROXY}"

    docker exec temp-coding-image bash -c \
        "export http_proxy=${PROXY}; \
         export https_proxy=\$http_proxy; \
         apt-get update && \
         apt-get install -y wget systemd systemd-sysv openssh-server sudo chrony socat curl iputils-ping dnsutils iproute2 netcat-openbsd tcpdump passwd && \
         apt-get clean && \
         rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*"

    if [ $? -eq 0 ]; then
        log_info "System packages installed successfully"
    else
        log_error "Failed to install system packages"
        exit 1
    fi

    # Install websocat (required by E2B for WebSocket access)
    log_info "Installing websocat..."
    docker exec temp-coding-image bash -c \
        "export http_proxy=${PROXY}; \
         export https_proxy=\$http_proxy; \
         wget --no-check-certificate -O /usr/local/bin/websocat \
         http://github.com/vi/websocat/releases/latest/download/websocat.aarch64-unknown-linux-musl && \
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
    docker stop temp-coding-image

    log_info "Importing as new image ${CUSTOM_IMAGE}..."
    docker export temp-coding-image | docker import - "${CUSTOM_IMAGE}"

    log_info "Cleaning up temporary container..."
    docker rm -f temp-coding-image
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
    log_info "=== Starting E2B coding-bench image preparation ==="
    log_info "Proxy: ${PROXY}"
    log_info "Harbor IP: ${HARBOR_IP}"

    check_base_image
    cleanup_temp_container
    start_temp_container
    install_components
    export_container
    push_to_harbor

    log_info "=== Process completed successfully ==="
    log_info "Next step: python3 build_e2b.py --server-ip X --harbor-ip X --alias openclaw-coding-v1 --image ${HARBOR_IMAGE}"
}

main
