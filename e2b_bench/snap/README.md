# E2B Snap — Batch Snapshot Scripts

Batch creation and restoration of E2B sandbox snapshots with performance statistics reporting.

## Quick Start

```bash
# Step 1: Create snapshots (batch create sandboxes → snapshot each → save IDs to JSON)
python3 -m e2b_bench.snap.create -t uu -n 10 -o snapshots.json

# Step 2: Restore sandboxes from snapshots
python3 -m e2b_bench.snap.restore -i snapshots.json
```

The `create` command will print the `restore` command at the end for convenience.

## Prerequisites

1. **`.env` file** — copy `e2b_bench/scripts/.env.example` to `e2b_bench/scripts/.env` and fill in values:

```ini
E2B_API_URL=http://141.61.17.186:3000
E2B_HTTP_SSL=false
E2B_DOMAIN=e2b.app
```

Note: `E2B_API_KEY` and `E2B_ACCESS_TOKEN` are read from `~/.e2b/config.json` automatically. You don't need to set them in `.env` unless you want to override.

2. **`~/.e2b/config.json`** — should contain `accessToken` and `teamApiKey` fields (same as `delete_sandbox.sh`).

3. **`openpyxl`** — required for Excel report generation (`pip3 install openpyxl`).

## Command Reference

### `snap.create` — Batch Snapshot Creation

```
python3 -m e2b_bench.snap.create [OPTIONS]
```

| Short | Long | Default | Description |
|-------|------|---------|-------------|
| `-e` | `--env-file` | `e2b_bench/scripts/.env` | Path to .env file |
| `-t` | `--template` | `3g` | E2B template name |
| `-n` | `--count` | `1` | Number of sandboxes/snapshots to create |
| `-bs` | `--batch-size` | full concurrent | Sandboxes per creation batch |
| | `--batch-interval` | `3` | Seconds between batches |
| `-o` | `--output-json` | `snapshots.json` | Path to save snapshot IDs JSON |
| | `--output-xlsx` | auto-generated | Excel report path (default: `results/snap/snap_create_<timestamp>.xlsx`) |
| | `--timeout` | `86400` | Sandbox creation timeout (seconds) |
| | `--config` | `~/.e2b/config.json` | Path to E2B config JSON |
| | `--api-key` | None | Override E2B API key |
| | `--access-token` | None | Override E2B access token |

**Examples:**

```bash
# Create 10 snapshots with template "uu"
python3 -m e2b_bench.snap.create -t uu -n 10 -o snapshots.json

# Create 20 in batches of 5 (avoid resource spike)
python3 -m e2b_bench.snap.create -t openclaw-browser-v1 -n 20 -bs 5

# Override credentials for testing
python3 -m e2b_bench.snap.create -t uu -n 3 --api-key test_key --access-token test_token
```

### `snap.restore` — Batch Sandbox Restoration

```
python3 -m e2b_bench.snap.restore [OPTIONS]
```

| Short | Long | Default | Description |
|-------|------|---------|-------------|
| `-e` | `--env-file` | `e2b_bench/scripts/.env` | Path to .env file |
| `-i` | `--input-json` | `snapshots.json` | Path to snapshot IDs JSON |
| `-n` | `--count` | all in JSON | Number of sandboxes to create |
| `-k` | `--keep` | kill after timing | Keep sandboxes alive after creation |
| `-bs` | `--batch-size` | full concurrent | Sandboxes per creation batch |
| | `--batch-interval` | `3` | Seconds between batches |
| | `--output-xlsx` | auto-generated | Excel report path (default: `results/snap/snap_restore_<timestamp>.xlsx`) |
| | `--timeout` | `86400` | Sandbox creation timeout (seconds) |
| | `--config` | `~/.e2b/config.json` | Path to E2B config JSON |
| | `--api-key` | None | Override E2B API key |
| | `--access-token` | None | Override E2B access token |

**Examples:**

```bash
# Restore all sandboxes from snapshots.json
python3 -m e2b_bench.snap.restore -i snapshots.json

# Restore only 5 sandboxes
python3 -m e2b_bench.snap.restore -i snapshots.json -n 5

# Keep sandboxes alive (for subsequent testing)
python3 -m e2b_bench.snap.restore -i snapshots.json -k
```

## Output

### JSON file (`snapshots.json`)

```json
{
  "template": "uu",
  "created_at": "2026-07-28T10:30:00",
  "count": 10,
  "snapshots": [
    {
      "snapshot_id": "snap_abc123",
      "sandbox_id": "sbx_def456",
      "create_elapsed_s": 12.5,
      "snapshot_elapsed_s": 3.2,
      "total_elapsed_s": 15.7,
      "status": "success"
    }
  ]
}
```

### Excel report (3 sheets)

| Sheet | Content |
|-------|---------|
| **Raw** | Per-instance timing data (index, sandbox_id, snapshot_id, elapsed times, status, error) |
| **Summary** | Statistics: count, success_rate, avg, min, max, p50, p90, p99, std |
| **Snapshots** | Snapshot ID registry (snapshot_id, sandbox_id, template, timing, timestamp) |

## Credential Loading Priority

1. CLI args (`--api-key`, `--access-token`) — highest priority, for one-off overrides
2. `.env` file (`E2B_API_KEY`, `E2B_ACCESS_TOKEN`) — override config.json if present
3. `~/.e2b/config.json` (`teamApiKey`, `accessToken`) — baseline, same as `delete_sandbox.sh`

## Architecture

```
e2b_bench/snap/
├── __init__.py    # Package documentation
├── common.py      # load_env, compute_stats, write_excel_report
├── create.py      # Batch sandbox + snapshot creation → JSON + Excel
└── restore.py     # Load JSON → batch restore → Excel
```

`common.py` provides shared utilities used by both `create.py` and `restore.py` via relative imports (`from .common import ...`).
