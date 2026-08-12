#!/bin/bash
# ============================================================================
# Coding Benchmark Helper — E2B Sandbox Manual Testing (vuejs/core, JS)
# ============================================================================
#
# Trace-faithful loop (verified against a captured openclaw agent trajectory on
# vuejs/core): find -> read -> edit -> verify (write ad-hoc /tmp/test_*.mjs +
# `npx tsx`) -> git diff. The agent NEVER runs a production build or a resident
# dev server; verification is a transient `npx tsx` process loading the TS
# module graph (esbuild transpile + node execute) = the memory peak.
#
# Simulates a real AI coding agent workflow:
#   Step 0: find   — reset source files (git checkout) + verify target
#   Step 1: read   — inspect the target file (agent confirming context)
#   Step 2: edit   — apply a pre-configured find->replace pair (real semantic edit)
#   Step 3: verify — write an ad-hoc /tmp/bench_verify.mjs + `npx tsx` run it
#   Step 4: diff   — git diff -> patch file (agent's verification artifact)
#
# Memory pressure model:
#   Each verify run loads the vue+compiler+reactivity TS module graph transiently
#   (esbuild transpile + node execute). N sandboxes' staggered verify peaks
#   overlapping -> host memory overcommit. No resident process.
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-verify        # Skip verify step
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    vuejs/core project path (default: /opt/coding-bench)
#   BENCH_VERIFY_CMD     Verify run command (default: npx tsx /tmp/bench_verify.mjs)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 120)
#   BENCH_VERIFY_REPEAT  ts only: N independent npx tsx processes per verify (default: 3)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-npx tsx /tmp/bench_verify.mjs}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-120}"
TEMP_TEST_PATH="/tmp/bench_verify.mjs"
HEREDOC_EOF="EOF"

# ts multi-process verify: N independent npx tsx processes per verify step (raises
# steady-state CPU, the only faithful lever - see the runner _run_verify). Go has
# no equivalent (its go clean -cache cold-compile is already real load).
VERIFY_REPEAT="${BENCH_VERIFY_REPEAT:-3}"

# Shared template pool (mirrors e2b_bench/schemas.py DEFAULT_VERIFY_TEMPLATES).
# Each entry: a template (HTML passed to baseParse) + its JS assertion. Offset by
# ROUND so consecutive rounds pick different N-subsets (mirrors the agent
# rewriting its ad-hoc test per verify).
declare -a POOL_TEMPLATES=(
    '<div id="x">{{ msg }}</div>'
    '<textarea v-pre>{{ not interpolated }}</textarea>'
    '<ul><li v-for="i in list">{{ i }}</li></ul>'
    '<div><span v-if="ok">yes</span><span v-else>no</span></div>'
    '<div>a</div><div>b</div>'
    '<div :class="cls + extra" @click="onClick">text</div>'
)
declare -a POOL_ASSERTS=(
    "if (ast.children[0].tag !== 'div') throw new Error('expected div')"
    "if (ast.children[0].tag !== 'textarea') throw new Error('expected textarea')"
    "if (ast.children[0].tag !== 'ul') throw new Error('expected ul')"
    "if (ast.children[0].children.length < 2) throw new Error('expected 2 spans')"
    "if (ast.children.length < 2) throw new Error('expected 2 roots')"
    "const div = ast.children[0]; if (!div.props || !div.props.length) throw new Error('expected props')"
)

# Stamp a pool entry (by index) into a full .mjs body: 8 agent globals + compiler-core
# import + baseParse(template) + assert + print. Mirrors _stamp_verify_body.
stamp_body() {
    local idx="$1"
    cat <<BODY
globalThis.__DEV__ = true
globalThis.__BROWSER__ = false
globalThis.__COMPAT__ = false
globalThis.__ESM_BUNDLER__ = true
globalThis.__FEATURE_OPTIONS_API__ = true
globalThis.__FEATURE_PROD_DEVTOOLS__ = false
globalThis.__FEATURE_SUSPENSE__ = true
globalThis.__RUNTIME_COMPILE__ = true
import('${PROJECT_DIR}/packages/compiler-core/src/index.ts').then(m => {
  const ast = m.baseParse('${POOL_TEMPLATES[$idx]}', { parseMode: 'html' })
  ${POOL_ASSERTS[$idx]}
  console.log('All tests passed!')
})
BODY
}

