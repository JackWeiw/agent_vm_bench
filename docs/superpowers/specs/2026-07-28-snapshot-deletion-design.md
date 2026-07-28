# Snapshot Deletion Script Design

**Date:** 2026-07-28
**Author:** AI Assistant
**Status:** Draft
**Depends on:** [2026-07-28 Batch Snapshot Scripts Design](./2026-07-28-batch-snapshot-scripts-design.md) (the `snap` package: `common.py`, `create.py`, `restore.py`)

## Problem

The `snap.create` / `snap.restore` pipeline has no cleanup for the **snapshots themselves**:

- `snap.create` creates a sandbox, snapshots it, then **kills the sandbox** — but the E2B SDK docstring states: *"Snapshots are persistent and survive sandbox deletion."*
- `snap.restore` creates sandboxes from snapshots, times them, then kills the sandboxes — again leaving the source snapshots untouched.
- `delete_sandbox.sh` (the existing cleanup script) deletes **sandboxes**, not snapshots. It is a no-op against accumulated snapshots.

Result: every `snap.create` run leaves N snapshots permanently on the E2B server. Over repeated benchmark runs these accumulate without bound, consuming storage and polluting the snapshot namespace. There is currently no way — scripted or otherwise — in this repo to delete snapshots.

The gap: a batch snapshot deletion tool that mirrors the create/restore scripts.

## Technical findings (verified against installed E2B SDK)

1. **`Sandbox.delete_snapshot(snapshot_id)` is a `@staticmethod`** — needs only the `snapshot_id` plus the standard E2B env vars (`E2B_API_URL`, `E2B_API_KEY`, `E2B_ACCESS_TOKEN`, already set by `common.load_env`). No active sandbox connection required. Returns `True` if deleted, `False` if not found (idempotent — deleting a missing snapshot is not an error).

2. **Global snapshot listing is available via the SDK.** `Sandbox._cls_list_snapshots(sandbox_id=None, ...)` returns a `SnapshotPaginator` that, when `sandbox_id` is omitted, calls the `get_snapshots` API with `sandbox_id=UNSET` and lists **all snapshots on the server** (paginated). The public wrapper `Sandbox.list_snapshots()` is an instance method that pins to one sandbox; for the `--all` path we use the static `_cls_list_snapshots` form directly (or replicate the paginator construction), since the source sandboxes are long gone.

   Both `create_snapshot` and `delete_snapshot` are class-level/static, so we can drive the whole flow without ever instantiating a `Sandbox` — deletion is a pure API operation once `load_env` has set the environment.

3. **No new dependencies.** Everything reuses `e2b`, `openpyxl`, `concurrent.futures`, `json` — all already in the project.

## Decision: New `snap.delete` module (Approach A)

Add a fourth module `e2b_bench/snap/delete.py`, invoked as `python -m e2b_bench.snap.delete`. It mirrors the create/restore split exactly: one command, one responsibility, shared `common.py` utilities.

### Why not the alternatives

- **Flag on `restore.py` (Approach B):** Conflates two distinct lifecycle phases — restore *creates* sandboxes, delete *removes* snapshots. A single `--delete-snapshots` flag would couple cleanup to a script that may be run zero-to-many times against the same snapshots (you can restore from a snapshot set repeatedly before deleting it). Awkward semantics, harder to script.
- **Both module + restore flag (Approach C):** Doubles the implementation surface for marginal convenience. The standalone module already composes cleanly (`restore -k && delete -i ...`), so the extra flag buys little. Defer until a clear ergonomic need appears.

## Behavior

Two input modes (mutually exclusive), chosen by which flags are passed:

### Mode 1: `--input-json` (default) — delete known snapshots

- Load `snapshots.json` (the file `snap.create` produced).
- Collect every `snapshot_id` with status `success` (skip entries already marked `deleted`).
- Call `Sandbox.delete_snapshot(id)` for each (concurrent or batched, same `--batch-size` / `--batch-interval` knobs as create/restore).
- **Update `snapshots.json` in place:** set each processed entry's `status` to `deleted` (or `delete_failed` on failure), record `deleted_at`. This turns the JSON into an accurate ledger — re-running `delete --input-json` cleanly skips already-deleted entries.

