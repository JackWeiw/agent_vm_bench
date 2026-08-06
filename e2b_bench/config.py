"""
Configuration Management Module

Supports YAML config file loading, CLI argument override, E2B environment variable setup
"""

import argparse
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import yaml

from .schemas import (
    DEFAULT_CODING_GO_SOURCE_FILES,
    DEFAULT_CODING_SOURCE_FILES,
    DEFAULT_CODING_VERIFY_SCRIPT_GO,
    DEFAULT_CODING_VERIFY_SCRIPT_JS,
)


DOCUMENT_SCENE_LAYOUTS: Dict[str, Dict[str, str]] = {
    "pdf": {
        "seed_dir": "/opt/document-bench/pdf",
        "workspace_dir": "/root/.openclaw/workspace/tool-modeling/SUB-MEM-PDF-01",
    },
    "xlsx": {
        "seed_dir": "/opt/document-bench/xlsx",
        "workspace_dir": "/root/.openclaw/workspace/tool-modeling/SUB-MEM-OFFICE-01",
    },
}


def document_scene_layout(case_kind: str) -> Dict[str, str]:
    try:
        return DOCUMENT_SCENE_LAYOUTS[case_kind]
    except KeyError:
        raise ValueError("document.case_kind must be 'pdf' or 'xlsx'") from None


def _normalize_numa_bind(value: Any) -> Optional[List[int]]:
    """Normalize numa_bind input to a canonical list of node IDs or None.

    Accepts an int (single node), a list of ints, None, or an empty string.
    Returns None (no binding) for None / empty / all-negative input. NUMA
    node 0 is valid and kept. Negative node IDs are dropped. Duplicate IDs
    are removed, preserving first-seen order.
    """
    # Treat empty string as "no binding" (defensive; YAML null is the norm)
    if value is None or value == "" or value == []:
        return None

    # Single int -> singleton list
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            return None
        return [value]

    # List of ints: dedup preserving order, drop negative (node 0 is valid)
    if isinstance(value, list):
        seen = set()
        normalized: List[int] = []
        for item in value:
            # bool is a subclass of int; reject it explicitly
            if not isinstance(item, int) or isinstance(item, bool):
                raise TypeError(f"numa_bind list items must be ints, got {type(item).__name__}")
            if item < 0:
                continue
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized if normalized else None

    raise TypeError(f"numa_bind must be int, list[int], or null, got {type(value).__name__}")


def numa_node_for_index(index: int, nodes: Optional[Union[int, List[int]]]) -> Optional[int]:
    """Return the NUMA node for a sandbox at the given 0-based index.

    Round-robins across `nodes`. Accepts a list of ints, a single int (treated
    as a one-element list), or None (no binding). NUMA node 0 is valid. Returns
    None when `nodes` is None, an empty list, an empty string, or a negative
    int. Raises TypeError for other types, including bool.
    """
    if nodes is None or nodes == "" or nodes == []:
        return None

    # bool is a subclass of int; reject it explicitly (consistent with
    # _normalize_numa_bind) rather than letting len() fail later.
    if isinstance(nodes, bool):
        raise TypeError(f"numa_bind nodes must be int, list[int], or null, got {type(nodes).__name__}")

    # Tolerate a raw int (constructor passthrough) by treating it as a single node
    if isinstance(nodes, int):
        return nodes if nodes >= 0 else None

    return nodes[index % len(nodes)]


@dataclass(frozen=True)
class CodingLanguageProfile:
    """Per-language profile for the coding verify step.

    The coding workflow loop (find -> read -> edit -> verify -> diff) is identical
    across languages; only the verify mechanics differ. This profile captures
    those differences as data, so adding a new language (e.g. cpp) is a new
    registry entry - no runner code changes.

    Fields:
        temp_test_path: where the ad-hoc test file is written (/tmp/...).
        heredoc_eof: heredoc terminator string ("EOF"/"GOEOF").
        run_cmd: the verify run command (npx tsx ... / go run ...).
        source_find_names: list of -name patterns for the find fallback
            (["*.ts","*.tsx","*.js"] for ts, ["*.go"] for go).
        source_find_root: directory the find fallback searches under
            ("packages" for the vuejs/core monorepo, "." for hugo whose
            source is spread across markup/, hugofs/, ...). Keeps the fallback
            language-aware instead of hardcoding "packages".
        checkout_paths: paths reset by `git checkout --` in the find step.
        default_verify_script: shared default body for pairs without their own.
        pre_verify_cmd: optional command run before the verify write+run (empty
            for languages with no persistent compile cache). Set for go to
            `go clean -cache` so every verify is a real cold-compile: the Go
            toolchain caches compiled stdlib/packages under GOCACHE, so the
            first `go run` pays the full compile (40% CPU) and every later run
            hits cache (10%) - which would NOT reflect the real agent's CPU
            shape. The real openclaw agent never runs `go clean`, but within a
            single issue it repeatedly rewrites its ad-hoc /tmp/test_*.go and
            re-runs `go run`, i.e. each verify is effectively a fresh compile.
            Clearing the cache before each sandbox verify reproduces that
            per-verify cold-compile pressure (the behavior the customer needs
            to see), even though the literal trace shows no `go clean`. ts/tsx
            has no equivalent persistent cache (esbuild re-transpiles every
            run), so it stays empty.
    """

    temp_test_path: str
    heredoc_eof: str
    run_cmd: str
    source_find_names: tuple = ()
    source_find_root: str = "packages"
    checkout_paths: str = ""
    default_verify_script: str = ""
    pre_verify_cmd: str = ""


