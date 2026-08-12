# E2B Coding Benchmark Template (Python) — Build & Manual Test Guide

## Overview

This template creates an E2B sandbox containing **django/django** (60k+ GitHub
stars, the Django web framework) for testing host memory capacity sensitivity
under AI coding-agent scenarios. django is a **real repo used across
swe_bench / swe-bench-verified evaluations** — not a synthetic project.

**Trace-faithful loop.** The per-round workflow mirrors the captured openclaw
agent trajectories that already drive the [ts](../ts/README.md) and
[go](../go/README.md) variants:

```
find → read → edit → verify (write ad-hoc /tmp/bench_verify.py + python3) → git diff
```

A real coding agent on django verifies by writing a small ad-hoc `.py` that
imports django's module graph and runs it via `python3` — a transient CPython
process that loads the settings/urls/forms/db module graph into memory (the
memory peak). The agent NEVER runs a production `manage.py runserver`, NEVER
runs the full `tests/runtests.py` suite, and NEVER keeps a resident dev server.
N concurrent sandboxes' staggered verify peaks overlapping → host memory
overcommit.

## Memory Pressure Model

```
┌─ Sandbox Memory Timeline ──────────────────────────────────────────┐
│                                                                     │
│  Warmup:  one python3 verify  ████  (CPython imports django graph) │
│                                                                     │
│  Round:   find  read  edit  verify(peak)  diff                      │
│                              ↑                                      │
│                       python3 /tmp/bench_verify.py                 │
│                       (CPython loads settings/urls/forms/db graph, │
│                        transient)                                   │
│                                                                     │
│  × N concurrent sandboxes, staggered → host memory overcommit       │
└─────────────────────────────────────────────────────────────────────┘
```

| Process | Memory | Reason |
|---------|--------|--------|
| `python3 /tmp/bench_verify.py` | transient peak | CPython imports + executes the django settings/urls/forms/db module graph (the heavy `django.forms`/`django.db` graph is the real transient peak), then releases. |
| N sandboxes overlapping | host overcommit | Staggered verify peaks overlap at the host → real pressure, measured by `vm_monitor` / `smap_tool`. |

**Why NOT `manage.py runserver` / full test suite / resident server**: none of
these appear in a real django agent's verify pattern. Pressure comes honestly
from concurrent transient verify peaks — the same model as ts/go.

**Why no cache clear (unlike go)**: go runs `go clean -cache` before every
verify because the Go toolchain caches *compiled types* under `GOCACHE`, making
the first run ~40% CPU and every later run ~10%. Python's `__pycache__` holds
cheap *bytecode* (not compiled types) — the in-memory module graph (the actual
peak) is unchanged warm or cold. So python, like ts, needs no `pre_verify_cmd`
and does a plain single write+run.

## Project Structure (inside sandbox)

```
/opt/coding-bench/                    # django (git clone, shallow)
├── django/                           # the framework package (all edits live here)
│   ├── conf/global_settings.py       # ← round-robin edit target
│   ├── db/models/fields/__init__.py  # ← round-robin edit target
│   ├── http/response.py              # ← round-robin edit target
│   ├── utils/text.py                 # ← round-robin edit target
│   ├── template/base.py              # ← round-robin edit target
│   └── urls/resolvers.py             # ← round-robin edit target
├── pyproject.toml / tests/ ...
├── .git/                             # Git repo for checkout/reset + diff
└── bench_helper.sh                   # Manual testing helper script
```

Runtime deps (`asgiref`, `sqlparse` — the only third-party packages a bare
django import touches) are pip-installed. The rest of django resolves from the
cloned source via cwd on `sys.path`.

## Modification Strategy (per round)

Each benchmark round simulates a real AI coding agent's verification cycle
(mirrors ts/go):

```
Step 0: find    — git checkout -- django/ (reset) + verify/locate target file
Step 1: read    — head -20 target file (agent confirming context)
Step 2: edit    — apply a pre-configured find→replace pair (real type-safe edit)
Step 3: verify  — write /tmp/bench_verify.py + python3 run (memory peak)
Step 4: diff    — git diff > /tmp/bench_round_N.patch (verification artifact)
```

**Key design decisions**:

1. **`git checkout -- django/`** — config/support files (pyproject.toml, tests/)
   are NOT reset, so install settings persist across rounds. Agents revert the
   framework source but keep infrastructure config (mirrors ts/go).

2. **Real type-safe edit, not risky rewriting** — each round applies a
   pre-configured `find→replace` pair. Most pairs are safe comment-appends on
   stable class/function signatures (e.g. `class HttpResponse:` → `# bench`),
   guaranteeing the edit never breaks compilation. The first pair additionally
   carries a real `verify_script` that imports django and asserts a real
   invariant (`LANGUAGE_CODE == "en-us"`), demonstrating a genuine
   edit+verify coupling.

