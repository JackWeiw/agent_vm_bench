"""
Data Structure Definitions Module

Defines SandboxStatus, CreationMetrics, TaskMetricsBase, BrowserMetrics,
CodingMetrics, SandboxState, TestSnapshot, BatchTask, TaskGroup.

Step order constants for workflow dispatch:
- BROWSER_STEP_ORDER: steps in browser round-robin mode
- CODING_STEP_ORDER: steps in coding round-robin mode

Default source files for the vuejs/core coding benchmark (single definition,
referenced everywhere - config, runners, YAML templates).

vuejs/core is a real repo from the swe_bench_multilingual evaluation dataset
(github.com/vuejs/core, 5 real instances). Each entry is a {file, find, replace,
verify_script} replacement pair: a real, type-safe string edit applied to a
verified file in the vuejs/core repo, plus an ad-hoc verify script body. The
runner round-robins through the list, applying one pair per round then verifying
the edit by writing the script to /tmp and running it via `npx tsx` (the exact
verification a real openclaw agent used on this repo).
"""

import statistics
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# Step order constants for workflow dispatch
BROWSER_STEP_ORDER = ["open_tab", "page_load", "snapshot", "click", "screenshot"]
# Real AI coding agent workflow (verified against captured openclaw trajectories
# on vuejs/core and gohugoio/hugo): locate file (find), inspect it (read), apply a
# real edit, verify the edit by writing an ad-hoc test file and running it (verify),
# then produce the verification artifact (git diff). `git checkout --` reset runs as
# setup inside the `find` step, not a separate step. The `verify` step mirrors the
# trace: the agent writes /tmp/test_*.mjs (or .go) then runs `npx tsx` / `go run`.
# No production build, no full test suite, no resident dev server - none appear in
# the real traces. Memory pressure comes from N concurrent sandboxes' transient
# verify peaks overlapping, observed at host level via vm_monitor/smap_tool.
CODING_STEP_ORDER = ["find", "read", "edit", "verify", "diff"]

