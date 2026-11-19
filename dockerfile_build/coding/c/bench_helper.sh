#!/bin/bash
# ============================================================================
# Coding Benchmark Helper (C) — E2B Sandbox Manual Testing (redis/redis)
# ============================================================================
#
# Trace-faithful loop (mirrors the ts/go/python/java variants): find -> read ->
# edit -> verify -> git diff. Verify runs the instance's REAL Tcl suite
# (FAIL_TO_PASS + PASS_TO_PASS) through `make` + `./runtest` -- the project's own
# harness. Generic pairs fall back to a Makefile rebuild + gcc smoke probe.
# The agent NEVER runs `make distclean`, the full test suite, or an install
# step.
#
# Instance: redis__redis-13338 (swe_bench_multilingual), base commit 94b9072e.
# Pair 0 is a generic include-line edit (warmup runs pair 0 before any edit, so
# the FAIL_TO_PASS pair must not be first); pair 1 mirrors the three-hunk gold
# patch and runs the stream-cgroups unit file.
#
# Pairs live in bench_pairs.json (copied from bench_core.coding_payload) so this
# helper never drifts from the Python runner's content.
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-verify        # Skip verify step
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    redis project path (default: /opt/coding-bench)
#   BENCH_VERIFY_CMD     Fallback verify command (default: make + gcc probe)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 600)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-cd /opt/coding-bench && make -s -j\$(nproc) && gcc -O2 /tmp/bench_verify.c -o /tmp/bench_verify && /tmp/bench_verify}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-600}"
PAIRS_JSON="${PROJECT_DIR}/bench_pairs.json"
TEMP_TEST_PATH="/tmp/bench_verify.c"
TEST_CMD='make -j$(nproc) && TERM=dumb ./runtest --clients 1 --durable --single unit/type/stream-cgroups'

# Shared default verify script (generic pairs): a standalone gcc probe. Passes
# on the base commit too (warmup runs pair 0 before any edit).
default_verify_script='#include <stdio.h>
#include <string.h>

int main(void) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%s-%d", "bench", 13338);
    if (strcmp(buf, "bench-13338") != 0) {
        fprintf(stderr, "unexpected: %s\n", buf);
        return 1;
    }
    printf("All tests passed!\n");
    return 0;
}'

# ---- Argument Parsing ----
ROUND=0
SKIP_VERIFY=false

for arg in "$@"; do
    case "${arg}" in
        --round=*)       ROUND="${arg#--round=}" ;;
        --no-verify)     SKIP_VERIFY=true ;;
        --help|-h)
            echo "Coding Benchmark Helper (C) - redis/redis (trace-faithful)"
            echo ""
            echo "Loop: find -> read -> edit -> verify (make + ./runtest) -> git diff"
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
            echo "  BENCH_PROJECT_DIR     redis path (default: /opt/coding-bench)"
            echo "  BENCH_VERIFY_CMD      Fallback verify command (default: make + gcc probe)"
            echo "  BENCH_VERIFY_TIMEOUT  Verify timeout seconds (default: 600)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply the pair's edit list (real semantic edit)"
            echo "  3: verify  - real Tcl suite (FTP/PTP) or gcc smoke probe (memory peak)"
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

if [ ! -f "${PAIRS_JSON}" ]; then
    echo "ERROR: pair file not found: ${PAIRS_JSON}" >&2
    exit 1
fi

# ---- Banner ----
echo "============================================"
echo "  Coding Bench (C) - redis/redis - Round ${ROUND}"
echo "============================================"
echo ""

# ---- Resolve the pair for this round ----
PAIR_COUNT=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "${PAIRS_JSON}")
PAIR_IDX=$((ROUND % PAIR_COUNT))
PAIR_INFO=$(python3 -c 'import json,sys; p=json.load(open(sys.argv[1]))[int(sys.argv[2])]; print(p["file"]); print(1 if p["tests"] else 0)' "${PAIRS_JSON}" "${PAIR_IDX}")
TARGET_FILE=$(printf '%s\n' "${PAIR_INFO}" | sed -n '1p')
HAS_TESTS=$(printf '%s\n' "${PAIR_INFO}" | sed -n '2p')

# ---- Step 0: find — reset + locate target ----
echo "[Step 0: find] Preparing environment..."

# Reset source files (all redis edits live under src/)
cd "${PROJECT_DIR}" && git checkout -- src/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find src -name '*.c' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        HAS_TESTS=0
        echo "  Falling back to: ${TARGET_FILE} (generic marker pair)"
        python3 - "${TARGET_FILE}" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
open(p, 'w', encoding='utf-8').write("/* bench marker */\n" + s)
PYEOF
        echo "  Marked ${TARGET_FILE}"
        echo ""
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

# ---- Step 2: edit — apply the pair's edit list (literal, via python3) ----
echo "[Step 2: edit] Applying replacement..."
python3 - "${PAIRS_JSON}" "${PAIR_IDX}" "${TARGET_FILE}" <<'PYEOF'
import json, sys
pairs = json.load(open(sys.argv[1], encoding='utf-8'))
edits = pairs[int(sys.argv[2])]["edits"]
p = sys.argv[3]
s = open(p, encoding='utf-8').read()
for e in edits:
    f, r = e['find'], e['replace']
    if f not in s:
        sys.exit(2)
    s = s.replace(f, r, 1)
open(p, 'w', encoding='utf-8').write(s)
PYEOF
EDIT_EXIT=$?
if [ ${EDIT_EXIT} -ne 0 ]; then
    echo "  ERROR: edit failed (exit ${EDIT_EXIT}; 2 = find string absent)" >&2
    exit 1
fi
echo "  Edit applied (${PAIR_IDX} of ${PAIR_COUNT})"
echo ""

# ---- Step 3: verify — real Tcl tests (FTP/PTP) or gcc smoke probe ----
if [ "${SKIP_VERIFY}" = false ]; then
    VERIFY_START=$(date +%s%N)

    if [ "${HAS_TESTS}" = "1" ]; then
        # FAIL_TO_PASS / PASS_TO_PASS path: the instance's own Tcl suite.
        echo "[Step 3: verify] Rebuilding + running the real Tcl suite (memory peak)..."
        RUN_CMD="${TEST_CMD}"
    else
        # Generic edit pair: Makefile rebuild + ad-hoc gcc probe.
        echo "[Step 3: verify] Rebuilding redis + ad-hoc gcc probe (memory peak)..."
        printf '%s\n' "${default_verify_script}" > "${TEMP_TEST_PATH}"
        RUN_CMD="${VERIFY_CMD}"
    fi

    cd "${PROJECT_DIR}" && timeout "${VERIFY_TIMEOUT}" bash -c "${RUN_CMD}" > /tmp/verify_output.log 2>&1
    VERIFY_EXIT=$?
    VERIFY_END=$(date +%s%N)
    VERIFY_MS=$(( (VERIFY_END - VERIFY_START) / 1000000 ))

    tail -10 /tmp/verify_output.log
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
echo "  Pair:        ${PAIR_IDX} of ${PAIR_COUNT} (tests: ${HAS_TESTS})"
echo "  Verify:      ${VERIFY_MS}ms (exit: ${VERIFY_EXIT})"
echo "  Patch:       /tmp/bench_round_${ROUND}.patch (${PATCH_LINES} lines)"
echo "============================================"
echo ""
echo "  Next round:  bash ${PROJECT_DIR}/bench_helper.sh $((ROUND + 1))"
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- src/"
