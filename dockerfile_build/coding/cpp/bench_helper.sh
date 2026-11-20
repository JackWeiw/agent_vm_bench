#!/bin/bash
# ============================================================================
# Coding Benchmark Helper (C++) — E2B Sandbox Manual Testing (nlohmann/json)
# ============================================================================
#
# Trace-faithful loop (mirrors the ts/go/python/java/c variants): find -> read ->
# edit -> verify -> git diff. Verify runs the instance's REAL doctest target
# (`cmake --build ... --target test-udt_cpp11` + the binary with `-s -r=xml`)
# and asserts the FAIL_TO_PASS + PASS_TO_PASS "test case > section" paths --
# the project's own harness. Generic pairs fall back to a CMake target rebuild
# + g++ smoke probe.
# The agent NEVER runs a clean rebuild, the full test suite, or an install step.
#
# Instance: nlohmann__json-4237 (swe_bench_multilingual), base commit 3780b41d.
# Pair 0 is a generic comment edit (warmup runs pair 0 before any edit, so the
# FAIL_TO_PASS pair must not be first); pair 1 mirrors the two-file gold patch
# (include/.../to_json.hpp + single_include/.../json.hpp) and runs the doctest
# target.
#
# Pairs live in bench_pairs.json (generated from bench_core.coding_payload) so
# this helper never drifts from the Python runner's content.
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-verify        # Skip verify step
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    nlohmann/json project path (default: /opt/coding-bench)
#   BENCH_VERIFY_CMD     Fallback verify command (default: CMake rebuild + g++ probe)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 1200)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-cd /opt/coding-bench && cmake --build build -j\$(nproc) --target test-udt_cpp11 && g++ -std=c++11 -I single_include /tmp/bench_verify.cpp -o /tmp/bench_verify && /tmp/bench_verify}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-1200}"
PAIRS_JSON="${PROJECT_DIR}/bench_pairs.json"
TEMP_TEST_PATH="/tmp/bench_verify.cpp"
DOCTEST_XML="/tmp/bench_doctest.xml"
TEST_CMD='cmake -S . -B build && cmake --build build -j$(nproc) --target test-udt_cpp11 && ./build/tests/test-udt_cpp11 -s -r=xml > /tmp/bench_doctest.xml'

# Shared default verify script (generic pairs): a standalone g++ probe against
# the project's single header. Passes on the base commit too (warmup runs pair 0
# before any edit).
default_verify_script='#include <nlohmann/json.hpp>
#include <iostream>
#include <string>

