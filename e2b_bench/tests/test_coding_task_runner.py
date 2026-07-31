"""Unit tests for CodingMetrics, coding config, and workflow dispatch"""

import threading
import unittest
from e2b_bench.config import Config, CODING_LANGUAGE_PROFILES, _find_name_clause, get_coding_profile
from e2b_bench.schemas import CodingMetrics, CODING_STEP_ORDER, BROWSER_STEP_ORDER, SandboxState


class TestCodingMetrics(unittest.TestCase):
    """Test CodingMetrics thread-safety and functionality"""

    def test_basic_add(self):
        """Test basic metrics recording"""
        m = CodingMetrics()
        m.add(1.5, True, step_times={"verify": 1.2}, verify_success=True)
        self.assertEqual(m.total_tasks, 1)
        self.assertEqual(m.success_count, 1)
        self.assertEqual(m.failed_count, 0)
        self.assertEqual(m.verify_success_count, 1)
        self.assertAlmostEqual(m.avg_latency, 1.5)

    def test_failed_task(self):
        """Test recording a failed task"""
        m = CodingMetrics()
        m.add(3.0, False, verify_success=False)
        self.assertEqual(m.total_tasks, 1)
        self.assertEqual(m.success_count, 0)
        self.assertEqual(m.failed_count, 1)
        self.assertEqual(m.verify_success_count, 0)

    def test_timeout_task(self):
        """Test recording a timed-out task"""
        m = CodingMetrics()
        m.add(5.0, False, timeout=True, verify_success=False)
        self.assertEqual(m.timeout_count, 1)
        self.assertEqual(m.failed_count, 1)

    def test_verify_success_tracking(self):
        """Test verify success/failure counters"""
        m = CodingMetrics()
        m.add(2.0, True, verify_success=True)
        m.add(2.0, False, verify_success=False)
        self.assertEqual(m.verify_success_count, 1)
        self.assertEqual(m.failed_count, 1)

    def test_compile_only_tracked_separately(self):
        """compile_only passes are counted separately from real-assertion verify passes."""
        m = CodingMetrics()
        m.add(1.0, True, verify_success=True)  # real-assertion pass
        m.add(1.0, True, compile_only=True)  # compile-only pass (no assertion)
        m.add(1.0, True, verify_success=True, compile_only=False)
        self.assertEqual(m.verify_success_count, 2)  # assertion passes only
        self.assertEqual(m.compile_only_count, 1)  # compile-only passes only
        self.assertEqual(m.success_count, 3)  # both still count as task success

    def test_step_times(self):
        """Test step-level timing recording"""
        m = CodingMetrics()
        step_times = {
            "find": 0.1,
            "read": 0.05,
            "edit": 0.04,
            "verify": 1.2,
            "diff": 0.02,
        }
        m.add(1.5, True, step_times=step_times, verify_success=True)

        stats = m.get_step_stats()
        self.assertEqual(set(stats.keys()), set(step_times.keys()))
        self.assertAlmostEqual(stats["verify"]["avg"], 1.2)
        self.assertEqual(stats["verify"]["count"], 1)

    def test_multiple_tasks_p99(self):
        """Test p99 latency with multiple tasks"""
        m = CodingMetrics()
        for i in range(10):
            m.add(float(i), True, verify_success=True)
        self.assertEqual(m.total_tasks, 10)
        self.assertEqual(m.success_count, 10)
        # p99 with <100 samples = max
        self.assertAlmostEqual(m.p99_latency, 9.0)

    def test_get_latencies_since(self):
        """Test get_latencies_since for round delta calculation"""
        m = CodingMetrics()
        m.add(1.0, True, verify_success=True)
        m.add(2.0, True, verify_success=True)
        m.add(3.0, True, verify_success=True)

        # Get latencies after count 1
        since = m.get_latencies_since(1)
        self.assertEqual(len(since), 2)
        self.assertAlmostEqual(since[0], 2.0)
        self.assertAlmostEqual(since[1], 3.0)

        # Get latencies after count >= total
        since_empty = m.get_latencies_since(10)
        self.assertEqual(len(since_empty), 0)

    def test_last_error(self):
        """Test last_error property"""
        m = CodingMetrics()
        self.assertEqual(m.last_error, "")
        m.add(1.0, False)
        m.last_error = "verify failed: exit_code=1"
        self.assertEqual(m.last_error, "verify failed: exit_code=1")


