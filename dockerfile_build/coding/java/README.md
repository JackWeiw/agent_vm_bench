# E2B Coding Benchmark (Java) — google/gson

E2B sandbox coding benchmark for AI coding-agent memory stress testing, **Java
language** variant. The per-round workflow is the shared coding loop
(`find → read → edit → verify → git diff`), but unlike the ts/go variants the
verify step runs the project's **real Maven test suite** and asserts the
instance's **FAIL_TO_PASS / PASS_TO_PASS** methods from the surefire reports.

## Trace-faithful loop

```
find → read → edit → verify (mvn -o test on the real test class) → git diff
```

| Step | What the sandbox actually runs |
|------|--------------------------------|
| find  | `git checkout -- gson/src/main/java/` (reset) + locate the target file |
| read  | `head -20` of the target file |
| edit  | literal `find → replace` applied with `python3` (pairs are data, see below) |
| verify | `mvn -o -q -B -pl gson -Djava.version=8 -Dtest=<classes> -DfailIfNoTests=false test`, then a `python3` pass over `gson/target/surefire-reports/TEST-*.xml` asserts every listed method ran with no failure/error/skip |
| diff  | `git diff > /tmp/bench_round_N.patch` |

**NOT in the loop** (and therefore not in the bench):

- ❌ `mvn package` / full-module build — the agent never runs it.
- ❌ the whole module test suite — only the instance's own test class.
- ❌ a resident JVM/server — verification is transient.

### Verify = FTP + PTP, not a hand-written probe

The original ts/go variants only had a FAIL_TO_PASS assertion (a self-written
temp program). A PTP pass is what actually shows a coding agent's real resource
profile: the agent must not only make the new tests pass, it must keep the
existing suite green, so the run has to execute the real harness.

Per pair, the payload declares:

```python
"fail_to_pass": ("com.google.gson.internal.bind.JsonTreeReaderTest#testSkipValue_emptyJsonObject", ...),
"pass_to_pass": (),
```

The runner (`src/bench_core/task_runner/coding.py::_run_test_suite_verify`):

1. collects `fail_to_pass + pass_to_pass`, returns `None` when both are empty
   (generic edit pairs then fall back to `coding.verify_cmd` — the smoke probe);
2. selects only the **simple class names** for `-Dtest=...` (surefire 2.12.4,
   bound by the 2017 super POM, does not accept `Class#method`);
3. parses `TEST-<FQN>.xml` per class: missing testcase, `failure`, `error` or
   `skipped` → verify fails. Success prints `verified N tests`.

`google__gson-1014` declares **no PTP** (the official list is empty), so its
pair only carries FTP; the PTP code path is exercised by later instances
(lucene-11760 has 11 PTP methods).

## Replacement pairs

Canonical content lives in `src/bench_core/payload/coding_payload.py`
(`DEFAULT_CODING_JAVA_SOURCE_FILES`), so `config/common/coding-java.yaml`
carries no `source_files`.

- **pair[0]** is a generic class-declaration edit (`JsonParser`) — the warmup
  runs `source_files[0]` **before** any edit exists, so a FAIL_TO_PASS pair must
  never be first (it would fail on the base commit).
- **pair[1]** mirrors the instance's gold patch (`JsonTreeReader.skipValue`
  guards `stackSize` before touching `pathNames`/`pathIndices`) and carries the
  FTP class selection.
- pairs 2-5 add `// bench` to other class declarations, so a long round-robin
  keeps editing real Java source without re-running the same patch.

## Memory pressure model

```
Time → (per sandbox)
Warmup:    ████  mvn -o test of one stable class (surefire/JVM transient peak)
Round:     find read edit verify(peak) diff
                             ↑
                    mvn -o test -Dtest=JsonTreeReaderTest
                    (javac compiles the module + surefire forks a JVM)
                    × N concurrent sandboxes, staggered → host overcommit
```

- **Per-sandbox peak**: `mvn test` compiling the module and forking the surefire
  JVM. Transient (seconds), then released.
- **Host pressure**: N overlapping verify peaks (spread by `coding_interval_*` /
  round stagger) → host memory overcommit, measured by `vm_monitor` /
  `smap_tool`.
