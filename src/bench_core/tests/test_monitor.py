from __future__ import annotations

from bench_core.config import KernelConfig
from bench_core.monitor import MonitorConfig


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
