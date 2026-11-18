"""CLI parsing for the log-rotation uprof metrics flag.

--rotation-uprof-metrics is the only rotation knob with a non-trivial default
contract: the argparse default is vm_monitor's built-in set, an explicit empty
value passes through to LogCapture (which falls back to the same set), and
bench-core only forwards the flag when monitor.log_rotation.uprof_metrics is
set. These tests pin the CLI side of that contract.
"""

from vm_monitor.capture.log_capture import DEFAULT_UPROF_METRICS
from vm_monitor.cli import build_arg_parser


def _parse(args):
    return build_arg_parser().parse_args(args)


def test_rotation_uprof_metrics_flag_parsed():
    args = _parse(["--vmm", "qemu", "--log-rotation", "--rotation-uprof-metrics", "memory,bandwidth"])
    assert args.rotation_uprof_metrics == "memory,bandwidth"


def test_rotation_uprof_metrics_default_is_builtin_set():
    args = _parse(["--vmm", "qemu"])
    assert args.rotation_uprof_metrics == DEFAULT_UPROF_METRICS


def test_rotation_uprof_metrics_empty_value_passes_through():
    # "" reaches LogCapture untouched; _resolve_uprof_metrics does the
    # fallback to DEFAULT_UPROF_METRICS there.
    args = _parse(["--vmm", "qemu", "--rotation-uprof-metrics", ""])
    assert args.rotation_uprof_metrics == ""