class TestCodingConfig(unittest.TestCase):
    """Test Config with coding workflow type"""

    def test_default_workflow_type(self):
        """Test default workflow type is browser"""
        c = Config()
        self.assertEqual(c.workflow_type, "browser")

    def test_coding_workflow_type(self):
        """Test setting workflow type to coding"""
        c = Config(workflow_type="coding")
        self.assertEqual(c.workflow_type, "coding")

    def test_coding_config_defaults(self):
        """Test coding config default values"""
        c = Config(workflow_type="coding")
        self.assertEqual(c.coding_project_dir, "/opt/coding-bench")
        self.assertEqual(c.coding_language, "ts")
        self.assertEqual(c.coding_verify_cmd, "npx tsx /tmp/bench_verify.mjs")
        self.assertEqual(c.coding_verify_timeout, 120)
        self.assertEqual(c.coding_skip_verify, False)
        # DEFAULT_CODING_SOURCE_FILES is a list of {file, find, replace} pairs
        self.assertEqual(len(c.coding_source_files), 6)
        first = c.coding_source_files[0]
        self.assertIn("file", first)
        self.assertIn("find", first)
        self.assertIn("replace", first)

    def test_yaml_coding_config(self):
        """Test loading coding config from YAML file"""
        c = Config.load_from_yaml("config/e2b/coding_bench.yaml")
        self.assertEqual(c.workflow_type, "coding")
        self.assertEqual(c.template, "openclaw-coding-v1")
        self.assertEqual(c.coding_project_dir, "/opt/coding-bench")
        self.assertEqual(len(c.coding_source_files), 6)
        self.assertEqual(c.benchmark_mode, "round_robin")

    def test_yaml_coding_language_and_verify_cmd(self):
        """YAML configures the language and the verify command (npx tsx)"""
        c = Config.load_from_yaml("config/e2b/coding_bench.yaml")
        self.assertEqual(c.coding_language, "ts")
        self.assertEqual(c.coding_verify_cmd, "npx tsx /tmp/bench_verify.mjs")
        self.assertEqual(c.coding_verify_timeout, 120)
        self.assertEqual(c.coding_skip_verify, False)

    def test_yaml_coding_source_files_are_vuejs_pairs(self):
        """YAML source_files are verified vuejs/core paths with find/replace"""
        c = Config.load_from_yaml("config/e2b/coding_bench.yaml")
        files = [p["file"] for p in c.coding_source_files]
        # All targets are real vuejs/core paths under packages/
        self.assertTrue(all(f.startswith("packages/") for f in files), files)
        # Each pair carries a non-empty find and replace
        for p in c.coding_source_files:
            self.assertTrue(p["find"])
            self.assertTrue(p["replace"])

    def test_yaml_browser_config_unaffected(self):
        """Test that browser config loading still works"""
        c = Config.load_from_yaml("config/e2b/bench.yaml")
        self.assertEqual(c.workflow_type, "browser")
        self.assertEqual(c.template, "openclaw-browser-v1")

    def test_coding_verify_repeat_default(self):
        """Default verify_repeat is 3 (ts path: N independent npx tsx processes per verify)."""
        c = Config(workflow_type="coding")
        self.assertEqual(c.coding_verify_repeat, 3)

    def test_yaml_coding_verify_repeat_loaded(self):
        """YAML coding.verify_repeat is loaded (coding_bench.yaml sets 3)."""
        c = Config.load_from_yaml("config/e2b/coding_bench.yaml")
        self.assertEqual(c.coding_verify_repeat, 3)

    def test_yaml_go_verify_repeat_is_one(self):
        """Go config sets verify_repeat: 1 (go cold-compile is already real load)."""
        c = Config.load_from_yaml("config/e2b/coding_go_bench.yaml")
        self.assertEqual(c.coding_verify_repeat, 1)


class TestStepOrderConstants(unittest.TestCase):
    """Test step order constants"""

    def test_coding_step_order(self):
        """Test coding step order matches the trace-faithful steps"""
        self.assertEqual(CODING_STEP_ORDER, ["find", "read", "edit", "verify", "diff"])

    def test_browser_step_order(self):
        """Test browser step order unchanged"""
        self.assertEqual(BROWSER_STEP_ORDER, ["open_tab", "page_load", "snapshot", "click", "screenshot"])


