from __future__ import annotations

from bench_core.config import KernelConfig
from bench_core.observability.monitor import MonitorConfig, MonitorController


def test_monitor_config_defaults_when_absent():
    cfg = KernelConfig.from_raw({"sandbox": {"total_count": 1}})
    assert isinstance(cfg.monitor, MonitorConfig)
    assert cfg.monitor.enabled == "auto"
    assert cfg.monitor.vmm == "auto"
    assert cfg.monitor.capture == "auto"
    assert cfg.monitor.interval == 2
    assert cfg.monitor.merge_report is False
    assert cfg.monitor.report_timeout == 300
    assert cfg.monitor.log_dir is None
    assert cfg.monitor.disks == "all"
    assert cfg.monitor.numa == "all"


def test_monitor_config_from_raw_overrides():
    raw = {
        "sandbox": {"total_count": 1},
        "monitor": {
            "enabled": "true",
            "vmm": "qemu",
            "interval": 5,
            "capture": "false",
            "numa": "0,1",
            "disks": "sda,nvme0n1",
            "stress_file": "/tmp/lock",
            "log_dir": "out/vm",
            "merge_report": False,
            "report_timeout": 120,
        },
    }
    cfg = KernelConfig.from_raw(raw)
    assert cfg.monitor.enabled == "true"
    assert cfg.monitor.vmm == "qemu"
    assert cfg.monitor.interval == 5
    assert cfg.monitor.capture == "false"
    assert cfg.monitor.numa == "0,1"
    assert cfg.monitor.disks == "sda,nvme0n1"
    assert cfg.monitor.stress_file == "/tmp/lock"
    assert cfg.monitor.log_dir == "out/vm"
    assert cfg.monitor.merge_report is False
    assert cfg.monitor.report_timeout == 120


class _StubProvider:
    """Minimal provider stub for MonitorController (no SDK)."""

    def __init__(self, name="stub", vmm_type=None, test_duration=300):
        self.name = name
        self.vmm_type = vmm_type
        self.test_duration = test_duration


def _cfg(**over):
    raw = {"sandbox": {"total_count": 1}, "report": {"output_dir": "out"}}
    if over:
        raw["monitor"] = over
    return KernelConfig.from_raw(raw)


def test_start_skips_when_no_vmm(caplog):
    cfg = _cfg()
    prov = _StubProvider(vmm_type=None)
    mc = MonitorController(cfg, prov)
    mc.start()
    assert mc._started is False
    assert any("disabled" in r.message for r in caplog.records)


def test_start_skips_when_binary_missing(monkeypatch, caplog):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: None)
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(), prov)
    mc.start()
    assert mc._started is False
    assert any("binary not found" in r.message for r in caplog.records)


def test_command_construction(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(stress_file=str(tmp_path / "lock")), prov)
    cmd = mc._cmd
    assert "--vmm" in cmd and "firecracker" in cmd
    assert "--stress-file" in cmd
    assert "--auto-skip" in cmd and "--enable-capture" in cmd  # capture=auto
    assert "-i" in cmd and "2" in cmd
    assert cmd[cmd.index("--numa") + 1] == "all"
    assert "--disks" in cmd and "all" in cmd  # default
    assert "-t" in cmd  # hard upper-bound timer


def test_command_disks_override(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(disks="sda,nvme0n1", stress_file=str(tmp_path / "lock")), prov)
    cmd = mc._cmd
    # the custom disk list reaches the vm-monitor CLI verbatim
    i = cmd.index("--disks")
    assert cmd[i + 1] == "sda,nvme0n1"


def test_command_capture_false_omits_flags(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(capture="false", stress_file=str(tmp_path / "lock")), prov)
    assert "--enable-capture" not in mc._cmd
    assert "--auto-skip" not in mc._cmd


def test_start_removes_stale_lock(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: _FakeProc())
    lock = tmp_path / "lock"
    lock.touch()
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(stress_file=str(lock), log_dir=str(tmp_path)), prov)
    mc.start()
    assert mc._started is True
    assert not lock.exists()
    assert any("stale lock" in r.message.lower() for r in caplog.records)


def test_start_degrades_when_log_dir_unwritable(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    # Create a regular file; using it as a parent dir makes mkdir() fail (FileExistsError is OSError).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    bad_log_dir = str(blocker / "subdir")
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(stress_file=str(tmp_path / "lock"), log_dir=bad_log_dir), prov)
    mc.start()
    assert mc._started is False
    assert any("disabled" in r.message for r in caplog.records)


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        # Post-sampling vm_monitor ignores SIGTERM (the handler only breaks the
        # sampling loop, already done), so SIGTERM alone does not stop the
        # process -- returncode stays None, forcing the reaper to escalate.
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_begin_end_stress_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: _FakeProc())
    lock = tmp_path / "lock"
    mc = MonitorController(_cfg(stress_file=str(lock), log_dir=str(tmp_path)), _StubProvider(vmm_type="firecracker"))
    mc.start()
    assert mc.stress_window is None  # not begun yet

    mc.begin_stress()
    assert lock.exists()
    assert mc._begin_ts is not None

    mc.end_stress()
    assert not lock.exists()
    assert mc._end_ts is not None
    assert mc.stress_window is not None and mc.stress_window >= 0.0


