#!/bin/bash
# ============================================================================
# Coding Benchmark Helper (Java) — E2B Sandbox Manual Testing (google/gson)
# ============================================================================
#
# Trace-faithful loop (mirrors the ts/go/python variants): find -> read -> edit
# -> verify -> git diff. Verify runs the instance's REAL Maven tests
# (FAIL_TO_PASS; PASS_TO_PASS when an instance declares any) through
# `mvn -o test` -- the project's own JUnit harness. The agent NEVER runs
# `mvn package` or the full module suite.
#
# Instance: google__gson-1014 (swe_bench_multilingual), base commit 7719e73a.
# Loop 0 is a generic class-line edit (warmup runs pair 0 before any edit, so
# the FAIL_TO_PASS pair must not be first); loop 1 mirrors the gold patch and
# runs the instance's Fail-to-Pass test class.
#
# Usage:
#   bash bench_helper.sh                    # Round 0, all steps
#   bash bench_helper.sh 3                  # Round 3, all steps
#   bash bench_helper.sh --round=5          # Round 5
#   bash bench_helper.sh --no-verify        # Skip verify step
#   bash bench_helper.sh --help             # Show help
#
# Environment overrides:
#   BENCH_PROJECT_DIR    gson project path (default: /opt/coding-bench)
#   BENCH_VERIFY_CMD     Verify run command (default: mvn + javac + java)
#   BENCH_VERIFY_TIMEOUT Verify timeout seconds (default: 300)
# ============================================================================

# ---- Configuration (override via environment variables for extensibility) ----
PROJECT_DIR="${BENCH_PROJECT_DIR:-/opt/coding-bench}"
VERIFY_CMD="${BENCH_VERIFY_CMD:-cd /opt/coding-bench && mvn -o -q -B -pl gson -DskipTests -Djava.version=8 compile && javac -cp gson/target/classes /tmp/BenchVerify.java && java -cp gson/target/classes:/tmp BenchVerify}"
VERIFY_TIMEOUT="${BENCH_VERIFY_TIMEOUT:-300}"
TEMP_TEST_PATH="/tmp/BenchVerify.java"

# Replacement pairs for round-robin editing (verified against google/gson at
# 7719e73a). Format: "file|find|replace|test_classes" — find/replace use a
# literal \n between lines (decoded with printf '%b' below); test_classes is a
# comma-separated Maven -Dtest selection (empty = the shared smoke verify).
TARGET_FILES=(
    "gson/src/main/java/com/google/gson/JsonParser.java|public final class JsonParser {|public final class JsonParser { // bench|"
    "gson/src/main/java/com/google/gson/internal/bind/JsonTreeReader.java|      popStack();\n      pathNames[stackSize - 1] = \"null\";\n    }\n    pathIndices[stackSize - 1]++;|      popStack();\n      if (stackSize > 0) {\n        pathNames[stackSize - 1] = \"null\";\n      }\n    }\n    if (stackSize > 0) {\n      pathIndices[stackSize - 1]++;\n    }|JsonTreeReaderTest"
    "gson/src/main/java/com/google/gson/Gson.java|public final class Gson {|public final class Gson { // bench|"
    "gson/src/main/java/com/google/gson/GsonBuilder.java|public final class GsonBuilder {|public final class GsonBuilder { // bench|"
    "gson/src/main/java/com/google/gson/stream/JsonReader.java|public class JsonReader implements Closeable {|public class JsonReader implements Closeable { // bench|"
    "gson/src/main/java/com/google/gson/internal/Streams.java|public final class Streams {|public final class Streams { // bench|"
)

# Shared default verify script, used whenever a pair's 4th field (test_classes)
# is empty. A plain gson round-trip against the freshly rebuilt module — passes
# on the base commit too (warmup runs pair 0 before any edit).
default_verify_script='import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

public class BenchVerify {
  public static void main(String[] args) {
    Gson gson = new GsonBuilder().serializeNulls().create();
    String json = gson.toJson(new int[] {1, 2, 3});
    if (!"[1,2,3]".equals(json)) {
      throw new AssertionError("unexpected json: " + json);
    }
    JsonObject parsed = new JsonParser().parse("{\"a\":1}").getAsJsonObject();
    if (parsed.get("a").getAsInt() != 1) {
      throw new AssertionError("parse failed: " + parsed);
    }
    System.out.println("All tests passed!");
  }
}'