class TestSandboxStateCodingMetrics(unittest.TestCase):
    """Test SandboxState with coding_metrics field"""

    def test_coding_metrics_default(self):
        """Test SandboxState has coding_metrics by default"""
        state = SandboxState(sandbox_id=1)
        self.assertIsNotNone(state.coding_metrics)
        self.assertEqual(state.coding_metrics.total_tasks, 0)

    def test_browser_metrics_unchanged(self):
        """Test browser_metrics still works"""
        state = SandboxState(sandbox_id=1)
        self.assertIsNotNone(state.browser_metrics)
        self.assertEqual(state.browser_metrics.total_tasks, 0)


class TestCodingLanguageProfiles(unittest.TestCase):
    """Test the language-profile registry (extensible: ts/go, future cpp)."""

    def test_ts_profile(self):
        """ts profile: npx tsx verify, EOF heredoc, ts/tsx/js glob."""
        p = get_coding_profile("ts")
        self.assertEqual(p.temp_test_path, "/tmp/bench_verify.mjs")
        self.assertEqual(p.heredoc_eof, "EOF")
        self.assertEqual(p.run_cmd, "npx tsx /tmp/bench_verify.mjs")
        self.assertIn("*.ts", p.source_find_names)
        # ts/tsx has no persistent compile cache (esbuild re-transpiles every
        # run), so no pre-verify cache clear.
        self.assertEqual(p.pre_verify_cmd, "")

    def test_go_profile(self):
        """go profile: go run verify, GOEOF heredoc, *.go glob."""
        p = get_coding_profile("go")
        self.assertEqual(p.temp_test_path, "/tmp/bench_verify.go")
        self.assertEqual(p.heredoc_eof, "GOEOF")
        self.assertEqual(p.run_cmd, "go run /tmp/bench_verify.go")
        self.assertIn("*.go", p.source_find_names)
        # go caches compiled stdlib/packages under GOCACHE; clearing it before
        # every verify forces a real cold-compile (the real agent rewrites its
        # ad-hoc test and recompiles per verify, so per-verify is cold).
        self.assertEqual(p.pre_verify_cmd, "go clean -cache")

    def test_unknown_language_falls_back_to_ts(self):
        """An unregistered language falls back to the ts profile."""
        p = get_coding_profile("rust-not-yet")
        self.assertEqual(p.run_cmd, "npx tsx /tmp/bench_verify.mjs")

    def test_find_name_clause_single(self):
        """Single -name pattern is emitted bare."""
        self.assertEqual(_find_name_clause(("*.go",)), "-name '*.go'")

    def test_find_name_clause_multi(self):
        """Multiple patterns are grouped with -o inside \\( ... \\)."""
        clause = _find_name_clause(("*.ts", "*.tsx", "*.js"))
        self.assertIn("\\(", clause)
        self.assertIn("\\)", clause)
        self.assertIn("-name '*.ts'", clause)
        self.assertIn("-name '*.tsx'", clause)
        self.assertIn("-name '*.js'", clause)


class _FakeResult:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeCommands:
    """Captures commands.run() invocations for verify behavior tests."""

    def __init__(self, result=None):
        self.calls = []  # list of (cmd, kwargs)
        self._result = result or _FakeResult(exit_code=0, stdout="All tests passed!", stderr="")

    def run(self, cmd, timeout=None, user=None, background=False):
        self.calls.append((cmd, {"timeout": timeout, "background": background}))
        return self._result


class _FakeSbx:
    def __init__(self, result=None):
        self.sandbox_id = "fake"
        self.commands = _FakeCommands(result=result)


