"""
Configuration Management Module

Supports YAML config file loading, CLI argument override, E2B environment variable setup
"""

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import yaml

from .schemas import (
    DEFAULT_CODING_GO_SOURCE_FILES,
    DEFAULT_CODING_PY_SOURCE_FILES,
    DEFAULT_CODING_SOURCE_FILES,
    DEFAULT_CODING_VERIFY_SCRIPT_GO,
    DEFAULT_CODING_VERIFY_SCRIPT_JS,
    DEFAULT_CODING_VERIFY_SCRIPT_PY,
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
    "python": CodingLanguageProfile(
        temp_test_path="/tmp/bench_verify.py",
        heredoc_eof="PYEOF",
        run_cmd="python3 /tmp/bench_verify.py",
        source_find_names=("*.py",),
        source_find_root=".",
        # django/django: the framework package (all edits live under django/). Only
        # that subtree is reset; config/support files (pyproject.toml, tests/) persist.
        checkout_paths="django/",
        default_verify_script=DEFAULT_CODING_VERIFY_SCRIPT_PY,
        # No pre_verify_cmd (like ts): Python's __pycache__ holds cheap bytecode,
        # not compiled types, so the in-memory django module graph (the actual
        # verify peak) is unchanged warm or cold. A plain single write+run is the
        # trace-faithful shape - no `go clean`-style cold-cache reset needed.
    ),
}