def test_end_stress_idempotent_when_no_lock(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: _FakeProc())
    lock = tmp_path / "lock"
    mc = MonitorController(_cfg(stress_file=str(lock), log_dir=str(tmp_path)), _StubProvider(vmm_type="firecracker"))
    mc.start()
    mc.begin_stress()
    lock.unlink()  # simulate external removal
    mc.end_stress()  # must not raise


def test_begin_end_noop_when_not_started():
    mc = MonitorController(_cfg(), _StubProvider(vmm_type=None))
    mc.begin_stress()
    mc.end_stress()
    assert mc.stress_window is None


def test_stop_collects_report_and_closes_handles(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    proc = _FakeProc()
    proc.returncode = 0  # vm_monitor exited cleanly after producing the report
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: proc)
    mc = MonitorController(
        _cfg(stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path), report_timeout=2),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    # the report is already on disk (vm_monitor wrote it before exiting)
    (tmp_path / "resource_report.xlsx").write_text("x")
    artifacts = mc.stop()
    assert mc.report_xlsx == tmp_path / "resource_report.xlsx"
    assert artifacts and artifacts[0] == mc.report_xlsx
    assert proc.terminated is False  # clean exit -- no SIGTERM needed
    assert mc._stdout_fh is None and mc._stderr_fh is None  # closed


def test_stop_terminates_then_kills_overdue_process(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.time", _Clock())
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    proc = _FakeProc()
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: proc)
    mc = MonitorController(
        _cfg(stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path), report_timeout=1),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    # no xlsx ever appears; proc never exits and ignores SIGTERM (poll stays
    # None) -- a genuine hang. The reaper must SIGTERM, then SIGKILL past grace.
    mc.stop()
    assert proc.terminated is True  # SIGTERM fired at 2x report_timeout
    assert proc.killed is True  # unresponsive past grace -> SIGKILL


class _Clock:
    """Deterministic clock so the mid-export grace path is timing-independent."""

    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, n):
        self.t += n


class _ExportingProc:
    """Fake proc simulating vm_monitor finishing an xlsx export.

    Stays alive (poll -> None) until ``promote_on_poll`` polls, then promotes
    the build file to the final xlsx and exits cleanly (returncode 0). With
    ``promote_on_poll=None`` it never finishes (a hung export).
    """

    def __init__(self, build_path, xlsx_path, *, promote_on_poll=None):
        self._build = build_path
        self._xlsx = xlsx_path
        self._promote_on = promote_on_poll
        self._polls = 0
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        self._polls += 1
        if self._promote_on is not None and self._polls >= self._promote_on and self.returncode is None:
            # export done: atomic build -> xlsx promote, then exit
            if self._build.exists():
                self._build.unlink()
            self._xlsx.write_text("ok")
            self.returncode = 0
        return self.returncode

    def terminate(self):
        # SIGTERM does not interrupt an in-flight export (handler is moot
        # post-sampling), so the process stays alive -- returncode unchanged.
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_stop_waits_for_slow_export_no_sigterm(monkeypatch, tmp_path):
    """A slow export that finishes within the 2x report_timeout window is never
    SIGTERM'd -- the reaper waits for the process to exit on its own (the true
    completion signal) and collects the report once it is promoted. This is the
    core fix: the old code SIGTERM'd at report_timeout and SIGKILL'd mid-write
    5s later, leaving a corrupt orphan ``.build.xlsx`` and no report.
    """
    monkeypatch.setattr("bench_core.observability.monitor.time", _Clock())
    build = tmp_path / "resource_report.xlsx.build.xlsx"
    xlsx = tmp_path / "resource_report.xlsx"
    build.write_text("partial")  # export in flight
    proc = _ExportingProc(build, xlsx, promote_on_poll=2)  # finishes inside the wait window
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: proc)
    mc = MonitorController(
        _cfg(stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path), report_timeout=1),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    artifacts = mc.stop()
    assert proc.terminated is False  # finished inside the wait window -- no SIGTERM
    assert proc.killed is False
    assert mc.report_xlsx == xlsx  # report collected after the export finished
    assert artifacts and artifacts[0] == xlsx
    assert not build.exists()  # promoted away -- no corrupt orphan left behind