class TestStepVerify(unittest.TestCase):
    """Verify the trace-faithful multi-process _step_verify."""

    def _make_runner(self, config, round_id=0):
        from e2b_bench.coding_task_runner import CodingRoundRunner

        state = SandboxState(sandbox_id=1, workflow_type="coding")
        return CodingRoundRunner(state=state, config=config, stop_event=threading.Event(), round_id=round_id)

    def test_ts_verify_chains_n_write_run_in_one_command(self):
        """ts verify with repeat=3 emits ONE commands.run chaining 3 cat>npx tsx pairs (&& fail-fast)."""
        config = Config(workflow_type="coding", coding_language="ts", coding_verify_repeat=3)
        runner = self._make_runner(config, round_id=0)
        sbx = _FakeSbx()
        pair = {"file": "packages/shared/src/general.ts", "find": "x", "replace": "y"}
        ok, err, compile_only = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertTrue(ok, err)
        self.assertFalse(compile_only)  # pool entries have real assertions, not compile-only
        # ONE commands.run call carrying all 3 chained write+run pairs.
        self.assertEqual(len(sbx.commands.calls), 1)
        cmd = sbx.commands.calls[0][0]
        self.assertEqual(cmd.count("cat > /tmp/bench_verify_"), 3)
        self.assertEqual(cmd.count("npx tsx /tmp/bench_verify_"), 3)
        # Distinct temp files (0/1/2), && fail-fast chaining.
        self.assertIn("/tmp/bench_verify_0.mjs", cmd)
        self.assertIn("/tmp/bench_verify_1.mjs", cmd)
        self.assertIn("/tmp/bench_verify_2.mjs", cmd)
        self.assertIn("&&", cmd)
        # Each body imports compiler-core + baseParse (the agent's verify entry).
        self.assertEqual(cmd.count("compiler-core/src/index.ts"), 3)
        self.assertEqual(cmd.count("baseParse"), 3)
        # ts has no pre-verify cache clear.
        self.assertNotIn("go clean", cmd)

    def test_ts_verify_repeat_one_is_single_process(self):
        """ts verify with repeat=1 emits one cat>npx tsx pair (no chain)."""
        config = Config(workflow_type="coding", coding_language="ts", coding_verify_repeat=1)
        runner = self._make_runner(config, round_id=0)
        sbx = _FakeSbx()
        pair = {"file": "packages/shared/src/general.ts", "find": "x", "replace": "y"}
        ok, _err, _co = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertTrue(ok)
        self.assertEqual(len(sbx.commands.calls), 1)
        cmd = sbx.commands.calls[0][0]
        self.assertEqual(cmd.count("cat > /tmp/bench_verify_"), 1)

    def test_ts_verify_templates_offset_by_round(self):
        """Round N picks a different N-subset of the pool (offset by round_id % pool_len)
        so consecutive rounds don't repeat identical bytes."""
        from e2b_bench.schemas import DEFAULT_VERIFY_TEMPLATES

        config = Config(workflow_type="coding", coding_language="ts", coding_verify_repeat=2)
        runner0 = self._make_runner(config, round_id=0)
        runner1 = self._make_runner(config, round_id=1)
        pair = {"file": "packages/shared/src/general.ts", "find": "x", "replace": "y"}
        sbx0 = _FakeSbx()
        sbx1 = _FakeSbx()
        runner0._step_verify(sbx0, "/opt/coding-bench", pair, step_times={})
        runner1._step_verify(sbx1, "/opt/coding-bench", pair, step_times={})
        cmd0 = sbx0.commands.calls[0][0]
        cmd1 = sbx1.commands.calls[0][0]
        # Round 0 uses templates [0,1]; round 1 uses [1,2]. Different bytes.
        self.assertIn(DEFAULT_VERIFY_TEMPLATES[0]["template"], cmd0)
        self.assertNotIn(DEFAULT_VERIFY_TEMPLATES[2]["template"], cmd0)
        self.assertIn(DEFAULT_VERIFY_TEMPLATES[1]["template"], cmd1)
        self.assertNotIn(DEFAULT_VERIFY_TEMPLATES[0]["template"], cmd1)

    def test_go_verify_unchanged_single_process(self):
        """go verify: go clean -cache then ONE write+go run (no N-chain; go stays N=1
        regardless of coding_verify_repeat - its cold-compile is already real load)."""
        config = Config(
            workflow_type="coding",
            coding_language="go",
            coding_verify_cmd="go run /tmp/bench_verify.go",
            coding_verify_repeat=3,
        )
        runner = self._make_runner(config, round_id=0)
        sbx = _FakeSbx()
        pair = {"file": "markup/x.go", "find": "x", "replace": "y", "verify_script": "package main\nfunc main(){}"}
        step_times: dict = {}
        ok, _err, compile_only = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times=step_times)
        self.assertTrue(ok)
        self.assertFalse(compile_only)
        # Two commands: cache clear (call 0) then ONE write+go run (call 1) - no chain.
        self.assertEqual(len(sbx.commands.calls), 2)
        clean_cmd = sbx.commands.calls[0][0]
        verify_cmd = sbx.commands.calls[1][0]
        self.assertIn("go clean -cache", clean_cmd)
        self.assertIn("cat > /tmp/bench_verify.go", verify_cmd)
        self.assertIn("GOEOF", verify_cmd)
        self.assertIn("go run /tmp/bench_verify.go", verify_cmd)
        # Only ONE go run (go ignores coding_verify_repeat).
        self.assertEqual(verify_cmd.count("go run"), 1)
        self.assertIn("verify_clean", step_times)
        self.assertIn("verify", step_times)
        from e2b_bench.schemas import CODING_STEP_ORDER

        self.assertNotIn("verify_clean", CODING_STEP_ORDER)

    def test_verify_failure_returned(self):
        """A non-zero exit code from the chained verify run is reported as failure."""
        config = Config(workflow_type="coding", coding_language="ts", coding_verify_repeat=3)
        runner = self._make_runner(config, round_id=0)
        sbx = _FakeSbx(result=_FakeResult(exit_code=1, stdout="", stderr="boom"))
        pair = {"file": "packages/shared/src/general.ts", "find": "x", "replace": "y"}
        ok, err, _co = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertFalse(ok)
        self.assertIn("verify failed", err)
        self.assertIn("exit_code=1", err)

    def test_verify_compile_only_label_preserved(self):
        """A pair marked verify: compile_only still runs the N-chain but is labeled
        compile_only (the pool's assertions are generic health checks, not tied to the
        edited symbol - honestly labeled when the pair declares no assertable semantics)."""
        config = Config(workflow_type="coding", coding_language="ts", coding_verify_repeat=3)
        runner = self._make_runner(config, round_id=0)
        sbx = _FakeSbx()
        pair = {
            "file": "packages/reactivity/src/baseHandlers.ts",
            "find": "x",
            "replace": "y",
            "verify": "compile_only",
        }
        ok, _err, compile_only = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertTrue(ok)
        self.assertTrue(compile_only)
        cmd = sbx.commands.calls[0][0]
        self.assertEqual(cmd.count("cat > /tmp/bench_verify_"), 3)


