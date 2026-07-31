#!/bin/bash
# ============================================================================
# Coding Benchmark Helper (Go) — E2B Sandbox Manual Testing (gohugoio/hugo)
# ============================================================================
#
# Trace-faithful loop (verified against a captured openclaw agent trajectory on
# gohugoio/hugo issue #12768 GitHub Alert case-insensitivity): find -> read ->
# edit -> verify (write ad-hoc /tmp/bench_verify.go + `go run`) -> git diff.
# The agent NEVER runs `go build ./...` or `go test ./...`; verification is a
# transient `go run /tmp/test_alert.go` (Go compiler + execute, memory peak).
#
# Simulates a real AI coding agent workflow:
#   Step 0: find   — reset source files (git checkout) + locate target
#   Step 1: read   — inspect the target file (agent confirming context)
#   Step 2: edit   — apply a pre-configured find->replace pair (real semantic edit)
#   Step 3: verify — write an ad-hoc /tmp/bench_verify.go + `go run` it
#   Step 4: diff   — git diff -> patch file (agent's verification artifact)
#
# Memory pressure model:
#   Each verify run compiles + executes a `package main` (Go compiler loads
#   imported package types). N sandboxes' staggered verify peaks overlapping
#   -> host memory overcommit. No resident process.
#
# Cold-compile guarantee:
#   `go clean -cache` runs before every `go run`. The Go toolchain caches
#   compiled stdlib/packages under GOCACHE, so the first `go run` pays the full
#   compile (~40% CPU) and every later run hits cache (~10%) - which would NOT
#   reflect the real agent's per-verify CPU shape. The real openclaw agent never
#   runs `go clean`, but within a single issue it repeatedly rewrites its ad-hoc
#   /tmp/test_*.go and re-runs `go run`, i.e. each verify is effectively a fresh
#   compile. Clearing the cache before each verify reproduces that per-verify
#   cold-compile pressure (the behavior the customer needs to measure).
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-verify        # Skip verify step
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    hugo project path (default: /opt/coding-bench)
#   BENCH_VERIFY_CMD     Verify run command (default: go run /tmp/bench_verify.go)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 120)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-go run /tmp/bench_verify.go}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-120}"
TEMP_TEST_PATH="/tmp/bench_verify.go"

# Replacement pairs for round-robin editing (verified against gohugoio/hugo at
# base_commit 83235262). Each pair is a real, type-safe edit to the GitHub Alert
# regex path. The 3rd field is an optional verify_script body (a standalone
# `package main` exercising the edited behavior); empty = shared default (compiles
# + runs a no-op main, prints "All tests passed!" -> real Go compiler peak).
# Format: "file|find|replace|verify_script"
#
# NOTE: verify_script uses a literal \n between lines (sed-decoded below) because
# bash arrays can't carry real newlines cleanly across the | delimiter here.
TARGET_FILES=(
    "markup/goldmark/blockquotes/blockquotes.go|var gitHubAlertRe = regexp.MustCompile(\`^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]\`)|var gitHubAlertRe = regexp.MustCompile(\`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]\`)|package main\n\nimport (\n\t\"fmt\"\n\t\"regexp\"\n)\n\nvar gitHubAlertRe = regexp.MustCompile(\`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]\`)\n\nfunc main() {\n\tcases := []struct{ in string; want bool }{\n\t\t{\`<p>[!NOTE]\`, true},\n\t\t{\`<p>[!note]\`, true},\n\t\t{\`<p>[!Tip]\`, true},\n\t\t{\`<p>[!warning]\`, true},\n\t\t{\`<p>[!X]\`, false},\n\t}\n\tok := true\n\tfor _, c := range cases {\n\t\tif gitHubAlertRe.MatchString(c.in) != c.want { ok = false }\n\t}\n\tif ok { fmt.Println(\"All tests passed!\") } else { fmt.Println(\"Some tests failed!\") }\n}"
    "markup/goldmark/blockquotes/blockquotes.go|// resolveGitHubAlert returns one of note, tip, warning, important or caution.|// resolveGitHubAlert returns one of note, tip, warning, important or caution. // bench||"
    "markup/goldmark/blockquotes/blockquotes.go|// An empty string if no match.|// An empty string if no match. // bench||"
    "markup/goldmark/blockquotes/blockquotes.go|// https://docs.github.com/en/get-started/writing-on-github|// https://docs.github.com/en/get-started/writing-on-github // bench||"
    "markup/goldmark/blockquotes/blockquotes.go|// Five types:|// Five types: // bench||"
    "markup/goldmark/blockquotes/blockquotes.go|// [!NOTE], [!TIP], [!WARNING], [!IMPORTANT], [!CAUTION]|// [!NOTE], [!TIP], [!WARNING], [!IMPORTANT], [!CAUTION] // bench||"
)