def test_stop_grace_lets_sigterm_hit_export_finish(monkeypatch, tmp_path):
    """When the export outlives the 2x report_timeout window, the reaper SIGTERMs
    -- but SIGTERM does not interrupt the export (vm_monitor's handler only
    breaks sampling, moot post-sampling). The polled post-SIGTERM grace lets
    the export finish + exit, so the report is still collected and there is NO
    SIGKILL mid-write. (The old 5s grace guaranteed a mid-write SIGKILL here.)
    """
    monkeypatch.setattr("bench_core.observability.monitor.time", _Clock())
    build = tmp_path / "resource_report.xlsx.build.xlsx"
    xlsx = tmp_path / "resource_report.xlsx"
    build.write_text("partial")
    proc = _ExportingProc(build, xlsx, promote_on_poll=4)  # finishes during the post-SIGTERM grace
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: proc)
    mc = MonitorController(
        _cfg(stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path), report_timeout=1),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    artifacts = mc.stop()
    assert proc.terminated is True  # SIGTERM fired (export outlived 2x window)
    assert proc.killed is False  # but the grace let it finish -- no SIGKILL
    assert mc.report_xlsx == xlsx  # report still collected
    assert artifacts and artifacts[0] == xlsx
    assert not build.exists()  # promoted away -- no corrupt orphan


def test_stop_kills_when_export_hung_past_grace(monkeypatch, tmp_path, caplog):
    """A genuinely hung export (never finishes, ignores SIGTERM) is SIGKILL'd
    past the grace -- a true hang must not block run_benchmark forever. This is
    the rare degenerate case; the common slow-export case is covered above
    without any SIGKILL. The orphan ``.build.xlsx`` this leaves is cleaned at
    the start of the next export (exporters clean-on-start)."""
    monkeypatch.setattr("bench_core.observability.monitor.time", _Clock())
    build = tmp_path / "resource_report.xlsx.build.xlsx"
    xlsx = tmp_path / "resource_report.xlsx"
    build.write_text("partial")
    proc = _ExportingProc(build, xlsx, promote_on_poll=None)  # never finishes
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: proc)
    mc = MonitorController(
        _cfg(stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path), report_timeout=1),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    mc.stop()
    assert proc.terminated is True  # SIGTERM fired at 2x report_timeout
    assert proc.killed is True  # unresponsive past grace -> SIGKILL
    assert any("exceeded 2x report_timeout" in r.message.lower() for r in caplog.records)
    assert any("sigkill" in r.message.lower() for r in caplog.records)


def test_stop_noop_when_not_started():
    mc = MonitorController(_cfg(), _StubProvider(vmm_type=None))
    assert mc.stop() == []


def test_stop_handles_dead_subprocess(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    proc = _FakeProc()
    proc.returncode = 1  # already dead, no report
    monkeypatch.setattr("bench_core.observability.monitor.subprocess.Popen", lambda *a, **kw: proc)
    mc = MonitorController(
        _cfg(stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path), report_timeout=2),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    mc.stop()
    assert mc.report_xlsx is None
    assert any("without report" in r.message for r in caplog.records)


def _make_src_xlsx(path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "VM_Stats"
    ws.cell(row=1, column=1, value="vm")
    ws.cell(row=2, column=1, value="fc-1")
    wb.create_sheet("NUMA_Overview")
    wb["NUMA_Overview"].cell(row=1, column=1, value="node0")
    wb.save(path)


def test_merge_source_returns_report_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    src = tmp_path / "resource_report.xlsx"
    _make_src_xlsx(src)
    mc = MonitorController(
        _cfg(merge_report=True, stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path)),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.start()
    mc.report_xlsx = src  # pretend stop() found it
    assert mc.merge_source() == src


def test_merge_source_none_when_no_report(monkeypatch, tmp_path):
    mc = MonitorController(_cfg(), _StubProvider(vmm_type="firecracker"))
    # report_xlsx is None -> None, no raise
    assert mc.merge_source() is None


def test_merge_source_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.observability.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    src = tmp_path / "resource_report.xlsx"
    _make_src_xlsx(src)
    mc = MonitorController(
        _cfg(merge_report=False, stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path)),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.report_xlsx = src
    assert mc.merge_source() is None


def test_merge_source_none_when_missing(monkeypatch, tmp_path, caplog):
    mc = MonitorController(
        _cfg(merge_report=True, stress_file=str(tmp_path / "lock"), log_dir=str(tmp_path)),
        _StubProvider(vmm_type="firecracker"),
    )
    mc.report_xlsx = tmp_path / "does-not-exist.xlsx"
    # must not raise; warns and returns None
    assert mc.merge_source() is None
    assert any("report missing" in r.message.lower() for r in caplog.records)
