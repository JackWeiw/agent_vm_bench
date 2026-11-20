# Coding Benchmark Template (C) - E2B Sandbox for AI Coding Agent Memory Stress Testing
#
# Environment: Pure C coding benchmark (NO browser/Chromium, NO agent tools,
# NO resident dev server, NO production packaging step)
#
# Project: redis/redis (https://github.com/redis/redis)
#   - Real swe_bench_multilingual instance redis__redis-13338 (XINFO group lag
#     is wrong when a tombstone sits after the consumer group's
#     last-delivered-id), cloned at base_commit 94b9072e.
#   - Toolchain: gcc + make + tcl (the toolchain the project's own test suite
#     uses).
#
# Trace-faithful loop: find -> read -> edit -> verify -> git diff. Verify runs
# the instance's REAL Tcl suite (`make` + `./runtest --single
# unit/type/stream-cgroups`) and asserts every FAIL_TO_PASS / PASS_TO_PASS test
# reported `[ok]` -- the project's own harness, not a hand-written probe. That
# transient cc/link + spawned redis-server peak is what N sandboxes overlap.
# No production packaging, no full test suite, no resident process.
#
# Build:  docker build -t ubuntu-openclaw-coding-c:24.04-linuxarm64 -f Dockerfile .
# Push:   HARBOR_IP=X bash push_to_harbor.sh
# Template: python3 ../../build_e2b.py --server-ip X --harbor-ip X \
#     --alias openclaw-coding-c-v1 --image e2b-orchestration/ubuntu-coding-c-bench:custom

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

# Install the C toolchain + Tcl (redis tests are Tcl) + git.
# See Dockerfile.x86 for the full rationale (corp proxy MITMs TLS ->
# git sslVerify=false; python3 is required by the runner's edit/verify steps;
# gcc/make/tcl are checked at build time instead of version-pinned).
ARG PYTHON_VER=3.12.3-0ubuntu2.1

RUN apt update && \
    apt install -y --no-install-recommends \
        build-essential tcl python3=${PYTHON_VER} \
        git curl vim ca-certificates && \
    git config --global http.sslVerify false && \
    apt clean && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# Fail the build (instead of silently drifting) if the toolchain the benchmark
# measures is missing or too old for redis's test harness.
RUN gcc --version | head -1 && make --version | head -1 && \
    echo 'puts "tcl [info patchlevel]"' | tclsh && \
    python3 -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version" && \
    echo "Toolchain check: gcc + make + tcl 8.6 + python3 3.12"

# --- Coding benchmark project: redis/redis (real swe_bench_multilingual repo) ---
# Clone at the instance base_commit so edits target the real source state.
RUN cd /opt && \
    git clone https://github.com/redis/redis.git coding-bench && \
    cd coding-bench && \
    git checkout 94b9072e44af0bae8cfe2de5cfa4af7b8e399759

# Bake in the instance's test patch (upstream applies it at eval time). The new
# case is the FAIL_TO_PASS fixture; it lives under tests/, so the find step's
# `git checkout -- src/` never reverts it. Applied before the warm build so the
# image always ships the exact suite the runner asserts.
COPY tests/redis-13338-test.patch /tmp/redis-13338-test.patch
RUN cd /opt/coding-bench && \
    git apply /tmp/redis-13338-test.patch && \
    grep -q "tombstone after the last_id of consume group" tests/unit/type/stream-cgroups.tcl && \
    rm -f /tmp/redis-13338-test.patch

# Warm build: leaves src/redis-server + src/redis-cli populated so the runtime
# verify is an incremental `make -j$(nproc)` (the same rebuild a real agent
# triggers after editing src/). Fails the build if the tree does not compile.
RUN cd /opt/coding-bench && make -j"$(nproc)" && \
    src/redis-server --version && src/redis-cli --version

# Smoke check: the runtime fallback verify compiles + runs a standalone probe
# with gcc (same shape as the runner, so a broken toolchain fails the build).
RUN printf '#include <stdio.h>\nint main(void){printf("All tests passed!\\n");return 0;}\n' > /tmp/_Smoke.c && \
    gcc -O2 /tmp/_Smoke.c -o /tmp/_Smoke && /tmp/_Smoke && \
    rm -f /tmp/_Smoke.c /tmp/_Smoke

# Copy the manual testing helper script + the pair list it shares with the
# Python runner (bench_core.coding_payload), then register the helper.
COPY bench_helper.sh /opt/coding-bench/bench_helper.sh
COPY bench_pairs.json /opt/coding-bench/bench_pairs.json
RUN chmod +x /opt/coding-bench/bench_helper.sh

# Unset proxy environment variables after build
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY=

# Default command: keep container running for sandbox access
CMD ["sleep", "infinity"]
