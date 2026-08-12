# Contributing to Agent VM Bench

Thanks for contributing! This is a short guide to getting set up and opening a PR.

## Development setup

```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install
```

## Before you commit

```bash
pre-commit run --all-files
```

All commits must pass pre-commit (ruff / black / hooks). CI runs it on every PR.

## Running tests

Tests are per package:

```bash
pytest e2b_bench/tests -v --tb=short
pytest vm_bench/tests -v --tb=short
pytest docker_bench/tests -v --tb=short
pytest llm_replay/tests -v --tb=short
```

## Branching & pull requests

1. Branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Keep PRs focused — one concern per PR.
3. Follow the [PR template](.github/pull_request_template.md) checklist.
4. Target `main`. Squash-merge is the default.

## Commit messages

- Imperative, concise subject line (≤72 chars).
- English only.
- Reference issues / PRs where relevant (`Closes #123`, `Implements RFC NNNN`).

## When to write an RFC

Non-trivial design changes (new scenarios, cross-package interfaces, breaking
schema changes) need an **RFC** before code. See [docs/rfcs/README.md](docs/rfcs/README.md).
Small changes go straight to a PR.

## Reporting bugs & security

- Bugs → [issue tracker](https://github.com/JackWeiw/agent_vm_bench/issues) (use the bug template).
- Security → [private vulnerability reporting](https://github.com/JackWeiw/agent_vm_bench/security/advisories/new),
  **not** a public issue.
- Questions → [Discussions](https://github.com/JackWeiw/agent_vm_bench/discussions).

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
