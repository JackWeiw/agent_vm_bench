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

import time
import argparse
import threading
import subprocess
import signal
import os
import platform
import shutil
from pathlib import Path
from datetime import datetime

from .config import Config
from .sandbox_manager import SandboxManager
from .task_runner import TaskManager
from .stats_collector import StatsCollector
from .schemas import SandboxStatus


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
            print("[SmapTool] Disabled in config, skipping")
            return True

        if not self.config.smap_tool_path:
            print("[SmapTool] Path not configured, skipping")
            return True

        # Get firecracker PIDs
        try:
            result = subprocess.run(['pidof', 'firecracker'], capture_output=True, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                print("[SmapTool] No firecracker processes found")
                return False
            firecracker_pids = result.stdout.strip()
            print(f"[SmapTool] Found firecracker PIDs: {firecracker_pids}")
        except Exception as e:
            print(f"[SmapTool] Failed to get firecracker PIDs: {e}")
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
            print("[SmapTool] Cleaned up existing /dev/shm/smap_config")

        cmd = (
            f"./{smap_exe} {sandbox_count} {firecracker_pids} "
            f"--swap-size {self.config.smap_tool_swap_size} "
            f"--ratio {self.config.smap_tool_ratio} "
            f"--src-nid {self.config.smap_tool_src_nid} "
            f"--dest-nid {self.config.smap_tool_dest_nid}"
        )

        print(f"[SmapTool] Starting: {cmd}")
        print(f"[SmapTool] Working directory: {smap_dir}")

        # Prepare log files in result directory
        if self.log_dir:
            log_path = Path(self.log_dir)
        else:
            log_path = Path(self.config.output_dir) / "smap_tool"
        log_path.mkdir(parents=True, exist_ok=True)

        self.stdout_file = open(log_path / "smap_stdout.log", 'w')
        self.stderr_file = open(log_path / "smap_stderr.log", 'w')

        try:
            is_windows = platform.system() == 'Windows'

            if is_windows:
                # Windows: use CREATE_NEW_PROCESS_GROUP for process group management
                self.process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(smap_dir),
                    stdout=self.stdout_file,
                    stderr=self.stderr_file,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                # Unix/Linux: use preexec_fn=os.setpgrp for process group
                self.process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(smap_dir),
                    stdout=self.stdout_file,
                    stderr=self.stderr_file,
                    preexec_fn=os.setpgrp
                )

            self.pid = self.process.pid
            print(f"[SmapTool] Started with PID: {self.pid}")
            print(f"[SmapTool] Logs saved to: {log_path}")
            return True
        except Exception as e:
            print(f"[SmapTool] Failed to start: {e}")
            return False

    def stop(self) -> None:
        """Stop smap_tool process"""
        if self.process is None:
            return

        print(f"[SmapTool] Stopping process (PID: {self.pid})...")
        try:
            is_windows = platform.system() == 'Windows'

            if is_windows:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    print("[SmapTool] Process killed (timeout)")
            else:
                os.killpg(os.getpgid(self.pid), signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.pid), signal.SIGKILL)
                    print("[SmapTool] Process killed (timeout)")

            print("[SmapTool] Process stopped gracefully")
        except Exception as e:
            print(f"[SmapTool] Error stopping process: {e}")

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
            print("[VmMonitor] Disabled in config, skipping")
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
            "python3", str(vm_monitor_script),
            "--vmm", self.config.vm_monitor_vmm_type,
            "-t", str(self.config.vm_monitor_duration),
            "--numa", self.config.vm_monitor_numa,
            "--stress-file", str(stress_file),
            "--log-dir", str(log_path),
            "--auto-skip"
        ]

        print(f"[VmMonitor] Starting: {' '.join(cmd)}")
        print(f"[VmMonitor] Log directory: {log_path}")

        # Redirect stdout/stderr to log files (not PIPE)
        # PIPE buffer (64KB) can fill up and block the process when vm_monitor outputs lots of data
        monitor_stdout_log = log_path / "monitor_stdout.log"
        monitor_stderr_log = log_path / "monitor_stderr.log"

        try:
            self.stdout_file = open(monitor_stdout_log, 'w', buffering=1)
            self.stderr_file = open(monitor_stderr_log, 'w', buffering=1)

            self.process = subprocess.Popen(
                cmd,
                stdout=self.stdout_file,
                stderr=self.stderr_file,
                text=True
            )
            print(f"[VmMonitor] Started with PID: {self.process.pid}")
            print(f"[VmMonitor] Waiting for stress file: {stress_file}")
            print(f"[VmMonitor] Output redirected to: {monitor_stdout_log}")

            # Store expected analysis file path
            self.analysis_file = str(log_path / "analysis_report.xlsx")
            return True
        except Exception as e:
            print(f"[VmMonitor] Failed to start: {e}")
            return False

    def trigger_sampling(self) -> None:
        """Create stress file to trigger vm_monitor sampling"""
        stress_file = Path(self.config.vm_monitor_stress_file)
        stress_file.touch()
        print(f"[VmMonitor] Stress file created: {stress_file}")

    def stop_sampling(self) -> None:
        """Remove stress file to stop vm_monitor sampling"""
        stress_file = Path(self.config.vm_monitor_stress_file)
        if stress_file.exists():
            stress_file.unlink()
            print(f"[VmMonitor] Stress file removed: {stress_file}")

    def wait_for_report(self, timeout: int = 300) -> str:
        """
        Wait for analysis_report.xlsx to be generated

        Returns file path if found, None if timeout
        """
        if not self.analysis_file:
            return None

        analysis_path = Path(self.analysis_file)
        print(f"[VmMonitor] Waiting for report: {analysis_path}")

        start_time = time.time()
        check_interval = 10  # Check every 10 seconds
        while time.time() - start_time < timeout:
            if analysis_path.exists() and analysis_path.stat().st_size > 0:
                print(f"[VmMonitor] Report generated: {analysis_path}")
                return str(analysis_path)

            elapsed = int(time.time() - start_time)
            remaining = timeout - elapsed
            if elapsed % 30 == 0:  # Log every 30 seconds
                print(f"[VmMonitor] Waiting... {elapsed}s elapsed, {remaining}s remaining")
            time.sleep(check_interval)

        print(f"[VmMonitor] Report not found after {timeout}s timeout")
        return None

    def stop(self) -> None:
        """Stop vm_monitor process"""
        if self.process is None:
            return

        print(f"[VmMonitor] Stopping process (PID: {self.process.pid})...")
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
            print("[VmMonitor] Process stopped gracefully")
        except subprocess.TimeoutExpired:
            self.process.kill()
            print("[VmMonitor] Process killed (timeout)")
        except Exception as e:
            print(f"[VmMonitor] Error stopping process: {e}")

        # Close log file handles
        if self.stdout_file:
            self.stdout_file.close()
            self.stdout_file = None
        if self.stderr_file:
            self.stderr_file.close()
            self.stderr_file = None

        self.process = None


