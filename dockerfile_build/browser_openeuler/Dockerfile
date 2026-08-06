# Base image - openEuler 24.03-LTS-SP3 ARM64
# Minimal image: agent-browser + Playwright Chromium only (no openclaw/llama/supervisor)
#
# Prerequisite (Step 0 in README): the openEuler base tar is NOT on a pull-able
# registry. Load it before building:
#   wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/aarch64/openEuler-docker.aarch64.tar.xz
#   xz -d openEuler-docker.aarch64.tar.xz
#   docker load -i openEuler-docker.aarch64.tar   # -> image: openeuler-24.03-lts-sp3:latest
#
# Build:  docker build -t openeuler-agent-browser:24.03-lts-sp3-linuxarm64 .
# Push:   ARCH=arm HARBOR_IP=X bash push_to_harbor.sh
# Template: python ../build_e2b.py --server-ip X --harbor-ip X \
#             --image e2b-orchestration/openeuler-agent-browser:custom --alias openeuler-browser-v1
FROM openeuler-24.03-lts-sp3:latest

# Configure proxy (empty by default, set via --build-arg if needed)
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ENV http_proxy=${HTTP_PROXY}
ENV https_proxy=${HTTPS_PROXY}
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}

# Install Node24 and base tooling
# Node 24 tarball from nodejs.org (NOT dnf nodejs, which ships Node 18.x —
# already EOL — on openEuler 24.03-LTS-SP3). Keeps the same Node version as
# the Ubuntu image so agent-browser/Playwright behave identically across OSes.
RUN dnf install -y wget xz vim iproute net-tools git && \
    cd /usr/local && \
    wget --no-check-certificate https://nodejs.org/dist/v24.14.0/node-v24.14.0-linux-arm64.tar.xz && \
    tar -xvf node-v24.14.0-linux-arm64.tar.xz && \
    mv node-v24.14.0-linux-arm64 node24 && \
    rm -rf node-v24.14.0-linux-arm64.tar.xz && \
    ln -sf /usr/local/node24/bin/node /usr/local/bin/node && \
    ln -sf /usr/local/node24/bin/npm /usr/local/bin/npm && \
    ln -sf /usr/local/node24/bin/npx /usr/local/bin/npx && \
    git config --global http.sslVerify false

# Install agent-browser + Playwright Chromium + runtime libs
# Runtime lib names (nss nspr atk at-spi2-atk gtk3 alsa-lib libgbm libdrm
# mesa-libEGL) are the openEuler/RHEL-style names from the openEuler agent-browser
# install guide. Playwright's bundled Chromium is used (not an apt/dnf chromium).
RUN npm config set strict-ssl false && \
    npm config set registry https://registry.npmmirror.com && \
    npm install -g agent-browser && \
    ln -s /usr/local/node24/bin/agent-browser /usr/local/bin/agent-browser && \
    npx playwright install chromium && \
    dnf install -y nss nspr atk at-spi2-atk gtk3 alsa-lib libgbm libdrm mesa-libEGL && \
    dnf clean all

# Unset proxy environment variables after build
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY=

# Minimal image - no managed services (E2B sets sleep infinity at template build)
CMD ["sleep", "infinity"]