3. **Verify = write ad-hoc test + `python3`** — the default script configures
   bare settings (no DB engine, no installed apps) and imports the heavy graphs
   (`django.urls`, `django.forms`, `django.template`). `django.forms` pulls in
   `django.db`/models — the largest trace-faithful import graph a bare
   `python3` can load without a DB connection. Pairs without their own script
   fall back to this shared default, so every round pays a real import peak.

4. **No per-round `free -m`** — memory pressure is observed at the host level
   via `vm_monitor` / `smap_tool`, not from a per-round `free -m` inside the
   sandbox (no useful value).

## Build Steps

### 1. Build Docker Image

```bash
cd dockerfile_build/coding/python
docker build -t ubuntu-coding-python-bench:24.04-linuxarm64 -f Dockerfile .
```

**x86_64:**

```bash
docker build -t ubuntu-coding-python-bench:24.04-x86_64 -f Dockerfile.x86 .
```

**openEuler (optional):**

The openEuler variants build from `Dockerfile.openeuler` / `Dockerfile.openeuler.x86`
on an `openeuler-24.03-lts-sp3:latest` base (loaded from tar — not a registry).

Step 0 — load the openEuler base tar:

```bash
# ARM64
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/aarch64/openEuler-docker.aarch64.tar.xz
xz -d openEuler-docker.aarch64.tar.xz
docker load -i openEuler-docker.aarch64.tar
# x86_64
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/docker_img/x86_64/openEuler-docker.x86_64.tar.xz
xz -d openEuler-docker.x86_64.tar.xz
docker load -i openEuler-docker.x86_64.tar
```

Build:

```bash
# ARM64
docker build -t openeuler-coding-python-bench:24.03-lts-sp3-linuxarm64 -f Dockerfile.openeuler .
# x86_64
docker build -t openeuler-coding-python-bench:24.03-lts-sp3-x86_64 -f Dockerfile.openeuler.x86 .
```

### 2. Push to Harbor

```bash
HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
```

For x86_64, set `ARCH=x86` (the default is `arm`):

```bash
ARCH=x86 HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
```

For openEuler, set `OS=openeuler` (with optional `ARCH=x86`):

```bash
OS=openeuler HARBOR_IP=<your_harbor_ip> bash push_to_harbor.sh
```

This adds E2B-required packages (systemd, openssh-server, websocat) and pushes to Harbor registry.

### 3. Build E2B Template

`build_e2b.py` is shared and lives at `dockerfile_build/build_e2b.py`:

```bash
python3 ../../build_e2b.py \
    --server-ip <e2b_api_ip> \
    --harbor-ip <harbor_ip> \
    --alias openclaw-coding-python-v1 \
    --image e2b-orchestration/ubuntu-coding-python-bench:custom \
    --cpu 2 \
    --memory 4096
```

### 4. Manual Sandbox Testing

```bash
# Inside sandbox:
bash /opt/coding-bench/bench_helper.sh 0       # Round 0, all steps
bash /opt/coding-bench/bench_helper.sh 1       # Round 1
bash /opt/coding-bench/bench_helper.sh --no-verify   # skip the verify step
```

### 5. Memory Measurement Points

| Observation | Command | What to Check |
|-------------|---------|---------------|
| Baseline (idle) | `numastat -p firecracker` | ~200-300MB (OS + agent) |
| Verify peak | `numastat -p firecracker` during `python3` | transient peak (module graph loaded) |
| N sandboxes overlapping | `numastat -p firecracker` at peak | host overcommit pressure |

### 6. Script Options

```bash
bash bench_helper.sh [ROUND] [OPTIONS]
  --round=N       Round number
  --no-verify     Skip the verify step
  --help          Show help
```

## Language extensibility

`python` is a value of `coding.language` (`"python"`), not a hard-coded
workflow. The coding loop is shared across languages; only the verify mechanics
differ, captured as data in a `CodingLanguageProfile` registry
(`e2b_bench/config.py`). The ts/go/python variants all share the same
`find → read → edit → verify → diff` loop; adding C++ later = one registry
entry + its default verify script. (The profile is keyed `python` because
django's source is Python and the verify scripts import `.py` source and run
via `python3`.)

## Credibility argument

Every step in the loop mirrors the captured openclaw trajectory shape already
used to validate the ts/go variants. `python3` running a self-written ad-hoc
import of django's module graph is the exact verification shape a real django
coding agent uses. No production server, no full test suite, no resident
process — nothing is added "for memory" that isn't a real agent behavior.