def _find_name_clause(names: tuple) -> str:
    """Build a `find -name` clause: `\\( -name '*.ts' -o -name '*.go' \\)`."""
    if not names:
        return "-name '*.ts'"
    inner = " -o ".join(f"-name '{n}'" for n in names)
    return f"\\( {inner} \\)" if len(names) > 1 else f"-name '{names[0]}'"


# Extensible language registry. To add a language (cpp, rust, ...): add one
# entry here + its DEFAULT_CODING_*_SOURCE_FILES + default verify script in
# schemas.py. The runner reads the active profile via coding_language.
CODING_LANGUAGE_PROFILES: Dict[str, CodingLanguageProfile] = {
    "ts": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.mjs",
        heredoc_eof="EOF",
        run_cmd="npx tsx /tmp/bench_verify.mjs",
        source_find_names=("*.ts", "*.tsx", "*.js"),
        source_find_root="packages",
        # vuejs/core is a pnpm monorepo: all source lives under packages/<name>/src/,
        # there is NO top-level src/ directory. `git checkout -- packages/ src/` made git
        # emit "pathspec 'src/' did not match any tree entries" (stderr swallowed by 2>/dev/null
        # in the find step -> the misleading "may not be a git repo" warning). packages/ alone
        # covers every edited file.
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
        # Force a cold compile every verify (see pre_verify_cmd docstring): the
        # Go toolchain's GOCACHE makes the first `go run` 40% CPU and every later
        # one ~10% (cache hit). The real agent rewrites+recompiles its ad-hoc
        # test per verify, so per-verify is cold. Clearing the cache reproduces
        # that cold-compile CPU pressure the customer needs to measure.
        pre_verify_cmd="go clean -cache",
    ),
}

# Maps a language to its default replacement-pair list (DEFAULT_CODING_*_SOURCE_FILES).
CODING_LANGUAGE_DEFAULT_SOURCE_FILES: Dict[str, list] = {
    "ts": DEFAULT_CODING_SOURCE_FILES,
    "go": DEFAULT_CODING_GO_SOURCE_FILES,
}


def get_coding_profile(language: str) -> CodingLanguageProfile:
    """Return the CodingLanguageProfile for `language`, falling back to ts."""
    return CODING_LANGUAGE_PROFILES.get(language, CODING_LANGUAGE_PROFILES["ts"])


def _normalize_source_files(raw: Any) -> List[Dict[str, str]]:
    """Normalize coding source files into a list of replacement pairs.

    Accepts:
    - A list of dicts [{"file": str, "find": str, "replace": str}, ...] (canonical form
      used by YAML and DEFAULT_CODING_SOURCE_FILES).
    - A list of bare file-path strings (legacy/CLI raw-file mode). Each is wrapped in a
      generic comment-marker pair so the old single-file workflow still triggers a rebuild.
    - A single bare file-path string (CLI --coding-source-file with one value).

    Returns the canonical list of replacement pairs. Filters out entries missing a `file`.
    """
    if raw is None:
        return list(DEFAULT_CODING_SOURCE_FILES)

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        return list(DEFAULT_CODING_SOURCE_FILES)

    result: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("file"):
            pair = {
                "file": str(item["file"]),
                "find": str(item.get("find", "// bench marker")),
                "replace": str(item.get("replace", f"// bench round\n// bench marker")),
            }
            # Preserve the optional `verify: compile_only` flag (a pair with no
            # assertable semantics, honestly compile-only). verify_script is gone -
            # the verify workload comes from the shared DEFAULT_VERIFY_TEMPLATES pool.
            if item.get("verify"):
                pair["verify"] = str(item["verify"])
            result.append(pair)
        elif isinstance(item, str) and item:
            # CLI raw-file mode: safe generic comment marker (non-breaking, triggers rebuild)
            result.append({"file": item, "find": "// bench marker", "replace": "// bench round\n// bench marker"})
    return result or list(DEFAULT_CODING_SOURCE_FILES)