# Default replacement pairs for the vuejs/core coding benchmark.
# Single definition - referenced by Config dataclass default, _from_dict,
# from_args, YAML templates, and bench_helper.sh.
#
# vuejs/core is a pnpm monorepo (github.com/vuejs/core), part of the
# swe_bench_multilingual dataset (real evaluation instances, not synthetic).
# Each pair is verified against the repo. The `find` string is a real value
# that exists in the file; `replace` is a type-safe substitute (comment append
# or equivalent return value) that does not break compilation.
#
# Each pair may carry an optional `verify_script` - the body of an ad-hoc
# /tmp/bench_verify.mjs that exercises the edited symbol (mirrors the real
# openclaw trace: agent writes a focused .mjs importing the raw .ts source,
# runs it via `npx tsx`). Pairs without `verify_script` fall back to a shared
# default body (import compiler-core + run baseParse + print "All tests
# passed!"). The default loads the full TS module graph -> real transient
# memory peak; it never asserts complex logic so a round never dies from a
# broken assertion.
#
# Why compiler-core (not the edited package's own index): the real openclaw
# agent on vuejs/core imported compiler-core's baseParse/parse to verify edits
# (its captured trace imports only compiler-core). compiler-core is the
# heaviest trace-faithful entry that runs under a bare `npx tsx` without
# hitting the __TEST__ build global - the vue/runtime-core/compiler-dom/
# compiler-sfc package graphs all reach compiler-dom/src/errors.ts which
# references __TEST__ (intentionally not injected, see below) and crash on a
# real call. compiler-core alone (parser) avoids that path while still loading
# the parser+AST module graph = a real transient CPU/memory peak (~467ms user
# steady vs ~299ms for the lightweight shared package). Each pair carries a
# different template/assertion so consecutive rounds don't repeat identical
# bytes (mirrors the agent rewriting its ad-hoc test per verify).
DEFAULT_CODING_SOURCE_FILES = [
    {
        "file": "packages/shared/src/general.ts",
        "find": "export const NOOP = (): void => {}",
        "replace": "export const NOOP = (): void => undefined",
        # v1: basic div + interpolation
        "verify_script": (
            "globalThis.__DEV__ = true\n"
            "globalThis.__BROWSER__ = false\n"
            "globalThis.__COMPAT__ = false\n"
            "globalThis.__ESM_BUNDLER__ = true\n"
            "globalThis.__FEATURE_OPTIONS_API__ = true\n"
            "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
            "globalThis.__FEATURE_SUSPENSE__ = true\n"
            "globalThis.__RUNTIME_COMPILE__ = true\n"
            "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
            "  const ast = m.baseParse('<div id=\"x\">{{ msg }}</div>', { parseMode: 'html' })\n"
            "  if (ast.children[0].tag !== 'div') throw new Error('expected div')\n"
            "  console.log('All tests passed!')\n"
            "})\n"
        ),
    },
    {
        "file": "packages/shared/src/general.ts",
        "find": "Always return false.",
        "replace": "Always returns false.",
        # v2: v-pre textarea (the real agent's issue scenario)
        "verify_script": (
            "globalThis.__DEV__ = true\n"
            "globalThis.__BROWSER__ = false\n"
            "globalThis.__COMPAT__ = false\n"
            "globalThis.__ESM_BUNDLER__ = true\n"
            "globalThis.__FEATURE_OPTIONS_API__ = true\n"
            "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
            "globalThis.__FEATURE_SUSPENSE__ = true\n"
            "globalThis.__RUNTIME_COMPILE__ = true\n"
            "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
            "  const ast = m.baseParse('<textarea v-pre>{{ not interpolated }}</textarea>', { parseMode: 'html' })\n"
            "  if (ast.children[0].tag !== 'textarea') throw new Error('expected textarea')\n"
            "  console.log('All tests passed!')\n"
            "})\n"
        ),
    },
    {
        "file": "packages/shared/src/index.ts",
        "find": "export * from './general'",
        "replace": "export * from './general' // bench round",
        # v3: v-for list
        "verify_script": (
            "globalThis.__DEV__ = true\n"
            "globalThis.__BROWSER__ = false\n"
            "globalThis.__COMPAT__ = false\n"
            "globalThis.__ESM_BUNDLER__ = true\n"
            "globalThis.__FEATURE_OPTIONS_API__ = true\n"
            "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
            "globalThis.__FEATURE_SUSPENSE__ = true\n"
            "globalThis.__RUNTIME_COMPILE__ = true\n"
            "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
            "  const ast = m.baseParse('<ul><li v-for=\"i in list\">{{ i }}</li></ul>', { parseMode: 'html' })\n"
            "  if (ast.children[0].tag !== 'ul') throw new Error('expected ul')\n"
            "  console.log('All tests passed!')\n"
            "})\n"
        ),
    },
    {
        "file": "packages/vue/src/index.ts",
        "find": '// This entry is the "full-build"',
        "replace": '// This entry is the "full-build" (bench)',
        # v4: nested v-if/v-else
        "verify_script": (
            "globalThis.__DEV__ = true\n"
            "globalThis.__BROWSER__ = false\n"
            "globalThis.__COMPAT__ = false\n"
            "globalThis.__ESM_BUNDLER__ = true\n"
            "globalThis.__FEATURE_OPTIONS_API__ = true\n"
            "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
            "globalThis.__FEATURE_SUSPENSE__ = true\n"
            "globalThis.__RUNTIME_COMPILE__ = true\n"
            "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
            "  const ast = m.baseParse('<div><span v-if=\"ok\">yes</span><span v-else>no</span></div>', { parseMode: 'html' })\n"
            "  if (ast.children[0].children.length < 2) throw new Error('expected 2 spans')\n"
            "  console.log('All tests passed!')\n"
            "})\n"
        ),
    },
    {
        "file": "packages/reactivity/src/baseHandlers.ts",
        "find": "export const mutableHandlers: ProxyHandler<object> =",
        "replace": "export const mutableHandlers: ProxyHandler<object> = // bench",
        # v5: multi-root fragment
        "verify_script": (
            "globalThis.__DEV__ = true\n"
            "globalThis.__BROWSER__ = false\n"
            "globalThis.__COMPAT__ = false\n"
            "globalThis.__ESM_BUNDLER__ = true\n"
            "globalThis.__FEATURE_OPTIONS_API__ = true\n"
            "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
            "globalThis.__FEATURE_SUSPENSE__ = true\n"
            "globalThis.__RUNTIME_COMPILE__ = true\n"
            "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
            "  const ast = m.baseParse('<div>a</div><div>b</div>', { parseMode: 'html' })\n"
            "  if (ast.children.length < 2) throw new Error('expected 2 roots')\n"
            "  console.log('All tests passed!')\n"
            "})\n"
        ),
    },
    {
        "file": "packages/runtime-core/src/errorHandling.ts",
        "find": "import { EMPTY_OBJ, isArray, isFunction, isPromise } from '@vue/shared'",
        "replace": "import { EMPTY_OBJ, isArray, isFunction, isPromise } from '@vue/shared' // bench",
        # v6: baseParse + complex expression (props) - the agent's baseParse entry
        "verify_script": (
            "globalThis.__DEV__ = true\n"
            "globalThis.__BROWSER__ = false\n"
            "globalThis.__COMPAT__ = false\n"
            "globalThis.__ESM_BUNDLER__ = true\n"
            "globalThis.__FEATURE_OPTIONS_API__ = true\n"
            "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
            "globalThis.__FEATURE_SUSPENSE__ = true\n"
            "globalThis.__RUNTIME_COMPILE__ = true\n"
            "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
            "  const parse = m.baseParse\n"
            "  const ast = parse('<div :class=\"cls + extra\" @click=\"onClick\">text</div>', { parseMode: 'html' })\n"
            "  const div = ast.children[0]\n"
            "  if (!div.props || !div.props.length) throw new Error('expected props')\n"
            "  console.log('All tests passed!')\n"
            "})\n"
        ),
    },
]


