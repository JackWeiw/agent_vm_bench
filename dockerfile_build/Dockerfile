# Base image - Ubuntu 24.04 ARM64
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/ubuntu:24.04-linuxarm64

# Set environment variables to avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Configure proxy (empty by default, set via --build-arg if needed)
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ENV http_proxy=${HTTP_PROXY}
ENV https_proxy=${HTTPS_PROXY}
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}

# Install Node24 and git
RUN apt update && apt install -y wget xz-utils vim libgomp1 iproute2 net-tools supervisor gnupg git && \
    cd /usr/local && \
    wget --no-check-certificate https://nodejs.org/dist/v24.14.0/node-v24.14.0-linux-arm64.tar.xz && \
    tar -xvf node-v24.14.0-linux-arm64.tar.xz && \
    mv node-v24.14.0-linux-arm64 node24 && \
    rm -rf node-v24.14.0-linux-arm64.tar.xz && \
    ln -sf /usr/local/node24/bin/node /usr/local/bin/node && \
    ln -sf /usr/local/node24/bin/npm /usr/local/bin/npm && \
    ln -sf /usr/local/node24/bin/npx /usr/local/bin/npx && \
    git config --global http.sslVerify false

# Install openclaw 2026.6.6 and agent-browser skill
RUN npm config set strict-ssl false && \
    npm config set registry https://registry.npmmirror.com && \
    npm install -g @larksuiteoapi/node-sdk && \
    npm install -g @slack/web-api @slack/bolt && \
    npm install -g openclaw@2026.6.6 --unsafe-perm=true --registry=https://registry.npmmirror.com --strict-ssl=false --verbose && \
    cd /usr/local/node24/lib/node_modules/openclaw && \
    npm install @slack/web-api @slack/bolt && \
    ln -s /usr/local/node24/bin/openclaw /usr/local/bin/openclaw && \
    npm install -g agent-browser && \
    ln -s /usr/local/node24/bin/agent-browser /usr/local/bin/agent-browser && \
    mkdir -p /root/.openclaw/skills && \
    cd /root/.openclaw/skills && \
    npm install @anthropic-ai/agent-browser-skill 2>/dev/null || \
    git clone --depth 1 https://github.com/vercel-labs/agent-browser.git agent-browser

# Create openclaw config directory and configuration
RUN mkdir -p /root/.openclaw/workspace

COPY openclaw.json /root/.openclaw/openclaw.json

# Download llama-server
RUN cd /root && \
    wget --no-check-certificate https://github.com/ggml-org/llama.cpp/releases/download/b9660/llama-b9660-bin-ubuntu-arm64.tar.gz && \
    tar -zxvf llama-b9660-bin-ubuntu-arm64.tar.gz && \
    rm -rf llama-b9660-bin-ubuntu-arm64.tar.gz && \
    wget --no-check-certificate -O bge-small-zh-v1.5-f16.gguf "https://hf-mirror.com/CompendiumLabs/bge-small-zh-v1.5-gguf/resolve/main/bge-small-zh-v1.5-f16.gguf"

# Setup supervisor configuration for llama-server and openclaw gateway
RUN mkdir -p /etc/supervisor/conf.d && \
    touch /var/log/llama.log /var/log/openclaw.log

COPY llama_openclaw.conf /etc/supervisor/conf.d/llama_openclaw.conf

# Add xtradeb PPA key for Chromium ARM64 (download directly)
RUN mkdir -p /usr/share/keyrings && \
    wget --no-check-certificate -O /tmp/xtradeb.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x82BB6851C64F6880" && \
    gpg --dearmor -o /usr/share/keyrings/xtradeb-keyring.gpg /tmp/xtradeb.asc && \
    rm /tmp/xtradeb.asc

# Create xtradeb apt sources
RUN echo 'Types: deb\nURIs: https://ppa.launchpadcontent.net/xtradeb/apps/ubuntu/\nSuites: noble\nComponents: main\nSigned-By: /usr/share/keyrings/xtradeb-keyring.gpg' > /etc/apt/sources.list.d/xtradeb-apps.sources && \
    echo 'Types: deb\nURIs: https://ppa.launchpadcontent.net/xtradeb/play/ubuntu/\nSuites: noble\nComponents: main\nSigned-By: /usr/share/keyrings/xtradeb-keyring.gpg' > /etc/apt/sources.list.d/xtradeb-play.sources

# Update apt and install Chromium (non-Snap version)
RUN apt update -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false && \
    apt install -y -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false chromium && \
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# Clear logs for clean image
RUN > /var/log/llama.log && \
    > /var/log/openclaw.log

# Unset proxy environment variables after build
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY=

# Set the default command to run supervisor
CMD ["/usr/bin/supervisord", "-n"]