"""Tests for the coding workflow runners (host-agnostic).

Runners are driven through a :class:`FakeProvider`, so these tests exercise the
exec-only code path with no real sandbox. Thread bodies are invoked directly
(``runner.run()``) rather than via ``start()`` to keep timing deterministic.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
from pathlib import Path

from bench_core.config import KernelConfig
from bench_core.payload.coding_payload import (
    CODING_LANGUAGE_DEFAULT_SOURCE_FILES,
    DEFAULT_CODING_SOURCE_FILES,
    get_coding_profile,
)
from bench_core.schemas import BenchSandbox
from bench_core.task_runner.coding import (
    CodingConfig,
    CodingRoundRunner,
    CodingTaskRunner,
    CodingWarmupRunner,
    _build_edit_command,
    _build_multi_edit_command,
    _run_test_suite_verify,
    _run_verify,
)
from bench_core.workflow_registry import RunContext
from env_provider import CommandResult
from env_provider.fake import FakeProvider


def _ready_sandbox(index: int = 0) -> BenchSandbox:
    return BenchSandbox(id=f"fake-{index}", index=index, ready=True, is_alive=True)


class _VerifyFailingProvider(FakeProvider):
    """FakeProvider whose verify runs (npx tsx / go run / python3 verify) fail."""

    def exec(self, inst, command, **kw):  # type: ignore[override]
        if "npx tsx" in command or "go run" in command or "python3 /tmp/bench_verify" in command:
            return CommandResult(1, "", "verify boom")
        return super().exec(inst, command, **kw)


class TestBuildEditCommand:
    def test_builds_literal_replace_heredoc(self):
        find = "export const NOOP = (): void => {}"
        replace = "export const NOOP = (): void => undefined"
        target = "packages/shared/src/general.ts"
        cmd = _build_edit_command("/opt/coding-bench", target, find, replace)

        # find/replace are base64-carried so no quoting can break them.
        assert base64.b64encode(find.encode()).decode() in cmd
        assert base64.b64encode(replace.encode()).decode() in cmd
        # target file + python3 stdin + quoted heredoc terminator.
        assert target in cmd
        assert "python3 -" in cmd
        assert "<<'PYEOF'" in cmd
        # the literal-edit script body is present verbatim.
        assert "s.replace(f, r, 1)" in cmd
        assert "sys.exit(2)" in cmd  # absent find -> explicit failure


class TestRunVerify:
    def test_ts_success_records_verify_timing(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts", coding_verify_repeat=2))
        pair = DEFAULT_CODING_SOURCE_FILES[0]
        provider = FakeProvider()
        state = _ready_sandbox()
        step_times: dict[str, float] = {}

        ok, err, compile_only = _run_verify(
            provider, state, "/opt/coding-bench", config.workflow_config, pair, step_times=step_times, round_id=0
        )

        assert ok is True
        assert err == ""
        # pair has no "verify" key -> compile_only stays False.
        assert compile_only is False
        assert "verify" in step_times
        # ts profile has no pre_verify_cmd -> no verify_clean timing.
        assert "verify_clean" not in step_times

    def test_go_records_verify_clean_then_verify(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="go"))
        pair = config.workflow_config.coding_source_files[0]  # hugo pair with its own verify_script
        provider = FakeProvider()
        state = _ready_sandbox()
        step_times: dict[str, float] = {}

        ok, _err, compile_only = _run_verify(
            provider, state, "/opt/coding-bench", config.workflow_config, pair, step_times=step_times, round_id=0
        )

        assert ok is True
        assert compile_only is False
        # go profile has pre_verify_cmd="go clean -cache" -> timed separately.
        assert "verify_clean" in step_times
        assert "verify" in step_times

    def test_verify_failure_returns_error(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts", coding_verify_repeat=1))
        pair = DEFAULT_CODING_SOURCE_FILES[0]
        provider = _VerifyFailingProvider()
        state = _ready_sandbox()

        ok, err, _compile_only = _run_verify(
            provider, state, "/opt/coding-bench", config.workflow_config, pair, round_id=0
        )

        assert ok is False
        assert "verify failed" in err
        assert "exit_code=1" in err


class TestCodingWarmupRunner:
    def test_runs_initial_verify_and_marks_done(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        provider = FakeProvider()
        state = _ready_sandbox()
        CodingWarmupRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), provider=provider)
        ).run()

        assert state.warmup_done is True

    def test_skips_when_not_ready(self):
        # A non-ready instance is skipped before the command-issuing body; it is
        # NOT marked warmup_done -- matching the readiness-gate early-return.
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        provider = FakeProvider()
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        CodingWarmupRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), provider=provider)
        ).run()
        assert state.warmup_done is False

    def test_skips_when_project_marker_missing(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        provider = FakeProvider(
            exec_results={"ls /opt/coding-bench/package.json": CommandResult(1, "", "no such file")}
        )
        state = _ready_sandbox()
        CodingWarmupRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), provider=provider)
        ).run()
        # Project missing -> warmup bails out, but still marks itself done.
        assert state.warmup_done is True

    def test_skip_verify_still_marks_done(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts", coding_skip_verify=True))
        provider = _VerifyFailingProvider()  # verify would fail, but it's skipped
        state = _ready_sandbox()
        CodingWarmupRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), provider=provider)
        ).run()
        assert state.warmup_done is True


class TestCodingTaskRunner:
    def test_single_task_success(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        provider = FakeProvider()
        state = _ready_sandbox()
        runner = CodingTaskRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), provider=provider)
        )

        success, latency, verify_success, compile_only, timed_out = runner._run_single_task()

        assert success is True
        assert verify_success is True
        assert compile_only is False
        assert timed_out is False
        assert latency > 0.0

    def test_edit_failure_records_error(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        pair = config.workflow_config.coding_source_files[0]
        edit_cmd = _build_edit_command(
            config.workflow_config.coding_project_dir, pair["file"], pair["find"], pair["replace"]
        )
        provider = FakeProvider(exec_results={edit_cmd: CommandResult(1, "", "edit boom")})
        state = _ready_sandbox()
        runner = CodingTaskRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), provider=provider)
        )

        success, _latency, verify_success, _compile_only, _timed_out = runner._run_single_task()

        assert success is False
        assert verify_success is False  # verify never ran (edit failed first)
        assert "edit failed" in state.coding_metrics.last_error

    def test_offline_sandbox_yields_no_task(self):
        state = _ready_sandbox()
        state.is_alive = False
        runner = CodingTaskRunner(
            RunContext(
                state=state,
                config=KernelConfig(workflow_config=CodingConfig()),
                stop_event=threading.Event(),
                provider=FakeProvider(),
            )
        )
        success, latency, verify_success, compile_only, timed_out = runner._run_single_task()
        assert success is False
        assert latency == 0.0
        assert verify_success is False
        assert compile_only is False
        assert timed_out is False


class TestCodingRoundRunner:
    def test_round_records_all_step_timings(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        provider = FakeProvider()
        state = _ready_sandbox()
        runner = CodingRoundRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), round_id=0, provider=provider)
        )

        runner.run()  # one round, synchronous

        metrics = state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        assert metrics.verify_success_count == 1
        # every step in CODING_STEP_ORDER should have recorded a timing.
        assert set(metrics.get_step_times_copy()) == {"find", "read", "edit", "verify", "diff"}
        assert state.get_last_task_time() > 0.0

    def test_edit_failure_is_recorded(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        pair = config.workflow_config.coding_source_files[0]
        edit_cmd = _build_edit_command(
            config.workflow_config.coding_project_dir, pair["file"], pair["find"], pair["replace"]
        )
        provider = FakeProvider(exec_results={edit_cmd: CommandResult(1, "", "edit boom")})
        state = _ready_sandbox()
        runner = CodingRoundRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), round_id=0, provider=provider)
        )

        runner.run()

        metrics = state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.failed_count == 1
        assert metrics.verify_success_count == 0
        assert "edit failed" in metrics.last_error

    def test_verify_failure_records_failed_step(self):
        config = KernelConfig(workflow_config=CodingConfig(coding_language="ts"))
        provider = _VerifyFailingProvider()
        state = _ready_sandbox()
        runner = CodingRoundRunner(
            RunContext(state=state, config=config, stop_event=threading.Event(), round_id=0, provider=provider)
        )

        runner.run()

        metrics = state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.failed_count == 1
        # edit ran (timed), verify ran (timed) then failed.
        assert "edit" in metrics.get_step_times_copy()
        assert "verify" in metrics.get_step_times_copy()
        assert "verify failed" in metrics.last_error

    def test_edits_only_pair_runs_the_round(self):
        # django__django-16631's gold pair (the only python pair) carries an
        # ``edits`` list (two files, three hunks) and no top-level find/replace.
        # It used to raise KeyError before any step ran, silently dropping the
        # round (round 1 of the docker e2e recorded no task at all).
        config = KernelConfig(workflow_config=CodingConfig(coding_language="python"))
        pairs = config.workflow_config.coding_source_files
        assert len(pairs) == 1
        assert "edits" in pairs[0] and "find" not in pairs[0]

        runner = CodingRoundRunner(
            RunContext(
                state=_ready_sandbox(),
                config=config,
                stop_event=threading.Event(),
                round_id=1,
                provider=FakeProvider(),
            )
        )
        runner.run()

        metrics = runner.state.coding_metrics
        assert metrics.total_tasks == 1
        assert metrics.success_count == 1
        assert "edit" in metrics.get_step_times_copy()

    def test_skips_when_not_ready(self):
        state = BenchSandbox(id="fake-0", index=0, ready=False)
        runner = CodingRoundRunner(
            RunContext(
                state=state,
                config=KernelConfig(workflow_config=CodingConfig()),
                stop_event=threading.Event(),
                round_id=0,
                provider=FakeProvider(),
            )
        )
        runner.run()
        assert state.coding_metrics.total_tasks == 0


class _RecordingProvider(FakeProvider):
    """FakeProvider that records the generated verify command."""

    def __init__(self):
        super().__init__()
        self.commands: list[str] = []

    def exec(self, inst, command, **kw):  # type: ignore[override]
        self.commands.append(command)
        return CommandResult(0, "", "")


def _suite_pair(language: str) -> dict:
    """First default pair of ``language`` that declares a fail_to_pass selection."""
    for pair in CODING_LANGUAGE_DEFAULT_SOURCE_FILES[language]:
        if pair.get("fail_to_pass"):
            return pair
    raise AssertionError(f"no test-bearing pair for {language}")


def _capture_suite_cmd(language: str) -> str:
    """Generate the real suite-verify command without a container."""
    config = CodingConfig(coding_language=language)
    provider = _RecordingProvider()
    _run_test_suite_verify(
        provider,
        _ready_sandbox(),
        "/opt/coding-bench",
        config,
        _suite_pair(language),
        get_coding_profile(language),
    )
    assert provider.commands, "suite verify did not issue a command"
    return provider.commands[-1]


def _heredoc_body(cmd: str) -> str:
    """Extract the embedded python3 heredoc from a generated command."""
    return cmd.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]


def _run_python_body(body: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-", *args],
        input=body,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def _run_parser(cmd: str, ids: list[str], path) -> subprocess.CompletedProcess:
    ids_b64 = base64.b64encode(json.dumps(ids).encode()).decode()
    return _run_python_body(_heredoc_body(cmd), ids_b64, str(path))


class TestReportParsers:
    """Each report format must fail on a missing, failing, or skipped id.

    These parsers are the only thing standing between a renamed/skipped test and
    a silently green verify, so each branch runs the exact heredoc embedded in
    the generated verify command against a fixture.
    """

    def test_surefire_xml(self, tmp_path):
        cmd = _capture_suite_cmd("java")
        reports = tmp_path / "surefire-reports"
        reports.mkdir()
        (reports / "TEST-com.example.RegressionTest.xml").write_text(
            '<?xml version="1.0"?>\n'
            '<testsuite name="com.example.RegressionTest">\n'
            '  <testcase name="testPasses" classname="com.example.RegressionTest"/>\n'
            '  <testcase name="testFails" classname="com.example.RegressionTest">\n'
            '    <failure message="boom"/>\n'
            "  </testcase>\n"
            "</testsuite>\n",
            encoding="utf-8",
        )

        ok = _run_parser(cmd, ["com.example.RegressionTest#testPasses"], reports)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert "verified 1 tests" in ok.stdout

        failed = _run_parser(cmd, ["com.example.RegressionTest#testFails"], reports)
        assert failed.returncode == 1
        assert "NOT PASSED" in failed.stdout

        not_run = _run_parser(cmd, ["com.example.RegressionTest#testNeverRan"], reports)
        assert not_run.returncode == 1
        assert "NOT RUN" in not_run.stdout

        no_report = _run_parser(cmd, ["com.example.Absent#testPasses"], reports)
        assert no_report.returncode == 1
        assert "MISSING REPORT" in no_report.stdout

    def test_doctest_xml(self, tmp_path):
        cmd = _capture_suite_cmd("cpp")
        xml = tmp_path / "doctest.xml"
        xml.write_text(
            '<?xml version="1.0"?>\n'
            '<TestSuite name="udt">\n'
            '  <TestCase name="basic usage">\n'
            '    <SubCase name="conversion to json via free-functions">\n'
            '      <Expression success="true"/>\n'
            "    </SubCase>\n"
            '    <SubCase name="broken case">\n'
            '      <Expression success="false"/>\n'
            "    </SubCase>\n"
            "  </TestCase>\n"
            "</TestSuite>\n",
            encoding="utf-8",
        )

        ok = _run_parser(cmd, ["basic usage > conversion to json via free-functions"], xml)
        assert ok.returncode == 0, ok.stdout + ok.stderr

        failed = _run_parser(cmd, ["basic usage > broken case"], xml)
        assert failed.returncode == 1
        assert "NOT PASSED" in failed.stdout

        not_run = _run_parser(cmd, ["basic usage > never ran"], xml)
        assert not_run.returncode == 1
        assert "NOT RUN" in not_run.stdout

        no_report = _run_parser(cmd, ["basic usage > conversion to json via free-functions"], tmp_path / "absent.xml")
        assert no_report.returncode == 1
        assert "MISSING REPORT" in no_report.stdout

    def test_django_log(self, tmp_path):
        cmd = _capture_suite_cmd("python")
        log = tmp_path / "django.log"
        log.write_text(
            "test_pass (auth_tests.test_basic.TestGetUser.test_pass) ... ok\n"
            "The current user model can be retrieved ... ok\n"
            "test_fail (auth_tests.test_basic.TestGetUser.test_fail) ... FAIL\n"
            "test_skip (auth_tests.test_basic.TestGetUser.test_skip) ... skipped 'needs db'\n",
            encoding="utf-8",
        )

        ok = _run_parser(cmd, ["The current user model can be retrieved"], log)
        assert ok.returncode == 0, ok.stdout + ok.stderr

        failed = _run_parser(cmd, ["test_fail (auth_tests.test_basic.TestGetUser.test_fail)"], log)
        assert failed.returncode == 1
        assert "NOT PASSED" in failed.stdout

        skipped = _run_parser(cmd, ["test_skip (auth_tests.test_basic.TestGetUser.test_skip)"], log)
        assert skipped.returncode == 1
        assert "NOT PASSED" in skipped.stdout

        not_run = _run_parser(cmd, ["test_never (auth_tests.test_basic.TestGetUser.test_never)"], log)
        assert not_run.returncode == 1
        assert "NOT RUN" in not_run.stdout

    def test_tcl_log(self, tmp_path):
        cmd = _capture_suite_cmd("c")
        log = tmp_path / "redis.log"
        log.write_text(
            "[ok]: Consumer Group Lag with XDELs and tombstone after the last_id of consume group (12.5 ms)\n"
            "[ok]: XGROUP CREATE: creation and duplicate group name detection\n"
            "[err]: XREADGROUP will return only new elements\n"
            "[skip]: Some skipped test (0 ms)\n",
            encoding="utf-8",
        )

        ok = _run_parser(cmd, ["Consumer Group Lag with XDELs and tombstone after the last_id of consume group"], log)
        assert ok.returncode == 0, ok.stdout + ok.stderr

        failed = _run_parser(cmd, ["XREADGROUP will return only new elements"], log)
        assert failed.returncode == 1
        assert "NOT PASSED" in failed.stdout

        skipped = _run_parser(cmd, ["Some skipped test"], log)
        assert skipped.returncode == 1
        assert "NOT PASSED" in skipped.stdout

        not_run = _run_parser(cmd, ["never selected"], log)
        assert not_run.returncode == 1
        assert "NOT RUN" in not_run.stdout


class TestVerifyCommandWiring:
    def test_maven_clears_stale_reports_first(self):
        # Without this, a round that selects nothing can grade the previous
        # round's passing XML (surefire exits 0 with -DfailIfNoTests=false).
        cmd = _capture_suite_cmd("java")
        assert "rm -rf /opt/coding-bench/gson/target/surefire-reports; mvn " in cmd

    def test_parser_failure_wins_over_suite_exit_code(self):
        # A parser sys.exit(1) must not be discarded when the suite itself exits 0.
        for language in ("c", "cpp", "python"):
            cmd = _capture_suite_cmd(language)
            assert "prc=$?" in cmd
            tail = cmd[cmd.index("prc=$?") :]
            assert 'if [ "$prc" -ne 0 ]; then exit "$prc"; fi' in tail
            assert tail.rstrip().endswith('exit "$rc"')


class TestEmptyFindGuard:
    """An empty find string must reject the edit, not insert at offset 0."""

    def test_single_edit_exits_2_and_leaves_file_untouched(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("class Foo:\n", encoding="utf-8")
        cmd = _build_edit_command(str(tmp_path), str(target), "", "// bench")

        result = _run_python_body(
            _heredoc_body(cmd),
            base64.b64encode(b"").decode(),
            base64.b64encode(b"// bench").decode(),
            str(target),
        )

        assert result.returncode == 2
        assert target.read_text(encoding="utf-8") == "class Foo:\n"

    def test_multi_edit_exits_2_and_leaves_file_untouched(self, tmp_path):
        target = tmp_path / "sample.py"
        target.write_text("class Foo:\n", encoding="utf-8")
        edits = [{"file": str(target), "find": "", "replace": "// bench"}]
        cmd = _build_multi_edit_command(str(tmp_path), str(target), edits)

        result = _run_python_body(
            _heredoc_body(cmd),
            base64.b64encode(json.dumps(edits).encode()).decode(),
            str(target),
        )

        assert result.returncode == 2
        assert target.read_text(encoding="utf-8") == "class Foo:\n"


class TestHelperPairFixtures:
    """The helper's bench_pairs.json must not drift from coding_payload."""

    repo_root = Path(__file__).resolve().parents[4]

    @staticmethod
    def _project(pair: dict) -> dict:
        edits = pair.get("edits")
        if edits is None:
            edits = [{"find": pair["find"], "replace": pair["replace"]}]
        out = {
            "file": pair["file"],
            "edits": [dict(e) for e in edits],
            "tests": bool(pair.get("tests", bool(pair.get("fail_to_pass") or pair.get("pass_to_pass")))),
        }
        for key in ("fail_to_pass", "pass_to_pass"):
            if pair.get(key):
                out[key] = list(pair[key])
        return out

    def test_helper_json_matches_payload(self):
        for language in ("c", "cpp", "python"):
            path = self.repo_root / "dockerfile_build" / "coding" / language / "bench_pairs.json"
            assert json.loads(path.read_text(encoding="utf-8")) == [
                self._project(p) for p in CODING_LANGUAGE_DEFAULT_SOURCE_FILES[language]
            ], f"{language}/bench_pairs.json drifted from coding_payload"
