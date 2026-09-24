"""Host-level vm_monitor orchestration for run_benchmark.

MonitorController wraps the ``vm-monitor`` CLI as a subprocess bracketed around
the active-stress phase. Trigger = stress-file sync: vm_monitor idles waiting
for a lock file, samples while it exists, exports on removal. The controller
degrades to a no-op when the provider has no VMM (``vmm_type is None``), the
binary is missing, or the lock dir is unwritable -- never compromising the bench.
"""
from __future__ import annotations

import atexit
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_VM_MONITOR_BIN = "vm-monitor"

# Valid ``monitor.skip`` tokens (hyphenated flag stems). These forward to the
# vm-monitor CLI as ``--no-{stem}``. bench_core wraps vm-monitor as an opaque
# subprocess, so this literal is the contract with
# ``vm_monitor/cli.py COLLECTOR_FLAGS`` stems -- keep them in sync. An unknown
# token is warned + dropped (never silently no-op, never crash the bench on a
# typo). Runtime WARNING surfaces any drift between this set and the CLI.
_VALID_SKIP_STEMS = frozenset(
    {
        "hugepage",
        "numa-cpu",
        "host-stats",
        "swap",
        "host-mem-detail",
        "pressure",
        "numa-memory",
        "vm-total",
        "disk",
        "ublk",
        "pss",
        "ublk-daemon",
        "devkit-mem",
        "devkit-topdown",
    }
)


@dataclass
class MonitorConfig:
    """Host-level monitor toggles (the ``monitor:`` YAML section)."""

    enabled: str = "auto"  # auto | true | false   (auto = decide by provider.vmm_type)
    vmm: str = "auto"  # auto | qemu | firecracker  (auto = take provider hint)
    interval: int = 2  # sampling interval (seconds)
    capture: str = "auto"  # auto | true | false  (auto/true -> --enable-capture --auto-skip)
    numa: str = "all"  # NUMA nodes: "all" = every node, or comma-separated "0,1"
    disks: str = "all"  # block devices for I/O, comma-separated (sda,nvme0n1); "all" auto-discovers
    stress_file: str = "/dev/shm/bench_core_monitor.lock"
    log_dir: str | None = None  # None -> <report.output_dir>/vm_monitor
    # False = keep vm_monitor's system-resource report (resource_report.xlsx) as a
    # standalone file; the replay obs workbook stays a trajectory-metrics-only file.
    # True = copy VM_Stats/NUMA_Overview/DevKit_TopDown into the obs workbook.
    merge_report: bool = False
    report_timeout: int = 300  # vm_monitor exit/export budget (s): 2x = no-SIGTERM wait, 1x = post-SIGTERM grace
    # Collectors/devkit sub-tools to skip, forwarded to vm-monitor as --no-X.
    # Accepts a YAML list ("skip: [swap, hugepage]") or a comma string
    # ("skip: swap, hugepage"). Tokens are hyphenated flag stems; unknown names
    # are warned + dropped in _build_cmd. Default empty = all collectors on.
    skip: list[str] | None = None
    # Skip the xlsx chart phase in vm_monitor's export (forwards --no-charts).
    # For huge runs (many VMs x long sampling) the openpyxl chart build (load +
    # ~14 chart builders + full re-save) dominates export time and can exceed
    # the courtesy wait in stop(); skipping still writes every sheet, just
    # without charts. Default false (charts on); opt-in for oversub-scale runs.
    skip_charts: bool = False

    @classmethod
    def from_raw(cls, raw: dict | None) -> MonitorConfig:
        if not raw:
            return cls()
        raw_skip = raw.get("skip")
        if raw_skip is None:
            skip: list[str] | None = None
        elif isinstance(raw_skip, str):
            skip = [t.strip() for t in raw_skip.split(",") if t.strip()]
        else:
            skip = [str(t).strip() for t in raw_skip if str(t).strip()]
        return cls(
            enabled=str(raw.get("enabled", "auto")),
            vmm=str(raw.get("vmm", "auto")),
            interval=int(raw.get("interval", 2)),
            capture=str(raw.get("capture", "auto")),
            numa=str(raw.get("numa", "all")),
            disks=str(raw.get("disks", "all")),
            stress_file=str(raw.get("stress_file", "/dev/shm/bench_core_monitor.lock")),
            log_dir=raw.get("log_dir"),
            merge_report=bool(raw.get("merge_report", False)),
            report_timeout=int(raw.get("report_timeout", 300)),
            skip=skip,
            skip_charts=bool(raw.get("skip_charts", False)),
        )


