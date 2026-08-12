#!/usr/bin/env python3
"""
E2B Sandbox Bench - Main Entry Point

Integrates all components, runs test workflow:
Create sandboxes -> Warmup -> Start stats -> Start tasks -> Run duration -> Stop -> Report

Supports multiple modes:
1. Full workflow: create -> port check -> warmup -> tasks -> stats
2. Create-only: create -> port check -> exit (Phase 0)
3. Detect existing: detect -> warmup -> tasks -> stats
4. Warmup-only: create/detect -> warmup -> exit
"""

import argparse
import logging
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict

from .config import Config
from .schemas import SandboxState, SandboxStatus
from .utils import setup_logging

logger = logging.getLogger(__name__)

# Warmup wave size constant - max sandboxes per wave in warmup-only mode.
# Used by the batch scheduler's wave path (the single-test kernel defers
# >100-sandbox warmup to the scheduler); retained here as the shared definition.
WARMUP_WAVE_SIZE = 100


class SmapToolManager:
    """Manage smap_tool process lifecycle for memory migration monitoring"""

    def __init__(self, config, log_dir: str = None):
        self.config = config
        self.log_dir = log_dir  # Custom log directory (for batch test result)
        self.process = None
        self.pid = None
        self.stdout_file = None
        self.stderr_file = None

    def start(self, sandbox_count: int) -> bool:
        """
        Start smap_tool process

        Command format:
        ./smap_tool <count> `pidof firecracker` --swap-size <size> --ratio <ratio> --src-nid <nid> --dest-nid <nid>
        """
        if not self.config.smap_tool_enabled:
            logger.warning("[SmapTool] Disabled in config, skipping")
            return True

        if not self.config.smap_tool_path:
            logger.warning("[SmapTool] Path not configured, skipping")
            return True

        # Get firecracker PIDs
        try:
            result = subprocess.run(["pidof", "firecracker"], capture_output=True, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                logger.info("[SmapTool] No firecracker processes found")
                return False
            firecracker_pids = result.stdout.strip()
            logger.info(f"[SmapTool] Found firecracker PIDs: {firecracker_pids}")
        except Exception as e:
            logger.error(f"[SmapTool] Failed to get firecracker PIDs: {e}")
            return False

        # Build command
        smap_dir = Path(self.config.smap_tool_path).parent
        smap_exe = Path(self.config.smap_tool_path).name

        # Clean up existing smap_config (Linux only)
        smap_config_path = Path("/dev/shm/smap_config")
        if smap_config_path.exists():
            if smap_config_path.is_dir():
                shutil.rmtree(smap_config_path)
            else:
                smap_config_path.unlink()
            logger.info("[SmapTool] Cleaned up existing /dev/shm/smap_config")

        cmd = (
            f"./{smap_exe} {sandbox_count} {firecracker_pids} "
            f"--swap-size {self.config.smap_tool_swap_size} "
            f"--ratio {self.config.smap_tool_ratio} "
            f"--src-nid {self.config.smap_tool_src_nid} "
            f"--dest-nid {self.config.smap_tool_dest_nid}"
        )

        logger.info(f"[SmapTool] Starting: {cmd}")
        logger.info(f"[SmapTool] Working directory: {smap_dir}")

        # Prepare log files in result directory
        if self.log_dir:
            log_path = Path(self.log_dir)
        else:
            log_path = Path(self.config.output_dir) / "smap_tool"
        log_path.mkdir(parents=True, exist_ok=True)

        self.stdout_file = open(log_path / "smap_stdout.log", "w")
        self.stderr_file = open(log_path / "smap_stderr.log", "w")

        try:
            is_windows = platform.system() == "Windows"

            if is_windows:
                # Windows: use CREATE_NEW_PROCESS_GROUP for process group management
                self.process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(smap_dir),
                    stdout=self.stdout_file,
                    stderr=self.stderr_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                # Unix/Linux: use preexec_fn=os.setpgrp for process group
                self.process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(smap_dir),
                    stdout=self.stdout_file,
                    stderr=self.stderr_file,
                    preexec_fn=os.setpgrp,
                )

            self.pid = self.process.pid
            logger.info(f"[SmapTool] Started with PID: {self.pid}")
            logger.info(f"[SmapTool] Logs saved to: {log_path}")
            return True
        except Exception as e:
            logger.error(f"[SmapTool] Failed to start: {e}")
            return False

    def stop(self) -> None:
        """Stop smap_tool process"""
        if self.process is None:
            return

        logger.info(f"[SmapTool] Stopping process (PID: {self.pid})...")
        try:
            is_windows = platform.system() == "Windows"

            if is_windows:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    logger.warning("[SmapTool] Process killed (timeout)")
            else:
                os.killpg(os.getpgid(self.pid), signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.pid), signal.SIGKILL)
                    logger.warning("[SmapTool] Process killed (timeout)")

            logger.info("[SmapTool] Process stopped gracefully")
        except Exception as e:
            logger.error(f"[SmapTool] Error stopping process: {e}")

        if self.stdout_file:
            self.stdout_file.close()
        if self.stderr_file:
            self.stderr_file.close()

        self.process = None
        self.pid = None

    def is_running(self) -> bool:
        """Check if smap_tool process is still running"""
        if self.process is None:
            return False
        return self.process.poll() is None


