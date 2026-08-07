# bench_looper — shared in-image benchmark looper

This directory is the single source for the `bench_looper` Python package,
vendored (via `COPY _bench_looper/bench_looper /opt/bench-looper/bench_looper`)
into the openEuler ARM images built from:

- `dockerfile_build/browser_openeuler/Dockerfile`
- `dockerfile_build/coding/go/Dockerfile.openeuler`
- `dockerfile_build/coding/ts/Dockerfile.openeuler`

It moves the host-side E2B-API single-sandbox drivers
(`e2b_bench/task_runner.py` for the browser workflow,
`e2b_bench/coding_task_runner.py` for the coding workflows) into the image, so
a container runs one scenario end-to-end via a single entry point — mirroring
the document-bench in-image looper (`document-bench-pdf` / `document-bench-xlsx`).

The build context for those three ARM builds is `dockerfile_build/` (the
parent) so this shared package is reachable; the x86 Dockerfiles stay
dir-scoped and minimal (no looper).

## Layout

```
bench_looper/
├── core.py          loop control, timing, JSONL + summary.json, exit policy
├── coding_base.py   shared find->read->edit->verify->diff skeleton
├── browser.py       browser scenario plugin (agent-browser tab mode)
├── coding_go.py     coding (Go) scenario plugin (go run cold-compile verify)
├── coding_ts.py     coding (TypeScript) scenario plugin (N chained npx tsx)
├── runner.py        CLI entry point (scenario dispatch)
└── operations/      baked scenario configs (urls, source pairs, verify pool)
    ├── browser_urls.json
    ├── coding_go_pairs.json
    └── coding_ts_pairs.json
```

## Entry points

The Dockerfiles install three `/usr/local/bin` shims that exec `runner.py`
with their scenario name:

| Shim             | Scenario   | Image                                   |
|------------------|------------|-----------------------------------------|
| `browser-bench`   | browser     | openeuler-agent-browser                 |
| `coding-bench-go` | coding-go   | openeuler-coding-go-bench               |
| `coding-bench-ts` | coding-ts   | openeuler-coding-bench                  |

Default CMD is `sleep infinity` (long-running container for slicing
attachment); the entry points run one scenario end-to-end and exit.

## Results contract

```
<results_dir>/<scenario>/<run-id>/
├── iterations.jsonl   one JSON object per round (per-step ms, failure detail)
└── summary.json        counts, success rate, per-step avg/P50/P95/P99, failure histogram
```

`<results_dir>` defaults to `/opt/bench-looper/results`; override with the
`BENCH_RESULTS_DIR` env var (the run commands mount a host dir to `/results`
and set `BENCH_RESULTS_DIR=/results`).

## CLI

```
browser-bench --loops N [--duration S] [--no-warmup] [--urls ...] [--warmup-urls ...]
coding-bench-go --loops N [--skip-verify] [--verify-timeout S]
coding-bench-ts --loops N [--skip-verify] [--verify-repeat N] [--verify-timeout S]
```

`--loops` defaults to 20000 (matches the document-bench default). A single
failed iteration does not abort the run; the process exits non-zero only if
any iteration failed.
