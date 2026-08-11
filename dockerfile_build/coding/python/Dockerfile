# Coding Benchmark Template (Python) - E2B Sandbox for AI Coding Agent Memory Stress Testing
#
# Environment: Pure Python coding benchmark (NO browser/Chromium, NO agent tools,
# NO resident dev server, NO production build step)
#
# Project: django/django (https://github.com/django/django)
#   - 60k+ GitHub stars, the Django web framework (large popular Python monorepo)
#   - Real repo used across swe_bench / swe-bench-verified evaluations
#   - Toolchain: CPython (the interpreter an agent touches); stdlib+django verify scripts.
#   - Pure Python, ARM64 compatible
#
# Trace-faithful loop (mirrors the captured openclaw trajectories behind the ts/go
# variants): find -> read -> edit -> verify (write ad-hoc /tmp/test_*.py + `python3`)
# -> git diff. A real AI coding agent on django verifies by writing a small ad-hoc
# Python script that imports django's module graph and running it via `python3` —
# a transient interpreter process that loads the settings/urls/forms/db module graph
# into memory (the transient peak). N concurrent sandboxes' staggered verify peaks
# overlapping -> host memory overcommit. No production `python manage.py runserver`,
# no full `python tests/runtests.py` suite, no resident dev server — none of those
# appear in the real agent verify pattern.
#
# Django's runtime deps (asgiref, sqlparse) are pip-installed so a bare
# `python3 /tmp/bench_verify.py` importing `django.*` resolves the cloned source
# (via cwd on sys.path) plus its third-party deps. The heavy `django.forms` / db
# module graph is the memory peak (same role `go run`'s compiler and `npx tsx`'s
# esbuild+node play in the ts/go variants).
#
# Build:  docker build -t ubuntu-coding-python-bench:24.04-linuxarm64 -f Dockerfile .
# Push:   HARBOR_IP=X bash push_to_harbor.sh
# Template: python3 ../../build_e2b.py --server-ip X --harbor-ip X \
#     --alias openclaw-coding-python-v1 --image e2b-orchestration/ubuntu-coding-python-bench:custom

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

# Install CPython 3.12 (Ubuntu 24.04 default), pip, git and system packages.
#
# The corp proxy presents a self-signed cert ("Huawei Web Secure Internet Gateway
# CA"), so every TLS fetch must bypass verification. ca-certificates is NOT
# installed - it is useless here (the proxy MITMs with its own root the system
# store won't trust). For git we set http.sslVerify=false; for pip we point at a
# mirror (tuna) and pass --trusted-host so cert verification is skipped per host.
# (Same TLS-bypass strategy the ts/go Dockerfiles use for their node/go tarballs.)
RUN apt update && apt install -y --no-install-recommends python3 python3-pip git curl vim && \
    # Django's runtime third-party deps. asgiref (async support, imported by
    # django.utils / django.core.management / django.forms) and sqlparse (imported
    # by django.db.backends) are the only external packages a bare django import
    # touches; the rest of django resolves from the cloned source via cwd on sys.path.
    # Ubuntu 24.04 pip is externally-managed (PEP 668) -> --break-system-packages.
    pip3 install --no-cache-dir --break-system-packages \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        asgiref==3.8.1 sqlparse==0.5.0 && \
    git config --global http.sslVerify false && \
    apt clean && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# --- Coding benchmark project: django/django (real popular Python repo) ---
# Clone + verify the interpreter can resolve `import django` from the project dir.
# No production build, no manage.py runserver: the runtime verify step is a bare
# `python3 /tmp/bench_verify.py` that imports django's module graph (exactly the
# shape of a real coding agent's ad-hoc verify). Pinned to the SWE-bench
# django__django-15781 base_commit so trace-faithful edits target real source state.
RUN cd /opt && \
    git clone https://github.com/django/django.git coding-bench && \
    cd coding-bench && \
    git checkout 8d160f154f0240a423e83ffe0690e472f837373c

ENV PYTHONPATH=/opt/coding-bench

# Smoke check: run in script mode (not -c) so sys.path[0] is /tmp/, not CWD,
# catching the same ModuleNotFoundError the runtime verify would hit.
RUN echo 'import django; from django.conf import settings; print("django", django.__version__)' > /tmp/_smoke.py && \
    python3 /tmp/_smoke.py && rm /tmp/_smoke.py

# Copy the manual testing helper script
COPY bench_helper.sh /opt/coding-bench/bench_helper.sh
RUN chmod +x /opt/coding-bench/bench_helper.sh

# Unset proxy environment variables after build
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY=

# Default command: keep container running for sandbox access
CMD ["sleep", "infinity"]
