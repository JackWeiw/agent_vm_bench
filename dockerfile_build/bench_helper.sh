#!/bin/bash
# ============================================================================
# Coding Benchmark Helper — E2B Sandbox Manual Testing
# ============================================================================
#
# Project: devias Material Kit React (https://github.com/devias-io/material-kit-react)
#   - 5.6k GitHub stars, popular React + MUI + TypeScript admin dashboard
#   - Uses MUI (60+ components + 2000+ icons) + Next.js
#
# Simulates a real AI coding agent workflow:
#   Step 0: find   — reset source files (git checkout) + verify/locate target file
#   Step 1: read   — inspect the target file (agent confirming context)
#   Step 2: edit   — apply a pre-configured find→replace pair (real semantic edit)
#   Step 3: build  — production build (overlaps with running dev server, ~3GB peak)
#   Step 4: test   — run test suite (verify correctness)
#   Step 5: diff   — git diff → patch file (agent's verification artifact)
#
# Memory pressure model:
#   Dev server (~1.5GB persistent) + production build (~1GB peak)
#   → ~3GB overlapping peak when both processes are active.
#   This reflects real coding agent environments where dev server is always
#   running while the agent triggers production build verification.
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-dev-server    # Skip dev server
#   bash bench_helper.sh --no-build         # Skip production build
#   bash bench_helper.sh --no-test          # Skip test suite
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    Project path (default: /opt/coding-bench)
#   BENCH_DEV_WAIT       Dev server startup wait in seconds (default: 20)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
DEV_WAIT="${BENCH_DEV_WAIT:-20}"

# Replacement pairs for round-robin editing (verified against the devias repo).
# Each pair is a real, type-safe string edit that triggers a Next rebuild without
# breaking compilation. The runner round-robins through the list.
TARGET_FILES=(
    "src/config.ts|name: 'Devias Kit'|name: 'Devias Kit Pro'"
    "src/paths.ts|customers: '/dashboard/customers'|customers: '/dashboard/customer-list'"
    "src/app/layout.tsx|<html lang=\"en\">|<html lang=\"en-US\">"
    "src/app/page.tsx|redirect('/dashboard')|redirect('/dashboard/overview')"
    "src/app/dashboard/layout.tsx|<html lang=\"en\">|<html lang=\"en-US\">"
    "src/app/dashboard/page.tsx|redirect('/dashboard')|redirect('/dashboard/overview')"
)

# ---- Argument Parsing ----
ROUND=0
SKIP_DEV_SERVER=false
SKIP_BUILD=false
SKIP_TEST=false

for arg in "$@"; do
    case "${arg}" in
        --round=*)       ROUND="${arg#--round=}" ;;
        --no-dev-server) SKIP_DEV_SERVER=true ;;
        --no-build)      SKIP_BUILD=true ;;
        --no-test)       SKIP_TEST=true ;;
        --help|-h)
            echo "Coding Benchmark Helper — devias Material Kit React"
            echo ""
            echo "Simulates real AI coding agent workflow with dev server + build overlap."
            echo ""
            echo "Usage: bash bench_helper.sh [ROUND] [OPTIONS]"
            echo ""
            echo "Positional:"
            echo "  ROUND              Round number (default: 0)"
            echo ""
            echo "Options:"
            echo "  --round=N          Round number (alternative syntax)"
            echo "  --no-dev-server    Skip dev server startup"
            echo "  --no-build         Skip production build"
            echo "  --no-test          Skip test suite"
            echo "  --help             Show this help"
            echo ""
            echo "Environment:"
            echo "  BENCH_PROJECT_DIR   Project path (default: /opt/coding-bench)"
            echo "  BENCH_DEV_WAIT      Dev server startup wait seconds (default: 20)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    — git checkout reset + verify/locate target file"
            echo "  1: read    — inspect target file (head -20)"
            echo "  2: edit    — apply find→replace pair (real semantic edit, triggers rebuild)"
            echo "  3: build   — npm run build (clean rebuild, overlaps with dev server)"
            echo "  4: test    — npm test (verify correctness)"
            echo "  5: diff    — git diff > patch file (verification artifact)"
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

# ---- Helper Functions ----
is_dev_server_running() {
    # devias uses 'next dev' (via npm run dev); also match umi/max for other projects
    ps aux | grep -E 'npm run dev|next dev|umi dev|max dev' | grep -v grep | grep -v pgrep | grep -v 'sh -c' > /dev/null 2>&1
}

# ---- Banner ----
echo "============================================"
echo "  Coding Bench — devias Material Kit React — Round ${ROUND}"
echo "============================================"
echo ""

