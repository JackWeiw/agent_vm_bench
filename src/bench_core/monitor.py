"""Host-level vm_monitor orchestration for run_benchmark.

MonitorController wraps the ``vm-monitor`` CLI as a subprocess bracketed around
the active-stress phase. Trigger = stress-file sync: vm_monitor idles waiting
for a lock file, samples while it exists, exports on removal. The controller
degrades to a no-op when the provider has no VMM (``vmm_type is None``), the
binary is missing, or the lock dir is unwritable -- never compromising the bench.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MonitorConfig:
    """Host-level monitor toggles (the ``monitor:`` YAML section)."""

    enabled: str = "auto"  # auto | true | false   (auto = decide by provider.vmm_type)
    vmm: str = "auto"  # auto | qemu | firecracker  (auto = take provider hint)
    interval: int = 3  # sampling interval (seconds)
    capture: str = "auto"  # auto | true | false  (auto/true -> --enable-capture --auto-skip)
    numa: str = "1"  # NUMA nodes, comma-separated
    stress_file: str = "/dev/shm/bench_core_monitor.lock"
    log_dir: str | None = None  # None -> <report.output_dir>/vm_monitor
    merge_report: bool = True  # replay xlsx: copy key host sheets into obs workbook
    report_timeout: int = 300  # max wait for analysis_report.xlsx (seconds)

    @classmethod
    def from_raw(cls, raw: dict | None) -> MonitorConfig:
        if not raw:
            return cls()
        return cls(
            enabled=str(raw.get("enabled", "auto")),
            vmm=str(raw.get("vmm", "auto")),
            interval=int(raw.get("interval", 3)),
            capture=str(raw.get("capture", "auto")),
            numa=str(raw.get("numa", "1")),
            stress_file=str(raw.get("stress_file", "/dev/shm/bench_core_monitor.lock")),
            log_dir=raw.get("log_dir"),
            merge_report=bool(raw.get("merge_report", True)),
            report_timeout=int(raw.get("report_timeout", 300)),
        )
