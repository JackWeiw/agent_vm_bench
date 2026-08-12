# RFCs (Request for Comments)

An RFC proposes a **non-trivial change** to Agent VM Bench — architecture, a new
benchmark scenario, a workflow change, or anything that benefits from design
discussion before code.

Small changes (bug fixes, config tweaks, doc edits) do **not** need an RFC — just
open a PR.

## When to write an RFC

Write one when a change:

- adds or reworks a benchmark scenario, metric source, or VMM type;
- changes a cross-package interface (e.g. `vm_monitor` base, `bench.py` flow);
- breaks the `results/` output schema or a config schema;
- shifts project conventions (testing, packaging, CI).

When in doubt, open a short issue first and ask.

## Lifecycle

| Status | Meaning |
|---|---|
| **Draft** | PR open for discussion (`0000-<name>.md`) |
| **Active** | Merged and accepted; renamed to `NNNN-<name>.md` |
| **Implemented** | Shipped in code; the implementing PR references the RFC # |
| **Declined / Withdrawn / Superseded** | Rejected or replaced; reason recorded in the file |

## How to submit

1. Copy `0000-template.md` → `docs/rfcs/0000-<short-name>.md`.
2. Fill it in. Keep it concise — design over prose.
3. Open a PR. Discussion happens on the PR.
4. On merge, a maintainer renames the file to the next number
   (`NNNN-<short-name>.md`), sets status to **Active**, and adds a row to the
   index below.

## Index

| # | Title | Status | Owner |
|---|---|---|---|
| 0001 | Abstract EnvironmentProvider layer | Active | @JackWeiw |

<!--
Index format (keep sorted by number):
| 0001 | Short title | Active | @user |
-->