# ---- Resolve the replacement pair for this round ----
PAIR_IDX=$((ROUND % ${#TARGET_FILES[@]}))
PAIR="${TARGET_FILES[$PAIR_IDX]}"
TARGET_FILE="${PAIR%%|*}"
REST="${PAIR#*|}"
FIND_STR="${REST%%|*}"
REPLACE_STR="${REST#*|}"

# ---- Step 0: find — reset + dev server ----
echo "[Step 0: find] Preparing environment..."

# Reset source files to clean state (simulates agent reverting previous round's changes)
cd "${PROJECT_DIR}" && git checkout -- src/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no src/ changes)"

# Verify the target file exists; fall back to a located file with a generic comment-marker pair
if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find src -name '*.tsx' -o -name '*.ts' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        FIND_STR="// bench marker"
        REPLACE_STR="// bench round
// bench marker"
        echo "  Falling back to: ${TARGET_FILE} (generic comment-marker pair)"
    else
        echo "  ERROR: No target file available for modification" >&2
        exit 1
    fi
else
    echo "  Target: ${TARGET_FILE}"
fi

# Start dev server if not already running
# Dev server provides live preview — every coding agent working on web apps
# starts a dev server (Devin, OpenHands, Claude Code all do this)
if [ "${SKIP_DEV_SERVER}" = false ]; then
    if is_dev_server_running; then
        echo "  Dev server: already running"
    else
        echo "  Starting dev server (BROWSER=none, background)..."
        cd "${PROJECT_DIR}" && BROWSER=none npm run dev > /tmp/dev_server.log 2>&1 &
        echo "  Waiting ${DEV_WAIT}s for initial compilation..."
        sleep "${DEV_WAIT}"
        if is_dev_server_running; then
            echo "  Dev server: ready"
        else
            echo "  WARNING: Dev server may not be ready yet. Check /tmp/dev_server.log"
        fi
    fi
else
    echo "  Dev server: skipped (--no-dev-server)"
fi
echo ""

# ---- Step 1: read — inspect target file ----
echo "[Step 1: read] Inspecting ${TARGET_FILE}..."
cd "${PROJECT_DIR}" && head -20 "${TARGET_FILE}"
echo ""
echo ""

# ---- Step 2: edit — apply find→replace pair ----
echo "[Step 2: edit] Applying replacement (triggers rebuild)..."
echo "  find:    ${FIND_STR}"
echo "  replace: ${REPLACE_STR}"
# sed with | delimiter; escape | and & in the replacement to be safe
ESC_REPLACE=$(printf '%s' "${REPLACE_STR}" | sed 's/[&|]/\\&/g')
cd "${PROJECT_DIR}" && sed -i "s|${FIND_STR}|${ESC_REPLACE}|" "${TARGET_FILE}"
EDIT_EXIT=$?
if [ ${EDIT_EXIT} -ne 0 ]; then
    echo "  ERROR: edit failed (sed exit ${EDIT_EXIT})" >&2
    exit 1
fi
echo "  Edit applied"
echo ""

# ---- Step 3: build — production build (clean rebuild for max memory) ----
if [ "${SKIP_BUILD}" = false ]; then
    echo "[Step 3: build] Production build (clean rebuild + dev server overlap)..."
    BUILD_START=$(date +%s%N)

    # Remove build output and bundler cache (forces full recompilation).
    # NOTE: remove .next/ (Next.js) and node_modules/.cache/. Do NOT remove
    # .next/cache when the dev server is running if it depends on it — but a
    # clean rebuild here intentionally forces full recompile for memory peak.
    rm -rf "${PROJECT_DIR}/.next/" "${PROJECT_DIR}/node_modules/.cache/"

    cd "${PROJECT_DIR}" && npm run build > /tmp/build_output.log 2>&1
    BUILD_EXIT=$?
    BUILD_END=$(date +%s%N)
    BUILD_MS=$(( (BUILD_END - BUILD_START) / 1000000 ))

    tail -8 /tmp/build_output.log
    if [ ${BUILD_EXIT} -eq 0 ]; then
        echo "  Build: SUCCESS (${BUILD_MS}ms)"
    else
        echo "  Build: FAILED (exit ${BUILD_EXIT}, ${BUILD_MS}ms)"
    fi
else
    echo "[Step 3: build] Skipped (--no-build)"
    BUILD_MS=0
    BUILD_EXIT=0
fi
echo ""

# ---- Step 4: test — run test suite ----
if [ "${SKIP_TEST}" = false ]; then
    echo "[Step 4: test] Running test suite..."
    TEST_START=$(date +%s%N)

    cd "${PROJECT_DIR}" && npm test > /tmp/test_output.log 2>&1
    TEST_EXIT=$?
    TEST_END=$(date +%s%N)
    TEST_MS=$(( (TEST_END - TEST_START) / 1000000 ))

    tail -5 /tmp/test_output.log
    echo "  Test: completed (${TEST_MS}ms, exit ${TEST_EXIT})"
else
    echo "[Step 4: test] Skipped (--no-test)"
    TEST_MS=0
    TEST_EXIT=0
fi
echo ""

# ---- Step 5: diff — produce verification artifact ----
echo "[Step 5: diff] Producing git patch..."
DIFF_START=$(date +%s%N)
cd "${PROJECT_DIR}" && git diff > "/tmp/bench_round_${ROUND}.patch" 2>&1
DIFF_EXIT=$?
DIFF_END=$(date +%s%N)
DIFF_MS=$(( (DIFF_END - DIFF_START) / 1000000 ))
PATCH_LINES=$(wc -l < "/tmp/bench_round_${ROUND}.patch" 2>/dev/null || echo 0)
echo "  Patch: /tmp/bench_round_${ROUND}.patch (${PATCH_LINES} lines, ${DIFF_MS}ms)"
echo ""

# ---- Summary ----
DEV_STATUS="stopped"
is_dev_server_running && DEV_STATUS="running"

echo "============================================"
echo "  Round ${ROUND} Complete"
echo "============================================"
echo "  Round:       ${ROUND}"
echo "  Target:      ${TARGET_FILE}"
echo "  Edit:        '${FIND_STR}' -> '${REPLACE_STR}'"
echo "  Build:       ${BUILD_MS}ms (exit: ${BUILD_EXIT})"
echo "  Test:        ${TEST_MS}ms (exit: ${TEST_EXIT})"
echo "  Patch:       /tmp/bench_round_${ROUND}.patch (${PATCH_LINES} lines)"
echo "  Dev server:  ${DEV_STATUS}"
echo "============================================"
echo ""
echo "  Next round:  bash ${PROJECT_DIR}/bench_helper.sh $((ROUND + 1))"
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- src/"
echo "  Stop dev:    pkill -f 'next dev'; pkill -f 'npm run dev'"
