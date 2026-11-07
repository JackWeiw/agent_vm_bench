from __future__ import annotations

from bench_core.bench import build_arg_parser
from bench_core.config import KernelConfig
from bench_core.observability import monitor as monitor_mod
from bench_core.observability.monitor import MonitorConfig, MonitorController
from env_provider.fake import FakeProvider


def _parse(args):
    return build_arg_parser().parse_args(args)


def test_vm_monitor_default_auto():
    assert _parse(["--config", "x"]).vm_monitor == "auto"


def test_no_vm_monitor_short_circuits():
    args = _parse(["--config", "x", "--no-vm-monitor"])
    assert args.no_vm_monitor is True


def test_cli_override_applied_to_config():
    from bench_core.bench import _apply_monitor_override

    cfg = KernelConfig.from_raw({"sandbox": {"total_count": 1}, "monitor": {"enabled": "true"}})
    _apply_monitor_override(cfg, _parse(["--config", "x", "--no-vm-monitor"]))
    assert cfg.monitor.enabled == "false"

    cfg2 = KernelConfig.from_raw({"sandbox": {"total_count": 1}})
    _apply_monitor_override(cfg2, _parse(["--config", "x", "--vm-monitor", "false"]))
    assert cfg2.monitor.enabled == "false"

    cfg3 = KernelConfig.from_raw({"sandbox": {"total_count": 1}})
    _apply_monitor_override(cfg3, _parse(["--config", "x", "--vm-monitor", "true"]))
    assert cfg3.monitor.enabled == "true"


# ---- monitor.skip forwarding (vm-monitor --no-X flags) ----


def test_monitor_config_skip_from_raw_list():
    mc = MonitorConfig.from_raw({"skip": ["swap", "devkit-mem"]})
    assert mc.skip == ["swap", "devkit-mem"]


def test_monitor_config_skip_from_comma_string_is_stripped():
    # A YAML scalar string is tolerated: split on comma + strip whitespace.
    mc = MonitorConfig.from_raw({"skip": "swap, devkit-mem , bogus"})
    assert mc.skip == ["swap", "devkit-mem", "bogus"]


def _controller_with_skip(skip, monkeypatch, tmp_path):
    """Build a MonitorController whose _cmd reflects the skip list (vm-monitor
    binary faked so _build_cmd does not short-circuit on a missing binary)."""
    monkeypatch.setattr(monitor_mod.shutil, "which", lambda _name: "/fake/vm-monitor")
    cfg = KernelConfig.from_raw(
        {
            "sandbox": {"total_count": 1},
            "monitor": {"enabled": "true", "vmm": "qemu", "skip": skip},
        }
    )
    # output_dir must be a real path the controller can reference (no mkdir in __init__).
    cfg.output_dir = str(tmp_path)
    return MonitorController(cfg, FakeProvider(count=1))


def test_build_cmd_forwards_skip_as_no_flags(monkeypatch, tmp_path):
    c = _controller_with_skip(["swap", "devkit-mem"], monkeypatch, tmp_path)
    assert "--no-swap" in c._cmd
    assert "--no-devkit-mem" in c._cmd


def test_build_cmd_warns_and_drops_unknown_skip(monkeypatch, tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="bench_core.observability.monitor")
    c = _controller_with_skip(["swap", "bogus"], monkeypatch, tmp_path)
    assert "--no-swap" in c._cmd
    assert "--no-bogus" not in c._cmd
    assert any("Unknown collector name: bogus" in r.message for r in caplog.records)
