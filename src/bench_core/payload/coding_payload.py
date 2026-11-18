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


# --- python: django/django replacement pairs -----------------------------------------
# django (github.com/django/django) is a real repo used across swe_bench /
# swe-bench-verified evaluations. Each pair is a real, type-safe string edit to
# a verified django framework source file (all under django/). The first pair
# carries its own verify_script (a bare python3 script importing django and
# asserting LANGUAGE_CODE); pairs without a verify_script fall back to the
# shared Python default.
DEFAULT_CODING_PY_SOURCE_FILES: list[dict[str, str]] = [
    {
        "file": "django/conf/global_settings.py",
        "find": 'LANGUAGE_CODE = "en-us"',
        "replace": 'LANGUAGE_CODE = "en-us"  # bench round',
        "verify_script": (
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
            "\n"
            'assert settings.LANGUAGE_CODE == "en-us", settings.LANGUAGE_CODE\n'
            'print("All tests passed!")'
        ),
    },
    {
        "file": "django/db/models/fields/__init__.py",
        "find": "class Field(RegisterLookupMixin):",
        "replace": "class Field(RegisterLookupMixin):  # bench",
    },
    {
        "file": "django/http/response.py",
        "find": "class HttpResponse:",
        "replace": "class HttpResponse:  # bench",
    },
    {
        "file": "django/utils/text.py",
        "find": "def slugify(value, allow_unicode=False):",
        "replace": "def slugify(value, allow_unicode=False):  # bench",
    },
    {
        "file": "django/template/base.py",
        "find": "class Template:",
        "replace": "class Template:  # bench",
    },
    {
        "file": "django/urls/resolvers.py",
        "find": "class URLResolver:",
        "replace": "class URLResolver:  # bench",
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
}

# Maps a language to its default replacement-pair list.
CODING_LANGUAGE_DEFAULT_SOURCE_FILES: dict[str, list] = {
    "ts": DEFAULT_CODING_SOURCE_FILES,
    "go": DEFAULT_CODING_GO_SOURCE_FILES,
    "python": DEFAULT_CODING_PY_SOURCE_FILES,
    "java": DEFAULT_CODING_JAVA_SOURCE_FILES,
}


def get_coding_profile(language: str) -> CodingLanguageProfile:
    """Return the :class:`CodingLanguageProfile` for ``language``, falling back to ts."""
    return CODING_LANGUAGE_PROFILES.get(language, CODING_LANGUAGE_PROFILES["ts"])