### Mode 2: `--all` — delete every snapshot on the server

- List all snapshots via `_cls_list_snapshots(sandbox_id=None)`, paginating through `SnapshotPaginator` until `has_next` is false.
- Collect every `snapshot_id`.
- Delete each.

Because `--all` can wipe snapshots belonging to other runs/users on a shared server, it is **gated**: print the count and the full list of snapshot IDs (or the first 10 + `... and N more` when N > 10), then prompt `Proceed? (y/N)`. `--yes` bypasses the prompt for scripting. The same gate applies to `--input-json` when the entry count is non-trivial.

## CLI

```
python3 -m e2b_bench.snap.delete [OPTIONS]
```

| Short | Long | Default | Description |
|-------|------|---------|-------------|
| `-e` | `--env-file` | `e2b_bench/scripts/.env` | Path to .env file |
| | `--config` | `~/.e2b/config.json` | Path to E2B config JSON |
| `-i` | `--input-json` | `snapshots.json` | Delete snapshots listed in this JSON (default mode) |
| | `--all` | False | List and delete **all** snapshots on the server (overrides `--input-json`) |
| | `--yes` / `-y` | False | Skip confirmation prompt (for scripting) |
| `-n` | `--count` | None (all) | Limit: delete only the first N matching snapshots (applies to both modes) |
| `-bs` | `--batch-size` | None (full concurrent) | Deletions per batch |
| | `--batch-interval` | `3` | Seconds between batches |
| | `--output-xlsx` | auto | Excel report path (default: `results/snap/snap_delete_<timestamp>.xlsx`) |
| | `--timeout` | `86400` | API call timeout (seconds) |
| | `--api-key` | None | Override E2B API key |
| | `--access-token` | None | Override E2B access token |

**Mode selection rule:** `--all` wins if present; otherwise `--input-json` (default path `snapshots.json`).

### Examples

```bash
# Delete the snapshots recorded in snapshots.json
python3 -m e2b_bench.snap.delete -i snapshots.json

# Delete all snapshots on the server (will prompt)
python3 -m e2b_bench.snap.delete --all

# Delete all, no prompt (scripting)
python3 -m e2b_bench.snap.delete --all --yes

# Delete only the first 3 from the JSON
python3 -m e2b_bench.snap.delete -i snapshots.json -n 3
```

## Flow

1. Parse CLI args. Resolve mode: `--all` or `--input-json` (default).
2. `load_env` → set E2B environment.
3. **Build the target list:**
   - `--input-json`: load JSON, filter to entries with `snapshot_id` and `status != deleted`, apply `--count` if given.
   - `--all`: paginate `_cls_list_snapshots(sandbox_id=None)`, collect IDs, apply `--count` if given.
4. **Safety gate:** print `N snapshots will be deleted` (+ sample of IDs if N is large). If N == 0, print "nothing to delete" and exit 0. If `--yes` unset, prompt `Proceed? (y/N)`; abort on anything but `y`.
5. **Delete** concurrently (or in batches), one result dict per snapshot: `{index, snapshot_id, status, error, delete_elapsed_s}`.
   - `status`: `success` (delete_snapshot returned True), `not_found` (returned False — treat as success/no-op in stats), `failed` (exception).
6. **Update `snapshots.json`** (only in `--input-json` mode): set each processed entry's `status` to `deleted` / `delete_failed` and `deleted_at` timestamp; write back.
7. **Excel report** via `common.write_excel_report`: Raw sheet (per-snapshot results), Summary sheet (`delete_snapshot_s` stats + success_rate), Snapshots sheet (snapshot_id, status, deleted_at). Same 3-sheet shape as create/restore.
8. Print final summary: `X deleted, Y not found, Z failed`.

**Signal handling:** No sandbox handles to clean up (deletion is stateless API calls), so no `_created_sandboxes` list is needed. `Ctrl+C` simply stops in-flight batches; deleted-so-far results are still written to JSON + Excel. Keep the signal handler for consistent UX (flush partial results).

