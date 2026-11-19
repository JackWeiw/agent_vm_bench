# Coding Benchmark Template (C++) - E2B Sandbox for AI Coding Agent Memory Stress Testing
#
# Environment: Pure C++ coding benchmark (NO browser/Chromium, NO agent tools,
# NO resident dev server, NO production packaging step)
#
# Project: nlohmann/json (https://github.com/nlohmann/json)
#   - Real swe_bench_multilingual instance nlohmann__json-4237 (enum
#     serialization uses number_integer even for unsigned underlying types),
#     cloned at base_commit 3780b41d.
#   - Toolchain: g++ + cmake (the project's own CMake test build) + python3
#     (the runner's edit/verify steps).
#
# Trace-faithful loop: find -> read -> edit -> verify -> git diff. Verify runs
# the instance's REAL doctest target (`cmake --build ... --target
# test-udt_cpp11` + the binary with `-r=xml`) and asserts every FAIL_TO_PASS /
# PASS_TO_PASS test case > section path passed -- the project's own test
# harness, not a hand-written probe. That transient template-heavy g++ compile
# peak is what N sandboxes overlap.
# No production packaging, no full test suite, no resident process.
#
# Build:  docker build -t ubuntu-openclaw-coding-cpp:24.04-linuxarm64 -f Dockerfile .
# Push:   HARBOR_IP=X bash push_to_harbor.sh
# Template: python3 ../../build_e2b.py --server-ip X --harbor-ip X \
#     --alias openclaw-coding-cpp-v1 --image e2b-orchestration/ubuntu-coding-cpp-bench:custom

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

# Install the C++ toolchain + cmake + git. See Dockerfile.x86 for the full
# rationale (corp proxy MITMs TLS -> git sslVerify=false; python3 is required by
# the runner's edit/verify steps; g++/cmake are checked at build time instead of
# version-pinned).
ARG PYTHON_VER=3.12.3-0ubuntu2.1

RUN apt update && \
    apt install -y --no-install-recommends \
        build-essential cmake python3=${PYTHON_VER} \
        git curl vim ca-certificates && \
    git config --global http.sslVerify false && \
    apt clean && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# Fail the build (instead of silently drifting) if the toolchain the benchmark
# measures is missing or too old for nlohmann/json's CMake test build.
RUN g++ --version | head -1 && cmake --version | head -1 && make --version | head -1 && \
    python3 -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version" && \
    echo "Toolchain check: g++ + cmake + make + python3 3.12"

# --- Coding benchmark project: nlohmann/json (real swe_bench_multilingual repo) ---
# Shallow-fetch exactly the instance base commit (no full history in the image).
RUN cd /opt && mkdir coding-bench && cd coding-bench && \
    git init -q && \
    git remote add origin https://github.com/nlohmann/json.git && \
    git fetch -q --depth 1 origin 3780b41dd070436f3f55327b0a88f27a52e2dfa8 && \
    git checkout -q FETCH_HEAD

# Bake in the instance's test patch (upstream applies it at eval time). The new
# case is the FAIL_TO_PASS fixture; it lives under tests/, so the find step's
# `git checkout -- include/ single_include/` never reverts it. Applied before
# the warm build so the image always ships the exact suite the runner asserts.
COPY tests/nlohmann-json-4237-test.patch /tmp/nlohmann-json-4237-test.patch
RUN cd /opt/coding-bench && \
    git apply /tmp/nlohmann-json-4237-test.patch && \
    grep -q "large unsigned enums are handled correctly" tests/src/unit-udt.cpp && \
    rm -f /tmp/nlohmann-json-4237-test.patch

# Warm build: configure the project's own CMake build and compile the exact
# doctest target the verify step runs, so the runtime rebuild is incremental
# (the same work a real agent triggers after editing a header). Fails the build
# if the tree does not compile. The base commit is buggy at runtime only, so a
# successful compile is expected here.
RUN cd /opt/coding-bench && \
    cmake -S . -B build && \
    cmake --build build -j"$(nproc)" --target test-udt_cpp11 && \
    test -x build/tests/test-udt_cpp11

# Smoke check: the runtime fallback verify compiles + runs a standalone probe
# against the project's single header (same shape as the runner, so a broken
# toolchain fails the build).
RUN printf '#include <nlohmann/json.hpp>\n#include <iostream>\nint main(){nlohmann::json j={{"bench",4237}}; if(j["bench"].get<int>()!=4237) return 1; std::cout << "All tests passed!\\n";}\n' > /tmp/_Smoke.cpp && \
    g++ -std=c++11 -I /opt/coding-bench/single_include /tmp/_Smoke.cpp -o /tmp/_Smoke && /tmp/_Smoke && \
    rm -f /tmp/_Smoke.cpp /tmp/_Smoke

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