# ---- Argument Parsing ----
ROUND=0
SKIP_VERIFY=false

for arg in "$@"; do
    case "${arg}" in
        --round=*)       ROUND="${arg#--round=}" ;;
        --no-verify)     SKIP_VERIFY=true ;;
        --help|-h)
            echo "Coding Benchmark Helper (Java) - google/gson (trace-faithful)"
            echo ""
            echo "Loop: find -> read -> edit -> verify (mvn compile + javac + java) -> git diff"
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
            echo "  BENCH_PROJECT_DIR     gson path (default: /opt/coding-bench)"
            echo "  BENCH_VERIFY_CMD      Verify run command (default: mvn + javac + java)"
            echo "  BENCH_VERIFY_TIMEOUT  Verify timeout seconds (default: 300)"
            echo ""
            echo "Workflow steps per round:"
            echo "  0: find    - git checkout reset + verify/locate target file"
            echo "  1: read    - inspect target file (head -20)"
            echo "  2: edit    - apply find->replace pair (real semantic edit)"
            echo "  3: verify  - real mvn tests (FTP/PTP) or smoke probe (memory peak)"
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
echo "  Coding Bench (Java) - google/gson - Round ${ROUND}"
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
PAIR_TESTS="${REST#*|}"

# Decode the literal \n escapes carried in the bash array.
FIND_STR=$(printf '%b' "${FIND_STR}")
REPLACE_STR=$(printf '%b' "${REPLACE_STR}")

# ---- Step 0: find — reset + locate target ----
echo "[Step 0: find] Preparing environment..."

# Reset source files (all java edits live under gson/src/main/java)
cd "${PROJECT_DIR}" && git checkout -- gson/src/main/java/ 2>/dev/null || echo "  WARNING: git checkout failed (not a git repo or no changes)"

if [ ! -f "${PROJECT_DIR}/${TARGET_FILE}" ]; then
    echo "  WARNING: target not found: ${TARGET_FILE}"
    FOUND_FILE=$(cd "${PROJECT_DIR}" && find gson/src/main/java -name '*.java' 2>/dev/null | head -1)
    if [ -n "${FOUND_FILE}" ]; then
        TARGET_FILE="${FOUND_FILE}"
        FIND_STR="// bench marker"
        REPLACE_STR="// bench round
// bench marker"
        PAIR_TESTS=""
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
# Literal str.replace (not sed regex): Java source pairs contain regex
# metacharacters ((), [], {}) that break sed. find/replace are passed to
# python3 as base64 so quoting/newlines are inert; exit 2 if the find string is
# absent (no-op edit surfaced, not a silent fake verify pass).
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

# ---- Step 3: verify — real Maven tests (FTP/PTP) or smoke probe ----
if [ "${SKIP_VERIFY}" = false ]; then
    VERIFY_START=$(date +%s%N)

    if [ -n "${PAIR_TESTS}" ]; then
        # FAIL_TO_PASS / PASS_TO_PASS path: the instance's own JUnit tests.
        echo "[Step 3: verify] Running real Maven tests: ${PAIR_TESTS} (memory peak)..."
        RUN_CMD="mvn -o -q -B -pl gson -Djava.version=8 -Dtest=${PAIR_TESTS} -DfailIfNoTests=false test"
    else
        # Generic edit pair: rebuild + ad-hoc smoke probe.
        echo "[Step 3: verify] Rebuilding gson + ad-hoc smoke probe (memory peak)..."
        printf '%s\n' "${default_verify_script}" > "${TEMP_TEST_PATH}"
        RUN_CMD="${VERIFY_CMD}"
    fi

    cd "${PROJECT_DIR}" && timeout "${VERIFY_TIMEOUT}" bash -c "${RUN_CMD}" > /tmp/verify_output.log 2>&1
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
echo "  Reset only:  cd ${PROJECT_DIR} && git checkout -- gson/src/main/java/"
