# E2B Bench Sandbox IDs File Feature Design

## Overview

Add two features to e2b_bench:
1. **Save sandbox IDs** - In `--create-only` mode, save successful sandbox IDs to a file
2. **Filter by sandbox IDs** - In `--detect` mode, only benchmark sandboxes listed in a file

## Requirements

### Feature 1: Save Sandbox IDs (Create-Only Mode)

When running `--create-only` with `--sandbox-ids-file <filepath>`:
- After all sandboxes are created and port-checked
- Save E2B sandbox IDs of all successful sandboxes to the specified file
- File format: plain text, one ID per line

### Feature 2: Filter by Sandbox IDs (Detect Mode)

When running `--detect` with `--sandbox-ids-file <filepath>`:
- Read target sandbox IDs from the file
- Call `Sandbox.list()` to get all running sandboxes
- Match: only benchmark sandboxes that exist in both file and running list
- "Best effort" mode: skip missing IDs with warning, don't block the benchmark

## Configuration

### CLI Argument

```bash
--sandbox-ids-file <filepath>
```

- Specify file path to save/load sandbox IDs
- Works with both `--create-only` and `--detect` modes
- Optional: if not specified, existing behavior unchanged

### YAML Configuration

```yaml
sandbox:
  template: "openclaw-browser-v1"
  total_count: 100
  sandbox_ids_file: "sandbox_ids.txt"  # Optional
```

- New field: `sandbox_ids_file`
- Can be overridden by CLI argument

## File Format

**sandbox_ids.txt:**
```
sbx_abc123def456
sbx_xyz789ghi012
sbx_qrs345tuv678
```

- Plain text file
- One E2B sandbox ID per line
- Empty lines ignored
- No metadata, just IDs

## Data Flow

### Save Flow (Create-Only Mode)

```
--create-only --sandbox-ids-file ids.txt
    |
    v
Create all sandboxes -> Port check
    |
    v
Collect E2B sandbox_ids from successful sandboxes (PORT_READY status)
    |
    v
Write to ids.txt (one per line)
    |
    v
Print: "Saved N sandbox IDs to ids.txt"
```

### Filter Flow (Detect Mode)

```
--detect --sandbox-ids-file ids.txt
    |
    v
Read ids.txt -> Parse target ID set
    |
    v
Call Sandbox.list() -> Get all running sandboxes
    |
    v
Intersection match: keep only sandboxes in both sets
    |
    +-- IDs in file but not running -> Warning: "Sandbox X not found or stopped"
    +-- IDs running but not in file -> Ignored (not benchmarked)
    |
    v
Connect matched sandboxes -> Port check -> Benchmark
```

## Implementation

### Files to Modify

1. **config.py** - Add `sandbox_ids_file` field
2. **bench.py** - Add CLI argument, save IDs logic in create-only mode
3. **sandbox_manager.py** - Add `detect_from_file()` method

### config.py Changes

```python
@dataclass
class Config:
    # ... existing fields ...

    # Sandbox IDs file (for save/load sandbox IDs)
    sandbox_ids_file: Optional[str] = None
```

Update `_from_dict()`, `merge_with_args()`, `from_args()` methods accordingly.

### bench.py Changes

**CLI Argument:**
```python
parser.add_argument('--sandbox-ids-file', type=str,
                    help='File path to save/load sandbox IDs (one ID per line)')
```

**Save Logic (in run_benchmark, after create-only completes):**
```python
if config.create_only and config.sandbox_ids_file:
    successful_ids = [
        s.sandbox_obj.sandbox_id
        for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.PORT_READY
    ]
    with open(config.sandbox_ids_file, 'w') as f:
        for sid in successful_ids:
            f.write(f"{sid}\n")
    print(f"Saved {len(successful_ids)} sandbox IDs to {config.sandbox_ids_file}")
```

**Detect Logic (in run_benchmark):**
```python
if config.detect_existing:
    if config.sandbox_ids_file:
        sandbox_states = sandbox_manager.detect_from_file(config.sandbox_ids_file)
    else:
        sandbox_states = sandbox_manager.detect_existing()
```

### sandbox_manager.py Changes

**New Method:**
```python
def detect_from_file(self, ids_file: str) -> Dict[int, SandboxState]:
    """Detect sandboxes from ID file with matching

    Args:
        ids_file: Path to file containing sandbox IDs

    Returns:
        Dict of connected sandbox states
    """
    # 1. Read target IDs from file
    target_ids = set()
    with open(ids_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                target_ids.add(line)

    if not target_ids:
        print(f"  WARNING: No IDs found in {ids_file}")
        return {}

    print(f"  Target IDs from file: {len(target_ids)}")

    # 2. Get all running sandboxes
    paginator = Sandbox.list()
    running_list = []
    while paginator.has_next:
        sandboxes = paginator.next_items()
        running_list.extend(sandboxes)

    print(f"  Running sandboxes: {len(running_list)}")

    # 3. Match: only keep sandboxes in both sets
    matched = []
    not_found = []

    for listed_sandbox in running_list:
        e2b_id = listed_sandbox.sandbox_id
        if e2b_id in target_ids:
            matched.append(listed_sandbox)
            target_ids.remove(e2b_id)  # Mark as found

    # Remaining target_ids are not running
    not_found = list(target_ids)
    if not_found:
        print(f"  WARNING: {len(not_found)} IDs not found or stopped")
        for sid in not_found[:5]:  # Show first 5
            print(f"    - {sid}")

    print(f"  Matched sandboxes: {len(matched)}")

    # 4. Connect and check ports (same logic as detect_existing)
    for i, listed_sandbox in enumerate(matched):
        # ... connect and port check logic ...
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| File not found | Error message, exit |
| File empty | Warning, no sandboxes to benchmark |
| ID in file but not running | Warning, skip |
| No matched sandboxes | Warning, exit gracefully |
| Sandbox.connect fails | Warning, skip, continue with others |

## Usage Examples

### Create and Save IDs

```bash
# Create 50 sandboxes and save IDs
python -m e2b_bench --create-only -n 50 --sandbox-ids-file sandbox_ids.txt

# Or with YAML config
python -m e2b_bench --create-only -c config.yaml --sandbox-ids-file sandbox_ids.txt
```

### Benchmark Saved Sandboxes

```bash
# Detect and benchmark only saved sandboxes
python -m e2b_bench --detect --sandbox-ids-file sandbox_ids.txt -d 300

# Or with YAML config
python -m e2b_bench --detect -c config.yaml --sandbox-ids-file sandbox_ids.txt
```

### Full Workflow

```bash
# Step 1: Create and save IDs
python -m e2b_bench --create-only -n 100 --sandbox-ids-file ids.txt

# Step 2: Later, benchmark those exact sandboxes
python -m e2b_bench --detect --sandbox-ids-file ids.txt --duration 600
```

## Backward Compatibility

- If `--sandbox-ids-file` not specified, existing behavior unchanged
- `--detect` without file: detect all running sandboxes (original behavior)
- `--create-only` without file: create sandboxes without saving IDs (original behavior)

## Testing Considerations

1. Test file read/write with various edge cases:
   - Empty file
   - File with extra whitespace
   - Non-existent file path

2. Test matching logic:
   - All IDs matched
   - Some IDs missing
   - No IDs matched

3. Test integration with existing modes:
   - create-only + save
   - detect + filter
   - batch mode compatibility
