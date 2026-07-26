#!/bin/bash
# Helper script for manual benchmark testing inside sandbox
# Adapted for Ant Design Pro (real open-source project)
#
# Modification strategy: git checkout → modify → build → test → git checkout (reset)
# This simulates real AI coding agent workflow:
#   Agent reads code → makes targeted change → builds to verify → reverts if needed
#
# Usage: bash /opt/coding-bench/bench_helper.sh [round_number]

ROUND="${1:-0}"
PROJECT="/opt/coding-bench"

# Array of source files to modify (round-robin through them)
# These are real page/component files from Ant Design Pro
TARGET_FILES=(
    "src/pages/dashboard/analysis/index.tsx"
    "src/pages/dashboard/workplace/index.tsx"
    "src/pages/dashboard/monitor/index.tsx"
    "src/pages/form/basic-form/index.tsx"
    "src/pages/form/step-form/index.tsx"
    "src/pages/list/table-list/index.tsx"
    "src/pages/list/basic-list/index.tsx"
    "src/pages/list/card-list/index.tsx"
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
    "src/pages/editor/new-page/index.tsx"
)

echo "=== Coding Bench Helper (Ant Design Pro) - Round ${ROUND} ==="
echo ""

# Step 0: Reset to clean state (git checkout)
echo "[Step 0: reset] Reverting any previous changes..."
cd "${PROJECT}" && git checkout -- src/ 2>/dev/null || echo "  No changes to revert (clean state)"
echo ""

# Step 1: Modify a source file (simulates agent code edit)
FILE_IDX=$((ROUND % ${#TARGET_FILES[@]}))
TARGET_FILE="${TARGET_FILES[$FILE_IDX]}"

if [ -f "${PROJECT}/${TARGET_FILE}" ]; then
    echo "[Step 1: edit_file] Modifying ${TARGET_FILE} for round ${ROUND}..."
    # Inject a unique comment at the first line (non-breaking, triggers rebuild)
    sed -i "1i// Bench Round ${ROUND}" "${PROJECT}/${TARGET_FILE}"
    echo "  Injected round marker into ${TARGET_FILE}"
else
    echo "[Step 1: edit_file] Target file not found: ${TARGET_FILE}"
    echo "  Falling back to config modification..."
    # Fallback: modify a config file (always exists)
    sed -i "1i// Bench Round ${ROUND}" "${PROJECT}/config/config.ts"
fi
echo ""

# Step 2: Run production build
echo "[Step 2: build] Running Ant Design Pro production build..."
BUILD_START=$(date +%s%N)
cd "${PROJECT}" && npm run build 2>&1 | tail -8
BUILD_END=$(date +%s%N)
BUILD_MS=$(( (BUILD_END - BUILD_START) / 1000000 ))
echo "  Build completed in ${BUILD_MS}ms"
echo ""

# Step 3: Run tests (if available)
echo "[Step 3: test] Running tests..."
TEST_START=$(date +%s%N)
cd "${PROJECT}" && npm test 2>&1 | tail -5 || echo "  Tests may not be configured in this project"
TEST_END=$(date +%s%N)
TEST_MS=$(( (TEST_END - TEST_START) / 1000000 ))
echo "  Test phase completed in ${TEST_MS}ms"
echo ""

# Step 4: Memory report
echo "[Memory Report] Current memory usage:"
free -m
echo ""
echo "=== Round ${ROUND} Complete ==="
echo "  Build time:  ${BUILD_MS}ms"
echo "  Test time:   ${TEST_MS}ms"
echo "  Target file: ${TARGET_FILE}"
echo ""
echo "To reset before next round: cd ${PROJECT} && git checkout -- src/"
