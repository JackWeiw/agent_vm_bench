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
        self.assertEqual(c.coding_language, "js")
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
        c = Config.load_from_yaml("config/e2b_coding_bench.yaml")
        self.assertEqual(c.workflow_type, "coding")
        self.assertEqual(c.template, "openclaw-coding-v1")
        self.assertEqual(c.coding_project_dir, "/opt/coding-bench")
        self.assertEqual(len(c.coding_source_files), 6)
        self.assertEqual(c.benchmark_mode, "round_robin")

    def test_yaml_coding_language_and_verify_cmd(self):
        """YAML configures the language and the verify command (npx tsx)"""
        c = Config.load_from_yaml("config/e2b_coding_bench.yaml")
        self.assertEqual(c.coding_language, "js")
        self.assertEqual(c.coding_verify_cmd, "npx tsx /tmp/bench_verify.mjs")
        self.assertEqual(c.coding_verify_timeout, 120)
        self.assertEqual(c.coding_skip_verify, False)

    def test_yaml_coding_source_files_are_vuejs_pairs(self):
        """YAML source_files are verified vuejs/core paths with find/replace"""
        c = Config.load_from_yaml("config/e2b_coding_bench.yaml")
        files = [p["file"] for p in c.coding_source_files]
        # All targets are real vuejs/core paths under packages/
        self.assertTrue(all(f.startswith("packages/") for f in files), files)
        # Each pair carries a non-empty find and replace
        for p in c.coding_source_files:
            self.assertTrue(p["find"])
            self.assertTrue(p["replace"])

    def test_yaml_browser_config_unaffected(self):
        """Test that browser config loading still works"""
        c = Config.load_from_yaml("config/e2b_bench.yaml")
        self.assertEqual(c.workflow_type, "browser")
        self.assertEqual(c.template, "openclaw-browser-v1")


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
    """Test the language-profile registry (extensible: js/go, future cpp)."""

    def test_js_profile(self):
        """js profile: npx tsx verify, EOF heredoc, ts/tsx/js glob."""
        p = get_coding_profile("js")
        self.assertEqual(p.temp_test_path, "/tmp/bench_verify.mjs")
        self.assertEqual(p.heredoc_eof, "EOF")
        self.assertEqual(p.run_cmd, "npx tsx /tmp/bench_verify.mjs")
        self.assertIn("*.ts", p.source_find_names)

    def test_go_profile(self):
        """go profile: go run verify, GOEOF heredoc, *.go glob."""
        p = get_coding_profile("go")
        self.assertEqual(p.temp_test_path, "/tmp/bench_verify.go")
        self.assertEqual(p.heredoc_eof, "GOEOF")
        self.assertEqual(p.run_cmd, "go run /tmp/bench_verify.go")
        self.assertIn("*.go", p.source_find_names)

    def test_unknown_language_falls_back_to_js(self):
        """An unregistered language falls back to the js profile."""
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
    """Verify the trace-faithful _step_verify (write temp test file + run)."""

    def _make_runner(self, config):
        from e2b_bench.coding_task_runner import CodingRoundRunner

        state = SandboxState(sandbox_id=1, workflow_type="coding")
        return CodingRoundRunner(state=state, config=config, stop_event=threading.Event(), round_id=0)

    def test_verify_writes_temp_file_and_runs_npx_tsx(self):
        """_step_verify writes /tmp/bench_verify.mjs and runs npx tsx in one command."""
        config = Config(workflow_type="coding", coding_language="js")
        runner = self._make_runner(config)
        sbx = _FakeSbx()
        pair = {
            "file": "packages/shared/src/general.ts",
            "find": "x",
            "replace": "y",
            "verify_script": "console.log('All tests passed!')",
        }
        ok, err = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertTrue(ok, err)
        # The single combined write+run command must contain both the heredoc
        # write and the npx tsx run (mirrors the trace's combined write+run).
        self.assertEqual(len(sbx.commands.calls), 1)
        cmd = sbx.commands.calls[0][0]
        self.assertIn("cat > /tmp/bench_verify.mjs", cmd)
        self.assertIn("npx tsx /tmp/bench_verify.mjs", cmd)
        self.assertIn("console.log('All tests passed!')", cmd)

    def test_verify_go_profile_uses_go_run(self):
        """go language: _step_verify writes .go and runs `go run`."""
        config = Config(workflow_type="coding", coding_language="go", coding_verify_cmd="go run /tmp/bench_verify.go")
        runner = self._make_runner(config)
        sbx = _FakeSbx()
        pair = {"file": "markup/x.go", "find": "x", "replace": "y", "verify_script": "package main\nfunc main(){}"}
        ok, _ = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertTrue(ok)
        cmd = sbx.commands.calls[0][0]
        self.assertIn("cat > /tmp/bench_verify.go", cmd)
        self.assertIn("GOEOF", cmd)
        self.assertIn("go run /tmp/bench_verify.go", cmd)

    def test_verify_failure_returned(self):
        """A non-zero exit code from the verify run is reported as failure."""
        config = Config(workflow_type="coding", coding_language="js")
        runner = self._make_runner(config)
        sbx = _FakeSbx(result=_FakeResult(exit_code=1, stdout="", stderr="boom"))
        pair = {
            "file": "packages/shared/src/general.ts",
            "find": "x",
            "replace": "y",
            "verify_script": "console.log('x')",
        }
        ok, err = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertFalse(ok)
        self.assertIn("verify failed", err)
        self.assertIn("exit_code=1", err)

    def test_verify_uses_shared_default_when_pair_has_no_script(self):
        """A pair without verify_script falls back to the shared default body."""
        config = Config(workflow_type="coding", coding_language="js")
        runner = self._make_runner(config)
        sbx = _FakeSbx()
        pair = {"file": "packages/reactivity/src/baseHandlers.ts", "find": "x", "replace": "y"}
        ok, _ = runner._step_verify(sbx, "/opt/coding-bench", pair, step_times={})
        self.assertTrue(ok)
        cmd = sbx.commands.calls[0][0]
        # Shared default imports the edited package's index.ts (with {pkg} substituted)
        self.assertIn("packages/reactivity/src/index.ts", cmd)