# Maps a language to its default replacement-pair list (DEFAULT_CODING_*_SOURCE_FILES).
CODING_LANGUAGE_DEFAULT_SOURCE_FILES: Dict[str, list] = {
    "ts": DEFAULT_CODING_SOURCE_FILES,
    "go": DEFAULT_CODING_GO_SOURCE_FILES,
    "python": DEFAULT_CODING_PY_SOURCE_FILES,
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
            # assertable semantics, honestly compile-only).
            if item.get("verify"):
                pair["verify"] = str(item["verify"])
            # Preserve `verify_script` when present (Go and Python pairs carry their
            # own ad-hoc verify scripts; ts pairs source from the shared pool).
            if item.get("verify_script"):
                pair["verify_script"] = str(item["verify_script"])
            result.append(pair)
        elif isinstance(item, str) and item:
            # CLI raw-file mode: safe generic comment marker (non-breaking, triggers rebuild)
            result.append({"file": item, "find": "// bench marker", "replace": "// bench round\n// bench marker"})
    return result or list(DEFAULT_CODING_SOURCE_FILES)


# Single source of truth for Config construction. _FIELDS lists each field once
# (CLI attr, YAML source, default, transform, merge/from_args rules); one _build
# loop resolves them for all three paths (_from_dict / merge_with_args / from_args),
# which are now thin wrappers — replacing the former three near-duplicate kwargs blocks.

# Sentinel for "no value found" (distinct from None, which is a valid value).
_MISSING: Any = object()

# Sentinel default for _FieldSpec.cli: "the argparse attr equals the field name".
# Resolved to the field name in __post_init__, so rows with cli==field just omit cli.
# A field with no CLI override sets cli=None explicitly; a different dest sets cli="...".
_SAME: Any = object()

# Every YAML section Config reads. _sections normalizes absent/null to {} so
# extractors can call .get() without NoneType guards.
_SECTION_NAMES = (
    "e2b_env",
    "sandbox",
    "create_batch",
    "task_batch",
    "browser",
    "coding",
    "document",
    "test",
    "report",
    "smap_tool",
    "vm_monitor",
)


def _sections(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return every YAML section as a dict (absent or null section -> {})."""
    return {name: (data.get(name) or {}) for name in _SECTION_NAMES}


def _extract_workflow_type(data: Dict[str, Any], sec: Dict[str, Dict[str, Any]]) -> Any:
    """workflow.type wins over the legacy top-level workflow_type; _MISSING if neither."""
    wf = data.get("workflow") or {}
    val = wf.get("type", _MISSING)
    if val is _MISSING:
        val = data.get("workflow_type", _MISSING)
    return val


# Default E2B CLI config path. The E2B_CONFIG env var overrides it (mirrors
# e2b_bench/scripts/delete_sandbox.sh), so tests / alternate installs can point
# elsewhere without touching the real ~/.e2b/config.json.
DEFAULT_E2B_CONFIG_PATH = str(Path.home() / ".e2b" / "config.json")


def _load_e2b_cli_config(path: Optional[str] = None) -> Dict[str, str]:
    """Read the E2B CLI config (~/.e2b/config.json) for credentials.

    Mirrors ``e2b_bench/scripts/delete_sandbox.sh``: the path defaults to
    ``~/.e2b/config.json`` and is overridable via the ``E2B_CONFIG`` env var.
    Returns ``{"api_key": ..., "access_token": ...}``; each is ``""`` when the
    file is absent, unreadable, or the key is missing, so callers fall back to
    it transparently without raising.
    """
    cfg_path = path or os.environ.get("E2B_CONFIG") or DEFAULT_E2B_CONFIG_PATH
    creds: Dict[str, str] = {"api_key": "", "access_token": ""}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = json.load(f)
        creds["api_key"] = str(data.get("teamApiKey") or "")
        creds["access_token"] = str(data.get("accessToken") or "")
    except (OSError, ValueError):
        # File absent / unreadable / bad JSON -> stay empty; setup_e2b_env then
        # just leaves the env vars unset (same behavior as before this fallback).
        pass
    return creds


def _verify_cmd_default(ctx: Dict[str, Any]) -> str:
    """Default coding_verify_cmd from the active language profile's run command."""
    return get_coding_profile(ctx.get("coding_language", "ts")).run_cmd


def _source_files_default(ctx: Dict[str, Any]) -> List[Dict[str, str]]:
    """Default coding source-file pairs for the active language."""
    lang = ctx.get("coding_language", "ts")
    return CODING_LANGUAGE_DEFAULT_SOURCE_FILES.get(lang, DEFAULT_CODING_SOURCE_FILES)


def _browser_urls_default(ctx: Dict[str, Any]) -> List[str]:
    return ["http://192.168.110.10:8080/Weibo.html"]


def _empty_list(ctx: Dict[str, Any]) -> list:
    """Factory for list-valued defaults (avoids shared mutable defaults)."""
    return []


@dataclass(frozen=True)
class _FieldSpec:
    """One Config field's resolution rules across all three construction paths.

    field: Config dataclass attribute name.
    cli:   argparse attribute. Omitted (the default) -> the field name (auto),
           or None for yaml_only fields (no CLI). An explicit dest overrides
           (e.g. total_count -> "total"); None forces no-CLI on a non-yaml_only field.
    y:     YAML source: a (section, key) tuple -> section.get(key), or a callable
           (data, sec) -> value for special cases. _MISSING = absent.
    d:     static default when YAML and CLI are both absent.
    df:    dynamic default (ctx) -> value; receives resolved-so-far fields so a
           default can depend on an earlier one (e.g. coding_verify_cmd on
           coding_language). Overrides d.
    t:     optional transform on the resolved value. Skipped in merge for
           yaml_only fields (their value already came from a built Config).
    m:     merge rule: "cli_or_yaml" (None-check), "truthy_or_yaml" (bool flags),
           or "yaml_only" (no CLI; value taken from yaml_config).
    fa:    from_args rule: "none" (None-check), "truthy" (falsy -> default),
           or "getattr" (present-as-None kept; absent -> default).
    """

    field: str
    cli: Any = _SAME
    y: Any = None
    d: Any = _MISSING
    df: Optional[Callable[..., Any]] = None
    t: Optional[Callable[..., Any]] = None
    m: str = "cli_or_yaml"
    fa: str = "none"

    def __post_init__(self) -> None:
        if self.cli is _SAME:
            # No explicit CLI: yaml_only fields have none, others share the field name.
            object.__setattr__(self, "cli", None if self.m == "yaml_only" else self.field)


# Compact alias so each table row fits on one line.
_F = _FieldSpec


def _default(spec: _FieldSpec, ctx: Dict[str, Any]) -> Any:
    """Resolve a spec's default: dynamic (df) wins over static (d)."""
    return spec.df(ctx) if spec.df else spec.d


def _yaml_val(spec: _FieldSpec, data: Dict[str, Any], sec: Dict[str, Dict[str, Any]]) -> Any:
    """Extract a field's value from raw YAML data, or _MISSING if absent."""
    y = spec.y
    if y is None:
        return _MISSING
    if callable(y):
        return y(data, sec)
    section, key = y
    return sec[section].get(key, _MISSING)


# Order matters where a default depends on an earlier field: coding_language is
# declared before coding_verify_cmd and coding_source_files, which read it from
# the resolved-so-far context.
_FIELDS: List[_FieldSpec] = [
    # --- E2B environment ---
    _F("e2b_access_token", y=("e2b_env", "E2B_ACCESS_TOKEN"), d=""),
    _F("e2b_api_key", y=("e2b_env", "E2B_API_KEY"), d=""),
    _F("e2b_domain", y=("e2b_env", "E2B_DOMAIN"), d="e2b.app"),
    _F("e2b_api_url", y=("e2b_env", "E2B_API_URL"), d="http://localhost:3000"),
    _F("e2b_http_ssl", y=("e2b_env", "E2B_HTTP_SSL"), d="false"),
    # --- Sandbox ---
    _F("template", y=("sandbox", "template"), d="openclaw-browser-v1"),
    _F("create_timeout", y=("sandbox", "create_timeout"), d=86400),
    _F("total_count", cli="total", y=("sandbox", "total_count"), d=100),
    _F("numa_bind", y=("sandbox", "numa_bind"), d=2, t=_normalize_numa_bind, m="yaml_only"),
    _F("detect_existing", cli="detect", y=("sandbox", "detect_existing"), d=False, m="truthy_or_yaml", fa="truthy"),
    _F("create_only", y=("sandbox", "create_only"), d=False, m="truthy_or_yaml", fa="truthy"),
    _F("sandbox_ids_file", y=("sandbox", "sandbox_ids_file"), d=None),
    # --- Batch control ---
    _F("create_batch_size", y=("create_batch", "size"), d=None),
    _F("create_batch_interval", y=("create_batch", "interval"), d=None),
    _F("task_batch_size", y=("task_batch", "size"), d=None),
    _F("task_batch_interval", y=("task_batch", "interval"), d=None),
    # --- Benchmark / round-robin ---
    _F("benchmark_percent", y=("test", "benchmark_percent"), d=1.0),
    _F("benchmark_mode", y=("test", "benchmark_mode"), d="fixed"),
    _F("round_count", y=("test", "round_count"), d=None),
    _F("round_size", y=("test", "round_size"), d=5),
    _F("round_interval", y=("test", "round_interval"), d=5),
    # --- Workflow type (dual-key: workflow.type then top-level workflow_type) ---
    _F("workflow_type", y=_extract_workflow_type, d="browser", fa="truthy"),
    # --- Browser ---
    _F("browser_urls", cli="browser_url", y=("browser", "urls"), df=_browser_urls_default),
    _F("browser_timeout", y=("browser", "task_timeout"), d=200),
    _F("browser_interval_min", y=("browser", "interval_min"), d=0.5),
    _F("browser_interval_max", y=("browser", "interval_max"), d=3.0),
    # --- Warmup ---
    _F("warmup_urls", cli="warmup_url", y=("browser", "warmup_urls"), df=_empty_list),
    _F("warmup_loops", y=("browser", "warmup_loops"), d=2),
    _F("warmup_delay", y=("browser", "warmup_delay"), d=10),
    _F("warmup_only", y=("browser", "warmup_only"), d=False, m="truthy_or_yaml", fa="truthy"),
    # --- Coding (language before its dependents) ---
    _F("coding_project_dir", y=("coding", "project_dir"), d="/opt/coding-bench", fa="getattr"),
    _F("coding_language", y=("coding", "language"), d="ts", fa="getattr"),
    _F("coding_verify_cmd", y=("coding", "verify_cmd"), df=_verify_cmd_default, m="yaml_only"),
    _F("coding_verify_timeout", y=("coding", "verify_timeout"), d=120, fa="getattr"),
    _F("coding_skip_verify", y=("coding", "skip_verify"), d=False, m="truthy_or_yaml", fa="getattr"),
    _F("coding_verify_repeat", y=("coding", "verify_repeat"), d=3, fa="getattr"),
    _F(
        "coding_source_files",
        cli="coding_source_file",
        y=("coding", "source_files"),
        df=_source_files_default,
        t=_normalize_source_files,
    ),
    _F("coding_interval_min", y=("coding", "interval_min"), d=2.0, m="yaml_only"),
    _F("coding_interval_max", y=("coding", "interval_max"), d=10.0, m="yaml_only"),
    # --- Document (truthy from_args: getattr(...,None) or default) ---
    _F("document_case_kind", y=("document", "case_kind"), d="xlsx", fa="truthy"),
    _F("document_operation_timeout", y=("document", "operation_timeout"), d=900, fa="truthy"),
    _F("document_recalc_timeout", y=("document", "recalc_timeout"), d=600, fa="truthy"),
    _F("document_task_timeout", y=("document", "task_timeout"), d=1800, fa="truthy"),
    _F("document_interval_min", y=("document", "interval_min"), d=3.0, m="yaml_only"),
    _F("document_interval_max", y=("document", "interval_max"), d=10.0, m="yaml_only"),
    # --- Test run ---
    _F("test_duration", cli="duration", y=("test", "duration"), d=600),
    _F("stats_interval", y=("test", "stats_interval"), d=10),
    # --- Report ---
    _F("output_dir", y=("report", "output_dir"), d="results/e2b"),
    _F("filename_prefix", y=("report", "filename_prefix"), d="e2b_bench"),
    # --- smap_tool (yaml-only in merge; hardcoded default in from_args) ---
    _F("smap_tool_enabled", y=("smap_tool", "enabled"), d=False, m="yaml_only"),
    _F("smap_tool_path", y=("smap_tool", "path"), d="", m="yaml_only"),
    _F("smap_tool_swap_size", y=("smap_tool", "swap_size"), d=81920, m="yaml_only"),
    _F("smap_tool_ratio", y=("smap_tool", "ratio"), d=15, m="yaml_only"),
    _F("smap_tool_src_nid", y=("smap_tool", "src_nid"), d=2, m="yaml_only"),
    _F("smap_tool_dest_nid", y=("smap_tool", "dest_nid"), d=5, m="yaml_only"),
    # --- vm_monitor (yaml-only in merge; hardcoded default in from_args) ---
    _F("vm_monitor_enabled", y=("vm_monitor", "enabled"), d=False, m="yaml_only"),
    _F("vm_monitor_vmm_type", y=("vm_monitor", "vmm_type"), d="firecracker", m="yaml_only"),
    _F("vm_monitor_duration", y=("vm_monitor", "duration"), d=600, m="yaml_only"),
    _F("vm_monitor_numa", y=("vm_monitor", "numa"), d="1", m="yaml_only"),
    _F("vm_monitor_log_dir", y=("vm_monitor", "log_dir"), d="results/e2b/vm_monitor", m="yaml_only"),
    _F("vm_monitor_stress_file", y=("vm_monitor", "stress_file"), d="/dev/shm/e2b_benchmark_lock", m="yaml_only"),
]


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
        return cls._build(data, None, None)

    @classmethod
    def merge_with_args(cls, yaml_config: "Config", args: argparse.Namespace) -> "Config":
        """Merge CLI arguments (CLI has higher priority)"""
        return cls._build(None, yaml_config, args)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        """Build Config from CLI arguments only (no YAML file)"""
        return cls._build(None, None, args)

    @classmethod
    def _build(cls, data, yaml_config, args) -> "Config":
        """Single source-of-truth builder for all three construction paths.

        _from_dict(data)            -> _build(data, None, None)
        merge_with_args(yaml, args)  -> _build(None, yaml, args)
        from_args(args)             -> _build(None, None, args)

        Resolves every field in _FIELDS against whichever sources are present,
        preserving the exact priority semantics of the former hand-written
        kwargs (CLI > YAML > default, with per-field None-check vs truthiness
        and dynamic language-dependent defaults).
        """
        sec = _sections(data) if data is not None else None
        resolved: Dict[str, Any] = {}
        for spec in _FIELDS:
            resolved[spec.field] = cls._resolve_one(spec, data, sec, yaml_config, args, resolved)
        return cls(**resolved)

    @staticmethod
    def _resolve_one(spec, data, sec, yaml_config, args, ctx) -> Any:
        """Resolve one field's value from the available sources."""
        # 1. YAML value.
        if data is not None:
            yv = _yaml_val(spec, data, sec)  # _from_dict: extract from raw dict
        elif yaml_config is not None:
            yv = getattr(yaml_config, spec.field)  # merge: already-resolved Config attr
        else:
            yv = _MISSING  # from_args: no YAML

        # 2. CLI value (_MISSING when the spec has no CLI attr or args is absent).
        cv = _MISSING
        if spec.cli is not None and args is not None:
            cv = getattr(args, spec.cli, _MISSING)

        # 3. Resolve per path.
        if data is not None:
            val = yv if yv is not _MISSING else _default(spec, ctx)
        elif yaml_config is not None:
            if spec.m == "yaml_only" or cv is _MISSING:
                val = yv
            elif spec.m == "truthy_or_yaml":
                val = cv if cv else yv
            else:  # cli_or_yaml
                val = cv if cv is not None else yv
        else:  # from_args
            if cv is not _MISSING:
                if spec.fa == "getattr":
                    val = cv  # attr present: keep as-is (even None)
                elif spec.fa == "truthy":
                    val = cv if cv else _default(spec, ctx)
                else:  # none
                    val = cv if cv is not None else _default(spec, ctx)
            else:
                val = _default(spec, ctx)

        # 4. Transform — skipped for yaml_only merge, whose value already came
        #    from a built Config and is therefore final (e.g. numa_bind).
        if spec.t is not None and not (yaml_config is not None and spec.m == "yaml_only"):
            val = spec.t(val)
        return val

    def setup_e2b_env(self) -> None:
        """Set E2B SDK environment variables.

        Credentials fall back to the E2B CLI config (~/.e2b/config.json) when not
        set on this Config (YAML/CLI), so a user can authenticate via the CLI
        config without repeating key/token in YAML. Mirrors
        ``e2b_bench/scripts/delete_sandbox.sh``.
        """
        file_creds = _load_e2b_cli_config()
        token = self.e2b_access_token or file_creds["access_token"]
        api_key = self.e2b_api_key or file_creds["api_key"]
        if token:
            os.environ["E2B_ACCESS_TOKEN"] = token
        if api_key:
            os.environ["E2B_API_KEY"] = api_key
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
