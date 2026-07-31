# Config & Dockerfile Build Reorganization — Design

**Date:** 2026-07-31
**Status:** Approved
**Related:** PR #58 (E2B coding agent scenario), which added the second E2B scenario
(coding, with TS and Go variants) alongside the existing browser/openstack scenarios
and made `config/` and `dockerfile_build/` feel cluttered.

## Problem

After PR #58, both directories hold a flat mix of unrelated scenarios with inconsistent
naming. `config/` has 10 YAML files mixing OpenStack, E2B (browser + coding variants),
Docker, and tool configs. `dockerfile_build/` has 18 files mixing browser images, shared
build scripts, and the new coding/coding-go variants, using ad-hoc `.coding` /
`_coding_go` suffixes to disambiguate.

## Goal

Reorganize `config/` and `dockerfile_build/` by scenario so related files are grouped,
naming is consistent and non-redundant, and all references in code, tests, scripts, and
docs are updated. No behavior change to running benchmarks — paths move, nothing else.

## Non-Goals

- Changing config *schemas* or the `Config` loader.
- Restructuring the `e2b_bench/` Python package or any source beyond path defaults.
- Touching historical records under `docs/superpowers/specs/`, `docs/superpowers/plans/`,
  or `results/` (frozen at write time).
- Unrelated refactoring.

## Target Layout

### `config/` — grouped by scenario; `e2b/` de-prefixed

```
config/
├── openstack/
│   ├── batch_config.yaml          # was config/batch_config.yaml
│   ├── test_config_template.yaml
│   └── vm_bench.yaml
├── e2b/                           # de-prefixed: drop redundant "e2b_"
│   ├── bench.yaml                 # was e2b_bench.yaml (browser)
│   ├── batch_matrix.yaml          # was e2b_batch_matrix.yaml
│   ├── batch_template.yaml        # was e2b_batch_template.yaml
│   ├── coding_bench.yaml          # was e2b_coding_bench.yaml (TS)
│   └── coding_go_bench.yaml       # was e2b_coding_go_bench.yaml (Go)
├── docker/
│   └── docker_bench.yaml
└── tools/
    └── getfre_config.yaml         # tool config, not a scenario
```

### `dockerfile_build/` — `browser/` + `coding/{ts,go}/`

```
dockerfile_build/
├── browser/
│   ├── Dockerfile                 # arm64 default (was Dockerfile)
│   ├── Dockerfile.x86
│   ├── README.md
│   ├── openclaw.json
│   ├── llama_openclaw.conf
│   ├── build_e2b.py               # shared E2B template builder
│   └── push_to_harbor.sh
├── coding/
│   ├── setup_coding_project.sh    # deprecated; kept at coding/ level
│   ├── ts/
│   │   ├── Dockerfile             # was Dockerfile.coding
│   │   ├── README.md              # was README_CODING.md
│   │   ├── bench_helper.sh        # was bench_helper.sh
│   │   └── push_to_harbor.sh      # was push_to_harbor_coding.sh
│   └── go/
│       ├── Dockerfile             # was Dockerfile.coding-go
│       ├── README.md              # was README_CODING_GO.md
│       ├── bench_helper.sh        # was bench_helper_go.sh
│       └── push_to_harbor.sh      # was push_to_harbor_coding_go.sh
```

### Naming rationale

- **`config/e2b/` de-prefixed:** the directory already says "e2b", so `e2b_bench.yaml`
  inside it is redundant. De-prefixed paths read cleanly
  (e.g. `config/e2b/coding_bench.yaml`).
- **`dockerfile_build/coding/{ts,go}/` generic names:** each variant carries an identical
  set (Dockerfile, README, bench_helper, push_to_harbor). Using Docker-conventional generic
  names within each subdir removes the `.coding` / `_coding_go` suffix spaghetti. Splitting
  into `ts/` + `go/` is warranted because each set is 4 files (8 total) with no cross-set
  sharing.
- **`build_e2b.py` stays in `browser/`:** it is the shared E2B template builder invoked via
  CLI args (no internal path coupling); it originates from and is documented alongside the
  browser flow.

## Reference-Update Plan

### 1. Code defaults (1 spot)
- `e2b_bench/batch_scheduler.py:326` — default `template_path`
  `"config/e2b_batch_template.yaml"` → `"config/e2b/batch_template.yaml"`.

No other hardcoded config-path defaults exist (e2b_bench/docker_bench take `--config`
explicitly; `batch_test_scheduler.py` default `config/batch_config.yaml` moves to
`config/openstack/batch_config.yaml`).

### 2. Tests (5 spots)
- `e2b_bench/tests/test_coding_task_runner.py` lines 139, 148, 156, 167.
- `e2b_bench/tests/test_config.py` line 460.

### 3. Scripts — internal cross-references
- `coding/ts/push_to_harbor.sh` and `coding/go/push_to_harbor.sh`: the
  `docker build -f Dockerfile.coding` / `Dockerfile.coding-go` calls become `-f Dockerfile`,
  resolved relative to the script's own directory (`$(dirname "$0")`).
- `bench_helper.sh` scripts: verify PROJECT_DIR / path resolution uses
  `$(dirname "$0")` so location is independent.
- `build_e2b.py`: CLI-arg-driven, no internal path changes; only doc/help strings if they
  mention paths.

### 4. Docs (bulk)
- `CLAUDE.md` — `config/` tree (~line 266) and every `config/e2b_*.yaml` mention in the E2B
  workflow section.
- `README.md` — build commands (`docker build -f Dockerfile.x86`, `push_to_harbor.sh`,
  `build_e2b.py`) and the config file table (~lines 237-238).
- `docs/e2b-bench-usage.md`, `docs/e2b-bench-usage-zh.md`, `docs/e2b-batch-usage.md`,
  `docs/e2b-batch-usage-en.md`, `docs/round_robin_design.md`.
- dockerfile_build READMEs themselves (`browser/README.md`, `coding/ts/README.md`,
  `coding/go/README.md`) and their internal cross-links.

### 5. Out of scope (not touched)
- `docs/superpowers/specs/*`, `docs/superpowers/plans/*` — historical design records.
- `results/` — historical results.

## Migration Mechanics

- Use `git mv` for every file (including de-prefix renames) so history follows.
- Commit in logical chunks: (a) moves, (b) code defaults + tests, (c) script self-refs,
  (d) docs. No `Co-Authored-By` trailer.
- Pre-commit runs before each commit (ruff/ruff-format, yaml/json checks, EOF/trailing
  whitespace). Config YAMLs already pass `check-yaml`.

## Verification

- Run `e2b_bench` tests (`test_config.py`, `test_coding_task_runner.py`) to confirm
  updated paths load.
- Grep for lingering `config/e2b_`, `config/batch_config`, `config/vm_bench`,
  `config/docker_bench`, `config/test_config`, `config/getfre`, `Dockerfile.coding`,
  `README_CODING`, `bench_helper_go`, `push_to_harbor_coding` references across the repo
  (excluding historical `docs/superpowers/` and `results/`).

## New Tests

Add a unit test asserting the new config paths exist and load via `Config.load_from_yaml`
for each moved e2b config (`bench.yaml`, `batch_template.yaml`, `coding_bench.yaml`,
`coding_go_bench.yaml`), guarding against future path drift. Placement:
`e2b_bench/tests/test_config_paths.py`.
