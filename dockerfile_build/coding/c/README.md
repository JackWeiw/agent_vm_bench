# E2B Coding Benchmark (C) — redis/redis

E2B sandbox coding benchmark for AI coding-agent memory stress testing, **C
language** variant. The per-round workflow is the shared coding loop
(`find → read → edit → verify → git diff`), but like the Java variant the verify
step runs the project's **real test suite** (`make` + `./runtest`) and asserts
the instance's **FAIL_TO_PASS / PASS_TO_PASS** tests from the Tcl output.

## Trace-faithful loop

```
find → read → edit → verify (make + ./runtest --single unit/type/stream-cgroups) → git diff
```

| Step | What the sandbox actually runs |
|------|--------------------------------|
| find  | `git checkout -- src/` (reset) + locate the target file |
| read  | `head -20` of the target file |
| edit  | literal `find → replace` applied with `python3`; the gold pair carries a 3-hunk `edits` list (pairs are data, see below) |
| verify | `make -j$(nproc) && TERM=dumb ./runtest --clients 1 --durable --single unit/type/stream-cgroups`, then a `python3` pass asserts every listed test printed `[ok]` and none printed `[err]`/`[skip]`/`[ignore]` |
| diff  | `git diff > /tmp/bench_round_N.patch` |

**NOT in the loop** (and therefore not in the bench):

- ❌ `make distclean` / a clean rebuild — the image ships a warm tree; verify is
  the incremental rebuild a real agent triggers after editing `src/`.
- ❌ the full `./runtest` suite — only the instance's own unit file.
- ❌ a resident redis server — verification is transient (the suite spawns and
  tears its own servers).

### Verify = FTP + PTP, not a hand-written probe

The original ts/go variants only had a FAIL_TO_PASS assertion (a self-written
temp program). A PTP pass is what actually shows a coding agent's real resource
profile: the agent must not only make the new test pass, it must keep the
existing suite green, so the run has to execute the real harness.

Per pair, the payload declares:

```python
"fail_to_pass": ("Consumer Group Lag with XDELs and tombstone after the last_id of consume group",),
"pass_to_pass": (... 67 tests from tests/unit/type/stream-cgroups.tcl ...),
```

The runner (`src/bench_core/task_runner/coding.py::_run_test_suite_verify`):

1. collects `fail_to_pass + pass_to_pass`, returns `None` when both are empty
   (generic edit pairs then fall back to `coding.verify_cmd` — the smoke probe);
2. runs the profile's `test_suite_cmd` (the project's own Makefile + Tcl
   harness) and captures the output;
3. parses `[ok]: <name> (<n> ms)` lines: a listed test that is missing, or that
   appears as `[err]`/`[skip]`/`[ignore]`, fails verify. The suite's own exit
   code is preserved, so a build/exception failure also fails. Success prints
   `verified N tests`.

## Replacement pairs

Canonical content lives in `src/bench_core/payload/coding_payload.py`
(`DEFAULT_CODING_C_SOURCE_FILES`), so `config/common/coding-c.yaml` carries no
`source_files`.

- **pair[0]** is a generic include-line edit (`src/t_stream.c`) — the warmup
  runs `source_files[0]` **before** any edit exists, so a FAIL_TO_PASS pair must
  never be first (it would fail on the base commit).
- **pair[1]** mirrors the instance's gold patch, a **three-hunk** change to
  `src/t_stream.c` (`edits` list):
  1. drop the "latest tombstone is before the first entry" early return in
     `streamRangeHasTombstones`;
  2. report an invalid entries-read counter in
     `streamEstimateDistanceFromFirstEverEntry` when a tombstone lies between
     the id and the stream's last-generated-id;
  3. test tombstones from the group's last-delivered-id, not the entry being
     delivered, in `streamReplyWithRange`.
  It carries the instance's FTP/PTP selection.
- pairs 2-5 add `/* bench */` to `#include "server.h"` in other `src/` files,
  so a long round-robin keeps editing real C source without re-running the same
  patch.

A pair may use either a single `find`/`replace` or an `edits` list of
`{find, replace}` hunks applied in order to the same file
(`_build_multi_edit_command`); a missing hunk exits 2, so a partial patch can
never masquerade as the gold fix.