class VmMonitorManager:
    """Manage vm_monitor process lifecycle for performance monitoring"""

    def __init__(self, config, log_dir: str = None):
        self.config = config
        self.log_dir = log_dir  # Custom log directory (for batch test result)
        self.process = None
        self.analysis_file = None
        self.stdout_file = None  # Log file handle for stdout
        self.stderr_file = None  # Log file handle for stderr

    def start(self, task_id: str = "") -> bool:
        """
        Start vm_monitor process with stress-file sync

        Command format:
        python3 vm_monitor.py --vmm firecracker -t <duration> --stress-file <file> --log-dir <dir>
        """
        if not self.config.vm_monitor_enabled:
            logger.warning("[VmMonitor] Disabled in config, skipping")
            return True

        # Prepare log directory - use provided log_dir or default
        if self.log_dir:
            log_path = Path(self.log_dir)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_dir_name = f"vm_monitor_{task_id}_{timestamp}" if task_id else f"vm_monitor_{timestamp}"
            log_path = Path(self.config.vm_monitor_log_dir) / log_dir_name
        log_path.mkdir(parents=True, exist_ok=True)

        # Clean up existing stress file
        stress_file = Path(self.config.vm_monitor_stress_file)
        if stress_file.exists():
            stress_file.unlink()

        # Build command - use vm_monitor.py directly (not vm_monitor/cli.py)
        project_root = Path(__file__).parent.parent
        vm_monitor_script = project_root / "vm_monitor.py"

        cmd = [
            "python3",
            str(vm_monitor_script),
            "--vmm",
            self.config.vm_monitor_vmm_type,
            "-t",
            str(self.config.vm_monitor_duration),
            "--numa",
            self.config.vm_monitor_numa,
            "--stress-file",
            str(stress_file),
            "--log-dir",
            str(log_path),
            "--enable-capture",  # Enable all capture tools by default
            "--auto-skip",  # Skip tools that are not available
        ]

        logger.info(f"[VmMonitor] Starting: {' '.join(cmd)}")
        logger.info(f"[VmMonitor] Log directory: {log_path}")

        # Redirect stdout/stderr to log files (not PIPE)
        # PIPE buffer (64KB) can fill up and block the process when vm_monitor outputs lots of data
        monitor_stdout_log = log_path / "monitor_stdout.log"
        monitor_stderr_log = log_path / "monitor_stderr.log"

        try:
            self.stdout_file = open(monitor_stdout_log, "w", buffering=1)
            self.stderr_file = open(monitor_stderr_log, "w", buffering=1)

            self.process = subprocess.Popen(cmd, stdout=self.stdout_file, stderr=self.stderr_file, text=True)
            logger.info(f"[VmMonitor] Started with PID: {self.process.pid}")
            logger.info(f"[VmMonitor] Waiting for stress file: {stress_file}")
            logger.info(f"[VmMonitor] Output redirected to: {monitor_stdout_log}")

            # Store expected analysis file path
            self.analysis_file = str(log_path / "analysis_report.xlsx")
            return True
        except Exception as e:
            logger.error(f"[VmMonitor] Failed to start: {e}")
            return False

    def trigger_sampling(self) -> None:
        """Create stress file to trigger vm_monitor sampling"""
        stress_file = Path(self.config.vm_monitor_stress_file)
        stress_file.touch()
        logger.info(f"[VmMonitor] Stress file created: {stress_file}")

    def stop_sampling(self) -> None:
        """Remove stress file to stop vm_monitor sampling"""
        stress_file = Path(self.config.vm_monitor_stress_file)
        if stress_file.exists():
            stress_file.unlink()
            logger.info(f"[VmMonitor] Stress file removed: {stress_file}")

    def wait_for_report(self, timeout: int = 300) -> str:
        """
        Wait for analysis_report.xlsx to be generated

        Returns file path if found, None if timeout
        """
        if not self.analysis_file:
            return None

        analysis_path = Path(self.analysis_file)
        logger.info(f"[VmMonitor] Waiting for report: {analysis_path}")

        start_time = time.time()
        check_interval = 10  # Check every 10 seconds
        while time.time() - start_time < timeout:
            if analysis_path.exists() and analysis_path.stat().st_size > 0:
                logger.info(f"[VmMonitor] Report generated: {analysis_path}")
                return str(analysis_path)

            elapsed = int(time.time() - start_time)
            remaining = timeout - elapsed
            if elapsed % 30 == 0:  # Log every 30 seconds
                logger.info(f"[VmMonitor] Waiting... {elapsed}s elapsed, {remaining}s remaining")
            time.sleep(check_interval)

        logger.error(f"[VmMonitor] Report not found after {timeout}s timeout")
        return None

    def stop(self) -> None:
        """Stop vm_monitor process"""
        if self.process is None:
            return

        logger.info(f"[VmMonitor] Stopping process (PID: {self.process.pid})...")
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
            logger.info("[VmMonitor] Process stopped gracefully")
        except subprocess.TimeoutExpired:
            self.process.kill()
            logger.warning("[VmMonitor] Process killed (timeout)")
        except Exception as e:
            logger.error(f"[VmMonitor] Error stopping process: {e}")

        # Close log file handles
        if self.stdout_file:
            self.stdout_file.close()
            self.stdout_file = None
        if self.stderr_file:
            self.stderr_file.close()
            self.stderr_file = None

        self.process = None


