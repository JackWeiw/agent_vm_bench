# E2B Coding Benchmark (C++) — nlohmann/json

E2B sandbox coding benchmark for AI coding-agent memory stress testing, **C++
language** variant. The per-round workflow is the shared coding loop
(`find → read → edit → verify → git diff`), but like the Java and C variants the
verify step runs the project's **real test suite** — here the CMake-built
doctest target `test-udt_cpp11` — and asserts the instance's
**FAIL_TO_PASS / PASS_TO_PASS** cases from its XML report.

## Trace-faithful loop

```
find → read → edit → verify (cmake build test-udt_cpp11 + doctest -r=xml) → git diff
```

| Step | What the sandbox actually runs |
|------|--------------------------------|
| find  | `git checkout -- include/ single_include/` (reset) + locate the target file |
| read  | `head -20` of the target file |
| edit  | literal `find → replace` applied with `python3`; the gold pair carries a 2-entry `edits` list, each entry naming its own `file` (pairs are data, see below) |
| verify | `cmake -S . -B build && cmake --build build -j$(nproc) --target test-udt_cpp11 && ./build/tests/test-udt_cpp11 -s -r=xml`, then a `python3` pass asserts every listed `test case > section` path ran and has no failing assertion |
| diff  | `git diff > /tmp/bench_round_N.patch` |

**NOT in the loop** (and therefore not in the bench):

- ❌ a clean rebuild / `rm -rf build` — the image ships a warm CMake build; verify
  is the incremental rebuild a real agent triggers after editing a header.
- ❌ the full doctest suite (all targets) — only `test-udt_cpp11`, the instance's
  own target.
- ❌ any resident process — verification is transient (compile + run + exit).
- ❌ a packaging/install step — a header-only library is consumed, not installed.

### Verify = FTP + PTP, not a hand-written probe

The original ts/go variants only had a FAIL_TO_PASS assertion (a self-written
temp program). A PTP pass is what actually shows a coding agent's real resource
profile: the agent must not only make the new test pass, it must keep the
existing suite green, so the run has to execute the real harness.

Per pair, the payload declares:

```python
"fail_to_pass": ("basic usage > conversion to json via free-functions",),
"pass_to_pass": (... 21 doctest case/section paths from tests/src/unit-udt.cpp ...),
```

The runner (`src/bench_core/task_runner/coding.py::_run_test_suite_verify`):

1. collects `fail_to_pass + pass_to_pass`, returns `None` when both are empty
   (generic edit pairs then fall back to `coding.verify_cmd` — the g++ probe);
2. runs the profile's `test_suite_cmd`, which writes the doctest XML report to
   `/tmp/bench_doctest.xml` (build noise stays in the outer log);
3. parses the report with `python3`: names are normalized to
   `<TestCase> > <innermost SubCase>` (the upstream eval's convention — nested
   sections are reported by their innermost name, so
   `adl_serializer specialization > to_json` matches both the partial and the
   total specialization, and `basic usage > via explicit calls to get` matches
   the section nested under `conversion from json via free-functions`). A
   listed path that is missing, skipped, or carries an `Expression
   success="false"` fails verify. The binary's own exit code is preserved, so a
   compile error or crash also fails. Success prints `verified N tests`.

`test_report_format="doctest_xml"` is what selects this branch; the C/Tcl path
(`[ok]: <name>` line parsing) is unchanged.

## Replacement pairs

Canonical content lives in `src/bench_core/payload/coding_payload.py`
(`DEFAULT_CODING_CPP_SOURCE_FILES`), so `config/common/coding-cpp.yaml` carries
no `source_files`.

- **pair[0]** is a generic comment edit (`include/nlohmann/json.hpp`) — the
  warmup runs `source_files[0]` **before** any edit exists, so a FAIL_TO_PASS
  pair must never be first (it would fail on the base commit).
- **pair[1]** mirrors the instance's gold patch: select
  `value_t::number_unsigned` for enums whose underlying type is unsigned. The
  upstream commit applies the identical two-line change to the multi-header
  source (`include/nlohmann/detail/conversions/to_json.hpp`) **and** its
  generated single-header mirror (`single_include/nlohmann/json.hpp`), so the
  pair's `edits` entries each carry a `file`. It carries the instance's FTP/PTP
  selection.
- pairs 2-5 add `// bench` to declarations in
  `detail/{value_t,input/parser,output/serializer,input/lexer}.hpp`, so a long
  round-robin keeps editing real template headers without re-running the same
  patch.

