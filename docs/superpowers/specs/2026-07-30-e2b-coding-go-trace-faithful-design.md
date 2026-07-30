# E2B Coding Benchmark — Go (gohugoio/hugo) Trace-Faithful Design

**Date:** 2026-07-30
**PR:** #58 (`feat: add E2B coding benchmark for AI agent memory stress testing`)
**Branch:** `coding`
**Companion to:** `2026-07-30-e2b-coding-trace-faithful-design.md` (JS / vuejs/core). This is the Go variant — same loop shape, Go toolchain and `gohugoio/hugo` repo.

## Goal

Add a Go coding benchmark whose per-round workflow is identical in shape to a **real captured openclaw agent trajectory** on `gohugoio/hugo`, so the memory pressure it induces is defensible to a technically strong customer reviewing "is this what an agent actually does?"

## Ground truth — the real hugo agent trajectory

A real openclaw agent run solving `gohugoio__hugo-12768` (GitHub Alert heading case-insensitivity: `[!NOTE]` should match `[!note]`) was captured at:

```
C:\Users\jack\Desktop\harness\swe_bench\go\93a3c799-*.trajectory.jsonl
```

The instance's gold patch edits one file:

```
markup/goldmark/blockquotes/blockquotes.go
- var gitHubAlertRe = regexp.MustCompile(`^<p>\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]`)
+ var gitHubAlertRe = regexp.MustCompile(`(?i)^<p>\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]`)
```

Extracted agent actions, in order:

| Phase | What the agent actually did |
|-------|------------------------------|
| find  | `find / -path "*/markup/goldmark/blockquotes/blockquotes.go" 2>/dev/null \| head -5` — locate the target file across the workspace |
| read  | inspected the file to confirm context (the agent reads the regex before editing) |
| edit  | edited `markup/goldmark/blockquotes/blockquotes.go` (adds `(?i)` to the alert regex) |
| read-confirm | re-read the edited region to confirm the change landed |
| verify | **single command** writing a temp test then running it: `cd …/hugo && cat > /tmp/test_alert.go << 'GOEOF' … GOEOF` + `go run /tmp/test_alert.go` — a standalone `package main` with a hardcoded input/expected-output table exercising the edited regex |
| diff  | `git diff -- markup/goldmark/blockquotes/blockquotes.go > /tmp/patch.txt && cat /tmp/patch.txt` |

**Key trace facts:**

- The agent writes a **standalone** `/tmp/test_alert.go` (`package main`, `import "regexp"`/`"fmt"`) — it does **not** run Hugo's own test suite, and does **not** run `go build ./...` of the whole module.
- **Write temp test file + run it is one command**: heredoc `cat > /tmp/test_alert.go << 'GOEOF' … GOEOF` immediately followed (newline-joined) by `go run /tmp/test_alert.go`. This compiles + runs the ad-hoc test in one shot (`go run` = compile + execute).
- **No resident process.** `go run` is transient — compiles to a temp binary, runs, exits. No dev server, no watcher.
- The `find` step uses `find / -path …` (workspace-wide locate), then `head -5`.

This matches the JS/vue and the sqlfluff traces in shape: explore → edit → self-written transient verification → diff. **None** run the project's full build or full test suite, and **none** keep a resident server.

## Background & Constraints

Same as the JS design. Per sandbox: 2vCPU / 4GB. CPU load low. Memory pressure from N concurrent sandboxes × transient verification peaks overlapping, observed via `vm_monitor` / `smap_tool`. No per-round `free -m`.