- **No fabricated resident baseline** — no daemon, no watcher.

## Project & image

- **Repo**: `github.com/google/gson` at base_commit
  `7719e73a108c450666613c279bedaed724cd9cc9` (`google__gson-1014`).
- **Toolchain**: JDK 11 + Maven, `openjdk-11-jdk` from Ubuntu 24.04.
- **Toolchain pinned**: `openjdk-11-jdk=11.0.33+9-1ubuntu1~24.04.1`,
  `maven=3.8.7-2`, `python3=3.12.3-0ubuntu2.1` (overridable via `ARG JDK_VER`
  / `MAVEN_VER` / `PYTHON_VER`). The JDK sets the JVM baseline and Maven 3.8.7's
  super POM binds surefire 2.12.4, so an unpinned install would silently change
  what `verify` measures when the image is rebuilt elsewhere. The build asserts
  both versions (`java -version` / `mvn -v`) and fails fast on a mismatch — the
  same pattern the ts image uses for `pnpm --version`. If Ubuntu retires a
  version, the build fails with `Unable to locate package <name>=<ver>`: bump the
  ARG rather than dropping the pin.
- **JDK choice**: the 2017 poms declare `<java.version>1.6</java.version>`,
  which javac 8+ rejects, so every compile passes `-Djava.version=8` — in the
  image prewarm, in `coding.verify_cmd`, and in the runner's test command.
- **Offline Maven**: the build warms `~/.m2` and `gson/target/classes`, so the
  runtime uses `mvn -o` (no network in the sandbox). `-DskipTests test` alone
  does not necessarily pull surefire's JUnit provider, so a second step runs one
  stable test class (`JsonObjectTest`, 14 tests) to resolve it.
- **Test patch baked in**: `tests/JsonTreeReaderTest.java` is the dataset's
  `test_patch`; it does not exist at the base commit. It is copied under
  `src/test/java`, so the `find` step's `git checkout -- gson/src/main/java/`
  never reverts it.
- **`python3` is required at runtime**: the edit step inlines a
  `python3 - <<'PYEOF'` literal replace and the verify step parses surefire XML
  with `python3`. `ubuntu:24.04` ships none, so it is installed explicitly.
- **Proxy**: corporate TLS MITM → `git config http.sslVerify=false`, aliyun
  Maven mirror (`settings.xml`) plus `MAVEN_OPTS` insecure-wagon flags. Build
  with `--build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...`.
- Image tag used by `config/common/coding-java.yaml`:
  `ubuntu-openclaw-coding-java:24.04-x86_64`.

## Language extensibility

Java is a value of `coding.language` (`"java"`), **not** a new `workflow_type`.
Only the verify mechanics are language data, captured in
`CodingLanguageProfile` (`src/bench_core/payload/coding_payload.py`):

| Profile field | ts (vuejs/core) | go (hugo) | java (gson) |
|---------------|-----------------|-----------|-------------|
| `temp_test_path` | `/tmp/bench_verify.mjs` | `/tmp/bench_verify.go` | `/tmp/BenchVerify.java` |
| `heredoc_eof` | `EOF` | `GOEOF` | `JAVAEOF` |
| `run_cmd` | `npx tsx …` | `go run …` | `mvn -o … compile && javac … && java …` |
| `source_find_names` | `*.ts *.tsx *.js` | `*.go` | `*.java` |
| `checkout_paths` | `packages/` | `markup/` | `gson/src/main/java/` |
| `project_marker` | `package.json` | `go.mod` | `pom.xml` |
| `test_module` | _(empty → probe)_ | _(empty)_ | `gson` (Maven `-pl`) |
| `test_args` | — | — | `-Djava.version=8` |

`test_module` is what switches verify from the ad-hoc probe to the real suite;
an empty `test_module` keeps the old probe behaviour, so adding C/C++/Python
later means a new profile + its own suite command/report parser.

## Build & push

```bash
cd dockerfile_build/coding/java

# x86_64 — this exact tag is what coding-java.yaml's docker.image uses, what
# push_to_harbor.sh looks for (BASE_IMAGE), and what the offline tarball
# (`docker load`) restores. One tag, three consumers — no retagging.
docker build -t ubuntu-openclaw-coding-java:24.04-x86_64 -f Dockerfile.x86 .

# ARM64
docker build -t ubuntu-openclaw-coding-java:24.04-linuxarm64 -f Dockerfile .
```

