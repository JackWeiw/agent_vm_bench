from __future__ import annotations

from bench_core.bench import build_arg_parser
from bench_core.config import KernelConfig


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
