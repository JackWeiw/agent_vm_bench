"""Tests for the CubeSandbox VMM monitor (containerd-shim-cube-rs)."""
from __future__ import annotations

from vm_monitor.vmm.cubesandbox import CubeSandboxMonitor


def test_process_names_use_kernel_truncated_comm():
    m = CubeSandboxMonitor()
    # psutil name() is comm, capped at 15 chars -- the 24-char binary name
    # never matched, so the whole per-VM series was empty (issue #270).
    assert m.get_process_names() == ("containerd-shim",)


def test_matches_only_cube_shims_not_runc_shims():
    m = CubeSandboxMonitor()
    assert m._matches_vm_process(
        "containerd-shim",
        "containerd-shim-cube-rs -namespace default -id sbx_abc123",
    )
    # Same truncated comm, different plugin -- must not be counted as a VM.
    assert not m._matches_vm_process(
        "containerd-shim",
        "containerd-shim-runc-v2 -namespace moby -id 3f9a1c -address /run/containerd/containerd.sock",
    )
    # If a psutil build reports the full command-line name, match it even when
    # the cmdline is unreadable (hidepid); "top" shows this 24-char form.
    assert m._matches_vm_process("containerd-shim-cube-rs", "")


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