class TestStepFindLanguageAware(unittest.TestCase):
    """_step_find uses the language profile's checkout_paths + find glob."""

    def _make_runner(self, config):
        from e2b_bench.coding_task_runner import CodingRoundRunner

        state = SandboxState(sandbox_id=1, workflow_type="coding")
        return CodingRoundRunner(state=state, config=config, stop_event=threading.Event(), round_id=0)

    def test_ts_find_uses_packages_checkout(self):
        """ts find resets packages/ (vuejs/core has no top-level src/) and locates *.ts/*.tsx/*.js on miss."""
        config = Config(workflow_type="coding", coding_language="ts")
        runner = self._make_runner(config)
        sbx = _FakeSbx(result=_FakeResult(exit_code=1, stdout="", stderr=""))  # file not found
        runner._step_find(sbx, "/opt/coding-bench", "packages/shared/src/missing.ts", "x", "y", step_times={})
        cmds = [c for c, _ in sbx.commands.calls]
        self.assertTrue(any("git checkout -- packages/" in c for c in cmds))
        self.assertFalse(any("git checkout -- packages/ src/" in c for c in cmds))
        self.assertTrue(any("\\( -name '*.ts'" in c for c in cmds))

    def test_go_find_uses_markup_checkout(self):
        """go find resets markup/ and locates *.go on miss."""
        config = Config(workflow_type="coding", coding_language="go", coding_verify_cmd="go run /tmp/bench_verify.go")
        runner = self._make_runner(config)
        sbx = _FakeSbx(result=_FakeResult(exit_code=1, stdout="", stderr=""))  # file not found
        runner._step_find(sbx, "/opt/coding-bench", "markup/missing.go", "x", "y", step_times={})
        cmds = [c for c, _ in sbx.commands.calls]
        self.assertTrue(any("git checkout -- markup/" in c for c in cmds))
        self.assertTrue(any("-name '*.go'" in c for c in cmds))


