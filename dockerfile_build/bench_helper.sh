#!/bin/bash
# ============================================================================
# Coding Benchmark Helper — E2B Sandbox Manual Testing
# ============================================================================
#
# Project: Ant Design Pro (https://github.com/ant-design/ant-design-pro)
#   - 36k+ GitHub stars, most popular React enterprise dashboard
#   - Uses antd (60+ UI components) + UmiJS + webpack
#
# Simulates real AI coding agent workflow:
#   Step 0: Setup — reset source files + start dev server (persistent live preview)
#   Step 1: Edit — modify a source file (sed injection, like agent editing code)
#   Step 2: Build — production build (overlaps with running dev server, ~3GB peak)
#   Step 3: Test — run test suite (verify correctness)
#   Step 4: Memory — collect metrics (free -m)
#
# Memory pressure model:
#   Dev server (~1-1.5GB persistent) + production build (~2GB peak)
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

# Source files for round-robin modification (verified against Ant Design Pro repo)
# These are the most common page types in enterprise dashboard apps:
#   dashboard, form, list, table-list, profile, result, exception, user, account, chatbot
TARGET_FILES=(
    "src/pages/dashboard/analysis/index.tsx"
    "src/pages/dashboard/workplace/index.tsx"
    "src/pages/dashboard/monitor/index.tsx"
    "src/pages/form/basic-form/index.tsx"
    "src/pages/form/step-form/index.tsx"
    "src/pages/form/advanced-form/index.tsx"
    "src/pages/list/basic-list/index.tsx"
    "src/pages/list/card-list/index.tsx"
    "src/pages/list/search/index.tsx"
    "src/pages/table-list/index.tsx"
    "src/pages/profile/basic/index.tsx"
    "src/pages/profile/advanced/index.tsx"
    "src/pages/result/success/index.tsx"
    "src/pages/result/fail/index.tsx"
    "src/pages/exception/403/index.tsx"
    "src/pages/exception/404/index.tsx"
    "src/pages/exception/500/index.tsx"
    "src/pages/user/login/index.tsx"
    "src/pages/user/register/index.tsx"
    "src/pages/account/settings/index.tsx"
    "src/pages/account/center/index.tsx"
    "src/pages/chatbot/index.tsx"
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
            echo "Coding Benchmark Helper — Ant Design Pro"
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
            echo "  0: setup    — git checkout reset + start dev server (if not running)"
            echo "  1: edit     — inject round marker into source file (triggers rebuild)"
            echo "  2: build    — npm run build (clean rebuild, overlaps with dev server)"
            echo "  3: test     — npm test (verify correctness)"
            echo "  4: memory   — free -m (collect memory metrics)"
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
    pgrep -f "umi dev" > /dev/null 2>&1 || pgrep -f "max dev" > /dev/null 2>&1
}

# ---- Banner ----
echo "============================================"
echo "  Coding Bench — Ant Design Pro — Round ${ROUND}"
echo "============================================"
echo ""

# ---- Step 0: Setup — reset + dev server ----
echo "[Step 0: setup] Preparing environment..."

# Reset source files to clean state (simulates agent reverting failed changes)
cd "${PROJECT_DIR}" && git checkout -- src/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no src/ changes)"
echo "  Source files reset (git checkout -- src/)"

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

# ---- Step 1: Edit — modify source file ----
FILE_IDX=$((ROUND % ${#TARGET_FILES[@]}))
TARGET_FILE="${TARGET_FILES[$FILE_IDX]}"

echo "[Step 1: edit] Round ${ROUND} → ${TARGET_FILE}"
if [ -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    sed -i "1i// Bench Round ${ROUND}" "${PROJECT_DIR}/${TARGET_FILE}"
    echo "  Injected round marker comment (triggers webpack rebuild)"
else
    echo "  File not found: ${TARGET_FILE}"
    echo "  Fallback: modifying config/config.ts"
    if [ -f "${PROJECT_DIR}/config/config.ts" ]; then
        sed -i "1i// Bench Round ${ROUND}" "${PROJECT_DIR}/config/config.ts"
        echo "  Injected round marker into config"
    else
        echo "  ERROR: No target file available for modification" >&2
        exit 1
    fi
fi
echo ""

# ---- Step 2: Build — production build (clean rebuild for max memory) ----
if [ "${SKIP_BUILD}" = false ]; then
    echo "[Step 2: build] Production build (clean rebuild + dev server overlap)..."
    BUILD_START=$(date +%s%N)

    # Remove build output and webpack filesystem cache (forces full recompilation)
    # NOTE: keep .umi/ intact when dev server is running — it depends on those
    # generated files. Only remove dist/ and node_modules/.cache/
    rm -rf "${PROJECT_DIR}/dist/" "${PROJECT_DIR}/node_modules/.cache/"

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
    echo "[Step 2: build] Skipped (--no-build)"
    BUILD_MS=0
    BUILD_EXIT=0
fi
echo ""

# ---- Step 3: Test — run test suite ----
if [ "${SKIP_TEST}" = false ]; then
    echo "[Step 3: test] Running test suite..."
    TEST_START=$(date +%s%N)

    cd "${PROJECT_DIR}" && npm test > /tmp/test_output.log 2>&1
    TEST_EXIT=$?
    TEST_END=$(date +%s%N)
    TEST_MS=$(( (TEST_END - TEST_START) / 1000000 ))

    tail -5 /tmp/test_output.log
    echo "  Test: completed (${TEST_MS}ms, exit ${TEST_EXIT})"
else
    echo "[Step 3: test] Skipped (--no-test)"
    TEST_MS=0
    TEST_EXIT=0
fi
echo ""

# ---- Step 4: Memory — collect metrics ----
echo "[Step 4: memory] Current memory usage:"
free -m
echo ""

# ---- Summary ----
DEV_STATUS="stopped"
is_dev_server_running && DEV_STATUS="running"

echo "============================================"
echo "  Round ${ROUND} Complete"
echo "============================================"
echo "  Round:       ${ROUND}"
echo "  Target:      ${TARGET_FILE}"
echo "  Build:       ${BUILD_MS}ms (exit: ${BUILD_EXIT})"
echo "  Test:        ${TEST_MS}ms (exit: ${TEST_EXIT})"
echo "  Dev server:  ${DEV_STATUS}"
echo "============================================"
echo ""
echo "  Next round:  bash ${PROJECT_DIR}/bench_helper.sh $((ROUND + 1))"
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- src/"
echo "  Stop dev:    pkill -f 'umi dev'; pkill -f 'max dev'"