@dataclass
class Config:
    """Test configuration"""

    # E2B environment variables
    e2b_access_token: str = ""
    e2b_api_key: str = ""
    e2b_domain: str = "e2b.app"
    e2b_api_url: str = "http://localhost:3000"
    e2b_http_ssl: str = "false"

    # Sandbox configuration
    template: str = "openclaw-browser-v1"
    create_timeout: int = 86400
    total_count: int = 100

    # NUMA binding for sandbox creation.
    # Accepts an int (single node), a list of ints (round-robin across nodes),
    # or null (no binding). Defaults to node 2. Normalized to a list or None
    # at load time (see _normalize_numa_bind).
    numa_bind: Optional[Union[int, List[int]]] = 2

    # Detect existing sandboxes mode
    detect_existing: bool = False  # Detect existing sandboxes instead of creating new ones

    # Create-only mode (create sandboxes without running tasks)
    create_only: bool = False

    # Sandbox IDs file (for save/load sandbox IDs)
    sandbox_ids_file: Optional[str] = None

    # Create batch control (for sandbox creation, None means full concurrent)
    create_batch_size: Optional[int] = None
    create_batch_interval: Optional[int] = None

    # Task batch control (for browser task execution, None means full concurrent)
    task_batch_size: Optional[int] = None
    task_batch_interval: Optional[int] = None

    # Benchmark stress percent (percentage of sandboxes to run benchmark)
    benchmark_percent: float = 1.0  # Percentage of sandboxes for benchmark (default 100%)

    # Round-robin mode configuration
    benchmark_mode: str = "fixed"  # "fixed" (default) or "round_robin"
    round_count: Optional[
        int
    ] = None  # Max number of rounds to run (termination condition, coexists with round_size and duration)
    round_size: int = 5  # Sandboxes per round (determines group count, coexists with round_count)
    round_interval: int = 5  # Round interval in seconds for round_robin mode (default: 5s)

    # smap_tool configuration (memory migration monitoring)
    smap_tool_enabled: bool = False
    smap_tool_path: str = ""
    smap_tool_swap_size: int = 81920
    smap_tool_ratio: int = 15
    smap_tool_src_nid: int = 2
    smap_tool_dest_nid: int = 5

    # vm_monitor configuration (performance monitoring)
    vm_monitor_enabled: bool = False
    vm_monitor_vmm_type: str = "firecracker"
    vm_monitor_duration: int = 600
    vm_monitor_numa: str = "1"  # NUMA nodes to monitor, comma-separated (e.g., "0,1")
    vm_monitor_log_dir: str = "results/e2b/vm_monitor"
    vm_monitor_stress_file: str = "/dev/shm/e2b_benchmark_lock"

    # Workflow type selection: determines which runners, metrics, and reports to use
    workflow_type: str = "browser"  # "browser", "coding", or "document"

    # Browser task
    browser_urls: List[str] = field(default_factory=lambda: ["http://192.168.110.10:8080/Weibo.html"])
    browser_timeout: int = 200
    browser_interval_min: float = 0.5
    browser_interval_max: float = 3.0

    # Coding task configuration
    coding_project_dir: str = "/opt/coding-bench"
    # Coding language - selects a CodingLanguageProfile (ts/go/future cpp) which
    # drives the verify step (temp test path, heredoc terminator, run command,
    # source glob, checkout paths). Extensible: adding a language = one registry
    # entry, no runner code changes. See CODING_LANGUAGE_PROFILES below.
    coding_language: str = "ts"
    # Verify step: write an ad-hoc test file to /tmp (heredoc) then run it. The
    # run command comes from the active language profile (npx tsx for ts,
    # go run for go). Mirrors the real openclaw trace's combined write+run.
    coding_verify_cmd: str = "npx tsx /tmp/bench_verify.mjs"
    coding_verify_timeout: int = 120  # Verify command timeout (seconds)
    coding_skip_verify: bool = False  # Skip the verify step (build-only / dry-run)
    # Multi-process verify (ts only): number of independent `npx tsx` processes
    # spun up serially per verify step. Each pays the fixed ~0.47s startup cost
    # (node + esbuild transpile + module graph load) - the only lever proven to
    # raise single-firecracker steady-state CPU while staying trace-faithful
    # (the real agent repeatedly spawns independent npx tsx verifies per issue).
    # N=3 -> ~1.5s/verify -> ~50% peak at round_interval=3s. Go stays N=1 (its
    # go clean -cache cold-compile is already real load). Configurable via
    # coding.verify_repeat (yaml) or --coding-verify-repeat (CLI).
    coding_verify_repeat: int = 3
    # List of replacement pairs: [{"file": str, "find": str, "replace": str,
    # "verify_script": str(optional)}, ...]. Each round applies one pair
    # (round-robin) - a real, type-safe string edit. `verify_script` is the body
    # of the ad-hoc test (between heredoc markers); pairs without it fall back
    # to the shared default verify script for the language. A bare file-path
    # string is accepted (CLI raw-file mode) and normalized to a generic
    # comment-marker pair so the single-file workflow still works.
    coding_source_files: List[Dict[str, str]] = field(default_factory=lambda: list(DEFAULT_CODING_SOURCE_FILES))
    coding_interval_min: float = 2.0  # Interval between coding tasks in fixed mode
    coding_interval_max: float = 10.0

    # PDF/XLSX document task configuration. Recipe and sandbox paths are fixed
    # by case_kind and intentionally are not Config fields.
    document_case_kind: str = "xlsx"
    document_operation_timeout: int = 900
    document_recalc_timeout: int = 600
    document_task_timeout: int = 1800
    document_interval_min: float = 3.0
    document_interval_max: float = 10.0

    # Warmup phase configuration
    warmup_urls: List[str] = field(default_factory=list)  # Warmup page URL list
    warmup_loops: int = 2  # Warmup loop count
    warmup_delay: int = 10  # Delay between warmup pages (seconds)
    warmup_only: bool = False  # Run warmup phase only, then exit

    # Test run
    test_duration: int = 600
    stats_interval: int = 10

    # Report
    output_dir: str = "results/e2b"
    filename_prefix: str = "e2b_bench"

    @classmethod
    def load_from_yaml(cls, path: str) -> "Config":
        """Load configuration from YAML file"""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Build Config from dictionary"""
        e2b_env = data.get("e2b_env", {})
        sandbox = data.get("sandbox", {})
        create_batch = data.get("create_batch", {})
        task_batch = data.get("task_batch", {})
        browser = data.get("browser", {})
        coding = data.get("coding", {})
        document = data.get("document", {})
        forbidden_document_options = {
            "operations_file",
            "max_repair_attempts",
            "workspace_dir",
            "seed_dir",
        } & set(document)
        if forbidden_document_options:
            options = ", ".join(sorted(forbidden_document_options))
            raise ValueError(f"document options are fixed by case_kind and must be removed: {options}")
        test = data.get("test", {})
        report = data.get("report", {})
        smap_tool = data.get("smap_tool", {})
        vm_monitor = data.get("vm_monitor", {})
        # workflow_type is parsed from top-level YAML key, not a section

        return cls(
            e2b_access_token=e2b_env.get("E2B_ACCESS_TOKEN", ""),
            e2b_api_key=e2b_env.get("E2B_API_KEY", ""),
            e2b_domain=e2b_env.get("E2B_DOMAIN", "e2b.app"),
            e2b_api_url=e2b_env.get("E2B_API_URL", "http://localhost:3000"),
            e2b_http_ssl=e2b_env.get("E2B_HTTP_SSL", "false"),
            template=sandbox.get("template", "openclaw-browser-v1"),
            create_timeout=sandbox.get("create_timeout", 86400),
            total_count=sandbox.get("total_count", 100),
            detect_existing=sandbox.get("detect_existing", False),
            create_only=sandbox.get("create_only", False),
            sandbox_ids_file=sandbox.get("sandbox_ids_file", None),
            numa_bind=_normalize_numa_bind(sandbox.get("numa_bind", 2)),
            create_batch_size=create_batch.get("size") if create_batch else None,
            create_batch_interval=create_batch.get("interval") if create_batch else None,
            task_batch_size=task_batch.get("size") if task_batch else None,
            task_batch_interval=task_batch.get("interval") if task_batch else None,
            benchmark_percent=test.get("benchmark_percent", 1.0),
            benchmark_mode=test.get("benchmark_mode", "fixed"),
            round_count=test.get("round_count"),
            round_size=test.get("round_size", 5),
            round_interval=test.get("round_interval", 5),
            workflow_type=data.get("workflow", {}).get("type", data.get("workflow_type", "browser")),
            browser_urls=browser.get("urls", ["http://192.168.110.10:8080/Weibo.html"]),
            browser_timeout=browser.get("task_timeout", 200),
            browser_interval_min=browser.get("interval_min", 0.5),
            browser_interval_max=browser.get("interval_max", 3.0),
            warmup_urls=browser.get("warmup_urls", []),
            warmup_loops=browser.get("warmup_loops", 2),
            warmup_delay=browser.get("warmup_delay", 10),
            warmup_only=browser.get("warmup_only", False),
            coding_project_dir=coding.get("project_dir", "/opt/coding-bench"),
            coding_language=coding.get("language", "ts"),
            coding_verify_cmd=coding.get(
                "verify_cmd",
                # Default verify command from the active language profile (npx tsx
                # for ts, go run for go) when YAML doesn't override it.
                get_coding_profile(coding.get("language", "ts")).run_cmd,
            ),
            coding_verify_timeout=coding.get("verify_timeout", 120),
            coding_skip_verify=coding.get("skip_verify", False),
            coding_verify_repeat=coding.get("verify_repeat", 3),
            coding_source_files=_normalize_source_files(
                coding.get(
                    "source_files",
                    CODING_LANGUAGE_DEFAULT_SOURCE_FILES.get(coding.get("language", "ts"), DEFAULT_CODING_SOURCE_FILES),
                )
            ),
            coding_interval_min=coding.get("interval_min", 2.0),
            coding_interval_max=coding.get("interval_max", 10.0),
            document_case_kind=document.get("case_kind", "xlsx"),
            document_operation_timeout=document.get("operation_timeout", 900),
            document_recalc_timeout=document.get("recalc_timeout", 600),
            document_task_timeout=document.get("task_timeout", 1800),
            document_interval_min=document.get("interval_min", 3.0),
            document_interval_max=document.get("interval_max", 10.0),
            test_duration=test.get("duration", 600),
            stats_interval=test.get("stats_interval", 10),
            output_dir=report.get("output_dir", "results/e2b"),
            filename_prefix=report.get("filename_prefix", "e2b_bench"),
            smap_tool_enabled=smap_tool.get("enabled", False),
            smap_tool_path=smap_tool.get("path", ""),
            smap_tool_swap_size=smap_tool.get("swap_size", 81920),
            smap_tool_ratio=smap_tool.get("ratio", 15),
            smap_tool_src_nid=smap_tool.get("src_nid", 2),
            smap_tool_dest_nid=smap_tool.get("dest_nid", 5),
            vm_monitor_enabled=vm_monitor.get("enabled", False),
            vm_monitor_vmm_type=vm_monitor.get("vmm_type", "firecracker"),
            vm_monitor_duration=vm_monitor.get("duration", 600),
            vm_monitor_numa=vm_monitor.get("numa", "1"),
            vm_monitor_log_dir=vm_monitor.get("log_dir", "results/e2b/vm_monitor"),
            vm_monitor_stress_file=vm_monitor.get("stress_file", "/dev/shm/e2b_benchmark_lock"),
        )

    @classmethod
    def merge_with_args(cls, yaml_config: "Config", args: argparse.Namespace) -> "Config":
        """Merge CLI arguments (CLI has higher priority)"""
        return cls(
            e2b_access_token=args.e2b_access_token
            if args.e2b_access_token is not None
            else yaml_config.e2b_access_token,
            e2b_api_key=args.e2b_api_key if args.e2b_api_key is not None else yaml_config.e2b_api_key,
            e2b_domain=args.e2b_domain if args.e2b_domain is not None else yaml_config.e2b_domain,
            e2b_api_url=args.e2b_api_url if args.e2b_api_url is not None else yaml_config.e2b_api_url,
            e2b_http_ssl=args.e2b_http_ssl if args.e2b_http_ssl is not None else yaml_config.e2b_http_ssl,
            template=args.template if args.template is not None else yaml_config.template,
            create_timeout=args.create_timeout if args.create_timeout is not None else yaml_config.create_timeout,
            total_count=args.total if args.total is not None else yaml_config.total_count,
            detect_existing=args.detect if hasattr(args, "detect") and args.detect else yaml_config.detect_existing,
            create_only=args.create_only
            if hasattr(args, "create_only") and args.create_only
            else yaml_config.create_only,
            sandbox_ids_file=args.sandbox_ids_file
            if args.sandbox_ids_file is not None
            else yaml_config.sandbox_ids_file,
            numa_bind=yaml_config.numa_bind,  # Use yaml config for numa_bind
            create_batch_size=args.create_batch_size
            if args.create_batch_size is not None
            else yaml_config.create_batch_size,
            create_batch_interval=args.create_batch_interval
            if args.create_batch_interval is not None
            else yaml_config.create_batch_interval,
            task_batch_size=args.task_batch_size if args.task_batch_size is not None else yaml_config.task_batch_size,
            task_batch_interval=args.task_batch_interval
            if args.task_batch_interval is not None
            else yaml_config.task_batch_interval,
            browser_urls=args.browser_url if args.browser_url is not None else yaml_config.browser_urls,
            browser_timeout=args.browser_timeout if args.browser_timeout is not None else yaml_config.browser_timeout,
            browser_interval_min=args.browser_interval_min
            if args.browser_interval_min is not None
            else yaml_config.browser_interval_min,
            browser_interval_max=args.browser_interval_max
            if args.browser_interval_max is not None
            else yaml_config.browser_interval_max,
            warmup_urls=args.warmup_url if args.warmup_url is not None else yaml_config.warmup_urls,
            warmup_loops=args.warmup_loops if args.warmup_loops is not None else yaml_config.warmup_loops,
            warmup_delay=args.warmup_delay if args.warmup_delay is not None else yaml_config.warmup_delay,
            warmup_only=args.warmup_only
            if hasattr(args, "warmup_only") and args.warmup_only
            else yaml_config.warmup_only,
            benchmark_percent=args.benchmark_percent
            if args.benchmark_percent is not None
            else yaml_config.benchmark_percent,
            benchmark_mode=getattr(args, "benchmark_mode", None)
            if getattr(args, "benchmark_mode", None) is not None
            else yaml_config.benchmark_mode,
            round_count=getattr(args, "round_count", None)
            if getattr(args, "round_count", None) is not None
            else yaml_config.round_count,
            round_size=getattr(args, "round_size", None)
            if getattr(args, "round_size", None) is not None
            else yaml_config.round_size,
            round_interval=getattr(args, "round_interval", None)
            if getattr(args, "round_interval", None) is not None
            else yaml_config.round_interval,
            workflow_type=getattr(args, "workflow_type", None)
            if getattr(args, "workflow_type", None) is not None
            else yaml_config.workflow_type,
            coding_project_dir=getattr(args, "coding_project_dir", None)
            if getattr(args, "coding_project_dir", None) is not None
            else yaml_config.coding_project_dir,
            coding_language=getattr(args, "coding_language", None)
            if getattr(args, "coding_language", None) is not None
            else yaml_config.coding_language,
            coding_verify_cmd=yaml_config.coding_verify_cmd,  # from language profile / YAML
            coding_verify_timeout=getattr(args, "coding_verify_timeout", None)
            if getattr(args, "coding_verify_timeout", None) is not None
            else yaml_config.coding_verify_timeout,
            coding_source_files=_normalize_source_files(
                getattr(args, "coding_source_file", None)
                if getattr(args, "coding_source_file", None) is not None
                else yaml_config.coding_source_files
            ),
            coding_interval_min=yaml_config.coding_interval_min,
            coding_interval_max=yaml_config.coding_interval_max,
            coding_skip_verify=getattr(args, "coding_skip_verify", False)
            if hasattr(args, "coding_skip_verify") and args.coding_skip_verify
            else yaml_config.coding_skip_verify,
            coding_verify_repeat=getattr(args, "coding_verify_repeat", None)
            if getattr(args, "coding_verify_repeat", None) is not None
            else yaml_config.coding_verify_repeat,
            document_case_kind=getattr(args, "document_case_kind", None)
            if getattr(args, "document_case_kind", None) is not None
            else yaml_config.document_case_kind,
            document_operation_timeout=getattr(args, "document_operation_timeout", None)
            if getattr(args, "document_operation_timeout", None) is not None
            else yaml_config.document_operation_timeout,
            document_recalc_timeout=getattr(args, "document_recalc_timeout", None)
            if getattr(args, "document_recalc_timeout", None) is not None
            else yaml_config.document_recalc_timeout,
            document_task_timeout=getattr(args, "document_task_timeout", None)
            if getattr(args, "document_task_timeout", None) is not None
            else yaml_config.document_task_timeout,
            document_interval_min=yaml_config.document_interval_min,
            document_interval_max=yaml_config.document_interval_max,
            test_duration=args.duration if args.duration is not None else yaml_config.test_duration,
            stats_interval=args.stats_interval if args.stats_interval is not None else yaml_config.stats_interval,
            output_dir=args.output_dir if args.output_dir is not None else yaml_config.output_dir,
            filename_prefix=args.filename_prefix if args.filename_prefix is not None else yaml_config.filename_prefix,
            # smap_tool and vm_monitor - use yaml values (no CLI override for these)
            smap_tool_enabled=yaml_config.smap_tool_enabled,
            smap_tool_path=yaml_config.smap_tool_path,
            smap_tool_swap_size=yaml_config.smap_tool_swap_size,
            smap_tool_ratio=yaml_config.smap_tool_ratio,
            smap_tool_src_nid=yaml_config.smap_tool_src_nid,
            smap_tool_dest_nid=yaml_config.smap_tool_dest_nid,
            vm_monitor_enabled=yaml_config.vm_monitor_enabled,
            vm_monitor_vmm_type=yaml_config.vm_monitor_vmm_type,
            vm_monitor_duration=yaml_config.vm_monitor_duration,
            vm_monitor_numa=yaml_config.vm_monitor_numa,
            vm_monitor_log_dir=yaml_config.vm_monitor_log_dir,
            vm_monitor_stress_file=yaml_config.vm_monitor_stress_file,
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        """Build Config from CLI arguments only (no YAML file)"""
        return cls(
            e2b_access_token=args.e2b_access_token if args.e2b_access_token is not None else "",
            e2b_api_key=args.e2b_api_key if args.e2b_api_key is not None else "",
            e2b_domain=args.e2b_domain if args.e2b_domain is not None else "e2b.app",
            e2b_api_url=args.e2b_api_url if args.e2b_api_url is not None else "http://localhost:3000",
            e2b_http_ssl=args.e2b_http_ssl if args.e2b_http_ssl is not None else "false",
            template=args.template if args.template is not None else "openclaw-browser-v1",
            create_timeout=args.create_timeout if args.create_timeout is not None else 86400,
            total_count=args.total if args.total is not None else 100,
            detect_existing=args.detect if hasattr(args, "detect") and args.detect else False,
            create_only=args.create_only if hasattr(args, "create_only") and args.create_only else False,
            sandbox_ids_file=args.sandbox_ids_file if args.sandbox_ids_file is not None else None,
            numa_bind=_normalize_numa_bind(2),  # Default to NUMA node 2 when using CLI args only
            create_batch_size=args.create_batch_size,
            create_batch_interval=args.create_batch_interval,
            task_batch_size=args.task_batch_size,
            task_batch_interval=args.task_batch_interval,
            browser_urls=args.browser_url
            if args.browser_url is not None
            else ["http://192.168.110.10:8080/Weibo.html"],
            browser_timeout=args.browser_timeout if args.browser_timeout is not None else 200,
            browser_interval_min=args.browser_interval_min if args.browser_interval_min is not None else 0.5,
            browser_interval_max=args.browser_interval_max if args.browser_interval_max is not None else 3.0,
            # Warmup configuration
            warmup_urls=args.warmup_url if args.warmup_url is not None else [],
            warmup_loops=args.warmup_loops if args.warmup_loops is not None else 2,
            warmup_delay=args.warmup_delay if args.warmup_delay is not None else 10,
            warmup_only=args.warmup_only if hasattr(args, "warmup_only") and args.warmup_only else False,
            benchmark_percent=args.benchmark_percent if args.benchmark_percent is not None else 1.0,
            benchmark_mode=getattr(args, "benchmark_mode", None)
            if getattr(args, "benchmark_mode", None) is not None
            else "fixed",
            round_count=getattr(args, "round_count", None),
            round_size=getattr(args, "round_size", None) if getattr(args, "round_size", None) is not None else 5,
            round_interval=getattr(args, "round_interval", None)
            if getattr(args, "round_interval", None) is not None
            else 5,
            workflow_type=getattr(args, "workflow_type", None) or "browser",
            coding_project_dir=getattr(args, "coding_project_dir", "/opt/coding-bench"),
            coding_language=getattr(args, "coding_language", "ts"),
            coding_verify_cmd=get_coding_profile(getattr(args, "coding_language", "ts")).run_cmd,
            coding_verify_timeout=getattr(args, "coding_verify_timeout", 120),
            coding_skip_verify=getattr(args, "coding_skip_verify", False),
            coding_verify_repeat=getattr(args, "coding_verify_repeat", 3),
            coding_source_files=_normalize_source_files(
                getattr(args, "coding_source_file", None)
                if getattr(args, "coding_source_file", None) is not None
                else CODING_LANGUAGE_DEFAULT_SOURCE_FILES.get(
                    getattr(args, "coding_language", "ts"), DEFAULT_CODING_SOURCE_FILES
                )
            ),
            coding_interval_min=2.0,
            coding_interval_max=10.0,
            document_case_kind=getattr(args, "document_case_kind", None) or "xlsx",
            document_operation_timeout=getattr(args, "document_operation_timeout", None) or 900,
            document_recalc_timeout=getattr(args, "document_recalc_timeout", None) or 600,
            document_task_timeout=getattr(args, "document_task_timeout", None) or 1800,
            document_interval_min=3.0,
            document_interval_max=10.0,
            test_duration=args.duration if args.duration is not None else 600,
            stats_interval=args.stats_interval if args.stats_interval is not None else 10,
            output_dir=args.output_dir if args.output_dir is not None else "results/e2b",
            filename_prefix=args.filename_prefix if args.filename_prefix is not None else "e2b_bench",
            smap_tool_enabled=False,
            smap_tool_path="",
            smap_tool_swap_size=81920,
            smap_tool_ratio=15,
            smap_tool_src_nid=2,
            smap_tool_dest_nid=5,
            vm_monitor_enabled=False,
            vm_monitor_vmm_type="firecracker",
            vm_monitor_duration=600,
            vm_monitor_numa="1",
            vm_monitor_log_dir="results/e2b/vm_monitor",
            vm_monitor_stress_file="/dev/shm/e2b_benchmark_lock",
        )

    def setup_e2b_env(self) -> None:
        """Setup E2B SDK environment variables"""
        if self.e2b_access_token:
            os.environ["E2B_ACCESS_TOKEN"] = self.e2b_access_token
        if self.e2b_api_key:
            os.environ["E2B_API_KEY"] = self.e2b_api_key
        if self.e2b_domain:
            os.environ["E2B_DOMAIN"] = self.e2b_domain
        if self.e2b_api_url:
            os.environ["E2B_API_URL"] = self.e2b_api_url
        if self.e2b_http_ssl:
            os.environ["E2B_HTTP_SSL"] = self.e2b_http_ssl

    @property
    def create_batch_count(self) -> int:
        """Calculate create batch count"""
        if not self.create_batch_size:
            return 1  # Full concurrent treated as 1 batch
        return (self.total_count + self.create_batch_size - 1) // self.create_batch_size

    @property
    def task_batch_count(self) -> int:
        """Calculate task batch count (based on ready sandboxes)"""
        # This will be calculated dynamically based on actual ready sandboxes
        # For planning purposes, use total_count as estimate
        if not self.task_batch_size:
            return 1
        return (self.total_count + self.task_batch_size - 1) // self.task_batch_size

    @property
    def benchmark_count(self) -> int:
        """Calculate actual sandbox count for benchmark phase

        Based on benchmark_percent (e.g., 0.5 = 50% of sandboxes)
        """
        return max(1, int(self.total_count * self.benchmark_percent))

    @property
    def document_seed_dir(self) -> str:
        return document_scene_layout(self.document_case_kind)["seed_dir"]

    @property
    def document_workspace_dir(self) -> str:
        return document_scene_layout(self.document_case_kind)["workspace_dir"]

    def validate(self) -> None:
        """Validate config values and raise errors for invalid settings.

        Called after construction to catch configuration mistakes early.
        """
        if self.workflow_type not in {"browser", "coding", "document"}:
            raise ValueError(f"Unsupported workflow_type: {self.workflow_type}")
        if self.round_size <= 0:
            raise ValueError(f"round_size must be > 0, got {self.round_size}")
        if self.workflow_type == "document":
            document_scene_layout(self.document_case_kind)
            if self.document_operation_timeout <= 0:
                raise ValueError("document.operation_timeout must be > 0")
            if self.document_recalc_timeout <= 0:
                raise ValueError("document.recalc_timeout must be > 0")
            if self.document_case_kind == "xlsx" and self.document_recalc_timeout >= self.document_operation_timeout:
                raise ValueError("document.recalc_timeout must be lower than operation_timeout")
            if self.document_task_timeout <= self.document_operation_timeout:
                raise ValueError("document.task_timeout must be greater than operation_timeout")
            if self.document_interval_min < 0 or self.document_interval_max < self.document_interval_min:
                raise ValueError("document interval must satisfy 0 <= interval_min <= interval_max")