# Shared default verify-script body for pairs that don't carry their own
# `verify_script`. Imports compiler-core (the real agent's verify entry, the
# heaviest trace-faithful entry that runs under bare npx tsx - see
# DEFAULT_CODING_SOURCE_FILES header) + runs baseParse + prints "All tests
# passed!". Loads the parser+AST module graph (esbuild transpile + node
# execute) -> real transient memory peak. `{pkg}` is no longer used (the
# default always imports compiler-core regardless of which file was edited)
# but kept for back-compat with any external caller; it's ignored if absent.
#
# The globalThis injections are VERBATIM the set the real openclaw agent injected
# at the top of its verify scripts in the captured vuejs/core trajectory
# (extracted from swe_bench_multilingual trace): __DEV__, __BROWSER__, __COMPAT__,
# __ESM_BUNDLER__, __FEATURE_OPTIONS_API__, __FEATURE_PROD_DEVTOOLS__,
# __FEATURE_SUSPENSE__, __RUNTIME_COMPILE__. vuejs/core's source references these
# build-time globals (esbuild `define` in a real build); a bare `npx tsx` run has
# them undefined, so the agent (and we) inject them at the top of the ad-hoc
# test. NOTE: __TEST__ is intentionally NOT injected - the agent didn't either.
# compiler-core alone (the parser) avoids the __TEST__ reference path (in
# compiler-dom/src/errors.ts); the vue/runtime-core/compiler-dom/compiler-sfc
# graphs reach it and crash on a real call, so those pairs use this
# compiler-core-based default rather than their own package's index.
DEFAULT_CODING_VERIFY_SCRIPT_JS = (
    "globalThis.__DEV__ = true\n"
    "globalThis.__BROWSER__ = false\n"
    "globalThis.__COMPAT__ = false\n"
    "globalThis.__ESM_BUNDLER__ = true\n"
    "globalThis.__FEATURE_OPTIONS_API__ = true\n"
    "globalThis.__FEATURE_PROD_DEVTOOLS__ = false\n"
    "globalThis.__FEATURE_SUSPENSE__ = true\n"
    "globalThis.__RUNTIME_COMPILE__ = true\n"
    "import('/opt/coding-bench/packages/compiler-core/src/index.ts').then(m => {\n"
    "  const ast = m.baseParse('<div id=\"x\">hello</div>', { parseMode: 'html' })\n"
    "  if (ast.children[0].tag !== 'div') throw new Error('expected div')\n"
    "  console.log('All tests passed!')\n"
    "})\n"
)