## JSON ledger semantics (`--input-json` mode)

The `snapshots.json` schema gains two optional fields per entry, written only by `snap.delete`:

```json
{
  "snapshot_id": "snap_abc123",
  "sandbox_id": "sbx_def456",
  "create_elapsed_s": 12.5,
  "snapshot_elapsed_s": 3.2,
  "total_elapsed_s": 15.7,
  "status": "deleted",
  "deleted_at": "2026-07-28T11:45:00"
}
```

- `snap.create` still writes `status: "success"` — unchanged.
- `snap.delete` flips `status` to `deleted` (or `delete_failed`) and sets `deleted_at`.
- `snap.delete` skips entries where `status == "deleted"` on input — so the file is safely re-runnable.
- `snap.restore` ignores the `deleted`/`deleted_at` fields (it reads only `snapshot_id`); deleting snapshots obviously invalidates them for restore, but that's the user's intent.

This makes `snapshots.json` a faithful ledger of the snapshot lifecycle: created → (optionally restored-from N times) → deleted.

## Error handling

- `delete_snapshot` returning `False` (not found) → status `not_found`, **not** an error. Counts as success in success_rate. Logged distinctly so an operator can spot "I tried to delete something that's already gone."
- Exception during delete → `failed`, recorded with error string, continue with next.
- JSON file missing (in `--input-json` mode) → FileNotFoundError, clear message, exit 1. (Same as `restore.py`.)
- `--all` listing fails (API/auth error) → print error, exit 1, delete nothing.
- Partial failure → still write JSON update + Excel with per-entry results.
- Empty target list → exit 0 with "nothing to delete" (do not create an empty Excel by default; this is a no-op, not a run).

## Reporting

Reuse `common.write_excel_report` and `common.compute_stats`.

| Sheet | Content |
|-------|---------|
| **Raw** | `index, snapshot_id, status, delete_elapsed_s, error` |
| **Summary** | `delete_snapshot_s` column with stats: count, success_rate (success+not_found over total), avg, min, max, p50, p90, p99, std |
| **Snapshots** | `snapshot_id, status, deleted_at` |

Note: this is the first `snap.*` script whose Raw sheet is short on timing columns — `delete_snapshot` is fast and the primary signal is success/fail counts, not latency. Keep the shape for consistency, but `delete_elapsed_s` is secondary.

## Module structure

```
e2b_bench/snap/
├── __init__.py    # unchanged (add delete to module docstring)
├── common.py      # unchanged (reuse load_env, compute_stats, write_excel_report)
├── create.py      # unchanged
├── restore.py     # unchanged
└── delete.py      # NEW — batch snapshot deletion
```

`delete.py` imports `from .common import load_env, compute_stats, write_excel_report`, matching the relative-import pattern of `create.py`/`restore.py`. No changes to `common.py` for v1.

## Out of scope (YAGNI)

- Bulk delete API (if E2B offers one) — not investigated; per-snapshot concurrent delete is fine for benchmark-scale N (tens to low hundreds).
- Age/templated filtering for `--all` — defer until a naming/tagging convention is enforced on creation.
- Deleting sandboxes (that's `delete_sandbox.sh`'s job, separate concern).
- Retry/backoff on rate limits — `delete_snapshot` failures are recorded; operator re-runs. Add only if observed in practice.

## Implementation note: global snapshot listing

The public `Sandbox.list_snapshots()` is an instance method pinned to one sandbox, but we need the *global* listing. The SDK exposes this via `Sandbox._cls_list_snapshots(sandbox_id=None, ...)` (a staticmethod that returns a `SnapshotPaginator`), which in turn calls the `get_snapshots` API with `sandbox_id=UNSET`. The `_cls_` prefix marks it as semi-internal.

**Decision:** Use `Sandbox._cls_list_snapshots(sandbox_id=None)` directly. It is the documented entry point the public wrapper delegates to, it was verified working in exploration, and it avoids re-implementing paginator construction. If a future SDK version removes it, fall back to constructing `SnapshotPaginator(sandbox_id=None)` — the implementation plan should include a one-line guard comment noting this fallback.