def run_benchmark(config: Config) -> dict:
    """Run E2B sandbox performance test

    Args:
        config: Test configuration object

    Returns:
        {'report': str, 'filepath': str}
    """
    # 1. Setup E2B environment variables
    config.setup_e2b_env()

    print("=" * 80)
    print("E2B Sandbox Bench - Batch Performance Test")
    print("=" * 80)
    print(f"  Template: {config.template}")

    # Mode display
    if config.detect_existing:
        print(f"  Mode:     Detect existing sandboxes")
    elif config.create_only:
        print(f"  Mode:     Create-only (Phase 0)")
    elif config.warmup_only:
        print(f"  Mode:     Warmup-only")
    else:
        print(f"  Mode:     Full workflow")

    print(f"  Total:    {config.total_count} sandboxes")

    # Batch config display
    if config.create_batch_size:
        print(f"  Create Batch: {config.create_batch_count} batches x {config.create_batch_size} (interval {config.create_batch_interval}s)")
    else:
        print(f"  Create Batch: Full concurrent creation")

    if not config.create_only:
        if config.task_batch_size:
            print(f"  Task Batch:   {config.task_batch_count} batches x {config.task_batch_size} (interval {config.task_batch_interval}s)")
        else:
            print(f"  Task Batch:   Full concurrent start")

        # Warmup config display
        if config.warmup_urls:
            print(f"  Warmup:       {len(config.warmup_urls)} pages x {config.warmup_loops} loops (delay {config.warmup_delay}s)")

    print(f"  Duration: {config.test_duration}s")

    # Benchmark percent display
    if config.benchmark_percent < 1.0:
        benchmark_count = config.benchmark_count
        print(f"  Benchmark: {benchmark_count}/{config.total_count} sandboxes ({config.benchmark_percent*100:.0f}%)")

    print("=" * 80)

    # Stop signal
    stop_event = threading.Event()

    # 2. Create or detect sandboxes
    sandbox_manager = SandboxManager(config, stop_event)

    if config.detect_existing:
        print("\n[Phase 1] Detecting existing sandboxes...")
        creation_start_time = time.time()
        sandbox_states = sandbox_manager.detect_existing()
        creation_end_time = time.time()
    else:
        print("\n[Phase 1] Creating sandboxes...")
        creation_start_time = time.time()
        sandbox_states = sandbox_manager.create_all()
        creation_end_time = time.time()

    ready_count = sum(
        1 for s in sandbox_states.values()
        if s.creation_metrics.status == SandboxStatus.PORT_READY
    )
    if ready_count == 0:
        print("No sandboxes ready for testing, exiting.")
        return {}

    print(f"\nSandboxes ready: {ready_count}")

    # Create-only mode: exit after creation with detailed timing report
    if config.create_only:
        print("\n[Phase 0 Complete] Create-only mode finished.")
        print(f"  Created: {len(sandbox_states)} sandboxes")
        print(f"  Ports Ready: {ready_count}")
        print(f"  Sandboxes left running for later use.")

        # Generate creation timing report
        from .utils import calc_percentiles

        # Sandbox status statistics
        ready_states = [s for s in sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_READY]
        failed_states = [s for s in sandbox_states.values() if s.creation_metrics.status == SandboxStatus.FAILED]
        port_failed_states = [s for s in sandbox_states.values() if s.creation_metrics.status == SandboxStatus.PORT_FAILED]

        print("\n" + "=" * 70)
        print("Creation Timing Report")
        print("=" * 70)

        # Total elapsed time for all sandboxes
        total_elapsed = creation_end_time - creation_start_time
        print(f"\n[Overall Creation Time]")
        print(f"  Total Wall Clock Time: {total_elapsed:.1f}s")
        print(f"  (From first sandbox creation start to last sandbox port ready)")
        print(f"  Throughput: {len(sandbox_states) / total_elapsed:.2f} sandboxes/sec")

        print(f"\n[Sandbox Status]")
        print(f"  Created (API):       {len([s for s in sandbox_states.values() if s.creation_metrics.status not in (SandboxStatus.PENDING, SandboxStatus.CREATING)])} / {len(sandbox_states)}")
        print(f"  Ports Ready:         {len(ready_states)} / {len(sandbox_states)}")
        print(f"  Create Failed:       {len(failed_states)}")
        print(f"  Port Check Failed:   {len(port_failed_states)}")
        if failed_states:
            print(f"  Create Failed IDs:   {[s.sandbox_id for s in failed_states[:10]]}")
        if port_failed_states:
            print(f"  Port Failed IDs:     {[s.sandbox_id for s in port_failed_states[:10]]}")

        # sandbox.create performance statistics
        create_times = [
            s.creation_metrics.create_elapsed for s in sandbox_states.values()
            if s.creation_metrics.create_elapsed > 0 and s.creation_metrics.status not in (SandboxStatus.FAILED, SandboxStatus.PENDING, SandboxStatus.CREATING)
        ]
        if create_times:
            stats = calc_percentiles(create_times)
            print(f"\n[Sandbox.create Performance]")
            print(f"  (sandbox.create API call time, excluding port wait)")
            print(f"  Min:  {stats['min']:.1f}s")
            print(f"  Max:  {stats['max']:.1f}s")
            print(f"  Avg:  {stats['avg']:.1f}s")
            print(f"  P50:  {stats['p50']:.1f}s")
            print(f"  P95:  {stats['p95']:.1f}s")
            print(f"  P99:  {stats['p99']:.1f}s")

        # Port wait performance statistics
        port_wait_times = [
            s.creation_metrics.port_wait_elapsed for s in ready_states
            if s.creation_metrics.port_wait_elapsed > 0
        ]
        if port_wait_times:
            stats = calc_percentiles(port_wait_times)
            print(f"\n[Port Check Wait Performance]")
            print(f"  (Waiting for 18789 openclaw-gateway + 11436 llama-server ports)")
            print(f"  Min:  {stats['min']:.1f}s")
            print(f"  Max:  {stats['max']:.1f}s")
            print(f"  Avg:  {stats['avg']:.1f}s")
            print(f"  P50:  {stats['p50']:.1f}s")
            print(f"  P95:  {stats['p95']:.1f}s")
            print(f"  P99:  {stats['p99']:.1f}s")

        # Total startup time (create + port_wait)
        total_times = [
            s.creation_metrics.total_elapsed for s in ready_states
            if s.creation_metrics.total_elapsed > 0
        ]
        if total_times:
            stats = calc_percentiles(total_times)
            print(f"\n[Total Startup Performance]")
            print(f"  (sandbox.create + port wait)")
            print(f"  Min:  {stats['min']:.1f}s")
            print(f"  Max:  {stats['max']:.1f}s")
            print(f"  Avg:  {stats['avg']:.1f}s")
            print(f"  P50:  {stats['p50']:.1f}s")
            print(f"  P95:  {stats['p95']:.1f}s")
            print(f"  P99:  {stats['p99']:.1f}s")

        print("\n" + "=" * 70)
        return {
            'report': f"Create-only: {ready_count}/{len(sandbox_states)} sandboxes ready",
            'filepath': None
        }

    # 3. Warmup phase (only if warmup-only mode)
    # Benchmark phase skips warmup - assumes sandboxes already warmed up
    task_manager = TaskManager(config, sandbox_states, stop_event)

    if config.warmup_only and config.warmup_urls:
        print("\n[Phase 2] Running warmup phase...")
        task_manager.start_warmup()
        warmup_start = time.time()

        completed, failed = task_manager.wait_warmup(timeout=300)
        warmup_duration = time.time() - warmup_start

        print(f"\nWarmup completed: {completed} sandboxes | {failed} failed | duration {warmup_duration:.1f}s")
        print("\n[Phase 2 Complete] Warmup-only mode finished.")
        print(f"  Warmup completed: {completed}/{ready_count}")
        print(f"  Sandboxes left running for later benchmark.")
        return {
            'report': f"Warmup-only: {completed}/{ready_count} sandboxes warmed up",
            'filepath': None
        }

    # 4. Benchmark phase (no warmup, just benchmark)
    # Mark all sandboxes as warmup_done so they can start benchmark immediately
    if not config.warmup_only:
        # If warmup_urls is configured but not warmup-only, assume sandboxes need warmup_done
        # For benchmark phase, mark all ready sandboxes as warmup_done (skip warmup)
        for state in sandbox_states.values():
            if state.creation_metrics.status == SandboxStatus.PORT_READY:
                state.warmup_done = True

    # 5. Start statistics collection
    print("\n[Phase 3] Starting stats collector...")
    stats_collector = StatsCollector(config, sandbox_states)
    stats_collector.start()

    # 6. Start task execution (with batch control and benchmark_percent)
    benchmark_count = max(1, int(ready_count * config.benchmark_percent))
    if config.benchmark_percent < 1.0:
        print(f"\n[Phase 4] Starting browser tasks on {benchmark_count}/{ready_count} sandboxes ({config.benchmark_percent*100:.0f}%)...")
    else:
        print(f"\n[Phase 4] Starting browser tasks...")
    task_manager.start_all()

    # 7. Run for specified duration
    print(f"\n[Phase 5] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt, stopping...")

    # 8. Stop all components
    print("\n[Phase 6] Stopping...")
    stop_event.set()
    task_manager.wait_all(timeout=5)
    stats_collector.stop()

    # Only kill if we created the sandboxes (not in detect mode)
    if not config.detect_existing:
        sandbox_manager.kill_all()
    else:
        print("Sandboxes left running (detect mode - not killing)")

    time.sleep(0.5)  # Allow daemon threads to complete output

    # 9. Generate and save report
    report = stats_collector.generate_report()
    print("\n" + report)

    filepath = stats_collector.save_report(report)
    print(f"\nReport saved to: {filepath}")

    return {'report': report, 'filepath': filepath}


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(
        description='E2B Sandbox Bench - E2B Sandbox Batch Performance Testing Tool'
    )

    # Configuration file
    parser.add_argument('-c', '--config', type=str, default=None,
                        help='YAML configuration file path')

    # E2B environment variables
    parser.add_argument('--e2b-access-token', type=str, help='E2B access token')
    parser.add_argument('--e2b-api-key', type=str, help='E2B API key')
    parser.add_argument('--e2b-domain', type=str, help='E2B domain')
    parser.add_argument('--e2b-api-url', type=str, help='E2B API URL')
    parser.add_argument('--e2b-http-ssl', type=str, help='E2B HTTP SSL setting')

    # Sandbox configuration
    parser.add_argument('-t', '--template', type=str, help='E2B template name')
    parser.add_argument('-n', '--total', type=int, help='Total sandbox count')
    parser.add_argument('--create-timeout', type=int, help='Sandbox creation timeout')
    parser.add_argument('-d', '--detect', action='store_true', help='Detect existing sandboxes instead of creating new ones')
    parser.add_argument('--create-only', action='store_true', help='Create sandboxes only without running tasks (Phase 0)')

    # Create batch control
    parser.add_argument('--create-batch-size', type=int, help='Sandboxes per creation batch (None = full concurrent)')
    parser.add_argument('--create-batch-interval', type=int, help='Creation batch interval seconds')

    # Task batch control
    parser.add_argument('--task-batch-size', type=int, help='Sandboxes to start tasks per batch (None = full concurrent)')
    parser.add_argument('--task-batch-interval', type=int, help='Task batch interval seconds')

    # Browser task
    parser.add_argument('--browser-url', type=str, action='append', help='Browser URL (can specify multiple)')
    parser.add_argument('--browser-timeout', type=int, help='Browser task timeout')
    parser.add_argument('--browser-interval-min', type=float, help='Task interval minimum')
    parser.add_argument('--browser-interval-max', type=float, help='Task interval maximum')

    # Warmup configuration
    parser.add_argument('-w', '--warmup-url', type=str, action='append', help='Warmup page URL (can specify multiple)')
    parser.add_argument('--warmup-loops', type=int, default=2, help='Warmup loop count')
    parser.add_argument('--warmup-delay', type=int, default=10, help='Warmup page delay (seconds)')
    parser.add_argument('-wp', '--warmup-only', action='store_true', help='Run warmup phase only, then exit')

    # Benchmark control
    parser.add_argument('-bp', '--benchmark-percent', type=float, default=None, help='Percentage of sandboxes for benchmark (e.g., 0.5 = 50%%)')

    # Test run
    parser.add_argument('--duration', type=int, help='Test duration seconds')
    parser.add_argument('--stats-interval', type=int, help='Stats snapshot interval')

    # Report
    parser.add_argument('-o', '--output-dir', type=str, help='Report output directory')
    parser.add_argument('--filename-prefix', type=str, help='Report filename prefix')

    return parser


def main() -> None:
    """CLI entry point"""
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
        print("Error: E2B access token is required. Use --e2b-access-token or --config")
        return

    # Run test
    run_benchmark(config)


if __name__ == "__main__":
    main()