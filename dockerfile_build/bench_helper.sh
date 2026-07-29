#!/bin/bash
# ============================================================================
# Coding Benchmark Helper — E2B Sandbox Manual Testing
# ============================================================================
#
# Project: vuejs/core (https://github.com/vuejs/core)
#   - 54k+ GitHub stars, Vue.js core framework (TypeScript monorepo, pnpm)
#   - Real repo from the swe_bench_multilingual evaluation dataset
#   - Build: rollup (production) + esbuild (dev) — fast, low CPU
#
# Dev server: a vite playground (/opt/vite-playground) that imports the built
# vue lib, so the dev server holds the full vue+rollup+esbuild module graph
# resident (~1.5GB). Real agent behavior: editing a frontend library and
# verifying changes via a local vite dev/HMR playground.
#
# Simulates a real AI coding agent workflow:
#   Step 0: find   — reset source files (git checkout packages/) + verify target
#   Step 1: read   — inspect the target file (agent confirming context)
#   Step 2: edit   — apply a pre-configured find→replace pair (real semantic edit)
#   Step 3: build  — production build (overlaps with running dev server, ~3GB peak)
#   Step 4: test   — run test suite (verify correctness)
#   Step 5: diff   — git diff → patch file (agent's verification artifact)
#
# Memory pressure model:
#   Dev server (~1.5GB persistent) + production build (~1GB peak)
#   → ~3GB overlapping peak when both processes are active.
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
#   BENCH_PROJECT_DIR    vuejs/core project path (default: /opt/coding-bench)
#   BENCH_DEV_DIR        vite playground path (default: /opt/vite-playground)
#   BENCH_DEV_WAIT       Dev server startup wait in seconds (default: 20)
#   BENCH_BUILD_CMD      Build command (default: node scripts/build.js)
#   BENCH_TEST_CMD       Test command (default: pnpm test)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
DEV_DIR="${BENCH_DEV_DIR:-/opt/vite-playground}"
DEV_WAIT="${BENCH_DEV_WAIT:-20}"
BUILD_CMD="${BENCH_BUILD_CMD:-node scripts/build.js}"
TEST_CMD="${BENCH_TEST_CMD:-pnpm test}"

# Replacement pairs for round-robin editing (verified against the vuejs/core repo).
# Each pair is a real, type-safe string edit that triggers a rollup/esbuild rebuild
# without breaking compilation. The runner round-robins through the list.
# Format: "file|find|replace"
TARGET_FILES=(
    "packages/shared/src/general.ts|export const NOOP = (): void => {}|export const NOOP = (): void => undefined"
    "packages/shared/src/general.ts|Always return false.|Always returns false."
    "packages/shared/src/index.ts|export * from './general'|export * from './general' // bench round"
    'packages/vue/src/index.ts|// This entry is the "full-build"|// This entry is the "full-build" (bench)'
    "packages/reactivity/src/baseHandlers.ts|export const mutableHandlers: ProxyHandler<object> =|export const mutableHandlers: ProxyHandler<object> = // bench"
    "packages/runtime-core/src/errorHandling.ts|import { EMPTY_OBJ, isArray, isFunction, isPromise } from '@vue/shared'|import { EMPTY_OBJ, isArray, isFunction, isPromise } from '@vue/shared' // bench"
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
            echo "Coding Benchmark Helper — vuejs/core"
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
            echo "  BENCH_PROJECT_DIR   vuejs/core path (default: /opt/coding-bench)"
            echo "  BENCH_DEV_DIR       vite playground path (default: /opt/vite-playground)"
            echo "  BENCH_DEV_WAIT      Dev server startup wait seconds (default: 20)"
            echo "  BENCH_BUILD_CMD     Build command (default: node scripts/build.js)"
            echo "  BENCH_TEST_CMD      Test command (default: pnpm test)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    — git checkout reset + verify/locate target file"
            echo "  1: read    — inspect target file (head -20)"
            echo "  2: edit    — apply find→replace pair (real semantic edit, triggers rebuild)"
            echo "  3: build   — node scripts/build.js (clean rebuild, overlaps with dev server)"
            echo "  4: test    — pnpm test (verify correctness)"
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
    # vite dev server (the playground's npm run dev → vite), plus common variants
    ps aux | grep -E 'npm run dev|vite|next dev|umi dev|max dev' | grep -v grep | grep -v pgrep | grep -v 'sh -c' > /dev/null 2>&1
}

# ---- Banner ----
echo "============================================"
echo "  Coding Bench — vuejs/core — Round ${ROUND}"
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
# vuejs/core source lives under packages/*/src; fall back to src/ for other layouts.
cd "${PROJECT_DIR}" && git checkout -- packages/ src/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

# Verify the target file exists; fall back to a located file with a generic comment-marker pair
if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find packages src -name '*.ts' -o -name '*.tsx' -o -name '*.js' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        FIND_STR="// bench marker"
        REPLACE_STR="// bench round"
        echo "  Falling back to: ${TARGET_FILE} (generic comment-marker pair)"
    else
        echo "  ERROR: No target file available for modification" >&2
        exit 1
    fi
else
    echo "  Target: ${TARGET_FILE}"
fi

# Start dev server (vite playground) if not already running
# A real agent editing a frontend lib verifies changes via a local vite dev/HMR
# playground. The playground imports the built vue lib so the dev server holds the
# full module graph resident (~1.5GB).
if [ "${SKIP_DEV_SERVER}" = false ]; then
    if is_dev_server_running; then
        echo "  Dev server: already running"
    else
        echo "  Starting vite dev server (background)..."
        cd "${DEV_DIR}" && BROWSER=none npm run dev > /tmp/dev_server.log 2>&1 &
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
    # vuejs/core outputs to packages/*/dist; clean node_modules/.cache and tsconfig cache.
    cd "${PROJECT_DIR}" && find packages -type d -name dist -prune -exec rm -rf {} + 2>/dev/null
    rm -rf "${PROJECT_DIR}/node_modules/.cache" "${PROJECT_DIR}/node_modules/.vite" 2>/dev/null

    cd "${PROJECT_DIR}" && ${BUILD_CMD} > /tmp/build_output.log 2>&1
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

    cd "${PROJECT_DIR}" && ${TEST_CMD} > /tmp/test_output.log 2>&1
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
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- packages/"
echo "  Stop dev:    pkill -f 'vite'; pkill -f 'npm run dev'"