class TestVerifyTemplatePool(unittest.TestCase):
    """The shared DEFAULT_VERIFY_TEMPLATES pool drives multi-process verify."""

    def test_pool_is_ordered_list_of_dicts(self):
        """DEFAULT_VERIFY_TEMPLATES is a non-empty ordered list of {template, assert} dicts."""
        from e2b_bench.schemas import DEFAULT_VERIFY_TEMPLATES

        self.assertIsInstance(DEFAULT_VERIFY_TEMPLATES, list)
        self.assertGreaterEqual(len(DEFAULT_VERIFY_TEMPLATES), 6)
        for entry in DEFAULT_VERIFY_TEMPLATES:
            self.assertIn("template", entry)
            self.assertIn("assert", entry)
            self.assertTrue(entry["template"])
            self.assertTrue(entry["assert"])

    def test_pool_templates_are_distinct(self):
        """Pool templates differ so consecutive rounds don't repeat identical bytes."""
        from e2b_bench.schemas import DEFAULT_VERIFY_TEMPLATES

        templates = [e["template"] for e in DEFAULT_VERIFY_TEMPLATES]
        self.assertEqual(len(set(templates)), len(templates), "pool templates must be distinct")

    def test_skeleton_has_global_header_and_compiler_core_import(self):
        """The single-template skeleton has the 8 verbatim agent globals + compiler-core import."""
        from e2b_bench.schemas import DEFAULT_CODING_VERIFY_SCRIPT_JS

        for g in (
            "__DEV__",
            "__BROWSER__",
            "__COMPAT__",
            "__ESM_BUNDLER__",
            "__FEATURE_OPTIONS_API__",
            "__FEATURE_PROD_DEVTOOLS__",
            "__FEATURE_SUSPENSE__",
            "__RUNTIME_COMPILE__",
        ):
            self.assertIn(f"globalThis.{g}", DEFAULT_CODING_VERIFY_SCRIPT_JS)
        self.assertIn("compiler-core/src/index.ts", DEFAULT_CODING_VERIFY_SCRIPT_JS)
        self.assertIn("baseParse", DEFAULT_CODING_VERIFY_SCRIPT_JS)
        self.assertNotIn("globalThis.__TEST__", DEFAULT_CODING_VERIFY_SCRIPT_JS)

    def test_default_pairs_carry_no_verify_script(self):
        """Pairs own edit semantics only ({file, find, replace}); verify workload
        comes from the shared pool, so no pair carries a verify_script anymore."""
        from e2b_bench.schemas import DEFAULT_CODING_SOURCE_FILES

        self.assertGreaterEqual(len(DEFAULT_CODING_SOURCE_FILES), 6)
        for pair in DEFAULT_CODING_SOURCE_FILES:
            self.assertIn("file", pair)
            self.assertIn("find", pair)
            self.assertIn("replace", pair)
            self.assertNotIn("verify_script", pair, "pairs must not carry verify_script (pool owns verify)")


