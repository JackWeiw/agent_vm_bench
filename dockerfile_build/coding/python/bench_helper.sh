#!/bin/bash
# ============================================================================
# Coding Benchmark Helper (Python) — E2B Sandbox Manual Testing (django/django)
# ============================================================================
#
# Trace-faithful loop (mirrors the ts/go/java/c/cpp variants): find -> read ->
# edit -> verify -> git diff. Verify runs the instance's REAL test selection
# through django's own runner (`./tests/runtests.py --verbosity 2
# --settings=test_sqlite --parallel 1 auth_tests.test_basic`) and asserts the
# FAIL_TO_PASS + PASS_TO_PASS ids -- the project's own harness. The agent NEVER
# runs a production build, a resident dev server, or an install step.
#
# Instance: django__django-16631 (swe-bench / verified), base commit 9b224579,
# Python 3.12. Python has exactly ONE pair: the instance's two-file gold patch
# (django/contrib/auth/__init__.py + base_user.py). Every round therefore runs
# the auth test module after re-applying the patch; the untouched base commit
# (the manual baseline check) is where the FAIL_TO_PASS test fails. The baked-in
# test patch lives under tests/, which the reset never touches.
#
# The pair list lives in bench_pairs.json (generated from
# bench_core.coding_payload) so this helper never drifts from the Python
# runner's content.
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-verify        # Skip verify step
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    django project path (default: /opt/coding-bench)
#   BENCH_VERIFY_CMD     Fallback verify command (default: python3 probe)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 300)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-python3 /tmp/bench_verify.py}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-300}"
PAIRS_JSON="${PROJECT_DIR}/bench_pairs.json"
TEMP_TEST_PATH="/tmp/bench_verify.py"
TEST_CMD='./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 auth_tests.test_basic'

# Ensure django is importable in script mode (sys.path[0] is /tmp/, not CWD).
# Dockerfile ENV PYTHONPATH handles this at build time; this is the belt-and-
# suspenders runtime guard for manual testing inside the sandbox.
export PYTHONPATH="${PROJECT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

# Default verify script used only if a pair has no verify_script: a transient
# CPython process importing django's module graph.
default_verify_script='import django
from django.conf import settings

settings.configure(
    DEBUG=True,
    DATABASES={},
    INSTALLED_APPS=[],
)

import django.urls
import django.forms
import django.template

print("All tests passed!")'

# ---- Argument Parsing ----
ROUND=0
SKIP_VERIFY=false

for arg in "$@"; do
    case "${arg}" in
        --round=*)       ROUND="${arg#--round=}" ;;
        --no-verify)     SKIP_VERIFY=true ;;
        --help|-h)
            echo "Coding Benchmark Helper (Python) - django/django (trace-faithful)"
            echo ""
            echo "Loop: find -> read -> edit -> verify (runtests.py or python3 probe) -> git diff"
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
            echo "  BENCH_PROJECT_DIR     django path (default: /opt/coding-bench)"
            echo "  BENCH_VERIFY_CMD      Fallback verify command (default: python3 probe)"
            echo "  BENCH_VERIFY_TIMEOUT  Verify timeout seconds (default: 300)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply the pair's edit list (real semantic edit)"
            echo "  3: verify  - real django test selection (FTP/PTP) or python3 probe"
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
echo "  Coding Bench (Python) - django/django - Round ${ROUND}"
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

# Reset the framework tree (all edits live under django/; the baked-in test
# patch under tests/ is never reverted).
cd "${PROJECT_DIR}" && git checkout -- django/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find django -name '*.py' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        HAS_TESTS=0
        echo "  Falling back to: ${TARGET_FILE} (generic marker pair)"
        python3 - "${TARGET_FILE}" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
open(p, 'w', encoding='utf-8').write("# bench marker\n" + s)
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

# ---- Step 3: verify — real django test selection (FTP/PTP) or python3 probe ----
if [ "${SKIP_VERIFY}" = false ]; then
    VERIFY_START=$(date +%s%N)

    if [ "${HAS_TESTS}" = "1" ]; then
        # FAIL_TO_PASS / PASS_TO_PASS path: django's own test runner.
        echo "[Step 3: verify] Running the real django test selection (memory peak)..."
        RUN_CMD="${TEST_CMD}"
    else
        # Generic edit pair: transient python3 probe importing django's graph.
        echo "[Step 3: verify] Running the python3 import probe (memory peak)..."
        python3 - "${PAIRS_JSON}" "${PAIR_IDX}" "${TEMP_TEST_PATH}" "${default_verify_script}" <<'PYEOF'
import json, sys
pair = json.load(open(sys.argv[1], encoding='utf-8'))[int(sys.argv[2])]
body = pair.get("verify_script") or sys.argv[4]
open(sys.argv[3], 'w', encoding='utf-8').write(body)
PYEOF
        RUN_CMD="${VERIFY_CMD}"
    fi

    cd "${PROJECT_DIR}" && timeout "${VERIFY_TIMEOUT}" bash -c "${RUN_CMD}" > /tmp/verify_output.log 2>&1
    VERIFY_EXIT=$?
    VERIFY_END=$(date +%s%N)
    VERIFY_MS=$(( (VERIFY_END - VERIFY_START) / 1000000 ))

    tail -10 /tmp/verify_output.log

    if [ ${VERIFY_EXIT} -eq 0 ] && [ "${HAS_TESTS}" = "1" ]; then
        # Same contract as the Python runner: every listed id must have run and
        # passed (the runner's exit code alone also catches failures).
        python3 - "${PAIRS_JSON}" "${PAIR_IDX}" /tmp/verify_output.log <<'PYEOF'
import json, sys
pair = json.load(open(sys.argv[1], encoding='utf-8'))[int(sys.argv[2])]
ids = list(pair.get("fail_to_pass") or ()) + list(pair.get("pass_to_pass") or ())
text = open(sys.argv[3], encoding='utf-8', errors='replace').read()
suffixes = [
    (" ... ok", True),
    (" ... expected failure", True),
    (" ... unexpected success", False),
    (" ... FAIL", False),
    (" ... ERROR", False),
    (" ... skipped", False),
]
status = {}
for ln in text.splitlines():
    for suf, good in suffixes:
        i = ln.rfind(suf)
        if i > 0 and (ln.endswith(suf) or suf == " ... skipped"):
            status[ln[:i].rstrip()] = good
            break
for tid in ids:
    if tid not in status:
        print("NOT RUN:", tid)
        sys.exit(1)
    if not status[tid]:
        print("NOT PASSED:", tid)
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
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- django/"