Go-specific note: `go run` of a `package main` does a real compile (Go compiler + linking) then executes — a genuine, transient CPU+memory peak (compilation loads the imported packages' AST/types). N sandboxes compiling+running staggered ad-hoc tests = host overcommit pressure. No fabricated resident process needed.

## Decision 1 — Workflow: find → read → edit → read-confirm → verify → git diff

Per-round step order (the trace's exact shape):

```
round:
  find         — git checkout reset + locate the target source file (find -path | head)
  read         — head/cat the file to confirm context
  edit         — apply a pre-configured find→replace pair (real semantic edit)
  read-confirm — re-read the edited region to confirm the change landed
  verify       — (a) write an ad-hoc temp test file, (b) go run it  [one command]
  diff         — git diff > patch file (agent's verification artifact)
```

`read-confirm` is folded into the `read` step timing (it is the same kind of action — the agent re-reading the file); it is listed for trace fidelity but does not need its own `CODING_STEP_ORDER` entry. So `CODING_GO_STEP_ORDER = [find, read, edit, verify, diff]`, matching the JS `CODING_STEP_ORDER` shape.

The `verify` step mirrors the trace's combined write+run:

- **(a)+(b) write temp test file + run**: a single `sbx.commands.run` executing
  `cd <project> && cat > /tmp/bench_verify.go << 'GOEOF' <body> GOEOF\n go run /tmp/bench_verify.go`.
  The `<body>` is a `package main` with a hardcoded input/expected-output table exercising the edited symbol — exactly the trace's `test_alert.go` shape.
- This single-command write+run is the trace-faithful form; splitting it into two `commands.run` calls would diverge from how the agent actually verified.

## Decision 2 — Verify step: standalone `package main` + `go run`

The `verify` step mirrors the trace exactly:

- A pre-staged ad-hoc Go test (`package main`, `import`-ing only stdlib like `regexp`/`fmt`) is written to `/tmp/bench_verify.go` via heredoc.
- It exercises the edited symbol with a hardcoded input/expected-output table (the trace's `test_alert.go` checks several alert strings against expected match/no-match).
- Executed via **`go run /tmp/bench_verify.go`** — `go run` compiles the temp file (loading imported packages' types into the compiler) and executes it. Transient.

Why `go run` of a standalone temp file (not `go test ./...` or `go build ./...`):

- The trace uses exactly `go run /tmp/test_alert.go` — a focused, self-written check, not the module's suite.
- `go test ./...` would compile+run Hugo's whole test binary set (very heavy, long) — **not** what the agent did, and not trace-faithful.
- `go build ./...` is the project's build, also not in the trace.
- `go run` of a tiny `package main` is the right fidelity/cost point: real compile+execute peak, focused on the edited behavior, seconds.

## Decision 3 — Project & image: gohugoio/hugo + Go toolchain (with CN mirrors)

- **Repo**: `github.com/gohugoio/hugo` (real `swe_bench_multilingual` instance `gohugoio__hugo-12768`, large popular Go project, ~70k+ stars).
- **Image**: a new `Dockerfile.coding-go` extending the base ubuntu:24.04-linuxarm64 with the Go toolchain (ARM64 Go tarball), git. Clone hugo at the instance `base_commit` shallowly. No Node/pnpm. `CMD ["sleep","infinity"]`.
- **CN Go proxy** (build-time, so runtime `go run` never hits the network):

  ```dockerfile
  RUN go env -w GOPROXY=https://goproxy.cn,direct && \
      go env -w GOSUMDB=sum.golang.google.cn && \
      go env -w GO111MODULE=on
  ```
- **Pre-install full module graph** at image build: `cd /opt/coding-bench && go mod download`. This pulls hugo's entire dependency graph into the module cache at build time. The verify temp file imports only stdlib so it would compile without this, but pre-downloading means `go run` from inside the project dir resolves the module root instantly and any future pair that imports hugo packages also works offline. Build cost is paid once at image build, not per sandbox.

## Decision 4 — Edit mechanism: replacement pairs (Go-flavored)

`DEFAULT_CODING_GO_SOURCE_FILES` in `schemas.py` carries verified `{file, find, replace, verify_script}` pairs against the hugo repo at its base commit. The first pair mirrors the gold patch:

```python
{
    "file": "markup/goldmark/blockquotes/blockquotes.go",
    "find": "var gitHubAlertRe = regexp.MustCompile(`^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)",
    "replace": "var gitHubAlertRe = regexp.MustCompile(`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)",
    "verify_script": "<package main body exercising case-insensitive alert matching>",
}
```

Each pair's `verify_script` is the body of the standalone `package main` (between `GOEOF` markers). **Pair-specific script when present, shared default otherwise** — a pair without `verify_script` falls back to a shared default body (compiles + runs the temp file, asserts "All tests passed!"). This keeps pairs that lack a focused test still producing a real compile+run peak.

## Decision 5 — Shared runner, language-driven verify via a language profile table (extensible)

The Go loop has the **same shape** as the JS loop (`find → read → edit → verify → diff`); only the verify commands, toolchain, and file globs differ. Because **future languages (C++, …) will be added**, the language dispatch is a **data-driven profile table**, not a chain of `if language == ...`:

- Add `coding_language: str = "js"` to `Config` (values: `js` | `go` | future `cpp` | …).
- Define a `CodingLanguageProfile` (a small dataclass / dict registry) keyed by language, holding:
  - `temp_test_path` — where the ad-hoc test is written (`/tmp/bench_verify.mjs` for js, `/tmp/bench_verify.go` for go)
  - `heredoc_eof` — heredoc terminator (`EOF` for js, `GOEOF` for go)
  - `run_cmd` — the verify run command (`npx tsx /tmp/bench_verify.mjs` for js, `go run /tmp/bench_verify.go` for go)
  - `source_glob` — the find fallback glob (`*.ts *.tsx *.js` for js, `*.go` for go)
  - `checkout_paths` — what `git checkout --` resets (`packages/ src/` for js, the relevant hugo path for go)
- The verify step reads the active profile from `coding_language` and builds the write+run command from the profile fields. **Adding C++ later = adding one profile entry + its `verify_cmd`** — no runner code changes, no new `workflow_type`.
- `CODING_STEP_ORDER` stays `[find, read, edit, verify, diff]` for all languages — step keys are language-agnostic, so `metrics_extractor.py` / `stats_collector.py` need no per-language changes.
- `DEFAULT_CODING_SOURCE_FILES` (js/vue) and `DEFAULT_CODING_GO_SOURCE_FILES` (go/hugo) are both defined; `Config` picks the default list from `coding_language` (registry maps language → default source-file list).

This avoids duplicating `CodingTaskRunner` / `CodingRoundRunner` / `CodingWarmupRunner` per language, keeps a single code path, and makes new languages a config/registry addition. The JS redesign's runner simplification (removing build/test/dev-server steps — see companion doc) lands first; Go rides the same simplified runner.

## Config changes (`config.py` + `config/e2b_coding_go_bench.yaml`)

New YAML (mirrors `e2b_coding_bench.yaml` but Go):

```yaml
workflow_type: "coding"
coding:
  language: "go"
  project_dir: "/opt/coding-bench"        # hugo clone
  verify_cmd: "go run /tmp/bench_verify.go"
  verify_timeout: 120
  source_files:
    - file: "markup/goldmark/blockquotes/blockquotes.go"
      find: "var gitHubAlertRe = regexp.MustCompile(`^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)"
      replace: "var gitHubAlertRe = regexp.MustCompile(`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)"
      verify_script: |
        package main
        import ("fmt"; "regexp")
        var re = regexp.MustCompile(`(?i)^<p>\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]`)
        func main() { /* assert matches ... */ fmt.Println("All tests passed!") }
  skip_verify: false
  interval_min: 2.0
  interval_max: 10.0
```

New `Config` field: `coding_language: str = "js"`. The JS redesign's removed fields (`coding_dev_*`, `coding_build_*`, `coding_test_*`) stay removed for both languages.

## Files to change / add

| File | Change |
|------|--------|
| `e2b_bench/coding_task_runner.py` | (After the JS redesign lands) verify step dispatches on `coding_language`: js = `npx tsx`, go = `go run`. Heredoc write of temp test file is part of verify. `find` step's fallback `find -name` glob uses `*.go` when `coding_language == "go"`. |
| `e2b_bench/schemas.py` | Add `DEFAULT_CODING_GO_SOURCE_FILES` (verified hugo pairs with `verify_script`). `CODING_STEP_ORDER` unchanged (shared). |
| `e2b_bench/config.py` | Add `coding_language: str = "js"`; default source-file list chosen by language; YAML + CLI parsing. |
| `dockerfile_build/Dockerfile.coding-go` | **New.** ubuntu:24.04-linuxarm64 + Go ARM64 toolchain + git + clone hugo at base_commit + `go mod download`. `CMD ["sleep","infinity"]`. |
| `dockerfile_build/push_to_harbor_coding_go.sh` | **New.** Mirror of `push_to_harbor_coding.sh` for the go image. |
| `dockerfile_build/bench_helper_go.sh` | **New.** Go step sequence `find → read → edit → verify (cat+go run) → diff`; hugo replacement pairs. |
| `dockerfile_build/README_CODING_GO.md` | **New.** Memory model, step sequence, trace source, build/push instructions. |
| `config/e2b_coding_go_bench.yaml` | **New.** Go single-test config (above). |
| `e2b_bench/tests/test_coding_task_runner.py` | Add Go-language verify dispatch assertions. |

## Memory pressure model (Go, trace-faithful)

```
Time → (per sandbox)
Warmup:    ████  one go run of a temp test (compiler peak transient)
Round:     find  read  edit  verify(peak)  diff
                              ↑
                       cat > /tmp/bench_verify.go + go run /tmp/bench_verify.go
                       (Go compiler loads imported package types → transient peak)
                       × N concurrent sandboxes, staggered → host overcommit
```

- **Per-sandbox peak**: `go run` compiling+executing the ad-hoc `package main`. Transient.
- **Host pressure**: N sandboxes' verify peaks overlapping (spread by `coding_interval_*` / round stagger) → host memory overcommit, measured by `vm_monitor` / `smap_tool`.
- **No fabricated resident baseline.** Same honest stance as JS; optional in-repo `go run`-style watcher is out of scope.

Customer-credibility: every step appears verbatim in a captured openclaw hugo trajectory. `go run` of a self-written temp test is the exact verification the agent used.

## Implementation order

1. **JS redesign first** (companion doc) — simplify runner to `find → read → edit → verify → diff`, remove dev-server/build/test. This is the foundation Go reuses.
2. **Go variant** — `Dockerfile.coding-go` + configs + `bench_helper_go.sh` + `DEFAULT_CODING_GO_SOURCE_FILES`; runner verify-dispatch on `coding_language`.
3. Tests + pre-commit for both.

## Out of scope

- No third workflow type (browser + coding only; Go is a `coding_language` value, not a new `workflow_type`).
- No hugo full test-suite / full `go build` step (not in trace).
- No resident dev server / watcher.
- `setup_coding_project.sh` left deprecated.