## Memory pressure model

```
Time → (per sandbox)
Warmup:    ████  make -j$(nproc) + gcc probe (cc/link transient peak)
Round:     find read edit verify(peak) diff
                             ↑
                    make -j$(nproc) && ./runtest --single unit/type/stream-cgroups
                    (cc rebuilds t_stream.c + tcl spawns redis-server)
                    × N concurrent sandboxes, staggered → host overcommit
```

- **Per-sandbox peak**: incremental `make` of the changed C file plus the Tcl
  suite spawning a temporary redis-server. Transient (seconds to ~1 minute),
  then released.
- **Host pressure**: N overlapping verify peaks (spread by `coding_interval_*` /
  round stagger) → host memory overcommit, measured by `vm_monitor` /
  `smap_tool`.
- **No fabricated resident baseline** — no daemon, no watcher.

## Project & image

- **Repo**: `github.com/redis/redis` at base_commit
  `94b9072e44af0bae8cfe2de5cfa4af7b8e399759` (`redis__redis-13338`).
- **Toolchain**: `build-essential` (gcc + make) + `tcl` + `python3` from Ubuntu
  24.04. The build asserts gcc/make/tcl exist, `python3` is 3.12, and
  `redis-server --version` runs after the warm build.
- **Warm build**: the image runs `make -j$(nproc)` at build time, leaving
  `src/redis-server` / `src/redis-cli` populated. The runtime verify is then an
  incremental rebuild — the same work a real agent triggers after editing
  `src/`.
- **Test patch baked in**: `tests/redis-13338-test.patch` is the dataset's
  `test_patch`, applied at build time. It lives under `tests/`, so the `find`
  step's `git checkout -- src/` never reverts it. The build greps for the new
  case so a silently-unapplied patch fails the image.
- **`python3` is required at runtime**: the edit step inlines a
  `python3 - <<'PYEOF'` literal replace and the verify step parses the Tcl
  output with `python3`. `ubuntu:24.04` ships none, so it is installed
  explicitly (`python3=3.12.3-0ubuntu2.1`, overridable via `ARG PYTHON_VER`).
- **Proxy**: corporate TLS MITM → `git config http.sslVerify=false`. Build with
  `--build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...`.
- Image tag used by `config/common/coding-c.yaml`:
  `ubuntu-openclaw-coding-c:24.04-x86_64`.

## Language extensibility

C is a value of `coding.language` (`"c"`), **not** a new `workflow_type`. Only
the verify mechanics are language data, captured in `CodingLanguageProfile`
(`src/bench_core/payload/coding_payload.py`):

| Profile field | ts (vuejs/core) | go (hugo) | java (gson) | c (redis) |
|---------------|-----------------|-----------|-------------|-----------|
| `temp_test_path` | `/tmp/bench_verify.mjs` | `/tmp/bench_verify.go` | `/tmp/BenchVerify.java` | `/tmp/bench_verify.c` |
| `heredoc_eof` | `EOF` | `GOEOF` | `JAVAEOF` | `CEOF` |
| `run_cmd` | `npx tsx …` | `go run …` | `mvn -o … compile && javac … && java …` | `make -j… && gcc … && /tmp/bench_verify` |
| `source_find_names` | `*.ts *.tsx *.js` | `*.go` | `*.java` | `*.c` |
| `checkout_paths` | `packages/` | `markup/` | `gson/src/main/java/` | `src/` |
| `project_marker` | `package.json` | `go.mod` | `pom.xml` | `Makefile` |
| `test_module` | _(empty → probe)_ | _(empty)_ | `gson` (Maven `-pl`) | _(empty)_ |
| `test_suite_cmd` | — | — | — | `make -j… && TERM=dumb ./runtest …` |
| `test_args` | — | — | `-Djava.version=8` | — |

`test_module` (Maven) or `test_suite_cmd` (any other harness) is what switches
verify from the ad-hoc probe to the real suite; an empty value keeps the old
probe behaviour. Adding C++/Rust later means a new profile + its own suite
command/report parser.

## Build & push