# Shared default verify script (used when a pair's verify_script is empty).
# A standalone `package main` that compiles + runs (real Go compiler peak) and
# prints "All tests passed!". Imports only stdlib so it compiles without the
# hugo module graph, but still loads the compiler/types for imported packages.
default_verify_script='package main

import "fmt"

func main() {
	fmt.Println("All tests passed!")
}'

# ---- Argument Parsing ----
ROUND=0
SKIP_VERIFY=false

for arg in "$@"; do
    case "${arg}" in
        --round=*)       ROUND="${arg#--round=}" ;;
        --no-verify)     SKIP_VERIFY=true ;;
        --help|-h)
            echo "Coding Benchmark Helper (Go) - gohugoio/hugo (trace-faithful)"
            echo ""
            echo "Loop: find -> read -> edit -> verify (go run) -> git diff"
            echo ""
            echo "Usage: bash bench_helper.sh [ROUND] [OPTIONS]"
            echo ""
            echo "Positional:"
            echo "  ROUND           Round number (default: 0)"
            echo ""
            echo "Options:"
            echo "  --round=N       Round number (alternative syntax)"
            echo "  --no-verify     Skip the verify step"
            echo "  --help          Show this help"
            echo ""
            echo "Environment:"
            echo "  BENCH_PROJECT_DIR     hugo path (default: /opt/coding-bench)"
            echo "  BENCH_VERIFY_CMD     Verify run command (default: go run /tmp/bench_verify.go)"
            echo "  BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 120)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply find->replace pair (real semantic edit)"
            echo "  3: verify  - write /tmp/bench_verify.go + go run (memory peak)"
            echo "  4: diff    - git diff > patch file (verification artifact)"
            exit 0
            ;;
        *)
            if [[ "${arg}" =~ ^[0-9]+$ ]]; then
                ROUND="${arg}"
            else
                echo "Unknown option: ${arg}. Use --help for usage." >&2
                exit 1
            fi
            ;;
    esac
done

# ---- Banner ----
echo "============================================"
echo "  Coding Bench (Go) - gohugoio/hugo - Round ${ROUND}"
echo "============================================"
echo ""