`apt` and the gson clone go through the corp proxy, so pass it explicitly:

```bash
docker build -t ubuntu-openclaw-coding-java:24.04-x86_64 -f Dockerfile.x86 \
  --build-arg HTTP_PROXY=http://<proxy>:8080 \
  --build-arg HTTPS_PROXY=http://<proxy>:8080 .
```

The toolchain is version-pinned (`JDK_VER` / `MAVEN_VER` / `PYTHON_VER` ARGs).
If a rebuilt environment no longer carries that apt version, apt fails loudly
with `Unable to locate package ...=<ver>`; override the ARG instead of editing
the Dockerfile:

```bash
docker build -t ubuntu-openclaw-coding-java:24.04-x86_64 -f Dockerfile.x86 \
  --build-arg JDK_VER=<available> --build-arg MAVEN_VER=<available> \
  --build-arg PYTHON_VER=<available> .
```

```bash
# Push to Harbor + install E2B-required system packages (systemd/sshd/sudo/websocat)
PROXY=http://<proxy>:8080 HARBOR_IP=X bash push_to_harbor.sh
# For x86_64, set ARCH=x86 (default is arm)
ARCH=x86 PROXY=http://<proxy>:8080 HARBOR_IP=X bash push_to_harbor.sh

# Build the E2B template (alias is free-form; no code registration needed)
python3 ../../build_e2b.py --server-ip X --harbor-ip X \
    --alias openclaw-coding-java-v1 \
    --image e2b-orchestration/ubuntu-coding-java-bench:custom \
    --cpu 2 --memory 4096
```

## Manual sandbox testing

```bash
# Inside a running sandbox (or via sbx.commands.run from the runner):
bash /opt/coding-bench/bench_helper.sh 0        # generic edit + smoke verify
bash /opt/coding-bench/bench_helper.sh 1        # gold patch + real JsonTreeReaderTest
bash /opt/coding-bench/bench_helper.sh --no-verify
```

Round 1 prints `Verify: 3368ms (exit: 0)` and writes
`/tmp/bench_round_1.patch`. On the unmodified base commit the same test class
fails with `ArrayIndexOutOfBoundsException: Index -1 out of bounds for length
32` (2 errors) — that is the FAIL_TO_PASS baseline.

## Run the benchmark

```bash
# e2b / aenv (same stress profile; pick via --provider)
bench-core --provider e2b  --config config/common/coding-java.yaml --create-only
bench-core --provider aenv --config config/common/coding-java.yaml --create-only

# docker: needs a reachable daemon (DOCKER_HOST for a remote/WSL daemon)
bench-core --provider docker --config config/common/coding-java.yaml -n 1

# Validate config/profile/runner without any SDK
bench-core --provider fake --config config/common/coding-java.yaml --create-only -n 1
```

Local (WSL2) docker preview: the image is already built; start the daemon in its
own terminal and point the runner at it:

```powershell
wsl -d CentOS7 -u root -- dockerd -H unix:///var/run/docker.sock -H tcp://0.0.0.0:2375 --tls=false --userland-proxy-path=/usr/bin/docker-proxy
$env:DOCKER_HOST = "tcp://localhost:2375"
bench-core --provider docker --config config/common/coding-java.yaml -n 1
```

E2B/aenv run the same image through `push_to_harbor.sh` + `build_e2b.py`; the
template alias is `openclaw-coding-java-v1` in both blocks of the config.

## Step order

`CODING_STEP_ORDER = [find, read, edit, verify, diff]` — shared with the other
variants. Metrics/reporting are language-agnostic: they key on step names, so
the Java `verify` peak flows through the same `Coding_verify_*` columns.

## Credibility argument

The edit pairs are real source state at the instance's base commit, and the
verify is the project's own JUnit harness asserting the dataset's
FAIL_TO_PASS/PASS_TO_PASS methods — not a synthetic probe. Nothing is added "for
memory" that a real coding agent wouldn't do: no packaging step, no full suite,
no resident process.
