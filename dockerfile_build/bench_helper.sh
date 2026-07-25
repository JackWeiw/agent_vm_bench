#!/bin/bash
# Helper script for manual benchmark testing inside sandbox
# Usage: bash /opt/coding-bench/bench_helper.sh [round_number]
#
# This script simulates one round of the coding benchmark workflow:
# 1. Modify bench-marker.ts (triggers webpack rebuild)
# 2. Run webpack production build (memory-intensive step)
# 3. Run jest tests
# 4. Report timing and memory usage

ROUND="${1:-0}"
PROJECT="/opt/coding-bench"

echo "=== Coding Bench Helper - Round ${ROUND} ==="
echo ""

# Step 1: Modify bench-marker.ts
echo "[Step 1: edit_file] Modifying bench-marker.ts for round ${ROUND}..."
sed -i "s/export const BENCH_ROUND = .*/export const BENCH_ROUND = ${ROUND};/" "${PROJECT}/src/bench-marker.ts"
echo "  BENCH_ROUND updated to ${ROUND}"
echo ""

# Step 2: Run production build
echo "[Step 2: build] Running webpack production build..."
BUILD_START=$(date +%s%N)
cd "${PROJECT}" && npm run build 2>&1 | tail -5
BUILD_END=$(date +%s%N)
BUILD_MS=$(( (BUILD_END - BUILD_START) / 1000000 ))
echo "  Build completed in ${BUILD_MS}ms"
echo ""

# Step 3: Run tests
echo "[Step 3: test] Running jest tests..."
TEST_START=$(date +%s%N)
cd "${PROJECT}" && npm test 2>&1 | tail -10
TEST_END=$(date +%s%N)
TEST_MS=$(( (TEST_END - TEST_START) / 1000000 ))
echo "  Tests completed in ${TEST_MS}ms"
echo ""

# Step 4: Memory report
echo "[Memory Report] Current memory usage:"
free -m
echo ""
echo "=== Round ${ROUND} Complete ==="
echo "  Build time:  ${BUILD_MS}ms"
echo "  Test time:   ${TEST_MS}ms"