A pair may use either a single `find`/`replace` or an `edits` list of
`{file?, find, replace}` hunks (`_build_multi_edit_command`); hunks are buffered
per file and written only after every hunk matched — a missing hunk exits 2, so
a partial patch can never masquerade as the gold fix.

## Memory pressure model

```
Time → (per sandbox)
Warmup:    ████  cmake target check + g++ probe
Round:     find read edit verify(peak) diff
                             ↑
                    cmake --build --target test-udt_cpp11 (template-heavy g++)
                    + doctest run
                    × N concurrent sandboxes, staggered → host overcommit
```

- **Per-sandbox peak**: recompiling `unit-udt.cpp` against the edited header tree
  (the template instantiation graph fans out through
  `json.hpp → binary_reader/parser/lexer/serializer`), then a sub-second doctest
  run. Transient (seconds), then released.
- **Host pressure**: N overlapping verify peaks (spread by `coding_interval_*` /
  round stagger) → host memory overcommit, measured by `vm_monitor` /
  `smap_tool`.
- **No fabricated resident baseline** — no daemon, no watcher.

## Project & image

- **Repo**: `github.com/nlohmann/json` at base_commit
  `3780b41dd070436f3f55327b0a88f27a52e2dfa8` (`nlohmann__json-4237`).
- **Toolchain**: `build-essential` (g++ + make) + `cmake` + `python3` from
  Ubuntu 24.04. The build asserts g++/cmake/make exist, `python3` is 3.12, and
  the warm test target is executable.
- **Warm build**: the image runs `cmake -S . -B build` and builds
  `test-udt_cpp11` at build time. The runtime verify is then an incremental
  rebuild — the same work a real agent triggers after editing a header.
  `JSON_MultipleHeaders` defaults to **ON** at the top level, so the test target
  compiles the `include/` tree (the gold patch changes it, and the
  `single_include/` mirror, as upstream does).
- **Test patch baked in**: `tests/nlohmann-json-4237-test.patch` is the
  dataset's `test_patch`, applied at build time. It lives under `tests/`, so the
  `find` step's `git checkout -- include/ single_include/` never reverts it.
  The build greps for the new case so a silently-unapplied patch fails the
  image. Note: as in the C variant, the baked patch is uncommitted, so the
  round's `git diff` artifact also includes it.
- **`python3` is required at runtime**: the edit step inlines a
  `python3 - <<'PYEOF'` literal replace and the verify step parses the doctest
  XML with `python3`. `ubuntu:24.04` ships none, so it is installed explicitly
  (`python3=3.12.3-0ubuntu2.1`, overridable via `ARG PYTHON_VER`).
- **Proxy**: corporate TLS MITM → `git config http.sslVerify=false`. Build with
  `--build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...`.
- Image tag used by `config/common/coding-cpp.yaml`:
  `ubuntu-openclaw-coding-cpp:24.04-x86_64`.

Base-commit behaviour (verified locally): the binary exits 1 and the F2P section
has three failing assertions —
`json(large_id) > 0u`, `to_string(json(large_id)) == "9223372036854775808"`,
`json(large_id).is_number_unsigned()` (all print a negative signed value). All
21 PTP paths are present and green; after the gold pair the whole target exits
0 and `verified 22 tests` is printed.

## Language extensibility

C++ is a value of `coding.language` (`"cpp"`), **not** a new `workflow_type`.
Only the verify mechanics are language data, captured in
`CodingLanguageProfile` (`src/bench_core/payload/coding_payload.py`):

| Profile field | ts (vuejs/core) | go (hugo) | java (gson) | c (redis) | cpp (nlohmann/json) |
|---------------|-----------------|-----------|-------------|-----------|---------------------|
| `temp_test_path` | `/tmp/bench_verify.mjs` | `/tmp/bench_verify.go` | `/tmp/BenchVerify.java` | `/tmp/bench_verify.c` | `/tmp/bench_verify.cpp` |
| `heredoc_eof` | `EOF` | `GOEOF` | `JAVAEOF` | `CEOF` | `CPPEOF` |
| `run_cmd` | `npx tsx …` | `go run …` | `mvn -o … compile && javac … && java …` | `make -j… && gcc … && /tmp/bench_verify` | `cmake --build … --target test-udt_cpp11 && g++ … && /tmp/bench_verify` |
| `source_find_names` | `*.ts *.tsx *.js` | `*.go` | `*.java` | `*.c` | `*.hpp *.cpp` |
| `checkout_paths` | `packages/` | `markup/` | `gson/src/main/java/` | `src/` | `include/ single_include/` |
| `project_marker` | `package.json` | `go.mod` | `pom.xml` | `Makefile` | `CMakeLists.txt` |
| `test_module` | _(empty → probe)_ | _(empty)_ | `gson` (Maven `-pl`) | _(empty)_ | _(empty)_ |
| `test_suite_cmd` | — | — | — | `make -j… && TERM=dumb ./runtest …` | `cmake … && cmake --build … --target test-udt_cpp11 && ./build/tests/test-udt_cpp11 -s -r=xml > …` |
| `test_report_format` | — | — | — | _(empty → Tcl `[ok]`)_ | `doctest_xml` |
| `test_args` | — | — | `-Djava.version=8` | — | — |