# Default replacement pairs for the gohugoio/hugo coding benchmark (Go language).
# Real swe_bench_multilingual instance gohugoio__hugo-12768 (GitHub Alert
# case-insensitivity). Each pair is verified against the repo at its base
# commit. The first pair mirrors the gold patch (adds (?i) to the alert regex);
# its `verify_script` is a standalone `package main` exercising case-insensitive
# alert matching - the exact shape of the ad-hoc /tmp/test_alert.go the real
# openclaw agent wrote and ran via `go run`.
DEFAULT_CODING_GO_SOURCE_FILES = [
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
    # Additional safe comment-append edits across hugo markup source - each
    # triggers a `go run` verify peak without risking a broken edit. Pairs
    # without verify_script fall back to the shared Go default (compiles +
    # runs a no-op main, asserts "All tests passed!").
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


class SandboxStatus(Enum):
    """Sandbox status enumeration"""

    PENDING = "pending"  # Waiting for creation
    CREATING = "creating"  # Creating in progress
    CREATED = "created"  # sandbox.create succeeded, waiting for ports
    PORT_READY = "port_ready"  # Ports ready, can execute tasks
    ACTIVE = "active"  # Active, executing tasks
    FAILED = "failed"  # Creation failed
    PORT_FAILED = "port_failed"  # Port check failed
    OFFLINE = "offline"  # Runtime offline
    KILLED = "killed"  # Killed


@dataclass
class CreationMetrics:
    """Sandbox creation performance metrics"""

    submit_time: float = 0.0  # Creation submit time
    create_ready_time: float = 0.0  # sandbox.create success time (excluding port wait)
    port_ready_time: float = 0.0  # Ports ready time
    create_elapsed: float = 0.0  # sandbox.create elapsed time (seconds)
    port_wait_elapsed: float = 0.0  # Port wait elapsed time (seconds)
    total_elapsed: float = 0.0  # Total elapsed = create_elapsed + port_wait_elapsed
    status: SandboxStatus = SandboxStatus.PENDING
    error_msg: str = ""
    port_check_error: str = ""  # Port check error message


class TaskMetricsBase:
    """Base class for workflow task metrics (thread-safe for concurrent access).

    Provides the shared metrics tracking pattern: total/success/failed/timeout
    counters, latency collection, step-level timing, and percentile calculations.
    Subclasses override `step_order` and may extend `add()` with workflow-specific
    parameters (e.g., verify_success for coding).

    Extending for a new workflow type:
        class DatabaseMetrics(TaskMetricsBase):
            step_order = DATABASE_STEP_ORDER
            # add build_success/test_success-like fields as needed
    """

    # Override in subclass to define workflow-specific step names
    step_order: List[str] = []

    def __init__(self):
        self._lock = threading.Lock()
        self._total_tasks: int = 0
        self._success_count: int = 0
        self._failed_count: int = 0
        self._timeout_count: int = 0
        self._latencies: List[float] = []
        self._last_error: str = ""
        self._step_times: Dict[str, List[float]] = {}

    def add(self, latency: float, success: bool, timeout: bool = False, step_times: Dict[str, float] = None) -> None:
        """Add a task result (thread-safe).

        Args:
            latency: Total latency for the task (seconds)
            success: Whether the task succeeded
            timeout: Whether the task timed out
            step_times: Optional dict of step name -> latency in seconds
        """
        with self._lock:
            self._total_tasks += 1
            if timeout:
                self._timeout_count += 1
                self._failed_count += 1
            elif success:
                self._success_count += 1
                self._latencies.append(latency)
            else:
                self._failed_count += 1

            if step_times:
                for step_name, step_latency in step_times.items():
                    if step_name not in self._step_times:
                        self._step_times[step_name] = []
                    self._step_times[step_name].append(step_latency)

    @property
    def total_tasks(self) -> int:
        with self._lock:
            return self._total_tasks

    @property
    def success_count(self) -> int:
        with self._lock:
            return self._success_count

    @property
    def failed_count(self) -> int:
        with self._lock:
            return self._failed_count

    @property
    def timeout_count(self) -> int:
        with self._lock:
            return self._timeout_count

    @property
    def latencies(self) -> List[float]:
        with self._lock:
            return list(self._latencies)  # Return a copy for thread safety

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @last_error.setter
    def last_error(self, value: str) -> None:
        with self._lock:
            self._last_error = value

    @property
    def avg_latency(self) -> float:
        """Average latency (seconds)"""
        with self._lock:
            return statistics.mean(self._latencies) if self._latencies else 0.0

    @property
    def p99_latency(self) -> float:
        """P99 latency (seconds)"""
        with self._lock:
            if not self._latencies:
                return 0.0
            sorted_lat = sorted(self._latencies)
            if len(sorted_lat) >= 100:
                return sorted_lat[int(len(sorted_lat) * 0.99)]
            return sorted_lat[-1]

    def get_step_stats(self) -> Dict[str, Dict[str, float]]:
        """Get statistics for each step (avg, p99, count).

        Returns:
            Dict of step_name -> {"avg": float, "p99": float, "count": int}
        """
        with self._lock:
            result = {}
            for step_name, times in self._step_times.items():
                if not times:
                    continue
                sorted_times = sorted(times)
                avg = statistics.mean(times)
                p99 = sorted_times[-1] if len(sorted_times) < 100 else sorted_times[int(len(sorted_times) * 0.99)]
                result[step_name] = {
                    "avg": avg,
                    "p99": p99,
                    "count": len(times),
                }
            return result

    def get_step_times_copy(self) -> Dict[str, List[float]]:
        """Get a thread-safe copy of all step times.

        Used for detailed tail latency analysis across all sandboxes.

        Returns:
            Dict of step_name -> list of latency values (copy)
        """
        with self._lock:
            return {step_name: list(times) for step_name, times in self._step_times.items()}

    def get_latencies_since(self, start_count: int) -> List[float]:
        """Get latencies added after a certain count.

        Args:
            start_count: The latency count at the start (e.g., from previous round)

        Returns:
            List of latencies added since start_count
        """
        with self._lock:
            if start_count >= len(self._latencies):
                return []
            return list(self._latencies[start_count:])


class BrowserMetrics(TaskMetricsBase):
    """Browser task metrics - inherits all shared logic from TaskMetricsBase.

    Step order: open_tab, page_load, snapshot, click, screenshot.
    No workflow-specific extensions; base add() signature is sufficient.
    """

    step_order = BROWSER_STEP_ORDER


class CodingMetrics(TaskMetricsBase):
    """Coding task metrics - extends TaskMetricsBase with verify-success tracking.

    Step order: find, read, edit, verify, diff.
    Adds verify_success_count beyond the base counters. The `verify` step
    mirrors the real agent trace: write an ad-hoc test file to /tmp + run it
    (npx tsx for js, go run for go). verify_success tracks whether that step
    passed (transient compile+run peak succeeded).
    """

    step_order = CODING_STEP_ORDER

    def __init__(self):
        super().__init__()
        self._verify_success_count: int = 0  # pairs with a real-assertion verify_script that passed
        self._compile_only_count: int = 0  # pairs marked verify: compile_only (or the no-script
        #                                  # fallback) that compiled+ran without an assertion

    def add(
        self,
        latency: float,
        success: bool,
        timeout: bool = False,
        step_times: Dict[str, float] = None,
        verify_success: bool = False,
        compile_only: bool = False,
    ) -> None:
        """Add a coding task result (thread-safe).

        Extends the base counters (total/success/failed/timeout/latencies/
        step_times) with verify-success tracking. The base counters are
        inlined here (not via super().add()) so both run under a single
        Lock acquisition - threading.Lock is non-reentrant, so calling
        super().add() while already holding self._lock would deadlock.

        Args:
            latency: Total latency for the task cycle (seconds)
            success: Whether the overall task succeeded
            timeout: Whether the task timed out
            step_times: Optional dict of step name -> latency in seconds
            verify_success: Whether the verify step (write temp test + run) succeeded
            compile_only: True if this pair ran via the shared default (no per-pair
                assertion - only compile/run was checked). verify_success and
                compile_only are mutually exclusive; reported separately so a
                compile-only pass is never mistaken for an assertion pass.
        """
        with self._lock:
            self._total_tasks += 1
            if timeout:
                self._timeout_count += 1
                self._failed_count += 1
            elif success:
                self._success_count += 1
                self._latencies.append(latency)
            else:
                self._failed_count += 1

            if verify_success:
                self._verify_success_count += 1
            if compile_only:
                self._compile_only_count += 1

            if step_times:
                for step_name, step_latency in step_times.items():
                    if step_name not in self._step_times:
                        self._step_times[step_name] = []
                    self._step_times[step_name].append(step_latency)

    @property
    def verify_success_count(self) -> int:
        with self._lock:
            return self._verify_success_count

    @property
    def compile_only_count(self) -> int:
        with self._lock:
            return self._compile_only_count


@dataclass
class SandboxState:
    """Sandbox complete state"""

    sandbox_id: int  # Sequence number (1, 2, 3...)
    sandbox_obj: Optional[object] = None  # E2B Sandbox object reference (handle)
    batch_id: int = -1  # Batch ID

    workflow_type: str = "browser"  # Determines which metrics are primary

    creation_metrics: CreationMetrics = field(default_factory=CreationMetrics)
    browser_metrics: BrowserMetrics = field(default_factory=BrowserMetrics)
    coding_metrics: CodingMetrics = field(default_factory=CodingMetrics)

    is_alive: bool = True  # Sandbox alive status
    last_task_time: float = 0.0  # Last task execution time (thread-safe via update_last_task_time)
    consecutive_failures: int = 0  # Consecutive failure count
    warmup_done: bool = False  # Warmup phase completed flag

    # Tab state (for round_robin tab-switch mode)
    tab_ids: List[str] = field(default_factory=list)  # Active tab IDs [t1, t2, ...]

    # Thread lock for last_task_time (not serialized)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self):
        """Initialize lock after dataclass creation."""
        if not hasattr(self, "_lock") or not hasattr(self._lock, "acquire"):
            object.__setattr__(self, "_lock", threading.Lock())

    @property
    def task_metrics(self) -> TaskMetricsBase:
        """Polymorphic metrics access - returns the metrics object for the active workflow.

        For browser workflow, returns browser_metrics.
        For coding workflow, returns coding_metrics.
        Enables unified code paths that don't need `if workflow_type == "coding"` dispatch.
        """
        if self.workflow_type == "coding":
            return self.coding_metrics
        return self.browser_metrics

    def update_last_task_time(self, timestamp: float) -> None:
        """Thread-safe update of last_task_time.

        Args:
            timestamp: Wall-clock timestamp (from time.time())
        """
        with self._lock:
            self.last_task_time = timestamp

    def get_last_task_time(self) -> float:
        """Thread-safe read of last_task_time.

        Returns:
            Last task execution timestamp
        """
        with self._lock:
            return self.last_task_time


@dataclass
class TestSnapshot:
    """Test snapshot"""

    timestamp: float  # Snapshot timestamp
    elapsed: float  # Time elapsed since test start (seconds)
    total_sandboxes: int  # Total sandbox count
    active_sandboxes: int  # Active sandbox count
    offline_sandboxes: int  # Offline sandbox count
    creation_stats: Dict[str, any] = field(
        default_factory=dict
    )  # {"create": {...}, "port_wait": {...}, "total": {...}}
    # Browser task metrics
    browser_total: int = 0
    browser_success: int = 0
    browser_avg_latency: float = 0.0
    browser_p99_latency: float = 0.0
    # Coding task metrics (populated when workflow_type="coding")
    coding_total: int = 0
    coding_success: int = 0
    coding_verify_success: int = 0  # pairs with a real-assertion verify_script that passed
    coding_compile_only: int = 0  # pairs marked verify: compile_only that compiled+ran (no assertion)
    coding_avg_latency: float = 0.0
    coding_p99_latency: float = 0.0
    # Round comparison fields (proper dataclass fields, not ad-hoc attributes)
    round_total: int = 0
    round_success: int = 0


@dataclass
class BatchTask:
    """Single batch test task parameters"""

    task_id: str  # Unique ID, e.g. "tc10_ratio10_bp0.5"
    total_count: int  # Sandbox count
    benchmark_percent: float  # Percentage of sandboxes for benchmark
    ratio: int  # Memory migration ratio (%)

    # Runtime state (filled after execution)
    result_dir: Optional[str] = None  # Result directory path
    report_file: Optional[str] = None  # bench_report.txt path
    analysis_file: Optional[str] = None  # analysis_report.xlsx path
    browser_metrics: Optional[Dict[str, Any]] = None  # Extracted browser metrics
    coding_metrics: Optional[Dict[str, Any]] = None  # Extracted coding metrics
    vm_metrics: Optional[Dict[str, Any]] = None  # Extracted vm_monitor metrics
    success: bool = False
    error_msg: Optional[str] = None


@dataclass
class TaskGroup:
    """Group of tasks that can reuse the same sandbox set"""

    group_id: str  # Group ID, e.g. "tc10_ratio10"
    total_count: int  # Shared by all tasks in group
    ratio: int  # Shared by all tasks in group
    tasks: List[BatchTask]  # Tasks with different benchmark_percent

    # Runtime state
    sandbox_states: Optional[Dict[int, Any]] = None  # Shared sandbox states
    smap_tool_manager: Optional[Any] = None  # Shared SmapToolManager