# ---- Resolve the replacement pair for this round ----
PAIR_IDX=$((ROUND % ${#TARGET_FILES[@]}))
PAIR="${TARGET_FILES[$PAIR_IDX]}"
TARGET_FILE="${PAIR%%|*}"
REST="${PAIR#*|}"
FIND_STR="${REST%%|*}"
REST="${REST#*|}"
REPLACE_STR="${REST%%|*}"
VERIFY_SCRIPT="${REST#*|}"

# ---- Step 0: find — reset + locate target ----
echo "[Step 0: find] Preparing environment..."

# Reset source files (hugo edits live under markup/)
cd "${PROJECT_DIR}" && git checkout -- markup/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find . -name '*.go' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        FIND_STR="// bench marker"
        REPLACE_STR="// bench round"
        VERIFY_SCRIPT=""
        echo "  Falling back to: ${TARGET_FILE} (generic comment-marker pair)"
    else
        echo "  ERROR: No target file available for modification" >&2
        exit 1
    fi
else
    echo "  Target: ${TARGET_FILE}"
fi
echo ""

# ---- Step 1: read — inspect target file ----
echo "[Step 1: read] Inspecting ${TARGET_FILE}..."
cd "${PROJECT_DIR}" && head -20 "${TARGET_FILE}"
echo ""
echo ""

# ---- Step 2: edit — apply find->replace pair (literal, via python3) ----
echo "[Step 2: edit] Applying replacement..."
echo "  find:    ${FIND_STR}"
echo "  replace: ${REPLACE_STR}"
# Literal str.replace (not sed regex): Go source pairs contain regex
# metacharacters (|, [, ], backticks) that break `sed 's|...|...|'`. find/replace
# are passed to python3 as base64 so quoting is inert; exit 2 if the find string
# is absent (no-op edit surfaced, not a silent fake verify pass).
FIND_B64=$(printf '%s' "${FIND_STR}" | base64 -w0)
REPL_B64=$(printf '%s' "${REPLACE_STR}" | base64 -w0)
cd "${PROJECT_DIR}" && python3 - "$FIND_B64" "$REPL_B64" "$TARGET_FILE" <<'PYEOF'
import base64, sys
f = base64.b64decode(sys.argv[1]).decode()
r = base64.b64decode(sys.argv[2]).decode()
p = sys.argv[3]
s = open(p, encoding='utf-8').read()
if f not in s:
    sys.exit(2)
open(p, 'w', encoding='utf-8').write(s.replace(f, r, 1))
PYEOF
EDIT_EXIT=$?
if [ ${EDIT_EXIT} -ne 0 ]; then
    echo "  ERROR: edit failed (exit ${EDIT_EXIT}; 2 = find string absent)" >&2
    exit 1
fi
echo "  Edit applied"
echo ""

# ---- Step 3: verify — write ad-hoc test file + run via go run ----
if [ "${SKIP_VERIFY}" = false ]; then
    echo "[Step 3: verify] Writing ad-hoc test + running go run (memory peak)..."
    VERIFY_START=$(date +%s%N)

    # Resolve the script body: pair-specific (decode \n to real newlines), else shared default
    if [ -n "${VERIFY_SCRIPT}" ]; then
        SCRIPT_BODY=$(printf '%b' "${VERIFY_SCRIPT}")
    else
        SCRIPT_BODY="${default_verify_script}"
    fi

    # Write the temp test file (printf handles multi-line script bodies safely).
    printf '%s\n' "${SCRIPT_BODY}" > "${TEMP_TEST_PATH}"

    # Run the ad-hoc test via `go run` (transient: Go compiler + execute).
    # Clear GOCACHE first so every verify is a real cold-compile (see header
    # cold-compile guarantee) - otherwise the 2nd+ run hits cache (~10% CPU
    # instead of ~40%), masking the real per-verify CPU pressure.
    go clean -cache 2>/dev/null || true
    cd "${PROJECT_DIR}" && timeout "${VERIFY_TIMEOUT}" ${VERIFY_CMD} > /tmp/verify_output.log 2>&1
    VERIFY_EXIT=$?
    VERIFY_END=$(date +%s%N)
    VERIFY_MS=$(( (VERIFY_END - VERIFY_START) / 1000000 ))

    tail -8 /tmp/verify_output.log
    if [ ${VERIFY_EXIT} -eq 0 ]; then
        echo "  Verify: SUCCESS (${VERIFY_MS}ms)"
    else
        echo "  Verify: FAILED (exit ${VERIFY_EXIT}, ${VERIFY_MS}ms)"
    fi
else
    echo "[Step 3: verify] Skipped (--no-verify)"
    VERIFY_MS=0
    VERIFY_EXIT=0
fi
echo ""

# ---- Step 4: diff — produce verification artifact ----
echo "[Step 4: diff] Producing git patch..."
DIFF_START=$(date +%s%N)
cd "${PROJECT_DIR}" && git diff > "/tmp/bench_round_${ROUND}.patch" 2>&1
DIFF_EXIT=$?
DIFF_END=$(date +%s%N)
DIFF_MS=$(( (DIFF_END - DIFF_START) / 1000000 ))
PATCH_LINES=$(wc -l < "/tmp/bench_round_${ROUND}.patch" 2>/dev/null || echo 0)
echo "  Patch: /tmp/bench_round_${ROUND}.patch (${PATCH_LINES} lines, ${DIFF_MS}ms)"
echo ""

# ---- Summary ----
echo "============================================"
echo "  Round ${ROUND} Complete"
echo "============================================"
echo "  Round:       ${ROUND}"
echo "  Target:      ${TARGET_FILE}"
echo "  Edit:        '${FIND_STR}' -> '${REPLACE_STR}'"
echo "  Verify:      ${VERIFY_MS}ms (exit: ${VERIFY_EXIT})"
echo "  Patch:       /tmp/bench_round_${ROUND}.patch (${PATCH_LINES} lines)"
echo "============================================"
echo ""
echo "  Next round:  bash ${PROJECT_DIR}/bench_helper.sh $((ROUND + 1))"
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- markup/"
