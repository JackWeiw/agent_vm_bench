#!/usr/bin/env python3
"""
VM Bench - Main Entry Point

Integrates all components, runs test workflow:
Phase 0: Create VMs (OpenStack) -> Phase 1: Connect (SSH) -> Benchmark -> Report
"""

import argparse
import threading
import time
from typing import Dict

from .config import Config
from .health_checker import HealthChecker, OpenStackVMChecker
from .schemas import VMState, VMStatus
from .stats_collector import StatsCollector
from .task_runner import BrowserTaskManager, QATaskManager, StressTaskManager, VMTaskRunner
from .utils import calc_percentiles
from .vm_manager import VMManager


class BatchController:
    """Batch startup controller"""

    def __init__(self, config: Config, vm_ids: list):
        self.config = config
        self.vm_ids = sorted(vm_ids)

        self.batch_ready: Dict[int, bool] = {}
        self.batch_started_count: Dict[int, int] = {}
        self.vm_batch_map: Dict[int, int] = {}

        batch_size = config.task_batch_size or config.total_count
        for i, vm_id in enumerate(self.vm_ids):
            batch_id = i // batch_size
            self.vm_batch_map[vm_id] = batch_id
            if batch_id not in self.batch_ready:
                self.batch_ready[batch_id] = False
                self.batch_started_count[batch_id] = 0

    def start(self):
        thread = threading.Thread(target=self._control_loop, daemon=True)
        thread.start()

    def _control_loop(self):
        max_batch = max(self.batch_ready.keys()) if self.batch_ready else 0

        for batch_id in range(max_batch + 1):
            vm_list = [vm_id for vm_id, bid in self.vm_batch_map.items() if bid == batch_id]

            print(f"\n{'=' * 60}")
            print(f"Batch {batch_id}/{max_batch} ready: VMs {vm_list}")
            print(f"{'=' * 60}")

            self.batch_ready[batch_id] = True

            if batch_id < max_batch and self.config.task_batch_interval:
                print(f"Waiting {self.config.task_batch_interval}s before next batch...")
                time.sleep(self.config.task_batch_interval)

        print(f"\nAll {max_batch + 1} batches ready")

    def is_batch_ready(self, batch_id: int) -> bool:
        return self.batch_ready.get(batch_id, False)

    def notify_stress_started(self, vm_id: int):
        batch_id = self.vm_batch_map.get(vm_id)
        if batch_id is not None:
            self.batch_started_count[batch_id] += 1


