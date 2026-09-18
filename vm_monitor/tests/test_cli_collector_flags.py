"""Tests for the --no-<collector> / --no-devkit-{mem,topdown} CLI flags.

The unified COLLECTOR_FLAGS map is the single source of truth for
flag_stem -> internal_name. collect_disabled() translates parsed args into
(base-collector disables, devkit disables). The argparse dest is derived from
the hyphenated STEM (not the internal underscore name), so
--no-host-mem-detail -> args.no_host_mem_detail (the old code derived dests from
internal names, which broke for hyphenated stems).
"""

from vm_monitor.cli import COLLECTOR_FLAGS, build_arg_parser, collect_disabled


def _parse(args):
    return build_arg_parser().parse_args(args)


def test_collector_flags_shape():
    stems = {stem for stem, _, _ in COLLECTOR_FLAGS}
    # every flag stem maps to exactly one (internal, target); targets are base/devkit
    targets = {t for _, _, t in COLLECTOR_FLAGS}
    assert targets == {"base", "devkit"}
    assert len(stems) == len(COLLECTOR_FLAGS)  # no duplicate stems


def test_no_flags_default_all_enabled():
    args = _parse(["--vmm", "qemu"])
    coll, dev = collect_disabled(args)
    assert coll == set()
    assert dev == set()


def test_no_swap_and_no_devkit_topdown():
    args = _parse(["--vmm", "qemu", "--no-swap", "--no-devkit-topdown"])
    coll, dev = collect_disabled(args)
    assert coll == {"swap"}
    assert dev == {"devkit_top_down"}


def test_multiple_base_collectors():
    args = _parse(["--vmm", "qemu", "--no-hugepage", "--no-numa-cpu", "--no-disk"])
    coll, dev = collect_disabled(args)
    assert coll == {"hugepage", "numa_cpu", "disk"}
    assert dev == set()


def test_no_devkit_mem():
    args = _parse(["--vmm", "qemu", "--no-devkit-mem"])
    coll, dev = collect_disabled(args)
    assert coll == set()
    assert dev == {"devkit_mem"}


def test_hyphenated_dest_resolved_from_stem():
    # dest is derived from the hyphenated STEM, so --no-host-mem-detail
    # becomes args.no_host_mem_detail (NOT a broken no_host_mem_detail lookup).
    args = _parse(["--vmm", "qemu", "--no-host-mem-detail"])
    assert args.no_host_mem_detail is True
    coll, dev = collect_disabled(args)
    assert coll == {"host_mem_detail"}
