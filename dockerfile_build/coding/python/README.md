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
find → read → edit → verify (./tests/runtests.py auth_tests.test_basic) → git diff
```

The repo is the real django tree pinned to the SWE-bench instance base commit;
verify runs the instance's own test module through django's runner, i.e. a
transient CPython process that loads the settings/urls/forms/db module graph and
executes 13 tests (the memory peak). The agent NEVER runs a production
`manage.py runserver`, NEVER runs the whole `tests/runtests.py` suite, and NEVER
keeps a resident dev server. N concurrent sandboxes' staggered verify peaks
overlapping → host memory overcommit.

**SWE-bench instance.** The image is pinned to the
`django__django-16631` base commit (`9b224579`, Django 5.0 dev) with that
instance's test patch baked in (`tests/auth_tests/test_basic.py`). Python has
exactly ONE pair: the upstream gold patch (SECRET_KEY_FALLBACKS session-hash
fallback, two files). It carries the instance's real FAIL_TO_PASS /
PASS_TO_PASS selection and its verify runs django's own runner
(`./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1
auth_tests.test_basic`), asserting all 13 ids ran and passed — exactly what the
upstream eval does. In fixed mode, warmup runs the same pair on the untouched base commit,
where the FAIL_TO_PASS test is expected to fail (that red run is the
baseline); round-robin (the default) applies the patch first, so every round
is green.

## Memory Pressure Model

```
┌─ Sandbox Memory Timeline ──────────────────────────────────────────┐
│                                                                     │
│  Warmup:  one verify run  ████  (fixed mode: base = expected red,   │
│                                  no metrics recorded)              │
│                                                                     │
│  Round:   find  read  edit  verify(peak)  diff                      │
│                              ↑                                      │
│                       ./tests/runtests.py --settings=test_sqlite    │
│                       (CPython loads the django module graph and    │
│                        runs 13 tests, transient)                    │
│                                                                     │
│  × N concurrent sandboxes, staggered → host memory overcommit       │
└─────────────────────────────────────────────────────────────────────┘
```

| Process | Memory | Reason |
|---------|--------|--------|
| `./tests/runtests.py ... auth_tests.test_basic` | transient peak | CPython imports the django settings/urls/forms/db graph and runs the 13-test auth module (the heavy `django.forms`/`django.db` graph is the real transient peak), then releases. |
| N sandboxes overlapping | host overcommit | Staggered verify peaks overlap at the host → real pressure, measured by `vm_monitor` / `smap_tool`. |

**Why the real test module (and nothing more)**: the upstream eval runs exactly
this module, so the verify is the instance's own harness — no `manage.py
runserver`, no full-suite run, no resident server, no invented extra steps.

**Why no cache clear (unlike go)**: go runs `go clean -cache` before every
verify because the Go toolchain caches *compiled types* under `GOCACHE`, making
the first run ~40% CPU and every later run ~10%. Python's `__pycache__` holds
cheap *bytecode* (not compiled types) — the in-memory module graph (the actual
peak) is unchanged warm or cold. So python, like ts, needs no `pre_verify_cmd`
and does a plain single write+run.

## Project Structure (inside sandbox)

```
/opt/coding-bench/                    # django @ django__django-16631 base_commit
├── django/                           # the framework package (all edits live here)
│   ├── contrib/auth/__init__.py      # ← gold-patch edit target
│   └── contrib/auth/base_user.py     # ← gold-patch edit target
├── pyproject.toml / tests/ ...
├── .git/                             # Git repo for checkout/reset + diff
├── bench_pairs.json                  # the single gold pair, from coding_payload
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
Step 2: edit    — apply the gold patch's 3 hunks across 2 files (atomic)
Step 3: verify  — ./tests/runtests.py ... auth_tests.test_basic + id assertion
Step 4: diff    — git diff > /tmp/bench_round_N.patch (verification artifact)
```

**Key design decisions**:

1. **`git checkout -- django/`** — config/support files (pyproject.toml, tests/)
   are NOT reset, so install settings persist across rounds. Agents revert the
   framework source but keep infrastructure config (mirrors ts/go).

2. **Real gold-patch edit, applied atomically** — each round applies the
   instance's two-file / three-hunk patch. A stale hunk aborts the edit (exit 2)
   without writing anything, so a partial patch can never fake a green verify.

3. **Verify = the instance's real test module** — `./tests/runtests.py
   --verbosity 2 --settings=test_sqlite --parallel 1 auth_tests.test_basic`,
   then every FAIL_TO_PASS + PASS_TO_PASS id must appear as `... ok` in the log;
   a missing id is `NOT RUN`, a non-ok status is `NOT PASSED`. This is the
   upstream eval's own command, so the verify is the project's real harness.

4. **No per-round `free -m`** — memory pressure is observed at the host level
   via `vm_monitor` / `smap_tool`, not from a per-round `free -m` inside the
   sandbox (no useful value).

5. **Warmup is the F2P baseline (fixed mode)** — warmup runs the same pair
   before any edit,
   so the base commit is expected to be red. That warning is the FAIL_TO_PASS
   evidence and never enters the coding metrics; rounds start from a healthy
   reset.

## Build Steps

### 1. Build Docker Image

The build shallow-fetches django at the `django__django-16631` base commit,
applies `tests/django-16631-test.patch` (the FAIL_TO_PASS fixture), and adds a
`python` shim so the upstream `./tests/runtests.py` command runs verbatim.

```bash
cd dockerfile_build/coding/python
docker build -t ubuntu-coding-python-bench:24.04-linuxarm64 -f Dockerfile .
```

**x86_64:**

```bash
docker build -t ubuntu-openclaw-coding-python:24.04-x86_64 -f Dockerfile.x86 .
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
(`src/bench_core/payload/coding_payload.py`). The ts/go/python variants all share the same
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