def run_benchmark(config: Config) -> dict:
    """Run VM performance test

    Args:
        config: Test configuration object

    Returns:
        {'report': str, 'filepath': str}
    """

    print("=" * 80)
    print("VM Bench - OpenStack VM Performance Test")
    print("=" * 80)

    # Display configuration
    print(f"  Total VMs:    {config.total_count}")
    print(f"  Start IP:     {config.start_ip}")
    print(f"  Flavor:       {config.flavor}")
    print(f"  Image:        {config.image}")

    if config.create_only:
        print("  Mode:         Create-only (Phase 0)")
    elif config.detect_existing:
        print("  Mode:         Detect existing VMs")
    else:
        print("  Mode:         Full workflow (Create + Benchmark)")

    if config.create_batch_size:
        print(f"  Create Batch: {config.create_batch_count} x {config.create_batch_size}")
    else:
        print("  Create Batch: Full concurrent")

    if not config.create_only:
        print(f"  Task Mode:    {config.task_mode}")
        print(f"  Duration:     {config.test_duration}s")

        if config.warmup_urls:
            print(f"  Warmup:       {len(config.warmup_urls)} pages x {config.warmup_loops} loops")

    print("=" * 80)

    stop_event = threading.Event()

    # === Phase 0: VM Creation / Detection ===
    vm_manager = VMManager(config, stop_event)

    if config.detect_existing:
        print("\n[Phase 0] Detecting existing VMs...")
        # Detect mode: connect to existing VMs via SSH based on IP range
        # No OpenStack creation needed
        ips = config.get_ip_range()
        print(f"  Target IPs: {ips[0]} ~ {ips[-1]} ({len(ips)} IPs)")

        # Initialize VM states for detection
        vm_states = {}
        for i, ip in enumerate(ips):
            vm_id = i + 1
            state = VMState(
                vm_id=vm_id,
                fixed_ip=ip,
                vm_name=f"{config.vm_prefix}_{vm_id}",
            )
            # Mark as "ACTIVE" to skip creation phase
            state.creation_metrics.status = VMStatus.ACTIVE
            vm_states[vm_id] = state
            vm_manager.vm_states[vm_id] = state

        print(f"  Initialized {len(vm_states)} VM states for detection")
        ready_count = len(vm_states)  # All IPs are "ready" for detection

    elif not config.create_only:
        print("\n[Phase 0] Creating VMs via OpenStack...")
        creation_start = time.time()
        vm_states = vm_manager.create_all()
        creation_end = time.time()

        ready_count = sum(1 for s in vm_states.values() if s.creation_metrics.status == VMStatus.ACTIVE)

        print(f"\nVMs created: {ready_count}/{config.total_count}")
        print(f"Creation elapsed: {creation_end - creation_start:.1f}s")

        if ready_count == 0:
            print("No VMs created successfully, exiting.")
            return {}

    else:
        # Create-only mode
        print("\n[Phase 0] Creating VMs (create-only mode)...")
        creation_start = time.time()
        vm_states = vm_manager.create_all()
        creation_end = time.time()

        ready_count = sum(1 for s in vm_states.values() if s.creation_metrics.status == VMStatus.ACTIVE)

        print("\n[Phase 0 Complete] Create-only mode finished.")
        print(f"  Created: {ready_count}/{config.total_count}")
        print(f"  Elapsed: {creation_end - creation_start:.1f}s")

        # Print creation timing report
        create_times = [s.creation_metrics.elapsed for s in vm_states.values() if s.creation_metrics.elapsed > 0]
        if create_times:
            stats = calc_percentiles(create_times)
            print("\n[Creation Performance]")
            print(f"  Min:  {stats['min']:.1f}s")
            print(f"  Max:  {stats['max']:.1f}s")
            print(f"  Avg:  {stats['avg']:.1f}s")
            print(f"  P50:  {stats['p50']:.1f}s")
            print(f"  P95:  {stats['p95']:.1f}s")
            print(f"  P99:  {stats['p99']:.1f}s")

        print("\nVMs left running for later use.")
        return {"report": f"Create-only: {ready_count}/{config.total_count}", "filepath": None}

    # === Phase 1: SSH Connection ===
    print("\n[Phase 1] Connecting to VMs via SSH...")
    vm_states = vm_manager.connect_all()

    connected_count = sum(1 for s in vm_states.values() if s.connection_metrics.status == VMStatus.CONNECTED)

    print(f"\nSSH connected: {connected_count}/{ready_count}")

    if connected_count == 0:
        print("No SSH connections established, exiting.")
        vm_manager.close_all()
        if config.delete_after_test:
            vm_manager.delete_all()
        return {}

    # Prepare VM connections dict
    vm_connections = vm_manager.vm_connections

    # Mark stress VMs if in mixed mode
    stress_vm_count = config.stress_vm_count
    stress_vm_set = set(range(1, stress_vm_count + 1))

    for vm_id, state in vm_states.items():
        state.is_stress_vm = vm_id in stress_vm_set

    # === Initialize Task Managers ===
    qa_manager = QATaskManager(config) if config.task_mode in ("qa", "mixed") else None
    stress_manager = StressTaskManager(config) if config.task_mode in ("stress", "mixed") else None
    browser_manager = BrowserTaskManager(config) if config.task_mode == "browser" else None

    # === Health Checker ===
    vm_ips = {vm_id: state.fixed_ip for vm_id, state in vm_states.items()}
    os_checker = OpenStackVMChecker(vm_ips, config)
    health_checker = HealthChecker(config, vm_states, vm_connections, os_checker)
    health_checker.start()

    # === Batch Controller ===
    benchmark_vm_ids = list(range(1, connected_count + 1))
    batch_controller = BatchController(config, benchmark_vm_ids)
    batch_controller.start()

    # === Warmup Phase (Browser mode) ===
    if config.task_mode == "browser" and config.warmup_only:
        print("\n[Phase 2] Warmup phase...")

        if not config.warmup_urls:
            print("  No warmup URLs configured, marking all VMs as warmup_done")
            for state in vm_states.values():
                state.warmup_done = True
            warmup_count = connected_count
        else:
            print(f"  Warmup URLs: {len(config.warmup_urls)} pages, {config.warmup_loops} loops")
            for vm_id, vm_conn in vm_connections.items():
                if vm_states[vm_id].connection_metrics.status == VMStatus.CONNECTED:
                    browser_manager.warmup_phase(vm_conn, vm_states[vm_id])

            warmup_count = sum(1 for s in vm_states.values() if s.warmup_done)

        print(f"\nWarmup completed: {warmup_count}/{connected_count}")
        print("[Phase 2 Complete] Warmup-only mode finished.")

        vm_manager.close_all()
        health_checker.stop()
        return {"report": f"Warmup: {warmup_count}/{connected_count}", "filepath": None}

    # === Stats Collector ===
    print("\n[Phase 2] Starting stats collector...")
    stats_collector = StatsCollector(config, vm_states)
    stats_collector.start()

    # === Task Execution ===
    # Select benchmark subset
    benchmark_count = max(1, int(connected_count * config.benchmark_percent))
    if config.benchmark_percent < 1.0:
        print(f"\n[Phase 3] Starting tasks on {benchmark_count}/{connected_count} VMs...")
    else:
        print(f"\n[Phase 3] Starting tasks on all {connected_count} VMs...")

    runners = []
    for vm_id, vm_conn in vm_connections.items():
        if vm_id > benchmark_count:
            continue  # Skip VMs not in benchmark subset

        state = vm_states[vm_id]
        if state.connection_metrics.status != VMStatus.CONNECTED:
            continue

        runner = VMTaskRunner(
            vm=vm_conn,
            state=state,
            config=config,
            stop_event=stop_event,
            qa_manager=qa_manager,
            stress_manager=stress_manager,
            browser_manager=browser_manager,
            batch_controller=batch_controller,
            health_checker=health_checker,
        )
        runners.append(runner)
        runner.start()

    # === Run for Duration ===
    print(f"\n[Phase 4] Running for {config.test_duration} seconds...")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt, stopping...")

    # === Graceful Shutdown ===
    print("\n[Phase 5] Stopping all components...")
    stop_event.set()
    # batch_controller runs daemon thread, no need to stop explicitly

    for runner in runners:
        runner.join(timeout=2)

    stats_collector.stop()
    health_checker.stop()
    vm_manager.close_all()

    if config.delete_after_test:
        vm_manager.delete_all()
    else:
        print("VMs left running (delete_after_test=False)")

    time.sleep(0.5)

    # === Generate Report ===
    report = stats_collector.generate_report()
    print("\n" + report)

    filepath = stats_collector.save_report(report)
    print(f"\nReport saved to: {filepath}")

    return {"report": report, "filepath": filepath}


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser"""
    parser = argparse.ArgumentParser(description="VM Bench - OpenStack VM Batch Performance Testing Tool")

    # Config file
    parser.add_argument("-c", "--config", type=str, default=None, help="YAML configuration file path")

    # OpenStack params
    parser.add_argument("--auth-source", type=str, help="OpenStack auth source file")
    parser.add_argument("--flavor", type=str, help="VM flavor")
    parser.add_argument("--image", type=str, help="Image name")
    parser.add_argument("--network-id", type=str, help="OpenStack network ID")
    parser.add_argument("--az", type=str, help="Availability zone")
    parser.add_argument("--subnet-prefix", type=str, help="Subnet IP prefix")
    parser.add_argument("--vm-prefix", type=str, help="VM name prefix")

    # VM creation
    parser.add_argument("-n", "--total", type=int, help="Total VM count")
    parser.add_argument("--start-ip", type=str, help="Starting IP address")
    parser.add_argument("--create-timeout", type=int, help="VM creation timeout")
    parser.add_argument("--create-only", action="store_true", help="Create VMs only, no benchmark")
    parser.add_argument("--detect", action="store_true", help="Detect existing VMs")

    # Create batch
    parser.add_argument("--create-batch-size", type=int, help="VMs per creation batch")
    parser.add_argument("--create-batch-interval", type=int, help="Creation batch interval")

    # SSH params
    parser.add_argument("--ssh-port", type=int, help="SSH port")
    parser.add_argument("--ssh-username", type=str, help="SSH username")
    parser.add_argument("--ssh-password", type=str, help="SSH password")
    parser.add_argument("--ssh-connect-timeout", type=int, help="SSH connect timeout")

    # Connect batch
    parser.add_argument("--connect-batch-size", type=int, help="Connections per batch")
    parser.add_argument("--connect-batch-interval", type=int, help="Connect batch interval")

    # Task batch
    parser.add_argument("--task-batch-size", type=int, help="Tasks per batch")
    parser.add_argument("--task-batch-interval", type=int, help="Task batch interval")

    # Task mode
    parser.add_argument(
        "--task-mode", type=str, choices=["qa", "stress", "browser", "mixed"], help="Task execution mode"
    )
    parser.add_argument("--duration", type=int, help="Benchmark duration")

    # Browser params
    parser.add_argument("--browser-url", type=str, action="append", help="Browser URL")
    parser.add_argument("--browser-timeout", type=int, help="Browser task timeout")
    parser.add_argument("--browser-interval-min", type=float, help="Browser interval min")
    parser.add_argument("--browser-interval-max", type=float, help="Browser interval max")
    parser.add_argument("--browser-use-llm", action="store_true", help="Use LLM for browser")
    parser.add_argument("--benchmark-percent", type=float, help="Percentage of VMs for benchmark")

    # Warmup
    parser.add_argument("-w", "--warmup-url", type=str, action="append", help="Warmup URL")
    parser.add_argument("--warmup-loops", type=int, help="Warmup loop count")
    parser.add_argument("--warmup-delay", type=int, help="Warmup page delay")
    parser.add_argument("--warmup-only", action="store_true", help="Run warmup only")

    # QA params
    parser.add_argument("--qa-timeout", type=int, help="QA timeout")
    parser.add_argument("--qa-init-timeout", type=int, help="QA init timeout")
    parser.add_argument("--qa-interval", type=float, help="QA interval")
    parser.add_argument("--qa-mode", type=str, choices=["cli", "http"], help="QA mode")

    # Stress params
    parser.add_argument("--stress-percent", type=float, help="Percentage of stress VMs")
    parser.add_argument("--stress-memory", type=int, help="Stress memory MB")
    parser.add_argument("--no-keepalive", action="store_true", help="Disable stress keepalive")

    # Report
    parser.add_argument("--output-dir", type=str, help="Report output directory")
    parser.add_argument("--filename-prefix", type=str, help="Report filename prefix")
    parser.add_argument("--stats-interval", type=int, help="Stats snapshot interval")

    # Cleanup
    parser.add_argument("--delete-after-test", action="store_true", help="Delete VMs after test")

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
        config = Config.from_args(args)

    # Validate network_id only for create mode (not for detect mode)
    if not config.detect_existing and not config.network_id:
        print("Error: network_id is required for VM creation. Use --network-id or --config")
        print("Hint: For detect mode, use --detect to connect to existing VMs")
        return

    # Run benchmark
    run_benchmark(config)


if __name__ == "__main__":
    main()
