from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench_core.config import KernelConfig
from bench_core.monitor import MonitorConfig, MonitorController


def test_monitor_config_defaults_when_absent():
    cfg = KernelConfig.from_raw({"sandbox": {"total_count": 1}})
    assert isinstance(cfg.monitor, MonitorConfig)
    assert cfg.monitor.enabled == "auto"
    assert cfg.monitor.vmm == "auto"
    assert cfg.monitor.capture == "auto"
    assert cfg.monitor.interval == 3
    assert cfg.monitor.merge_report is True
    assert cfg.monitor.report_timeout == 300
    assert cfg.monitor.log_dir is None


def test_monitor_config_from_raw_overrides():
    raw = {
        "sandbox": {"total_count": 1},
        "monitor": {
            "enabled": "true",
            "vmm": "qemu",
            "interval": 5,
            "capture": "false",
            "numa": "0,1",
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
    monkeypatch.setattr("bench_core.monitor.shutil.which", lambda _: None)
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(), prov)
    mc.start()
    assert mc._started is False
    assert any("binary not found" in r.message for r in caplog.records)


def test_command_construction(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(stress_file=str(tmp_path / "lock")), prov)
    cmd = mc._cmd
    assert "--vmm" in cmd and "firecracker" in cmd
    assert "--stress-file" in cmd
    assert "--auto-skip" in cmd and "--enable-capture" in cmd  # capture=auto
    assert "-i" in cmd and "3" in cmd
    assert "--numa" in cmd
    assert "-t" in cmd  # hard upper-bound timer


def test_command_capture_false_omits_flags(monkeypatch, tmp_path):
    monkeypatch.setattr("bench_core.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(capture="false", stress_file=str(tmp_path / "lock")), prov)
    assert "--enable-capture" not in mc._cmd
    assert "--auto-skip" not in mc._cmd


def test_start_removes_stale_lock(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("bench_core.monitor.shutil.which", lambda _: "/fake/vm-monitor")
    monkeypatch.setattr("bench_core.monitor.subprocess.Popen", lambda *a, **kw: _FakeProc())
    lock = tmp_path / "lock"
    lock.touch()
    prov = _StubProvider(vmm_type="firecracker")
    mc = MonitorController(_cfg(stress_file=str(lock), log_dir=str(tmp_path)), prov)
    mc.start()
    assert mc._started is True
    assert not lock.exists()
    assert any("stale lock" in r.message.lower() for r in caplog.records)


def test_start_degrades_when_log_dir_unwritable(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("bench_core.monitor.shutil.which", lambda _: "/fake/vm-monitor")
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

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
