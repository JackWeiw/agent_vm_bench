"""Host-agnostic coding benchmark payload (replacement pairs + verify content).

The coding workflow loop (find -> read -> edit -> verify -> diff) is identical
across sandbox backends; only the per-language verify mechanics differ. This
module holds the benchmark *content* -- the real replacement pairs (vuejs/core,
gohugoio/hugo, django/django from swe_bench_multilingual), the shared verify
template pool, and the per-language :class:`CodingLanguageProfile` registry.

This is benchmark content, not provider plumbing: the same pairs verify the
same way on e2b, docker, or kata. It lives in the kernel so provider packages
depend on the kernel (not the reverse). The metric machinery stays in
:mod:`bench_core.schemas`; the command-issuing runners live in
:mod:`bench_core.coding_task_runner`.

Adding a language (cpp, rust, ...): one profile entry + one
``DEFAULT_CODING_<LANG>_SOURCE_FILES`` list + a default verify script here --
no runner changes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodingLanguageProfile:
    """Per-language profile for the coding verify step.

    Fields:
        temp_test_path: where the ad-hoc test file is written (/tmp/...).
        heredoc_eof: heredoc terminator string ("EOF"/"GOEOF"/"PYEOF"/"JAVAEOF").
        run_cmd: the verify run command (npx tsx ... / go run ... / python3 ...).
        source_find_names: -name patterns for the find fallback.
        source_find_root: directory the find fallback searches under.
        checkout_paths: paths reset by ``git checkout --`` in the find step.
        default_verify_script: shared default body for pairs without their own.
        pre_verify_cmd: optional command run before the verify write+run (empty
            for languages with no persistent compile cache). Set for go to
            ``go clean -cache`` so every verify is a real cold-compile: the Go
            toolchain caches compiled stdlib/packages under GOCACHE, so the
            first ``go run`` pays the full compile (40% CPU) and every later
            run hits cache (10%) -- which would NOT reflect the real agent's
            per-verify rewrite+recompile shape.
        project_marker: file whose presence proves the project was baked into
            the image (checked by warmup before the initial verify).
    """

    temp_test_path: str
    heredoc_eof: str
    run_cmd: str
    source_find_names: tuple = ()
    source_find_root: str = "packages"
    checkout_paths: str = ""
    default_verify_script: str = ""
    pre_verify_cmd: str = ""
    project_marker: str = "package.json"
    # Build-tool module whose real test suite the FAIL_TO_PASS / PASS_TO_PASS
    # pairs run (Maven `-pl <module>`). Empty = the language verifies through
    # ``default_verify_script`` / ``run_cmd`` instead (ts/go/python today).
    test_module: str = ""
    # Extra flags for the test-suite run (java: ``-Djava.version=8`` overrides
    # gson's <java.version>1.6</java.version>, which javac 8+ rejects).
    test_args: str = ""
    # Real test-suite command for languages whose suite is not a Maven module
    # (c/redis: ``make -j$(nproc) && ./runtest --single unit/type/stream-cgroups``;
    # cpp/nlohmann: ``cmake --build ... --target test-udt_cpp11`` + the binary
    # with ``-r=xml``). Run after ``cd project_dir``; empty = language has no
    # suite path (ts/go/python verify through the smoke script only). See
    # _run_test_suite_verify.
    test_suite_cmd: str = ""
    # How the test-suite command reports results. Empty = Tcl harness log
    # (``[ok]: <name>`` lines, c/redis). ``doctest_xml`` = a doctest XML report
    # written by the command itself (cpp/nlohmann); the runner parses
    # TestCase/SubCase paths and fails on any non-passing or missing id.
    test_report_format: str = ""


def _find_name_clause(names: tuple) -> str:
    """Build a ``find -name`` clause: ``\\( -name '*.ts' -o -name '*.go' \\)``."""
    if not names:
        return "-name '*.ts'"
    inner = " -o ".join(f"-name '{n}'" for n in names)
    return f"\\( {inner} \\)" if len(names) > 1 else f"-name '{names[0]}'"


# --- ts: vuejs/core replacement pairs -------------------------------------------------
# Each pair owns only the EDIT semantics: {file, find, replace} -- a real,
# type-safe string edit to a verified vuejs/core file. The VERIFY workload
# (what templates stress the parser) is owned separately by
# DEFAULT_VERIFY_TEMPLATES. This decouples pair count, template count, and N
# (verify_repeat); maintenance touches one pool, not N yaml blocks.
DEFAULT_CODING_SOURCE_FILES: list[dict[str, str]] = [
    {
        "file": "packages/shared/src/general.ts",
        "find": "export const NOOP = (): void => {}",
        "replace": "export const NOOP = (): void => undefined",
    },
    {
        "file": "packages/shared/src/general.ts",
        "find": "Always return false.",
        "replace": "Always returns false.",
    },
    {
        "file": "packages/shared/src/index.ts",
        "find": "export * from './general'",
        "replace": "export * from './general' // bench round",
    },
    {
        "file": "packages/vue/src/index.ts",
        "find": '// This entry is the "full-build"',
        "replace": '// This entry is the "full-build" (bench)',
    },
    {
        "file": "packages/reactivity/src/baseHandlers.ts",
        "find": "export const mutableHandlers: ProxyHandler<object> =",
        "replace": "export const mutableHandlers: ProxyHandler<object> = // bench",
    },
    {
        "file": "packages/runtime-core/src/errorHandling.ts",
        "find": "import { EMPTY_OBJ, isArray, isFunction, isPromise } from '@vue/shared'",
        "replace": "import { EMPTY_OBJ, isArray, isFunction, isPromise } from '@vue/shared' // bench",
    },
]


# Single-template skeleton: the 8 verbatim agent globals + compiler-core import +
# baseParse. Stamped with one {template, assert} entry from DEFAULT_VERIFY_TEMPLATES
# to produce a full ad-hoc test body. __TEST__ is intentionally NOT injected; the
# real openclaw agent didn't either. compiler-core alone avoids the __TEST__
# reference path that the vue/runtime-core/compiler-dom/compiler-sfc graphs reach
# and crash on under a bare `npx tsx`.
_CODING_VERIFY_GLOBALS = (
    "globalThis.__DEV__ = true\n"
    "globalThis.__BROWSER__ = false\n"
    "globalThis.__COMPAT__ = false\n"
    "globalThis.__ESM_BUNDLER__ = true\n"
    "globalThis.__FEATURE_OPTIONS_API__ = true\n"
    "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
    "globalThis.__FEATURE_SUSPENSE__ = true\n"
    "globalThis.__RUNTIME_COMPILE__ = true\n"
)
# Relative path (within the coding project repo) to the compiler-core entry the
# real openclaw agent imported to verify vuejs/core edits. Stamped under
# config.coding_project_dir at verify time so the JS import() resolves against
# the same tree the shell cd'd into -- not a hardcoded path that would silently
# test the wrong tree if coding_project_dir is changed.
_CODING_VERIFY_IMPORT_REL = "packages/compiler-core/src/index.ts"


def _stamp_verify_body(project_dir: str, template: str, assert_code: str) -> str:
    """Stamp a {template, assert} pair into a full ad-hoc verify .mjs body.

    8 agent globals + compiler-core import + baseParse(template) + assert_code +
    print. The compiler-core import() path is anchored at ``project_dir`` (i.e.
    config.coding_project_dir) so the JS import resolves against the same tree
    the shell cd'd into, not a hardcoded path. Each body is a self-contained
    ad-hoc test (mirrors the real openclaw agent's /tmp/test_*.mjs).
    """
    import_path = f"{project_dir}/{_CODING_VERIFY_IMPORT_REL}"
    return (
        _CODING_VERIFY_GLOBALS
        + f"import({import_path!r}).then(m => {{\n"
        + f"  const ast = m.baseParse({template!r}, {{ parseMode: 'html' }})\n"
        f"  {assert_code}\n"
        "  console.log('All tests passed!')\n"
        "})\n"
    )


# Shared, ordered template pool for the ts multi-process verify step. Each entry is
# a {template, assert} pair: template = the HTML string passed to baseParse, assert
# = a JS snippet asserting the parsed AST (throws on mismatch). The runner picks N
# entries from this pool per verify step (offset by round_id % pool_len) so
# consecutive rounds don't repeat identical bytes (mirrors the agent rewriting its
# ad-hoc test per verify). All 6 are the compiler-core baseParse cases already
# sandbox-verified.
DEFAULT_VERIFY_TEMPLATES: list[dict[str, str]] = [
    {
        "template": '<div id="x">{{ msg }}</div>',
        "assert": "if (ast.children[0].tag !== 'div') throw new Error('expected div')",
    },
    {
        "template": "<textarea v-pre>{{ not interpolated }}</textarea>",
        "assert": "if (ast.children[0].tag !== 'textarea') throw new Error('expected textarea')",
    },
    {
        "template": '<ul><li v-for="i in list">{{ i }}</li></ul>',
        "assert": "if (ast.children[0].tag !== 'ul') throw new Error('expected ul')",
    },
    {
        "template": '<div><span v-if="ok">yes</span><span v-else>no</span></div>',
        "assert": "if (ast.children[0].children.length < 2) throw new Error('expected 2 spans')",
    },
    {
        "template": "<div>a</div><div>b</div>",
        "assert": "if (ast.children.length < 2) throw new Error('expected 2 roots')",
    },
    {
        "template": '<div :class="cls + extra" @click="onClick">text</div>',
        "assert": (
            "const div = ast.children[0]; " "if (!div.props || !div.props.length) throw new Error('expected props')"
        ),
    },
]

# Back-compat: the shared default body = pool[0] stamped against the default
# project dir. Callers that don't use the multi-process pool still get a valid
# single compiler-core baseParse body (byte-stable: default dir = /opt/coding-bench).
DEFAULT_CODING_VERIFY_SCRIPT_JS = _stamp_verify_body(
    "/opt/coding-bench",
    DEFAULT_VERIFY_TEMPLATES[0]["template"],
    DEFAULT_VERIFY_TEMPLATES[0]["assert"],
)


# --- go: gohugoio/hugo replacement pairs ----------------------------------------------
# Real swe_bench_multilingual instance gohugoio__hugo-12768 (GitHub Alert
# case-insensitivity). The first pair mirrors the gold patch (adds (?i) to the
# alert regex); its verify_script is a standalone `package main` exercising
# case-insensitive alert matching. Pairs without verify_script fall back to the
# shared Go default.
DEFAULT_CODING_GO_SOURCE_FILES: list[dict[str, str]] = [
    {
        "file": "markup/goldmark/blockquotes/blockquotes.go",
        "find": "var gitHubAlertRe = regexp.MustCompile(`^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)",
        "replace": "var gitHubAlertRe = regexp.MustCompile(`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)",
        "verify_script": (
            "package main\n"
            "\n"
            "import (\n"
            '\t"fmt"\n'
            '\t"regexp"\n'
            ")\n"
            "\n"
            "var gitHubAlertRe = regexp.MustCompile(`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)\n"
            "\n"
            "func main() {\n"
            "\tcases := []struct{ in string; want bool }{\n"
            "\t\t{`<p>[!NOTE]`, true},\n"
            "\t\t{`<p>[!note]`, true},\n"
            "\t\t{`<p>[!Tip]`, true},\n"
            "\t\t{`<p>[!warning]`, true},\n"
            "\t\t{`<p>[!X]`, false},\n"
            "\t}\n"
            "\tok := true\n"
            "\tfor _, c := range cases {\n"
            "\t\tif gitHubAlertRe.MatchString(c.in) != c.want {\n"
            "\t\t\tok = false\n"
            "\t\t}\n"
            "\t}\n"
            "\tif ok {\n"
            '\t\tfmt.Println("All tests passed!")\n'
            "\t} else {\n"
            '\t\tfmt.Println("Some tests failed!")\n'
            "\t}\n"
            "}\n"
        ),
    },
    # Additional safe comment-append edits across hugo markup source -- each
    # triggers a `go run` verify peak without risking a broken edit. Pairs
    # without verify_script fall back to the shared Go default.
    {
        "file": "markup/goldmark/blockquotes/blockquotes.go",
        "find": "// resolveGitHubAlert returns one of note, tip, warning, important or caution.",
        "replace": "// resolveGitHubAlert returns one of note, tip, warning, important or caution. // bench",
    },
    {
        "file": "markup/goldmark/blockquotes/blockquotes.go",
        "find": "// An empty string if no match.",
        "replace": "// An empty string if no match. // bench",
    },
    {
        "file": "markup/goldmark/blockquotes/blockquotes.go",
        "find": "// https://docs.github.com/en/get-started/writing-on-github",
        "replace": "// https://docs.github.com/en/get-started/writing-on-github // bench",
    },
    {
        "file": "markup/goldmark/blockquotes/blockquotes.go",
        "find": "// Five types:",
        "replace": "// Five types: // bench",
    },
    {
        "file": "markup/goldmark/blockquotes/blockquotes.go",
        "find": "// [!NOTE], [!TIP], [!WARNING], [!IMPORTANT], [!CAUTION]",
        "replace": "// [!NOTE], [!TIP], [!WARNING], [!IMPORTANT], [!CAUTION] // bench",
    },
]


# Shared default verify-script body for Go pairs without their own verify_script.
# A standalone `package main` that compiles + runs (real Go compiler peak) and
# prints "All tests passed!". Imports only stdlib so it compiles without the
# hugo module graph, but still loads the compiler/types for the imported packages.
DEFAULT_CODING_VERIFY_SCRIPT_GO = (
    "package main\n" "\n" 'import "fmt"\n' "\n" "func main() {\n" '\tfmt.Println("All tests passed!")\n' "}\n"
)


# --- python: django/django replacement pair -------------------------------------------
# django (github.com/django/django) is the repo of swe-bench instance
# django__django-16631. Python has exactly ONE pair: the instance's gold patch
# (fall back to settings.SECRET_KEY_FALLBACKS when validating a session auth
# hash), carrying the real FAIL_TO_PASS / PASS_TO_PASS selection which the runner
# executes through django's own test runner (see ``test_suite_cmd`` in the python
# profile).
#
# It is also ``source_files[0]`` (the only pair). Fixed-mode warmup runs it on
# the untouched base commit, where the test suite is expected to fail -- that red
# run is the FAIL_TO_PASS baseline; warmup only logs a warning and never records
# metrics. Round-robin (the default) applies the patch first, so every round
# verifies green.
#
# The patch touches two files, so every edit names its own ``file``; the runner
# applies the whole set atomically (a missing hunk aborts with no file written).
DEFAULT_CODING_PY_SOURCE_FILES: list[dict] = [
    {
        # Gold patch (django__django-16631): when the current SECRET_KEY does
        # not verify a session's auth hash, try SECRET_KEY_FALLBACKS and
        # re-hash the session with the current secret instead of flushing it.
        "file": "django/contrib/auth/__init__.py",
        "edits": [
            {
                "file": "django/contrib/auth/__init__.py",
                "find": (
                    "                session_hash_verified = session_hash and constant_time_compare(\n"
                    "                    session_hash, user.get_session_auth_hash()\n"
                    "                )\n"
                    "                if not session_hash_verified:\n"
                    "                    request.session.flush()\n"
                    "                    user = None"
                ),
                "replace": (
                    "                if not session_hash:\n"
                    "                    session_hash_verified = False\n"
                    "                else:\n"
                    "                    session_auth_hash = user.get_session_auth_hash()\n"
                    "                    session_hash_verified = constant_time_compare(\n"
                    "                        session_hash, session_auth_hash\n"
                    "                    )\n"
                    "                if not session_hash_verified:\n"
                    "                    # If the current secret does not verify the session, try\n"
                    "                    # with the fallback secrets and stop when a matching one is\n"
                    "                    # found.\n"
                    "                    if session_hash and any(\n"
                    "                        constant_time_compare(session_hash, fallback_auth_hash)\n"
                    "                        for fallback_auth_hash in user.get_session_auth_fallback_hash()\n"
                    "                    ):\n"
                    "                        request.session.cycle_key()\n"
                    "                        request.session[HASH_SESSION_KEY] = session_auth_hash\n"
                    "                    else:\n"
                    "                        request.session.flush()\n"
                    "                        user = None"
                ),
            },
            {
                "file": "django/contrib/auth/base_user.py",
                "find": "from django.contrib.auth import password_validation\n",
                "replace": "from django.conf import settings\nfrom django.contrib.auth import password_validation\n",
            },
            {
                "file": "django/contrib/auth/base_user.py",
                "find": (
                    '        key_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"\n'
                    "        return salted_hmac(\n"
                    "            key_salt,\n"
                    "            self.password,\n"
                    '            algorithm="sha256",\n'
                    "        ).hexdigest()"
                ),
                "replace": (
                    "        return self._get_session_auth_hash()\n"
                    "\n"
                    "    def get_session_auth_fallback_hash(self):\n"
                    "        for fallback_secret in settings.SECRET_KEY_FALLBACKS:\n"
                    "            yield self._get_session_auth_hash(secret=fallback_secret)\n"
                    "\n"
                    "    def _get_session_auth_hash(self, secret=None):\n"
                    '        key_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"\n'
                    "        return salted_hmac(\n"
                    "            key_salt,\n"
                    "            self.password,\n"
                    "            secret=secret,\n"
                    '            algorithm="sha256",\n'
                    "        ).hexdigest()"
                ),
            },
        ],
        "tests": True,
        "fail_to_pass": [
            "test_get_user_fallback_secret (auth_tests.test_basic.TestGetUser.test_get_user_fallback_secret)",
        ],
        "pass_to_pass": [
            "test_get_user (auth_tests.test_basic.TestGetUser.test_get_user)",
            "test_get_user_anonymous (auth_tests.test_basic.TestGetUser.test_get_user_anonymous)",
            "The current user model can be retrieved",
            "Check the creation and properties of a superuser",
            "test_superuser_no_email_or_password (auth_tests.test_basic.BasicTestCase.test_superuser_no_email_or_password)",
            "The current user model can be swapped out for another",
            "The alternate user setting must point to something in the format app.model",
            "The current user model must point to an installed model",
            "test_unicode_username (auth_tests.test_basic.BasicTestCase.test_unicode_username)",
            "Users can be created and can set their password",
            "Users can be created without an email",
            "Default User model verbose names are translatable (#19945)",
        ],
    },
]


# Shared default verify-script body for Python pairs without their own
# verify_script. A transient CPython process importing django's module graph --
# the memory peak (the same role `go run`'s compiler and `npx tsx`'s node+esbuild
# play in the go/ts variants).
DEFAULT_CODING_VERIFY_SCRIPT_PY = (
    "import django\n"
    "from django.conf import settings\n"
    "\n"
    "settings.configure(\n"
    "    DEBUG=True,\n"
    "    DATABASES={},\n"
    "    INSTALLED_APPS=[],\n"
    ")\n"
    "\n"
    "import django.urls\n"
    "import django.forms\n"
    "import django.template\n"
    "\n"
    'print("All tests passed!")\n'
)


# --- java: google/gson replacement pairs ----------------------------------------------
# Real swe_bench_multilingual instance google__gson-1014 (JsonTreeReader.skipValue
# throws ArrayIndexOutOfBoundsException on an empty root object). gson at the
# instance base_commit (7719e73a) is a Maven reactor with a single `gson` module
# (sources under gson/src/main/java).
#
# Pairs[0] is a generic class-line edit ON PURPOSE: warmup verifies
# ``source_files[0]`` BEFORE any edit is applied, so a pair carrying the
# FAIL_TO_PASS tests must not be first (the base commit is buggy). Pairs[1]
# mirrors the gold patch and carries the instance's FAIL_TO_PASS selection,
# which the runner executes as a REAL Maven test run (see ``test_module`` in the
# java profile) plus a surefire-report assertion that every listed test actually
# ran and passed. The remaining pairs are class-line edits that keep the module
# compiling and fall back to the shared Java smoke verify.
#
# PASS_TO_PASS: gson-1014 has an EMPTY official PASS_TO_PASS list (FAIL_TO_PASS
# holds only the two JsonTreeReaderTest methods, and the upstream eval script
# runs exactly those). The ``pass_to_pass`` key is still supported: put test ids
# ("com.foo.BarTest#method") there and the runner runs + asserts them alongside
# FAIL_TO_PASS -- future Java instances (e.g. lucene-11760) use it directly.
DEFAULT_CODING_JAVA_SOURCE_FILES: list[dict] = [
    {
        "file": "gson/src/main/java/com/google/gson/JsonParser.java",
        "find": "public final class JsonParser {",
        "replace": "public final class JsonParser { // bench",
    },
    {
        # Gold patch (google__gson-1014): guard the path bookkeeping in
        # skipValue() after popStack() empties the stack.
        "file": "gson/src/main/java/com/google/gson/internal/bind/JsonTreeReader.java",
        "find": (
            "      popStack();\n"
            '      pathNames[stackSize - 1] = "null";\n'
            "    }\n"
            "    pathIndices[stackSize - 1]++;"
        ),
        "replace": (
            "      popStack();\n"
            "      if (stackSize > 0) {\n"
            '        pathNames[stackSize - 1] = "null";\n'
            "      }\n"
            "    }\n"
            "    if (stackSize > 0) {\n"
            "      pathIndices[stackSize - 1]++;\n"
            "    }"
        ),
        # The instance's FAIL_TO_PASS selection, run as real Maven tests against
        # the baked-in test file (dockerfile_build/coding/java/tests/
        # JsonTreeReaderTest.java) -- the upstream eval runs exactly these two
        # methods. pass_to_pass is empty for gson-1014 (see the section note).
        "fail_to_pass": (
            "com.google.gson.internal.bind.JsonTreeReaderTest#testSkipValue_emptyJsonObject",
            "com.google.gson.internal.bind.JsonTreeReaderTest#testSkipValue_filledJsonObject",
        ),
        "pass_to_pass": (),
    },
    {
        "file": "gson/src/main/java/com/google/gson/Gson.java",
        "find": "public final class Gson {",
        "replace": "public final class Gson { // bench",
    },
    {
        "file": "gson/src/main/java/com/google/gson/GsonBuilder.java",
        "find": "public final class GsonBuilder {",
        "replace": "public final class GsonBuilder { // bench",
    },
    {
        "file": "gson/src/main/java/com/google/gson/stream/JsonReader.java",
        "find": "public class JsonReader implements Closeable {",
        "replace": "public class JsonReader implements Closeable { // bench",
    },
    {
        "file": "gson/src/main/java/com/google/gson/internal/Streams.java",
        "find": "public final class Streams {",
        "replace": "public final class Streams { // bench",
    },
]


# Shared default verify-script body for Java pairs without their own
# verify_script. A plain gson round-trip (toJson + parse) compiled against the
# freshly rebuilt gson module -- the transient javac/JVM peak (same role
# `go run`'s compiler, `npx tsx`'s node+esbuild and `python3`'s CPython play in
# the other variants). Deliberately does not touch the JsonTreeReader bug, so
# it passes on the base commit too (warmup runs it before any edit).
DEFAULT_CODING_VERIFY_SCRIPT_JAVA = (
    "import com.google.gson.Gson;\n"
    "import com.google.gson.GsonBuilder;\n"
    "import com.google.gson.JsonObject;\n"
    "import com.google.gson.JsonParser;\n"
    "\n"
    "public class BenchVerify {\n"
    "  public static void main(String[] args) {\n"
    "    Gson gson = new GsonBuilder().serializeNulls().create();\n"
    "    String json = gson.toJson(new int[] {1, 2, 3});\n"
    '    if (!"[1,2,3]".equals(json)) {\n'
    '      throw new AssertionError("unexpected json: " + json);\n'
    "    }\n"
    '    JsonObject parsed = new JsonParser().parse("{\\"a\\":1}").getAsJsonObject();\n'
    '    if (parsed.get("a").getAsInt() != 1) {\n'
    '      throw new AssertionError("parse failed: " + parsed);\n'
    "    }\n"
    '    System.out.println("All tests passed!");\n'
    "  }\n"
    "}\n"
)


# --- c: redis/redis replacement pairs -----------------------------------------------
# Real swe_bench_multilingual instance redis__redis-13338 (XINFO group lag is
# wrong when a tombstone sits after the consumer group's last-delivered-id).
# redis at base_commit 94b9072e is a plain Makefile C project (sources under
# src/, Tcl test suite under tests/).
#
# Pairs[0] is a generic include-line edit ON PURPOSE: warmup verifies
# ``source_files[0]`` BEFORE any edit is applied, so a pair carrying the
# FAIL_TO_PASS tests must not be first (the base commit is buggy). Pairs[1]
# mirrors the gold patch -- a THREE-hunk change to src/t_stream.c -- through the
# optional ``edits`` list (one find->replace per hunk, applied in order), and
# carries the instance's FAIL_TO_PASS / PASS_TO_PASS selection. The runner runs
# the real Tcl suite (``./runtest --single unit/type/stream-cgroups``) and
# asserts every listed test reported ``[ok]``. The remaining pairs are
# include-line edits that keep the build green and fall back to the shared C
# smoke verify.
DEFAULT_CODING_C_SOURCE_FILES: list[dict] = [
    {
        "file": "src/t_stream.c",
        "find": '#include "server.h"',
        "replace": '#include "server.h" /* bench */',
    },
    {
        # Gold patch (redis__redis-13338): three hunks in src/t_stream.c.
        # 1) drop the "latest tombstone is before the first entry" early return;
        # 2) report an invalid entries-read counter when a tombstone lies
        #    between the id and the stream's last-generated-id;
        # 3) test tombstones from the group's last-delivered-id, not the
        #    entry currently being delivered.
        "file": "src/t_stream.c",
        "find": (
            "    if (streamCompareID(&s->first_id,&s->max_deleted_entry_id) > 0) {\n"
            "        /* The latest tombstone is before the first entry. */\n"
            "        return 0;\n"
            "    }\n"
            "\n"
        ),
        "replace": "",
        "edits": [
            {
                "find": (
                    "    if (streamCompareID(&s->first_id,&s->max_deleted_entry_id) > 0) {\n"
                    "        /* The latest tombstone is before the first entry. */\n"
                    "        return 0;\n"
                    "    }\n"
                    "\n"
                ),
                "replace": "",
            },
            {
                "find": (
                    "    if (!s->length && streamCompareID(id,&s->last_id) < 1) {\n"
                    "        return s->entries_added;\n"
                    "    }\n"
                    "\n"
                    "    int cmp_last = streamCompareID(id,&s->last_id);\n"
                ),
                "replace": (
                    "    if (!s->length && streamCompareID(id,&s->last_id) < 1) {\n"
                    "        return s->entries_added;\n"
                    "    }\n"
                    "\n"
                    "    /* There are fragmentations between the `id` and the stream's last-generated-id. */\n"
                    "    if (!streamIDEqZero(id) && streamCompareID(id,&s->max_deleted_entry_id) < 0)\n"
                    "        return SCG_INVALID_ENTRIES_READ;\n"
                    "\n"
                    "    int cmp_last = streamCompareID(id,&s->last_id);\n"
                ),
            },
            {
                "find": (
                    "            if (group->entries_read != SCG_INVALID_ENTRIES_READ"
                    " && !streamRangeHasTombstones(s,&id,NULL)) {\n"
                    "                /* A valid counter and no future tombstones mean we can \n"
                    "                 * increment the read counter to keep tracking the group's\n"
                    "                 * progress. */\n"
                ),
                "replace": (
                    "            if (group->entries_read != SCG_INVALID_ENTRIES_READ"
                    " && !streamRangeHasTombstones(s,&group->last_id,NULL)) {\n"
                    "                /* A valid counter and no tombstones between the group's"
                    " last-delivered-id\n"
                    "                 * and the stream's last-generated-id mean we can"
                    " increment the read counter\n"
                    "                 * to keep tracking the group's progress. */\n"
                ),
            },
        ],
        # The instance's real Tcl tests: one FAIL_TO_PASS case plus the file's
        # PASS_TO_PASS set (the upstream eval runs the whole unit file).
        "fail_to_pass": ("Consumer Group Lag with XDELs and tombstone after the last_id of consume group",),
        "pass_to_pass": (
            "XGROUP CREATE: creation and duplicate group name detection",
            "XGROUP CREATE: with ENTRIESREAD parameter",
            "XGROUP CREATE: automatic stream creation fails without MKSTREAM",
            "XGROUP CREATE: automatic stream creation works with MKSTREAM",
            "XREADGROUP will return only new elements",
            "XREADGROUP can read the history of the elements we own",
            "XPENDING is able to return pending items",
            "XPENDING can return single consumer items",
            "XPENDING only group",
            "XPENDING with IDLE",
            "XPENDING with exclusive range intervals works as expected",
            "XACK is able to remove items from the consumer/group PEL",
            "XACK can't remove the same item multiple times",
            "XACK is able to accept multiple arguments",
            "XACK should fail if got at least one invalid ID",
            "PEL NACK reassignment after XGROUP SETID event",
            "XREADGROUP will not report data on empty history. Bug #5577",
            "XREADGROUP history reporting of deleted entries. Bug #5570",
            "Blocking XREADGROUP will not reply with an empty array",
            "Blocking XREADGROUP: key deleted",
            "Blocking XREADGROUP: key type changed with SET",
            "Blocking XREADGROUP: key type changed with transaction",
            "Blocking XREADGROUP: flushed DB",
            "Blocking XREADGROUP: swapped DB, key doesn't exist",
            "Blocking XREADGROUP: swapped DB, key is not a stream",
            "XREAD and XREADGROUP against wrong parameter",
            "Blocking XREAD: key deleted",
            "Blocking XREAD: key type changed with SET",
            "Blocking XREADGROUP for stream that ran dry (issue #5299)",
            "Blocking XREADGROUP will ignore BLOCK if ID is not >",
            "Blocking XREADGROUP for stream key that has clients blocked on list",
            "Blocking XREADGROUP for stream key that has clients blocked on stream - avoid endless loop",
            "Blocking XREADGROUP for stream key that has clients blocked on stream - reprocessing command",
            "XGROUP DESTROY should unblock XREADGROUP with -NOGROUP",
            "RENAME can unblock XREADGROUP with data",
            "RENAME can unblock XREADGROUP with -NOGROUP",
            "XCLAIM can claim PEL items from another consumer",
            "XCLAIM without JUSTID increments delivery count",
            "XCLAIM same consumer",
            "XAUTOCLAIM can claim PEL items from another consumer",
            "XAUTOCLAIM as an iterator",
            "XAUTOCLAIM COUNT must be > 0",
            "XCLAIM with XDEL",
            "XCLAIM with trimming",
            "XAUTOCLAIM with XDEL",
            "XAUTOCLAIM with XDEL and count",
            "XAUTOCLAIM with out of range count",
            "XINFO FULL output",
            "Consumer seen-time and active-time",
            "XGROUP CREATECONSUMER: create consumer if does not exist",
            "XGROUP CREATECONSUMER: group must exist",
            "XREADGROUP of multiple entries changes dirty by one",
            "XREADGROUP from PEL does not change dirty",
            "XREADGROUP with NOACK creates consumer",
            "Consumer without PEL is present in AOF after AOFRW",
            "Consumer group read counter and lag in empty streams",
            "Consumer group read counter and lag sanity",
            "Consumer group lag with XDELs",
            "Loading from legacy (Redis <= v6.2.x, rdb_ver < 10) persistence",
            "Loading from legacy (Redis <= v7.0.x, rdb_ver < 11) persistence",
            "Consumer group last ID propagation to slave (NOACK=0)",
            "Consumer group last ID propagation to slave (NOACK=1)",
            "Replication tests of XCLAIM with deleted entries (autoclaim=0)",
            "Replication tests of XCLAIM with deleted entries (autoclaim=1)",
            "XREADGROUP ACK would propagate entries-read",
            "XREADGROUP from PEL inside MULTI",
            "Empty stream with no lastid can be rewrite into AOF correctly",
        ),
    },
    {
        "file": "src/t_list.c",
        "find": '#include "server.h"',
        "replace": '#include "server.h" /* bench */',
    },
    {
        "file": "src/server.c",
        "find": '#include "server.h"',
        "replace": '#include "server.h" /* bench */',
    },
    {
        "file": "src/db.c",
        "find": '#include "server.h"',
        "replace": '#include "server.h" /* bench */',
    },
    {
        "file": "src/networking.c",
        "find": '#include "server.h"',
        "replace": '#include "server.h" /* bench */',
    },
]


# Shared default verify-script body for C pairs without their own verify_script.
# A standalone gcc-compiled probe (no project headers required) run after the
# Makefile rebuild -- the transient cc/link/execute peak that stands in for the
# project's own build load. Deliberately generic so it passes on the base commit
# too (warmup runs it before any edit).
DEFAULT_CODING_VERIFY_SCRIPT_C = (
    "#include <stdio.h>\n"
    "#include <string.h>\n"
    "\n"
    "int main(void) {\n"
    "    char buf[32];\n"
    '    snprintf(buf, sizeof(buf), "%s-%d", "bench", 13338);\n'
    '    if (strcmp(buf, "bench-13338") != 0) {\n'
    '        fprintf(stderr, "unexpected: %s\\n", buf);\n'
    "        return 1;\n"
    "    }\n"
    '    printf("All tests passed!\\n");\n'
    "    return 0;\n"
    "}\n"
)


# --- cpp: nlohmann/json replacement pairs -------------------------------------------
# Real swe_bench_multilingual instance nlohmann__json-4237 (enum serialization
# always used number_integer, so enums with an unsigned underlying type were
# serialized as signed numbers). nlohmann/json at base_commit 3780b41d is a
# header-only CMake project: sources under include/, the amalgamated mirror
# under single_include/, and the doctest suite under tests/.
#
# Pairs[0] is a generic comment edit ON PURPOSE: warmup verifies
# ``source_files[0]`` BEFORE any edit is applied, so a pair carrying the
# FAIL_TO_PASS tests must not be first (the base commit is buggy). Pairs[1]
# mirrors the gold patch -- the SAME two-line change in the multi-header source
# and its single-header mirror -- through per-edit ``file`` keys in ``edits``,
# and carries the instance's FAIL_TO_PASS / PASS_TO_PASS selection. The runner
# builds the real doctest target (``test-udt_cpp11``) and asserts every listed
# test case > section path from its XML report. The remaining pairs are
# comment edits that keep the build green and fall back to the shared C++ smoke
# verify.
DEFAULT_CODING_CPP_SOURCE_FILES: list[dict] = [
    {
        "file": "include/nlohmann/json.hpp",
        "find": "NLOHMANN_JSON_NAMESPACE_BEGIN",
        "replace": "NLOHMANN_JSON_NAMESPACE_BEGIN // bench",
    },
    {
        # Gold patch (nlohmann__json-4237): pick number_unsigned for enums whose
        # underlying type is unsigned. The upstream commit changes both the
        # multi-header source and its generated single-header mirror; ``edits``
        # entries carry their own ``file`` so one round applies the complete
        # upstream patch (see _build_multi_edit_command).
        "file": "include/nlohmann/detail/conversions/to_json.hpp",
        "find": "    external_constructor<value_t::number_integer>::construct(j, static_cast<underlying_type>(e));",
        "replace": (
            "    static constexpr value_t integral_value_t = "
            "std::is_unsigned<underlying_type>::value ? value_t::number_unsigned : value_t::number_integer;\n"
            "    external_constructor<integral_value_t>::construct(j, static_cast<underlying_type>(e));"
        ),
        "edits": [
            {
                "file": "include/nlohmann/detail/conversions/to_json.hpp",
                "find": "    external_constructor<value_t::number_integer>::construct(j, static_cast<underlying_type>(e));",
                "replace": (
                    "    static constexpr value_t integral_value_t = "
                    "std::is_unsigned<underlying_type>::value ? value_t::number_unsigned : value_t::number_integer;\n"
                    "    external_constructor<integral_value_t>::construct(j, static_cast<underlying_type>(e));"
                ),
            },
            {
                "file": "single_include/nlohmann/json.hpp",
                "find": "    external_constructor<value_t::number_integer>::construct(j, static_cast<underlying_type>(e));",
                "replace": (
                    "    static constexpr value_t integral_value_t = "
                    "std::is_unsigned<underlying_type>::value ? value_t::number_unsigned : value_t::number_integer;\n"
                    "    external_constructor<integral_value_t>::construct(j, static_cast<underlying_type>(e));"
                ),
            },
        ],
        # The instance's real doctest selection (names are "test case > section"
        # paths parsed from the doctest XML report; nested sections are reported
        # by their innermost name, matching the upstream eval).
        "fail_to_pass": ("basic usage > conversion to json via free-functions",),
        "pass_to_pass": (
            "basic usage > conversion from json via free-functions",
            "basic usage > via explicit calls to get",
            "basic usage > via explicit calls to get_to",
            "basic usage > implicit conversions",
            "adl_serializer specialization > partial specialization",
            "adl_serializer specialization > to_json",
            "adl_serializer specialization > from_json",
            "adl_serializer specialization > total specialization",
            "Non-copyable types > to_json",
            "Non-copyable types > from_json",
            "different basic_json types conversions > null",
            "different basic_json types conversions > boolean",
            "different basic_json types conversions > discarded",
            "different basic_json types conversions > array",
            "different basic_json types conversions > integer",
            "different basic_json types conversions > float",
            "different basic_json types conversions > unsigned",
            "different basic_json types conversions > string",
            "different basic_json types conversions > binary",
            "different basic_json types conversions > object",
            "different basic_json types conversions > get<custom_json>",
        ),
    },
    {
        "file": "include/nlohmann/detail/value_t.hpp",
        "find": "enum class value_t : std::uint8_t",
        "replace": "enum class value_t : std::uint8_t // bench",
    },
    {
        "file": "include/nlohmann/detail/input/parser.hpp",
        "find": "class parser",
        "replace": "class parser // bench",
    },
    {
        "file": "include/nlohmann/detail/output/serializer.hpp",
        "find": "class serializer",
        "replace": "class serializer // bench",
    },
    {
        "file": "include/nlohmann/detail/input/lexer.hpp",
        # Anchor on the full declaration: the bare `class lexer` prefix also
        # matches `class lexer_base` (line 35) and would comment out the base
        # class name, breaking the build.
        "find": "class lexer : public lexer_base<BasicJsonType>",
        "replace": "class lexer : public lexer_base<BasicJsonType> // bench",
    },
]


# Shared default verify-script body for C++ pairs without their own
# verify_script. A standalone g++-compiled probe against the project's single
# header, run after the real doctest target rebuild -- the transient
# template-heavy g++ peak that stands in for the project's own build load.
# Deliberately generic so it passes on the base commit too (warmup runs it
# before any edit).
DEFAULT_CODING_VERIFY_SCRIPT_CPP = (
    "#include <nlohmann/json.hpp>\n"
    "#include <iostream>\n"
    "#include <string>\n"
    "\n"
    "int main() {\n"
    '    nlohmann::json j = {{"bench", 4237}};\n'
    "    const std::string dumped = j.dump();\n"
    '    if (dumped != "{\\"bench\\":4237}") {\n'
    '        std::cerr << "unexpected: " << dumped << std::endl;\n'
    "        return 1;\n"
    "    }\n"
    '    std::cout << "All tests passed!" << std::endl;\n'
    "    return 0;\n"
    "}\n"
)


# Extensible language registry.
CODING_LANGUAGE_PROFILES: dict[str, CodingLanguageProfile] = {
    "ts": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.mjs",
        heredoc_eof="EOF",
        run_cmd="npx tsx /tmp/bench_verify.mjs",
        source_find_names=("*.ts", "*.tsx", "*.js"),
        source_find_root="packages",
        # vuejs/core is a pnpm monorepo: all source lives under packages/<name>/src/,
        # there is NO top-level src/ directory. packages/ alone covers every edited file.
        checkout_paths="packages/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_JS,
    ),
    "go": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.go",
        heredoc_eof="GOEOF",
        run_cmd="go run /tmp/bench_verify.go",
        source_find_names=("*.go",),
        source_find_root=".",
        checkout_paths="markup/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_GO,
        pre_verify_cmd="go clean -cache",
        project_marker="go.mod",
    ),
    "python": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.py",
        heredoc_eof="PYEOF",
        run_cmd="python3 /tmp/bench_verify.py",
        source_find_names=("*.py",),
        source_find_root=".",
        checkout_paths="django/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_PY,
        project_marker="pyproject.toml",
        # Pairs carrying fail_to_pass/pass_to_pass run the instance's own test
        # module through django's runner, verbatim from the upstream eval
        # (`./tests/runtests.py ...`, run from the repo root). The image provides
        # /usr/local/bin/python -> python3 for the script's shebang, and
        # --settings=test_sqlite makes `tests/` runnable outside runtests.py's
        # default discovery.
        test_suite_cmd=(
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite " "--parallel 1 auth_tests.test_basic"
        ),
        # django's runner prints unittest descriptions ("<id> ... ok").
        test_report_format="django_test",
    ),
    "java": CodingLanguageProfile(
        temp_test_path="/tmp/BenchVerify.java",
        heredoc_eof="JAVAEOF",
        # Two-step verify (config.coding_verify_cmd carries the real command):
        # recompile the edited gson module offline, then javac+run the ad-hoc
        # probe against the fresh classes. `-Djava.version=8` overrides the
        # pom's <java.version>1.6</java.version> (javac 8+ rejects source 6).
        run_cmd=(
            "cd /opt/coding-bench && mvn -o -q -B -pl gson -DskipTests -Djava.version=8 compile "
            "&& javac -cp gson/target/classes /tmp/BenchVerify.java "
            "&& java -cp gson/target/classes:/tmp BenchVerify"
        ),
        source_find_names=("*.java",),
        source_find_root="gson/src/main/java",
        # All pairs edit the gson module's main sources only.
        checkout_paths="gson/src/main/java/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_JAVA,
        # Maven has no `go clean -cache` analogue we want here: recompiling the
        # single changed module is the real agent verify, and the build-time
        # warm compile keeps ~/.m2 populated for the offline (-o) rebuild.
        project_marker="pom.xml",
        # Pairs carrying fail_to_pass/pass_to_pass run the real Maven test suite
        # of this module (see _run_test_suite_verify in task_runner/coding.py).
        test_module="gson",
        test_args="-Djava.version=8",
    ),
    "c": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.c",
        heredoc_eof="CEOF",
        # Rebuild the edited redis tree with the project's own Makefile, then
        # gcc+run the ad-hoc probe -- the real recompile peak after a src/ edit.
        run_cmd=(
            "cd /opt/coding-bench && make -s -j$(nproc) "
            "&& gcc -O2 /tmp/bench_verify.c -o /tmp/bench_verify && /tmp/bench_verify"
        ),
        source_find_names=("*.c",),
        source_find_root="src",
        # All pairs edit redis src/ only; the baked-in Tcl fixture lives under
        # tests/, so this reset never reverts the instance's test patch.
        checkout_paths="src/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_C,
        project_marker="Makefile",
        # Pairs carrying fail_to_pass/pass_to_pass rebuild + run the real Tcl
        # suite (see _run_test_suite_verify). --clients 1 keeps a single test
        # client so the suite output is deterministic and bounded on a 2-CPU box.
        test_suite_cmd=(
            "make -j$(nproc) && " "TERM=dumb ./runtest --clients 1 --durable --single unit/type/stream-cgroups"
        ),
    ),
    "cpp": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.cpp",
        heredoc_eof="CPPEOF",
        # Rebuild the edited header tree with the project's own CMake target,
        # then g+++run the ad-hoc probe against the single header -- the real
        # template-compile peak after an include/ edit.
        run_cmd=(
            "cd /opt/coding-bench && cmake --build build -j$(nproc) --target test-udt_cpp11 "
            "&& g++ -std=c++11 -I single_include /tmp/bench_verify.cpp -o /tmp/bench_verify && /tmp/bench_verify"
        ),
        source_find_names=("*.hpp", "*.cpp"),
        source_find_root="include",
        # All pairs edit the header trees; the baked-in test patch lives under
        # tests/, so this reset never reverts the instance's test patch.
        checkout_paths="include/ single_include/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_CPP,
        project_marker="CMakeLists.txt",
        # Pairs carrying fail_to_pass/pass_to_pass rebuild + run the real doctest
        # target (see _run_test_suite_verify). `-s -r=xml` mirrors the upstream
        # eval; the XML goes to a dedicated file so build noise stays in the log.
        test_suite_cmd=(
            "cmake -S . -B build && cmake --build build -j$(nproc) --target test-udt_cpp11 "
            "&& ./build/tests/test-udt_cpp11 -s -r=xml > /tmp/bench_doctest.xml"
        ),
        test_report_format="doctest_xml",
    ),
}

# Maps a language to its default replacement-pair list.
CODING_LANGUAGE_DEFAULT_SOURCE_FILES: dict[str, list] = {
    "ts": DEFAULT_CODING_SOURCE_FILES,
    "go": DEFAULT_CODING_GO_SOURCE_FILES,
    "python": DEFAULT_CODING_PY_SOURCE_FILES,
    "java": DEFAULT_CODING_JAVA_SOURCE_FILES,
    "c": DEFAULT_CODING_C_SOURCE_FILES,
    "cpp": DEFAULT_CODING_CPP_SOURCE_FILES,
}


def get_coding_profile(language: str) -> CodingLanguageProfile:
    """Return the :class:`CodingLanguageProfile` for ``language``, falling back to ts."""
    return CODING_LANGUAGE_PROFILES.get(language, CODING_LANGUAGE_PROFILES["ts"])