class MonitorController:
    """Spawns ``vm-monitor`` for the stress window; no-op when not applicable."""

    def __init__(self, config, provider):
        self._config = config
        self._provider = provider
        mc = config.monitor
        self._vmm_resolved = mc.vmm if mc.vmm != "auto" else provider.vmm_type
        self._capture_on = mc.capture in ("auto", "true")
        self._effective = mc.enabled == "true" or (mc.enabled == "auto" and self._vmm_resolved is not None)
        self._log_dir = Path(mc.log_dir) if mc.log_dir else Path(config.output_dir) / "vm_monitor"
        self._stress_file = Path(mc.stress_file)
        self._report_timeout = mc.report_timeout
        self._merge_report = mc.merge_report
        self._interval = mc.interval
        self._numa = mc.numa
        self._disks = mc.disks
        self.proc = None
        self._stdout_fh = None
        self._stderr_fh = None
        self._begin_ts: float | None = None
        self._end_ts: float | None = None
        self.report_xlsx: Path | None = None
        self._started = False
        self._detached = False  # stop() set this: vm_monitor left running to finish async
        self._cmd = self._build_cmd()

    def _build_cmd(self) -> list[str]:
        exe = shutil.which(_VM_MONITOR_BIN)
        if exe is None:
            return []
        cmd = [
            exe,
            "--vmm",
            self._vmm_resolved,
            "-i",
            str(self._interval),
            "--numa",
            self._numa,
            "--disks",
            self._disks,
            "--stress-file",
            str(self._stress_file),
            "--log-dir",
            str(self._log_dir),
        ]
        if self._capture_on:
            cmd += ["--enable-capture", "--auto-skip"]
        # Forward monitor.skip as --no-X flags. Unknown tokens are warned + dropped
        # (never silently no-op; never crash the bench on a YAML typo).
        for token in self._config.monitor.skip or []:
            if token in _VALID_SKIP_STEMS:
                cmd += [f"--no-{token}"]
            else:
                logger.warning("Unknown collector name: %s, skip ignored", token)
        # Skip the xlsx chart phase (forwards --no-charts). Opt-in for huge runs
        # where the openpyxl chart build dominates export time.
        if self._config.monitor.skip_charts:
            cmd += ["--no-charts"]
        # Hard upper bound: vm_monitor exits after this even if the lock is never
        # removed (SIGKILL/OOM on the kernel side cannot reap the subprocess).
        hard_t = getattr(self._config, "test_duration", self._report_timeout) + 60
        cmd += ["-t", str(hard_t)]
        return cmd

    def start(self) -> None:
        if not self._effective:
            logger.warning("vm_monitor disabled: provider=%s vmm=%s", self._provider.name, self._vmm_resolved)
            return
        if not self._cmd:
            logger.warning("vm_monitor disabled: %s binary not found on PATH", _VM_MONITOR_BIN)
            return
        # Stale-lock cleanup: a prior hard crash (SIGKILL/OOM) may have left the lock;
        # vm_monitor would otherwise treat a pre-existing file as "stress active" and exit.
        if self._stress_file.exists():
            try:
                self._stress_file.unlink()
            except OSError as e:
                logger.warning("vm_monitor disabled: cannot remove stale lock %s: %s", self._stress_file, e)
                return
            logger.warning("Stress lock file found from previous run, removing stale lock %s", self._stress_file)
        # Lock-dir / log-dir write probe (e.g. /dev/shm denied in restricted envs).
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._stdout_fh = open(self._log_dir / "stdout.log", "w", buffering=1)
            self._stderr_fh = open(self._log_dir / "stderr.log", "w", buffering=1)
        except OSError as e:
            logger.warning("vm_monitor disabled: cannot write log_dir %s: %s", self._log_dir, e)
            return
        try:
            # start_new_session: if stop() later detaches (vm_monitor still
            # exporting past the courtesy wait), it must survive bench/shell exit
            # to finish the xlsx build + os.replace async. Own session = immune to
            # group signals (SIGHUP/SIGTERM) sent to the bench's process group.
            self.proc = subprocess.Popen(
                self._cmd,
                stdout=self._stdout_fh,
                stderr=self._stderr_fh,
                start_new_session=True,
            )
        except OSError as e:
            logger.error("vm_monitor failed to spawn: %s", e)
            self._close_handles()
            return
        atexit.register(self._emergency_kill)
        self._started = True
        time.sleep(0.5)  # brief init; vm_monitor idles waiting for the lock

    def begin_stress(self) -> None:
        if not self._started:
            return
        try:
            self._stress_file.touch()
        except OSError as e:
            logger.warning("vm_monitor: cannot create stress lock %s: %s", self._stress_file, e)
            return
        self._begin_ts = time.time()

    def end_stress(self) -> None:
        if not self._started:
            return
        try:
            self._stress_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("vm_monitor: cannot remove stress lock %s: %s", self._stress_file, e)
        self._end_ts = time.time()

    @property
    def stress_window(self) -> float | None:
        """Seconds between begin_stress and end_stress, or None if the bracket never ran."""
        if self._begin_ts is not None and self._end_ts is not None:
            return self._end_ts - self._begin_ts
        return None

    def _close_handles(self) -> None:
        for attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, attr)
            if fh is not None and not fh.closed:
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass
            setattr(self, attr, None)

    def stop(self) -> list[Path]:
        """Wait briefly for vm_monitor to finish + write ``resource_report.xlsx``;
        if it isn't done, **detach** -- never kill.

        Killing is what loses the report. The xlsx is the LAST artifact
        (``export_to_excel`` after CSV/SVG), and for a large run the openpyxl
        build alone can exceed any fixed kill budget. SIGTERM cannot interrupt
        it (the handler only flags the sampling loop, already done), so a kill
        cliff escalates SIGTERM -> SIGKILL mid-build, leaving an orphan
        ``.build.xlsx`` and no ``resource_report.xlsx``. The atomic
        ``build -> os.replace`` only prevents a *corrupt* final file; a kill
        *before* the rename produces no report at all.

        So we do not kill. We block the bench only for a courtesy
        ``report_timeout`` window hoping to collect the report inline (small
        runs finish in seconds; ``merge_report`` still works). If vm_monitor
        is still exporting past that, we detach: it keeps running in its own
        session (``start_new_session`` at spawn) and finishes the build +
        ``os.replace`` asynchronously, writing ``resource_report.xlsx`` on its
        own. ``-t`` (sampling) + ``capture.wait``'s internal timeouts bound
        collection; the export then runs to completion. ``_emergency_kill``
        skips detached procs so a normal bench exit does not reap it.

        This also unblocks the bench's own ``obs.xlsx`` (rendered right after
        this returns instead of after a 15-min kill cliff); ``merge_source``
        is None-graceful when the report isn't ready yet.
        """
        if not self._started:
            return []
        xlsx = self._log_dir / "resource_report.xlsx"
        # Courtesy wait for a clean exit + synchronous report collection. This
        # is NOT a kill cliff (there is no kill); it is only how long we block
        # the bench hoping to collect the report inline.
        exit_rc = self.proc.poll()
        deadline = time.time() + self._report_timeout
        while exit_rc is None and time.time() < deadline:
            if xlsx.exists() and self.report_xlsx is None:
                self.report_xlsx = xlsx
            time.sleep(1)
            exit_rc = self.proc.poll()
        if exit_rc is not None:
            # Process exited on its own -- collect the report if it produced one.
            if not xlsx.exists():
                logger.error("vm_monitor subprocess exited (code=%s) without report", exit_rc)
            elif self.report_xlsx is None:
                self.report_xlsx = xlsx
            self._close_handles()
            self._started = False
            return [self.report_xlsx] if self.report_xlsx is not None else []
        # Still running -- DETACH. Never kill: the xlsx is written last and a
        # kill now (or at any cliff) lands mid-build and loses the report --
        # the orphan ``.build.xlsx`` such a kill leaves is exactly the bug we
        # are removing. vm_monitor finishes the build + os.replace async.
        logger.warning(
            "vm_monitor still exporting after %ds; detaching -- it will write %s "
            "asynchronously (not killed: a kill mid-build loses the report)",
            self._report_timeout,
            xlsx,
        )
        self._detached = True
        self._close_handles()  # bench releases its fds; vm_monitor keeps inherited copies
        self._started = False
        return [self.report_xlsx] if self.report_xlsx is not None else []

    def merge_source(self) -> Path | None:
        """Return the host report to merge into the obs workbook, or ``None``.

        The obs renderer ingests host sheets (VM_Stats/NUMA_Overview/DevKit_TopDown)
        during its single write pass, so this controller no longer touches the obs
        file -- a previous load/save round-trip there dropped every chart and PNG.
        Returns the report path when ``merge_report`` is set and a report exists;
        any failure (incl. openpyxl OOM on large workbooks) -> None; raw artifacts
        always remain in <log_dir>/.
        """
        if self.report_xlsx is None or not self._merge_report:
            return None
        if not Path(self.report_xlsx).exists():
            logger.warning("vm_monitor merge skipped: report missing (%s)", self.report_xlsx)
            return None
        return self.report_xlsx

    def _emergency_kill(self) -> None:
        """atexit backstop. Does NOT run on SIGKILL/OOM -- documented limitation;
        vm_monitor's own -t timer is the hard back-stop in those cases. Skipped
        when stop() detached vm_monitor: reaping it on bench exit would lose the
        report it is still asynchronously writing."""
        if self._detached:
            return  # detached: let it finish + os.replace; do not reap
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._close_handles()
