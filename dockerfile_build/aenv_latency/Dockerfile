# AgentENV snapshot lazy-load latency benchmark image (ARM64).
#
# Lean Ubuntu base + the `latbench` probe only. No coding/browser workload — this
# image is intentionally minimal so the snapshot baseline is small and the lazy-load
# signal of the 256MiB working set is clean. Build, then run `aenv pull` to register
# the template; drive the suite with scripts/aenv_latency_bench.py.
#
# Build:  docker build --build-arg HTTP_PROXY=http://your-proxy:8888 \
#           -t ubuntu-aenv-latency-bench:24.04-linuxarm64 -f Dockerfile .
# x86:    docker build --build-arg HTTP_PROXY=http://your-proxy:8888 \
#           -t ubuntu-aenv-latency-bench:24.04-x86_64 -f Dockerfile.x86 .

FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/ubuntu:24.04-linuxarm64 AS builder
ENV DEBIAN_FRONTEND=noninteractive

# Corporate proxy (set via --build-arg HTTP_PROXY=...). Empty by default.
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ENV http_proxy=${HTTP_PROXY}
ENV https_proxy=${HTTPS_PROXY}
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}

# The corp proxy presents a self-signed cert ("Huawei Web Secure Internet Gateway
# CA"), so ca-certificates is useless here (the system store won't trust the proxy
# root). Skip TLS verification for apt the same way the coding Dockerfiles skip it
# for pip (--trusted-host) and git (http.sslVerify=false).
RUN printf 'Acquire::https::Verify-Peer "false";\nAcquire::https::Verify-Host "false";\n' \
        > /etc/apt/apt.conf.d/99no-verify-proxy && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc libc-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*
COPY latbench.c /src/latbench.c
RUN cc -O2 -std=c11 -o /latbench /src/latbench.c -lrt

FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/ubuntu:24.04-linuxarm64
ENV DEBIAN_FRONTEND=noninteractive
# Final image is proxy-free (sandbox must not inherit a build-time corp proxy).
COPY --from=builder /latbench /usr/local/bin/latbench
RUN mkdir -p /opt/latbench
# Smoke: the probe must run so the harness can drive it.
RUN latbench help
CMD ["sleep", "infinity"]
