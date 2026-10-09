"""Tests for the CubeSandbox VMM monitor (containerd-shim-cube-rs)."""
from __future__ import annotations

from vm_monitor.cubesandbox import CubeSandboxMonitor


def test_process_names_match_cube_shim():
    m = CubeSandboxMonitor()
    assert m.get_process_names() == ("containerd-shim-cube-rs",)


def test_extract_vm_id_from_id_flag():
    m = CubeSandboxMonitor()
    cmdline = "containerd-shim-cube-rs -namespace default -id sbx_abc123 " "-address /run/containerd/sbx_abc123.sock"
    assert m.extract_vm_id(4242, cmdline) == "sbx_abc123"


def test_extract_vm_id_falls_back_to_pid_when_no_id_flag():
    m = CubeSandboxMonitor()
    # an unfamiliar invocation without -id -> stable cube-<pid> fallback
    assert m.extract_vm_id(4242, "containerd-shim-cube-rs --something-else") == "cube-4242"


def test_titles_and_prefix():
    m = CubeSandboxMonitor()
    assert "CubeSandbox" in m.get_monitor_title()
    assert "CubeSandbox" in m.get_no_vm_message()
    assert m.get_csv_filename_prefix() == "cubesandbox_monitor"