int main() {
    nlohmann::json j = {{"bench", 4237}};
    const std::string dumped = j.dump();
    if (dumped != "{\"bench\":4237}") {
        std::cerr << "unexpected: " << dumped << std::endl;
        return 1;
    }
    std::cout << "All tests passed!" << std::endl;
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
            echo "Coding Benchmark Helper (C++) - nlohmann/json (trace-faithful)"
            echo ""
            echo "Loop: find -> read -> edit -> verify (CMake + doctest XML) -> git diff"
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
            echo "  BENCH_PROJECT_DIR     nlohmann/json path (default: /opt/coding-bench)"
            echo "  BENCH_VERIFY_CMD      Fallback verify command (default: CMake + g++ probe)"
            echo "  BENCH_VERIFY_TIMEOUT  Verify timeout seconds (default: 1200)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply the pair's edit list (real semantic edit)"
            echo "  3: verify  - real doctest target (FTP/PTP) or g++ smoke probe (memory peak)"
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
echo "  Coding Bench (C++) - nlohmann/json - Round ${ROUND}"
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

# Reset header trees (all nlohmann edits live under include/ and single_include/;
# the baked-in test patch under tests/ is never reverted).
cd "${PROJECT_DIR}" && git checkout -- include/ single_include/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find include -name '*.hpp' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        HAS_TESTS=0
        echo "  Falling back to: ${TARGET_FILE} (generic marker pair)"
        python3 - "${TARGET_FILE}" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
open(p, 'w', encoding='utf-8').write("// bench marker\n" + s)
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
python3 - "${PAIRS_JSON}" "${PAIR_IDX}" "${PROJECT_DIR}" <<'PYEOF'
import json, os, sys
pairs = json.load(open(sys.argv[1], encoding='utf-8'))
pair = pairs[int(sys.argv[2])]
root = sys.argv[3]
files = {}
for e in pair["edits"]:
    p = os.path.join(root, e.get("file", pair["file"]))
    s = files.get(p)
    if s is None:
        s = open(p, encoding='utf-8').read()
    f, r = e['find'], e['replace']
    if f not in s:
        sys.exit(2)
    files[p] = s.replace(f, r, 1)
for p, s in files.items():
    open(p, 'w', encoding='utf-8').write(s)
PYEOF
EDIT_EXIT=$?
if [ ${EDIT_EXIT} -ne 0 ]; then
    echo "  ERROR: edit failed (exit ${EDIT_EXIT}; 2 = find string absent)" >&2
    exit 1
fi
echo "  Edit applied (${PAIR_IDX} of ${PAIR_COUNT})"
echo ""

# ---- Step 3: verify — real doctest target (FTP/PTP) or g++ smoke probe ----
if [ "${SKIP_VERIFY}" = false ]; then
    VERIFY_START=$(date +%s%N)

    if [ "${HAS_TESTS}" = "1" ]; then
        # FAIL_TO_PASS / PASS_TO_PASS path: the instance's own doctest target.
        echo "[Step 3: verify] Rebuilding + running the real doctest target (memory peak)..."
        rm -f "${DOCTEST_XML}"
        RUN_CMD="${TEST_CMD}"
    else
        # Generic edit pair: CMake target rebuild + ad-hoc g++ probe.
        echo "[Step 3: verify] Rebuilding the doctest target + ad-hoc g++ probe (memory peak)..."
        printf '%s\n' "${default_verify_script}" > "${TEMP_TEST_PATH}"
        RUN_CMD="${VERIFY_CMD}"
    fi

    cd "${PROJECT_DIR}" && timeout "${VERIFY_TIMEOUT}" bash -c "${RUN_CMD}" > /tmp/verify_output.log 2>&1
    VERIFY_EXIT=$?
    VERIFY_END=$(date +%s%N)
    VERIFY_MS=$(( (VERIFY_END - VERIFY_START) / 1000000 ))

    tail -10 /tmp/verify_output.log

    if [ ${VERIFY_EXIT} -eq 0 ] && [ "${HAS_TESTS}" = "1" ]; then
        # Same contract as the Python runner: every listed id must have run and
        # passed (the binary's exit code alone also catches failures).
        python3 - "${PAIRS_JSON}" "${PAIR_IDX}" "${DOCTEST_XML}" <<'PYEOF'
import json, os, sys
import xml.etree.ElementTree as ET
pair = json.load(open(sys.argv[1], encoding='utf-8'))[int(sys.argv[2])]
path = sys.argv[3]
ids = list(pair.get("fail_to_pass") or ()) + list(pair.get("pass_to_pass") or ())
if not os.path.exists(path):
    print("MISSING REPORT:", path)
    sys.exit(1)
seen, failed = set(), set()

def name(chain):
    return chain[0] + " > " + chain[-1] if len(chain) > 1 else chain[0]

def walk(el, chain):
    tag = el.tag
    if tag == "TestCase":
        chain = [el.get("name") or ""]
        if el.get("skipped") == "true":
            failed.add(name(chain))
    elif tag == "SubCase":
        chain = chain + [el.get("name") or ""]
    if tag in ("TestCase", "SubCase"):
        seen.add(name(chain))
    elif tag == "Expression" and el.get("success") == "false":
        failed.add(name(chain))
    for child in el:
        walk(child, chain)

walk(ET.parse(path).getroot(), [])
for tid in ids:
    if tid in failed:
        print("NOT PASSED:", tid)
        sys.exit(1)
    if tid not in seen:
        print("NOT RUN:", tid)
        sys.exit(1)
print("verified %d tests" % len(ids))
PYEOF
        ASSERT_EXIT=$?
        if [ ${ASSERT_EXIT} -ne 0 ]; then
            VERIFY_EXIT=1
        fi
    fi

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
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- include/ single_include/"