`test_module` (Maven) or `test_suite_cmd` (any other harness) is what switches
verify from the ad-hoc probe to the real suite; `test_report_format` picks how
that suite's result is parsed (empty = Tcl `[ok]` lines, `doctest_xml` = doctest
XML report). An empty `test_suite_cmd` keeps the old probe behaviour.

## Build & push

```bash
cd dockerfile_build/coding/cpp

# x86_64 — this exact tag is what coding-cpp.yaml's docker.image uses, what
# push_to_harbor.sh looks for (BASE_IMAGE), and what the offline tarball
# (`docker load`) restores. One tag, three consumers — no retagging.
docker build -t ubuntu-openclaw-coding-cpp:24.04-x86_64 -f Dockerfile.x86 .

# ARM64
docker build -t ubuntu-openclaw-coding-cpp:24.04-linuxarm64 -f Dockerfile .
```

`apt` and the nlohmann/json clone go through the corp proxy, so pass it
explicitly:

```bash
docker build -t ubuntu-openclaw-coding-cpp:24.04-x86_64 -f Dockerfile.x86 \
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
    --alias openclaw-coding-cpp-v1 \
    --image e2b-orchestration/ubuntu-coding-cpp-bench:custom \
    --cpu 2 --memory 4096
```

## Manual sandbox testing

```bash
# Inside a running sandbox (or via sbx.commands.run from the runner):
bash /opt/coding-bench/bench_helper.sh 0        # generic edit + g++ smoke verify
bash /opt/coding-bench/bench_helper.sh 1        # 2-file gold patch + real doctest suite
bash /opt/coding-bench/bench_helper.sh --no-verify
```

Round 1 echoes `verified 22 tests` / `Verify: SUCCESS (<n>ms)` and writes
`/tmp/bench_round_1.patch`. On the unmodified base commit the same section fails
(`json(large_id) > 0u` expands to `-9223372036854775808 > 0`) — that is the
FAIL_TO_PASS baseline. The helper reads `bench_pairs.json`, generated from
`DEFAULT_CODING_CPP_SOURCE_FILES`, so it never drifts from the Python runner.

## Run the benchmark

```bash
# e2b / aenv (same stress profile; pick via --provider)
bench-core --provider e2b  --config config/common/coding-cpp.yaml --create-only
bench-core --provider aenv --config config/common/coding-cpp.yaml --create-only

# docker: needs a reachable daemon (DOCKER_HOST for a remote/WSL daemon)
bench-core --provider docker --config config/common/coding-cpp.yaml -n 1

# Validate config/profile/runner without any SDK
bench-core --provider fake --config config/common/coding-cpp.yaml --create-only -n 1
```

Local (WSL2) docker preview: the image is already built; start the daemon in its
own terminal and point the runner at it:

```powershell
wsl -d CentOS7 -u root -- dockerd -H unix:///var/run/docker.sock -H tcp://0.0.0.0:2375 --tls=false --userland-proxy-path=/usr/bin/docker-proxy
$env:DOCKER_HOST = "tcp://localhost:2375"
$env:PYTHONPATH = "src"
python -m bench_core.bench --provider docker --config config/common/coding-cpp.yaml -n 1
```

E2B/aenv run the same image through `push_to_harbor.sh` + `build_e2b.py`; the
template alias is `openclaw-coding-cpp-v1` in both blocks of the config.

## Step order

`CODING_STEP_ORDER = [find, read, edit, verify, diff]` — shared with the other
variants. Metrics/reporting are language-agnostic: they key on step names, so
the C++ `verify` peak flows through the same `Coding_verify_*` columns.

## Credibility argument

The edit pairs are real source state at the instance's base commit, and the
verify is the project's own CMake+doctest target asserting the dataset's
FAIL_TO_PASS/PASS_TO_PASS tests — not a synthetic probe. Nothing is added "for
memory" that a real coding agent wouldn't do: no clean rebuild, no full suite,
no resident process.