class TestStepFindLanguageAware(unittest.TestCase):
    """_step_find uses the language profile's checkout_paths + find glob."""

    def _make_runner(self, config):
        from e2b_bench.coding_task_runner import CodingRoundRunner

        state = SandboxState(sandbox_id=1, workflow_type="coding")
        return CodingRoundRunner(state=state, config=config, stop_event=threading.Event(), round_id=0)

    def test_js_find_uses_packages_checkout(self):
        """js find resets packages/ src/ and locates *.ts/*.tsx/*.js on miss."""
        config = Config(workflow_type="coding", coding_language="js")
        runner = self._make_runner(config)
        sbx = _FakeSbx(result=_FakeResult(exit_code=1, stdout="", stderr=""))  # file not found
        runner._step_find(sbx, "/opt/coding-bench", "packages/shared/src/missing.ts", "x", "y", step_times={})
        cmds = [c for c, _ in sbx.commands.calls]
        self.assertTrue(any("git checkout -- packages/ src/" in c for c in cmds))
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

        self.assertIn("python3 -c", cmd)
        self.assertNotIn("|" + find, cmd)  # find is NOT inlined raw (base64 instead)
        # base64 round-trips back to the exact find/replace (proves no escaping loss)
        parts = cmd.split()
        self.assertEqual(base64.b64decode(parts[-3]).decode(), find)
        self.assertEqual(base64.b64decode(parts[-2]).decode(), replace)
        self.assertEqual(parts[-1], "markup/goldmark/blockquotes/blockquotes.go")
        # No sed, no raw pipe-delimited substitution
        self.assertNotIn("sed -i", cmd)

    def test_vuejs_pair_with_dot_and_parens(self):
        """vuejs/core pairs carry `.` and `()` - sed treated them as regex; literal replace is inert."""
        import base64
        from e2b_bench.coding_task_runner import _build_edit_command

        find = "export const NOOP = (): void => {}"
        replace = "export const NOOP = (): void => undefined"
        cmd = _build_edit_command("/opt/coding-bench", "packages/shared/src/general.ts", find, replace)
        parts = cmd.split()
        self.assertEqual(base64.b64decode(parts[-3]).decode(), find)
        self.assertEqual(base64.b64decode(parts[-2]).decode(), replace)
        self.assertNotIn("sed -i", cmd)

    def test_find_absent_exits_2(self):
        """The script exits 2 when the find string is absent (no-op edit surfaced, not silent success)."""
        from e2b_bench.coding_task_runner import _build_edit_command

        cmd = _build_edit_command("/opt/coding-bench", "x.ts", "needle", "replacement")
        self.assertIn("if f not in s: sys.exit(2)", cmd)

    def test_replaces_first_occurrence_only(self):
        """str.replace(f, r, 1) - only the first occurrence, matching a real agent's one-line edit."""
        from e2b_bench.coding_task_runner import _build_edit_command

        cmd = _build_edit_command("/opt/coding-bench", "x.ts", "needle", "replacement")
        self.assertIn("s.replace(f,r,1)", cmd)


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
        # Command went through the literal-replace path, not sed
        self.assertIn("python3 -c", sbx.commands.calls[0][0])
        self.assertNotIn("sed -i", sbx.commands.calls[0][0])

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