class TestBuildEditCommand(unittest.TestCase):
    """The literal find->replace command is robust to regex metacharacters.

    Guards against the sed regression: `sed -i 's|find|replace|'` broke when the
    find string contained `|` (hugo pair) and silently mis-matched on `. () *`
    (vuejs pairs). _build_edit_command carries find/replace as base64 and does
    a literal str.replace, so no metacharacter can break quoting or matching.
    """

    def test_hugo_pair_with_pipe_and_brackets(self):
        """The hugo pair's find holds `|`, `[`, `]`, backticks - must not break the command."""
        import base64
        from e2b_bench.coding_task_runner import _build_edit_command

        find = "var gitHubAlertRe = regexp.MustCompile(`^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)"
        replace = "var gitHubAlertRe = regexp.MustCompile(`(?i)^<p>\\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\\]`)"
        cmd = _build_edit_command("/opt/coding-bench", "markup/goldmark/blockquotes/blockquotes.go", find, replace)

        # Script is fed via a quoted heredoc + `python3 -` (NOT python3 -c "..."):
        # -c embedded the script in a JSON/shell double-quoted arg whose `\\n`/`\\"`
        # broke across E2B's commands.run serialization (SyntaxError line 1).
        self.assertIn("python3 - ", cmd)
        self.assertIn("<<'PYEOF'", cmd)
        self.assertNotIn("python3 -c", cmd)
        self.assertNotIn("|" + find, cmd)  # find is NOT inlined raw (base64 instead)
        self.assertNotIn("sed -i", cmd)
        # base64 round-trips back to the exact find/replace (proves no escaping loss).
        # The argv tokens are the 3 space-separated tokens on the prelude line
        # (before the heredoc body).
        prelude = cmd.split("<<'PYEOF'")[0]
        parts = prelude.split()
        self.assertEqual(base64.b64decode(parts[-3]).decode(), find)
        self.assertEqual(base64.b64decode(parts[-2]).decode(), replace)
        self.assertEqual(parts[-1], "markup/goldmark/blockquotes/blockquotes.go")

    def test_vuejs_pair_with_dot_and_parens(self):
        """vuejs/core pairs carry `.` and `()` - sed treated them as regex; literal replace is inert."""
        import base64
        from e2b_bench.coding_task_runner import _build_edit_command

        find = "export const NOOP = (): void => {}"
        replace = "export const NOOP = (): void => undefined"
        cmd = _build_edit_command("/opt/coding-bench", "packages/shared/src/general.ts", find, replace)
        self.assertIn("python3 - ", cmd)
        self.assertNotIn("sed -i", cmd)
        prelude = cmd.split("<<'PYEOF'")[0]
        parts = prelude.split()
        self.assertEqual(base64.b64decode(parts[-3]).decode(), find)
        self.assertEqual(base64.b64decode(parts[-2]).decode(), replace)

    def test_find_absent_exits_2(self):
        """The script exits 2 when the find string is absent (no-op edit surfaced, not silent success)."""
        from e2b_bench.coding_task_runner import _build_edit_command

        cmd = _build_edit_command("/opt/coding-bench", "x.ts", "needle", "replacement")
        self.assertIn("if f not in s:", cmd)
        self.assertIn("sys.exit(2)", cmd)

    def test_replaces_first_occurrence_only(self):
        """str.replace(f, r, 1) - only the first occurrence, matching a real agent's one-line edit."""
        from e2b_bench.coding_task_runner import _build_edit_command

        cmd = _build_edit_command("/opt/coding-bench", "x.ts", "needle", "replacement")
        self.assertIn("s.replace(f, r, 1)", cmd)


class TestStepEdit(unittest.TestCase):
    """_step_edit uses _build_edit_command and surfaces exit 2 as a failure."""

    def _make_runner(self, config):
        from e2b_bench.coding_task_runner import CodingRoundRunner

        state = SandboxState(sandbox_id=1, workflow_type="coding")
        return CodingRoundRunner(state=state, config=config, stop_event=threading.Event(), round_id=0)

    def test_edit_success(self):
        config = Config(workflow_type="coding", coding_language="go", coding_verify_cmd="go run /tmp/bench_verify.go")
        runner = self._make_runner(config)
        sbx = _FakeSbx(result=_FakeResult(exit_code=0, stdout="", stderr=""))
        ok, err = runner._step_edit(sbx, "/opt/coding-bench", "markup/x.go", "find", "replace", step_times={})
        self.assertTrue(ok)
        self.assertEqual(err, "")
        # Command went through the literal-replace heredoc path, not sed / -c
        cmd = sbx.commands.calls[0][0]
        self.assertIn("python3 - ", cmd)
        self.assertIn("<<'PYEOF'", cmd)
        self.assertNotIn("sed -i", cmd)
        self.assertNotIn("python3 -c", cmd)

    def test_edit_find_absent_is_failure(self):
        """Exit 2 (find absent) is a failure with a clear error, not a silent verify-pass."""
        config = Config(workflow_type="coding", coding_language="go", coding_verify_cmd="go run /tmp/bench_verify.go")
        runner = self._make_runner(config)
        sbx = _FakeSbx(result=_FakeResult(exit_code=2, stdout="", stderr=""))
        ok, err = runner._step_edit(sbx, "/opt/coding-bench", "markup/x.go", "find", "replace", step_times={})
        self.assertFalse(ok)
        self.assertIn("exit_code=2", err)


if __name__ == "__main__":
    unittest.main()
