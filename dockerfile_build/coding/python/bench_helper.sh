#!/bin/bash
# ============================================================================
# Coding Benchmark Helper (Python) — E2B Sandbox Manual Testing (django/django)
# ============================================================================
#
# Trace-faithful loop (mirrors the captured openclaw trajectories behind the ts/go
# variants): find -> read -> edit -> verify (write ad-hoc /tmp/bench_verify.py +
# `python3`) -> git diff. A real coding agent on django verifies by writing a small
# ad-hoc Python script that imports django's module graph and running it via
# `python3` — a transient interpreter process that loads the settings/urls/forms/db
# module graph into memory (the memory peak). The agent NEVER runs a production
# `manage.py runserver` or the full `tests/runtests.py` suite.
#
# Simulates a real AI coding agent workflow:
#   Step 0: find   — reset source files (git checkout) + locate target
#   Step 1: read   — inspect the target file (agent confirming context)
#   Step 2: edit   — apply a pre-configured find->replace pair (real semantic edit)
#   Step 3: verify — write an ad-hoc /tmp/bench_verify.py + `python3` run it
#   Step 4: diff   — git diff -> patch file (agent's verification artifact)
#
# Memory pressure model:
#   Each verify run imports django's module graph (settings + urls + forms + db)
#   transiently (CPython imports + executes the module graph). N sandboxes'
#   staggered verify peaks overlapping -> host memory overcommit. No resident
#   process. Unlike go there is NO persistent compile cache to clear: Python's
#   __pycache__ holds cheap bytecode (not compiled types), so the in-memory module
#   graph — the actual peak — is unchanged whether or not the cache is warm. The
#   verify is therefore a plain single write+run (no pre_verify_cmd), matching ts.
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
#   BENCH_VERIFY_CMD     Verify run command (default: python3 /tmp/bench_verify.py)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 120)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-python3 /tmp/bench_verify.py}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-120}"
TEMP_TEST_PATH="/tmp/bench_verify.py"

# Replacement pairs for round-robin editing (verified against django/django).
# Each pair is a real, type-safe edit. The 3rd field is an optional verify_script
# body (a Python script importing django and asserting something real); empty =
# the shared default below (imports the heavy django module graph + prints
# "All tests passed!" -> real CPython import peak).
# Format: "file|find|replace|verify_script"
#
# NOTE: verify_script uses a literal \n between lines (sed-decoded below) because
# bash arrays can't carry real newlines cleanly across the | delimiter here.
TARGET_FILES=(
    "django/conf/global_settings.py|LANGUAGE_CODE = \"en-us\"|LANGUAGE_CODE = \"en-us\"  # bench round|import django\nfrom django.conf import settings\n\nsettings.configure(\n    DEBUG=True,\n    DATABASES={},\n    INSTALLED_APPS=[],\n)\n\nimport django.urls\n\nassert settings.LANGUAGE_CODE == \"en-us\", settings.LANGUAGE_CODE\nprint(\"All tests passed!\")"
    "django/db/models/fields/__init__.py|class Field(RegisterLookupMixin):|class Field(RegisterLookupMixin):  # bench||"
    "django/http/response.py|class HttpResponse:|class HttpResponse:  # bench||"
    "django/utils/text.py|def slugify(value, allow_unicode=False):|def slugify(value, allow_unicode=False):  # bench||"
    "django/template/base.py|class Template:|class Template:  # bench||"
    "django/urls/resolvers.py|class URLResolver:|class URLResolver:  # bench||"
)

# Shared default verify script (used when a pair's verify_script is empty).
# A transient CPython process importing django's module graph — the memory peak.
# It configures bare settings (no DB engine, no installed apps) and imports the
# heavy graphs (django.urls -> conf/core, django.forms -> db/models/widgets).
# Prints "All tests passed!".
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
            echo "Loop: find -> read -> edit -> verify (python3) -> git diff"
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
            echo "  BENCH_VERIFY_CMD     Verify run command (default: python3 /tmp/bench_verify.py)"
            echo "  BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 120)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply find->replace pair (real semantic edit)"
            echo "  3: verify  - write /tmp/bench_verify.py + python3 (memory peak)"
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
echo "  Coding Bench (Python) - django/django - Round ${ROUND}"
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

# Reset source files (django edits live under django/ — the framework package)
cd "${PROJECT_DIR}" && git checkout -- django/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find django \( -name '*.py' \) 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        FIND_STR="# bench marker"
        REPLACE_STR="# bench round"
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
# Literal str.replace (not sed regex): Python source pairs contain regex
# metacharacters ((), [], :, .) that break sed. find/replace are passed to
# python3 as base64 so quoting is inert; exit 2 if the find string is absent
# (no-op edit surfaced, not a silent fake verify pass).
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

# ---- Step 3: verify — write ad-hoc test file + run via python3 ----
if [ "${SKIP_VERIFY}" = false ]; then
    echo "[Step 3: verify] Writing ad-hoc test + running python3 (memory peak)..."
    VERIFY_START=$(date +%s%N)

    # Resolve the script body: pair-specific (decode \n to real newlines), else shared default
    if [ -n "${VERIFY_SCRIPT}" ]; then
        SCRIPT_BODY=$(printf '%b' "${VERIFY_SCRIPT}")
    else
        SCRIPT_BODY="${default_verify_script}"
    fi

    # Write the temp test file (printf handles multi-line script bodies safely).
    printf '%s\n' "${SCRIPT_BODY}" > "${TEMP_TEST_PATH}"

    # Run the ad-hoc test via `python3`. cwd is /opt/coding-bench so `import django`
    # resolves the cloned source; asgiref/sqlparse come from the pip install.
    # No cache clear needed: Python's __pycache__ holds cheap bytecode, not a
    # persistent compile cache — the in-memory module graph (the actual peak) is
    # unchanged warm or cold (see header comment).
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
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- django/"