# Replacement pairs for round-robin editing (verified against the vuejs/core repo).
# Each pair is a real, type-safe string edit. The verify workload comes from the
# shared POOL_TEMPLATES pool below (mirrors the runner), not a per-pair script.
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
SKIP_VERIFY=false

for arg in "$@"; do
    case "${arg}" in
        --round=*)       ROUND="${arg#--round=}" ;;
        --no-verify)     SKIP_VERIFY=true ;;
        --help|-h)
            echo "Coding Benchmark Helper - vuejs/core (trace-faithful)"
            echo ""
            echo "Loop: find -> read -> edit -> verify (npx tsx) -> git diff"
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
            echo "  BENCH_PROJECT_DIR      vuejs/core path (default: /opt/coding-bench)"
            echo "  BENCH_VERIFY_CMD       Verify run command (default: npx tsx /tmp/bench_verify.mjs)"
            echo "  BENCH_VERIFY_TIMEOUT   Verify timeout seconds (default: 120)"
            echo "  BENCH_VERIFY_REPEAT    ts only: N independent npx tsx processes per verify (default: 3)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply find->replace pair (real semantic edit)"
            echo "  3: verify  - write N /tmp/bench_verify_{i}.mjs + npx tsx xN (memory peak)"
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
echo "  Coding Bench - vuejs/core - Round ${ROUND}"
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

# ---- Step 0: find — reset + locate target ----
echo "[Step 0: find] Preparing environment..."

# Reset source files to a clean state.
# vuejs/core is a pnpm monorepo with no top-level src/ - packages/ alone covers all edits.
cd "${PROJECT_DIR}" && git checkout -- packages/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find packages \( -name '*.ts' -o -name '*.tsx' -o -name '*.js' \) 2>/dev/null | head -1)
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
# Literal str.replace (not sed regex): vuejs/core pairs contain regex
# metacharacters (., (), *) that sed treats as regex - matched only by luck.
# find/replace are passed to python3 as base64 so quoting is inert; exit 2 if
# the find string is absent (no-op edit surfaced, not a silent fake verify pass).
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

# ---- Step 3: verify — write N ad-hoc test files + run N npx tsx (memory peak) ----
if [ "${SKIP_VERIFY}" = false ]; then
    echo "[Step 3: verify] Writing ${VERIFY_REPEAT} ad-hoc tests + running npx tsx x${VERIFY_REPEAT} (memory peak)..."
    VERIFY_START=$(date +%s%N)

    # N independent npx tsx processes, each stamped from a distinct pool template
    # (offset by ROUND so consecutive rounds differ). Chained with && (fail-fast),
    # mirroring the runner's one-command N-chain.
    POOL_LEN=${#POOL_TEMPLATES[@]}
    OFFSET=$((ROUND % POOL_LEN))
    CHAIN_CMD="cd \"${PROJECT_DIR}\""
    for i in $(seq 0 $((VERIFY_REPEAT - 1))); do
        IDX=$(( (OFFSET + i) % POOL_LEN ))
        BODY=$(stamp_body "${IDX}")
        FILE_I="/tmp/bench_verify_${i}.mjs"
        # printf the body to the temp file, then run it; chain with && (fail-fast).
        CHAIN_CMD="${CHAIN_CMD} && printf '%s\n' \"${BODY}\" > \"${FILE_I}\" && npx tsx \"${FILE_I}\""
    done
    eval "timeout \"${VERIFY_TIMEOUT}\" bash -c '${CHAIN_CMD}'" > /tmp/verify_output.log 2>&1
    VERIFY_EXIT=$?
    VERIFY_END=$(date +%s%N)
    VERIFY_MS=$(( (VERIFY_END - VERIFY_START) / 1000000 ))

    tail -8 /tmp/verify_output.log
    if [ ${VERIFY_EXIT} -eq 0 ]; then
        echo "  Verify: SUCCESS (${VERIFY_MS}ms, ${VERIFY_REPEAT} processes)"
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
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- packages/"