def append_sandbox_ids(config: Config, sandbox_states: Dict[int, SandboxState]) -> None:
    """Append sandbox IDs to file (one ID per line)

    Called after each wave completes, supports incremental ID saving.

    Args:
        config: Configuration object
        sandbox_states: Dictionary of sandbox states
    """
    if not config.sandbox_ids_file:
        return

    successful_ids = [
        s.sandbox_obj.sandbox_id
        for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.PORT_READY and s.sandbox_obj is not None
    ]

    if successful_ids:
        try:
            with open(config.sandbox_ids_file, "a") as f:  # Append mode
                for sid in successful_ids:
                    f.write(f"{sid}\n")
            logger.info(f"Appended {len(successful_ids)} sandbox IDs to: {config.sandbox_ids_file}")
        except OSError as e:
            logger.error(f"Failed to append sandbox IDs: {e}")


def run_benchmark(config: Config) -> dict:
    """Run an E2B sandbox benchmark via the host-agnostic bench-core kernel.

    Thin entry adapter: builds an E2BProvider from the (CLI-merged) e2b Config,
    translates it to a host-agnostic KernelConfig, and delegates to
    bench_core.bench.run_benchmark. The kernel owns the full create -> warmup
    -> benchmark -> report spine; this function is the host-specific glue.

    The wave-based >100-sandbox warmup path (create-in-batches during warmup)
    is deferred to the batch scheduler -- a follow-on phase.
    """
    config.validate()
    from bench_core.bench import run_benchmark as kernel_run
    from .provider import from_config, kernel_config_from_e2b

    kernel_config = kernel_config_from_e2b(config)
    stop_event = threading.Event()
    provider = from_config(config, stop_event)
    return kernel_run(kernel_config, provider)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(description="E2B Sandbox Bench - E2B Sandbox Batch Performance Testing Tool")

    # Configuration file
    parser.add_argument("-c", "--config", type=str, default=None, help="YAML configuration file path")

    # E2B environment variables
    parser.add_argument("--e2b-access-token", type=str, help="E2B access token")
    parser.add_argument("--e2b-api-key", type=str, help="E2B API key")
    parser.add_argument("--e2b-domain", type=str, help="E2B domain")
    parser.add_argument("--e2b-api-url", type=str, help="E2B API URL")
    parser.add_argument("--e2b-http-ssl", type=str, help="E2B HTTP SSL setting")

    # Sandbox configuration
    parser.add_argument("-t", "--template", type=str, help="E2B template name")
    parser.add_argument("-n", "--total", type=int, help="Total sandbox count")
    parser.add_argument("--create-timeout", type=int, help="Sandbox creation timeout")
    parser.add_argument(
        "-d", "--detect", action="store_true", help="Detect existing sandboxes instead of creating new ones"
    )
    parser.add_argument(
        "--create-only", action="store_true", help="Create sandboxes only without running tasks (Phase 0)"
    )
    parser.add_argument("--sandbox-ids-file", type=str, help="File path to save/load sandbox IDs (one ID per line)")

    # Create batch control
    parser.add_argument("--create-batch-size", type=int, help="Sandboxes per creation batch (None = full concurrent)")
    parser.add_argument("--create-batch-interval", type=int, help="Creation batch interval seconds")

    # Task batch control
    parser.add_argument(
        "--task-batch-size", type=int, help="Sandboxes to start tasks per batch (None = full concurrent)"
    )
    parser.add_argument("--task-batch-interval", type=int, help="Task batch interval seconds")

    # Workflow type selection
    parser.add_argument(
        "--workflow-type",
        choices=["browser", "coding", "document"],
        default=None,
        help="Workflow type: 'browser' (default), 'coding', or 'document'",
    )

    # Browser task
    parser.add_argument("--browser-url", type=str, action="append", help="Browser URL (can specify multiple)")
    parser.add_argument("--browser-timeout", type=int, help="Browser task timeout")
    parser.add_argument("--browser-interval-min", type=float, help="Task interval minimum")
    parser.add_argument("--browser-interval-max", type=float, help="Task interval maximum")

    # Coding task configuration
    parser.add_argument("--coding-project-dir", type=str, help="Project directory inside sandbox")
    parser.add_argument("--coding-language", type=str, help="Coding language (ts/go)")
    parser.add_argument("--coding-verify-timeout", type=int, help="Verify command timeout seconds")
    parser.add_argument(
        "--coding-source-file", type=str, action="append", help="Source file for modification (can specify multiple)"
    )
    parser.add_argument("--coding-skip-verify", action="store_true", help="Skip the verify step")
    parser.add_argument(
        "--coding-verify-repeat",
        type=int,
        default=None,
        help="ts only: number of independent npx tsx processes per verify step (default 3; go ignores this)",
    )

    # Document configuration. Recipe, seed and workspace paths are fixed by case kind.
    parser.add_argument("--document-case-kind", choices=["pdf", "xlsx"], help="Document scene kind")
    parser.add_argument("--document-operation-timeout", type=int, help="Document operation timeout seconds")
    parser.add_argument("--document-recalc-timeout", type=int, help="LibreOffice recalculation timeout seconds")
    parser.add_argument("--document-task-timeout", type=int, help="Complete document task timeout seconds")

    # Warmup configuration
    parser.add_argument("-w", "--warmup-url", type=str, action="append", help="Warmup page URL (can specify multiple)")
    parser.add_argument("--warmup-loops", type=int, default=None, help="Warmup loop count")
    parser.add_argument("--warmup-delay", type=int, default=None, help="Warmup page delay (seconds)")
    parser.add_argument("-wp", "--warmup-only", action="store_true", help="Run warmup phase only, then exit")

    # Benchmark control
    parser.add_argument(
        "-bp",
        "--benchmark-percent",
        type=float,
        default=None,
        help="Percentage of sandboxes for benchmark (e.g., 0.5 = 50%%)",
    )

    # Round-robin mode control
    parser.add_argument(
        "-bm",
        "--benchmark-mode",
        type=str,
        choices=["fixed", "round_robin"],
        default=None,
        help="Benchmark mode: 'fixed' (default) or 'round_robin'",
    )
    parser.add_argument(
        "-rc",
        "--round-count",
        type=int,
        default=None,
        help="Max number of rounds to run (termination condition, coexists with --round-size and duration)",
    )
    parser.add_argument(
        "-rs",
        "--round-size",
        type=int,
        default=None,
        help="Sandboxes per round (determines group count, coexists with --round-count, default: 5)",
    )
    parser.add_argument(
        "-ri",
        "--round-interval",
        type=int,
        default=None,
        help="Round interval in seconds for round_robin mode (default: 30)",
    )

    # Test run
    parser.add_argument("--duration", type=int, help="Test duration seconds")
    parser.add_argument("--stats-interval", type=int, help="Stats snapshot interval")

    # Report
    parser.add_argument("-o", "--output-dir", type=str, help="Report output directory")
    parser.add_argument("--filename-prefix", type=str, help="Report filename prefix")

    return parser


def main() -> None:
    """CLI entry point"""
    setup_logging()
    parser = build_arg_parser()
    args = parser.parse_args()

    # Load configuration
    if args.config:
        config = Config.load_from_yaml(args.config)
        config = Config.merge_with_args(config, args)
    else:
        # Without config file, use CLI arguments
        config = Config.from_args(args)

    # Validate required parameters
    if not config.e2b_access_token and not args.config:
        logger.error("E2B access token is required. Use --e2b-access-token or --config")
        return

    # Run test
    run_benchmark(config)


if __name__ == "__main__":
    main()
