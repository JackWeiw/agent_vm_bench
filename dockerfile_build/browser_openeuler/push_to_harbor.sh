#!/bin/bash
# Script to prepare and push the openEuler browser base image for E2B template.
# Installs E2B-required components via dnf and pushes to Harbor registry.
#
# Usage: HARBOR_IP=X bash push_to_harbor.sh                 # arm (default)
#        ARCH=x86 HARBOR_IP=X bash push_to_harbor.sh        # x86_64

set -e

# Configuration - modify these values
PROXY="${PROXY:-http://90.255.211.160:8888}"  # Proxy server address
HARBOR_IP="${HARBOR_IP:-localhost}"           # Harbor registry IP address

# Architecture: "arm" (default) builds from Dockerfile (linuxarm64 tag);
#               "x86"   builds from Dockerfile.x86 (x86_64 tag).
ARCH="${ARCH:-arm}"
case "${ARCH}" in
    arm)
        TAG_SUFFIX="linuxarm64"
        DOCKERFILE="Dockerfile"
        WEBSOCAT_ASSET="websocat.aarch64-unknown-linux-musl" ;;
    x86)
        TAG_SUFFIX="x86_64"
        DOCKERFILE="Dockerfile.x86"
        WEBSOCAT_ASSET="websocat.x86_64-unknown-linux-musl" ;;
    *)
        echo "ERROR: ARCH must be 'arm' or 'x86', got: ${ARCH}" >&2
        exit 1 ;;
esac

# Image names (the Harbor-side tag is arch-neutral — overwritten per build)
BASE_IMAGE="openeuler-agent-browser:24.03-lts-sp3-${TAG_SUFFIX}"
CUSTOM_IMAGE="openeuler-agent-browser:custom"
HARBOR_IMAGE="e2b-orchestration/openeuler-agent-browser:custom"

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
    if ! docker images "${BASE_IMAGE}" --format "{{.Repository}}" | grep -q "openeuler-agent-browser"; then
        log_error "Base image ${BASE_IMAGE} not found!"
        log_info "Please build it first with: cd dockerfile_build/browser_openeuler && docker build -t ${BASE_IMAGE} -f ${DOCKERFILE} ."
        exit 1
    fi
    log_info "Base image found: ${BASE_IMAGE}"
}

# Clean up any existing temp container
cleanup_temp_container() {
    log_info "Cleaning up any existing temp-image container..."
    docker rm -f temp-image 2>/dev/null || true
}

# Start temporary container
start_temp_container() {
    log_info "Starting temporary container..."
    docker run -d --name temp-image "${BASE_IMAGE}"
    log_info "Container started successfully"
}

# Install necessary components in container
install_components() {
    log_info "Installing necessary components (systemd, openssh-server, etc.)..."
    log_info "Using proxy: ${PROXY}"

    # openEuler package names differ from Ubuntu's apt names:
    #   iputils (not iputils-ping), bind-utils (not dnsutils),
    #   nmap-ncat (not netcat-openbsd), iproute (not iproute2)
    docker exec temp-image bash -c \
        "export http_proxy=${PROXY}; \
         export https_proxy=\$http_proxy; \
         dnf install -y wget systemd systemd-sysv openssh-server sudo chrony \
         socat curl iputils bind-utils iproute nmap-ncat tcpdump passwd && \
         dnf clean all && \
         rm -rf /var/cache/dnf /var/tmp/* /tmp/*"

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
    docker stop temp-image

    log_info "Importing as new image ${CUSTOM_IMAGE}..."
    docker export temp-image | docker import - "${CUSTOM_IMAGE}"

    log_info "Cleaning up temporary container..."
    docker rm -f temp-image
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
        log_info "You can access Harbor at: http://${HARBOR_IP}:2900/"
        log_info "Username: admin, Password: Harbor12345"
    else
        log_error "Failed to push image to Harbor"
        exit 1
    fi
}

# Main execution
main() {
    log_info "=== Starting openEuler E2B image preparation process ==="
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
