# E2B Coding Benchmark (Go) — gohugoio/hugo

E2B sandbox coding benchmark for AI coding-agent memory stress testing, **Go
language** variant. The per-round workflow mirrors a **real captured openclaw
agent trajectory** on `gohugoio/hugo` (issue #12768: GitHub Alert
case-insensitivity), so the memory pressure it induces is defensible to a
technically strong reviewer.

## Trace-faithful loop

Verified against the captured openclaw trajectory
(`swe_bench/go/93a3c799-*.trajectory.jsonl`):

```
find → read → edit → verify (write /tmp/test_*.go + go run) → git diff
```

| Step | What the agent actually did |
|------|------------------------------|
| find  | `find / -path "*/markup/goldmark/blockquotes/blockquotes.go" \| head -5` |
| read  | inspected the alert regex before editing |
| edit  | added `(?i)` to `gitHubAlertRe` in `blockquotes.go` |
| verify | **one command**: `cat > /tmp/test_alert.go << 'GOEOF' … GOEOF` + `go run /tmp/test_alert.go` — a standalone `package main` with a hardcoded input/expected-output table |
| diff  | `git diff -- markup/goldmark/blockquotes/blockquotes.go > /tmp/patch.txt` |

**NOT in the trace** (and therefore not in the bench):

- ❌ `go build ./...` (full module build) — the agent never runs it.
- ❌ `go test ./...` (full test suite) — the agent never runs it.
- ❌ resident dev server / watcher — never appears; verification is transient.

The Python runner (`e2b_bench/coding_task_runner.py::_run_verify`) mirrors the
trace's combined write+run as a **single** `commands.run`:
`cd <project> && cat > /tmp/bench_verify.go << 'GOEOF' <body> GOEOF\n go run /tmp/bench_verify.go`.

## Memory pressure model

```
Time → (per sandbox)
Warmup:    ████  one go run of a temp test (compiler peak transient)
Round:     find  read  edit  verify(peak)  diff
                              ↑
                       cat > /tmp/bench_verify.go + go run /tmp/bench_verify.go
                       (Go compiler loads imported package types → transient peak)
                       × N concurrent sandboxes, staggered → host overcommit
```

- **Per-sandbox peak**: `go run` compiling+executing the ad-hoc `package main`.
  Transient — seconds, then released.
- **Host pressure**: N sandboxes' verify peaks overlapping (spread by
  `coding_interval_*` / round stagger) → host memory overcommit, measured by
  `vm_monitor` / `smap_tool`.
- **No fabricated resident baseline.** Nothing is added "for memory" that the
  agent didn't actually do.

## Project & image

- **Repo**: `github.com/gohugoio/hugo` cloned at the instance base_commit
  `83235262d06a060bd22c168b3413903667b8aeb6` (`gohugoio__hugo-12768`).
- **Image**: `Dockerfile.coding-go` — ubuntu:24.04-linuxarm64 + Go ARM64
  toolchain + git. The hugo module graph is intentionally **not**
  pre-downloaded. The captured agent never ran `go mod download`, and every
  verify script imports only the Go stdlib, so `go run /tmp/bench_verify.go`
  compiles against the toolchain alone — no module fetch needed at runtime.
  Pre-fetching the tree would be non-faithful and also breaks behind this
  corp network's self-signed-cert proxy (the hugo deps pull
  `cloud.google.com/go` etc., whose TLS the MITM breaks).
- If a future pair imports a hugo package, that pair's verify step is where
  its module needs are handled — not the image build.

## Language extensibility

Go is a value of `coding.language` (`"go"`), **not** a new `workflow_type`. The
coding workflow loop (`find → read → edit → verify → diff`) is shared across
languages; only the verify mechanics differ, captured as data in a
`CodingLanguageProfile` registry (`e2b_bench/config.py`). Adding C++ later =
one registry entry + its default verify script — no runner code changes.

| Profile field | js (vuejs/core) | go (hugo) |
|---------------|-----------------|-----------|
| `temp_test_path` | `/tmp/bench_verify.mjs` | `/tmp/bench_verify.go` |
| `heredoc_eof` | `EOF` | `GOEOF` |
| `run_cmd` | `npx tsx /tmp/bench_verify.mjs` | `go run /tmp/bench_verify.go` |
| `source_find_names` | `*.ts *.tsx *.js` | `*.go` |
| `checkout_paths` | `packages/ src/` | `markup/` |

## Build & push

```bash
cd dockerfile_build

# Build the Go coding image
docker build -t ubuntu-coding-go-bench:24.04-linuxarm64 -f Dockerfile.coding-go .

# Push to Harbor + install E2B-required system packages
HARBOR_IP=X bash push_to_harbor_coding_go.sh

# Build the E2B template (alias is free-form; no code registration needed)
python3 build_e2b.py --server-ip X --harbor-ip X \
    --alias openclaw-coding-go-v1 \
    --image e2b-orchestration/ubuntu-coding-go-bench:custom \
    --cpu 2 --memory 4096
```

## Manual sandbox testing

```bash
# Inside a running sandbox (or via sbx.commands.run from the runner):
bash /opt/coding-bench/bench_helper_go.sh           # Round 0, all steps
bash /opt/coding-bench/bench_helper_go.sh 3         # Round 3
bash /opt/coding-bench/bench_helper_go.sh --no-verify   # edit+diff only
```

The helper writes `/tmp/bench_verify.go`, runs `go run /tmp/bench_verify.go`,
captures timing, and produces `/tmp/bench_round_N.patch`.

## Run the benchmark

```bash
# Create sandboxes
python -m e2b_bench -c config/e2b_coding_go_bench.yaml --create-only

# Run (round-robin recommended)
python -m e2b_bench -c config/e2b_coding_go_bench.yaml --detect -bm round_robin

# Delete sandboxes
cd e2b_bench/scripts && bash delete_sandbox.sh
```

## Step order

`CODING_STEP_ORDER = [find, read, edit, verify, diff]` — shared with the JS
variant. Metrics/reporting (`metrics_extractor`, `stats_collector`,
`report_aggregator`) are language-agnostic: they key on step names, so the Go
`verify` step flows through the same `Coding_verify_*` metric columns as JS.

## Credibility argument

Every step in the loop appears verbatim in a captured openclaw gohugoio/hugo
trajectory. `go run` of a self-written temp `package main` is the exact
verification the agent used. No production build, no full test suite, no resident
server — nothing is added "for memory" that isn't in the real trace.
