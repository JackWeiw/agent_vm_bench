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
# Django's runtime deps (asgiref, sqlparse) are pip-installed; ENV PYTHONPATH makes
# django importable in script mode (python3 resolves from /opt/coding-bench). The heavy `django.forms` / db
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
    # touches. django itself resolves from the cloned source via PYTHONPATH
    # (set below), matching the real agent's script-mode verify pattern.
    # Ubuntu 24.04 pip is externally-managed (PEP 668) -> --break-system-packages.
    pip3 install --no-cache-dir --break-system-packages \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        asgiref==3.8.1 sqlparse==0.5.0 && \
    git config --global http.sslVerify false && \
    apt clean && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# --- Coding benchmark project: django/django (real popular Python repo) ---
# Clone + ENV PYTHONPATH (lightest approach — no pip install needed).
# Script-mode python3 resolves `import django` from /opt/coding-bench via
# PYTHONPATH. Pinned to the SWE-bench django__django-15781 base_commit so
# trace-faithful edits target real source state.
ENV PYTHONPATH=/opt/coding-bench
RUN cd /opt && \
    git clone https://github.com/django/django.git coding-bench && \
    cd coding-bench && \
    git checkout 8d160f154f0240a423e83ffe0690e472f837373c

# Smoke check: run in script mode (python3 /tmp/_smoke.py) so it catches the
# same ModuleNotFoundError the runtime verify would hit. ENV PYTHONPATH (set
# above) makes django importable from /opt/coding-bench in script mode.
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