```bash
cd dockerfile_build/coding/c

# x86_64 — this exact tag is what coding-c.yaml's docker.image uses, what
# push_to_harbor.sh looks for (BASE_IMAGE), and what the offline tarball
# (`docker load`) restores. One tag, three consumers — no retagging.
docker build -t ubuntu-openclaw-coding-c:24.04-x86_64 -f Dockerfile.x86 .

# ARM64
docker build -t ubuntu-openclaw-coding-c:24.04-linuxarm64 -f Dockerfile .
```

`apt` and the redis clone go through the corp proxy, so pass it explicitly:

```bash
docker build -t ubuntu-openclaw-coding-c:24.04-x86_64 -f Dockerfile.x86 \
  --build-arg HTTP_PROXY=http://<proxy>:8080 \
  --build-arg HTTPS_PROXY=http://<proxy>:8080 .
```

```bash
# Push to Harbor + install E2B-required system packages (systemd/sshd/sudo/websocat)
PROXY=http://<proxy>:8080 HARBOR_IP=X bash push_to_harbor.sh
# For x86_64, set ARCH=x86 (default is arm)
ARCH=x86 PROXY=http://<proxy>:8080 HARBOR_IP=X bash push_to_harbor.sh

# Build the E2B template (alias is free-form; no code registration needed)
python3 ../../build_e2b.py --server-ip X --harbor-ip X \
    --alias openclaw-coding-c-v1 \
    --image e2b-orchestration/ubuntu-coding-c-bench:custom \
    --cpu 2 --memory 4096
```

## Manual sandbox testing

```bash
# Inside a running sandbox (or via sbx.commands.run from the runner):
bash /opt/coding-bench/bench_helper.sh 0        # generic edit + gcc smoke verify
bash /opt/coding-bench/bench_helper.sh 1        # 3-hunk gold patch + real Tcl suite
bash /opt/coding-bench/bench_helper.sh --no-verify
```

Round 1 echoes `Verify: SUCCESS (<n>ms)` (summary line `Verify: <n>ms (exit: 0)`)
and writes `/tmp/bench_round_1.patch` (78 lines). On the unmodified base commit
the same test case fails (`Expected '2' to be equal to ''` -- the lag assertion)
-- that is the FAIL_TO_PASS baseline. Note the helper only echoes the last 10
lines of the suite log; inspect `/tmp/verify_output.log` for per-test `[ok]`.
The helper reads `bench_pairs.json`, generated from
`DEFAULT_CODING_C_SOURCE_FILES`, so it never drifts from the Python runner.

## Run the benchmark

```bash
# e2b / aenv (same stress profile; pick via --provider)
bench-core --provider e2b  --config config/common/coding-c.yaml --create-only
bench-core --provider aenv --config config/common/coding-c.yaml --create-only

# docker: needs a reachable daemon (DOCKER_HOST for a remote/WSL daemon)
bench-core --provider docker --config config/common/coding-c.yaml -n 1

# Validate config/profile/runner without any SDK
bench-core --provider fake --config config/common/coding-c.yaml --create-only -n 1
```

Local (WSL2) docker preview: the image is already built; start the daemon in its
own terminal and point the runner at it:

```powershell
wsl -d CentOS7 -u root -- dockerd -H unix:///var/run/docker.sock -H tcp://0.0.0.0:2375 --tls=false --userland-proxy-path=/usr/bin/docker-proxy
$env:DOCKER_HOST = "tcp://localhost:2375"
bench-core --provider docker --config config/common/coding-c.yaml -n 1
```

E2B/aenv run the same image through `push_to_harbor.sh` + `build_e2b.py`; the
template alias is `openclaw-coding-c-v1` in both blocks of the config.

## Step order

`CODING_STEP_ORDER = [find, read, edit, verify, diff]` — shared with the other
variants. Metrics/reporting are language-agnostic: they key on step names, so
the C `verify` peak flows through the same `Coding_verify_*` columns.

## Credibility argument

The edit pairs are real source state at the instance's base commit, and the
verify is the project's own Tcl harness asserting the dataset's
FAIL_TO_PASS/PASS_TO_PASS tests — not a synthetic probe. Nothing is added "for
memory" that a real coding agent wouldn't do: no clean rebuild, no full suite,
no resident process.
