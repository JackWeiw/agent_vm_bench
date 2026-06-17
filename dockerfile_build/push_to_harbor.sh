#!/bin/bash
# Script to prepare and push the base image for E2B template
# This script installs necessary components and pushes to Harbor registry

set -e

# Configuration - modify these values
PROXY="${PROXY:-http://90.255.211.160:8888}"  # Proxy server address
HARBOR_IP="${HARBOR_IP:-localhost}"           # Harbor registry IP address

# Color output (requires echo -e to interpret escape sequences)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if base image exists
check_base_image() {
    if ! docker images ubuntu-openclaw-chromium:24.04-linuxarm64 --format "{{.Repository}}" | grep -q "ubuntu-openclaw-chromium"; then
        log_error "Base image ubuntu-openclaw-chromium:24.04-linuxarm64 not found!"
        log_info "Please build it first with: docker build -t ubuntu-openclaw-chromium:24.04-linuxarm64 ."
        exit 1
    fi
    log_info "Base image found: ubuntu-openclaw-chromium:24.04-linuxarm64"
}

# Clean up any existing temp container
cleanup_temp_container() {
    log_info "Cleaning up any existing temp-image container..."
    docker rm -f temp-image 2>/dev/null || true
}

# Start temporary container
start_temp_container() {
    log_info "Starting temporary container..."
    docker run -d --name temp-image ubuntu-openclaw-chromium:24.04-linuxarm64
    log_info "Container started successfully"
}

# Install necessary components in container
install_components() {
    log_info "Installing necessary components (systemd, openssh-server, etc.)..."
    log_info "Using proxy: ${PROXY}"

    # Install system packages
    docker exec temp-image bash -c \
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

    # Install websocat
    log_info "Installing websocat..."
    docker exec temp-image bash -c \
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
    docker stop temp-image

    log_info "Importing as new image ubuntu-openclaw-chromium:custom..."
    docker export temp-image | docker import - ubuntu-openclaw-chromium:custom

    log_info "Cleaning up temporary container..."
    docker rm -f temp-image
}

# Push to Harbor registry
push_to_harbor() {
    log_info "Tagging and pushing to Harbor registry..."
    log_info "Harbor IP: ${HARBOR_IP}"

    IMAGE_NAME="${HARBOR_IP}:2900/e2b-orchestration/ubuntu-openclaw-chromium:custom"

    docker tag ubuntu-openclaw-chromium:custom "${IMAGE_NAME}"

    log_info "Pushing image to Harbor: ${IMAGE_NAME}"
    docker push "${IMAGE_NAME}"

    if [ $? -eq 0 ]; then
        log_info "Image pushed successfully!"
        log_info "You can access Harbor at: http://${HARBOR_IP}:2900/"
        log_info "Username: admin, Password: Harbor12345"
    else
        log_error "Failed to push image to Harbor"
        exit 1
    fi
}

# Main execution
main() {
    log_info "=== Starting E2B image preparation process ==="
    log_info "Proxy: ${PROXY}"
    log_info "Harbor IP: ${HARBOR_IP}"

    check_base_image
    cleanup_temp_container
    start_temp_container
    install_components
    export_container
    push_to_harbor

    log_info "=== Process completed successfully ==="
    log_info "Next step: Run build_e2b.py to create E2B template"
}

# Run main function
main