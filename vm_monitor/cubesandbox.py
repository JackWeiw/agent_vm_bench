# vm_monitor/cubesandbox.py
"""
CubeSandboxMonitor - CubeSandbox (Cloud Hypervisor microVM) Monitor

Monitors ``containerd-shim-cube-rs`` processes -- the per-sandbox shim CubeSandbox
spawns for each Cloud Hypervisor microVM. Used by the ``cubesandbox`` provider,
which auto-selects this VMM via ``vmm_type``.
"""

import re

from .base import VMMonitorBase

# containerd-shim invocation: ``containerd-shim-cube-rs -namespace <ns> -id <id> -address <sock>``
_ID_RE = re.compile(r"(?:^|\s)-id\s+(\S+)")


class CubeSandboxMonitor(VMMonitorBase):
    """CubeSandbox microVM Monitor.

    Monitors ``containerd-shim-cube-rs`` shim processes.
    """

    # Truncated comm, not the 24-char binary name: psutil's name() reads
    # /proc/<pid>/stat field 2, which the kernel caps at 15 chars.
    PROCESS_NAMES = ("containerd-shim",)

    def get_process_names(self) -> tuple[str, ...]:
        """Return CubeSandbox shim process names to match"""
        return self.PROCESS_NAMES

    def _matches_vm_process(self, proc_name: str, cmdline: str) -> bool:
        """Match cube shims only -- every containerd shim shares the comm.

        ``containerd-shim-runc-v2`` truncates to the same ``containerd-shim``
        comm, so the comm alone would sweep in every ordinary container on the
        host. Prefer the full binary name when it survived in ``proc_name`` (a
        caller passing the untruncated name), else disambiguate on the cmdline;
        a truncated comm with an unreadable cmdline is skipped rather than
        miscounted.
        """
        return "containerd-shim" in proc_name and ("cube" in proc_name or "containerd-shim-cube-rs" in cmdline)

    def _cmdline_may_match(self, proc_name: str) -> bool:
        """Read shim command lines: the cube suffix does not fit the 15-char comm."""
        return "containerd-shim" in proc_name

    def extract_vm_id(self, pid: int, cmdline: str) -> str:
        """Extract sandbox ID from the cube shim command line.

        containerd-shim receives the sandbox id via ``-id <id>``. Falls back to
        ``cube-{pid}`` when the flag is absent (e.g. an unfamiliar invocation).
        """
        m = _ID_RE.search(cmdline)
        if m:
            return m.group(1)
        return f"cube-{pid}"

    def get_vms_realtime(self) -> list[dict]:
        """Get real-time information for all CubeSandbox microVMs.

        Uses two-phase collection:
        Phase 1: Discover VM processes (serial /proc scan)
        Phase 2: Collect per-VM metrics (parallel for large counts)
        """
        vm_candidates = self._discover_vm_processes()
        return self._collect_vm_metrics_parallel(vm_candidates)

    def get_monitor_title(self) -> str:
        return "CubeSandbox VM Real-time Monitoring"

    def get_no_vm_message(self) -> str:
        return "No running CubeSandbox microVMs detected"

    def get_csv_filename_prefix(self) -> str:
        return "cubesandbox_monitor"
