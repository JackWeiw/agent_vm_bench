#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coordinator Module

Main benchmark coordination and component orchestration.
"""

import os
import time
import ipaddress
from datetime import datetime
from typing import Dict, List

from .config import Config
from .models import VMState
from .connection import VMConnection
from .tasks.qa import QATaskManager
from .tasks.stress import StressTaskManager
from .tasks.browser import BrowserTaskManager
from .monitoring.health import HealthChecker
from .monitoring.batch import BatchController
from .monitoring.openstack import OpenStackVMChecker
from .monitoring.stats import StatsCollector
from .runner import VMTaskRunner


def run_benchmark(config: Config) -> dict:
    """Run benchmark"""

    print("=" * 80)
    if config.browser_mode and config.is_warmup_phase:
        print("VM Bench Lite - Browser Warmup Phase")
    elif config.browser_mode:
        print("VM Bench Lite - Browser Benchmark Phase")
    else:
        print("VM Bench Lite - QA+Stress Test")
    print("=" * 80)

    def ip_range(start_ip, count):
        start = ipaddress.IPv4Address(start_ip)
        return [str(start + i) for i in range(count)]

    # Calculate actual VM count to connect
    if config.browser_mode and not config.is_warmup_phase:
        # Benchmark phase: only connect browser_stress_percent of VMs
        actual_vm_count = int(config.total_vms * config.browser_stress_percent)
        actual_vm_count = max(1, actual_vm_count)  # At least 1 VM
        print(f"  Browser benchmark phase: connecting {actual_vm_count}/{config.total_vms} VMs ({config.browser_stress_percent*100:.0f}%)")
    else:
        # Warmup phase or QA/Stress mode: connect all VMs
        actual_vm_count = config.total_vms

    vm_ips = ip_range(config.start_ip, actual_vm_count)
    stress_vm_ids = list(range(1, config.stress_vm_count + 1))
    stress_vm_set = set(stress_vm_ids)

    # Establish SSH connections
    vm_connections: Dict[int, VMConnection] = {}
    for vm_id in range(1, actual_vm_count + 1):
        ip = vm_ips[vm_id - 1]
        vm = VMConnection(ip, config.port, config.username, config.password, vm_id)
        if vm.connect():
            vm_connections[vm_id] = vm

    if not vm_connections:
        print("No connectable VMs")
        return {}

    print(f"Successfully connected: {len(vm_connections)}/{actual_vm_count} VMs")

    # Create OpenStack VM status checker (for detecting shutdowns due to memory overcommit)
    os_vm_ips = {vm_id: vm.host for vm_id, vm in vm_connections.items()}
    os_checker = OpenStackVMChecker(os_vm_ips)

    # Create VM states
    vm_states: Dict[int, VMState] = {}
    for vm_id, vm in vm_connections.items():
        # Browser mode: no stress VMs, all VMs only run browser tasks
        # QA/Stress mode: mark VMs based on stress_vm_set
        is_stress = False if config.browser_mode else (vm_id in stress_vm_set)
        batch_id = (vm_id - 1) // config.batch_size if (config.browser_mode or is_stress) else -1
        vm_states[vm_id] = VMState(vm_id=vm_id, host=vm.host, is_stress_vm=is_stress, batch_id=batch_id)

    # Initialize managers
    qa_manager = QATaskManager(config)
    stress_manager = StressTaskManager(config)
    browser_manager = BrowserTaskManager(config) if config.browser_mode else None

    # Start components
    health_checker = HealthChecker(config, vm_states, vm_connections, os_checker)
    health_checker.start()
    batch_vm_ids = list(range(1, actual_vm_count + 1)) if config.browser_mode else stress_vm_ids
    batch_controller = BatchController(config, batch_vm_ids)
    batch_controller.start()

    # Stats collector
    stats_collector = StatsCollector(config, vm_states)
    if config.browser_mode and config.is_warmup_phase:
        # Warmup phase: don't start stats collector, no benchmark statistics needed
        pass
    else:
        stats_collector.start()

    # Start task threads
    stop_event = threading.Event()
    runners: List[VMTaskRunner] = []
    for vm_id, vm in vm_connections.items():
        runner = VMTaskRunner(vm, vm_states[vm_id], config, stop_event, batch_controller, qa_manager, stress_manager, health_checker, browser_manager)
        runners.append(runner)
        runner.start()

    # Warmup phase: wait for all VMs to complete warmup then exit
    if config.browser_mode and config.is_warmup_phase:
        print(f"\nWarmup phase starting...")
        print(f"   Total VMs: {actual_vm_count}")
        print(f"   Warmup pages: {len(config.warmup_urls)}")
        print(f"   Loop count: {config.warmup_loops}")
        print(f"   Page delay: {config.warmup_delay} seconds")
        warmup_start = time.time()
        last_progress_time = warmup_start

        while not stop_event.is_set():
            done_count = sum(1 for s in vm_states.values() if s.warmup_done)
            total_count = len(vm_states)
            fail_count = sum(1 for s in vm_states.values() if s.warmup_done and s.browser_failure_count > 0)

            # Print progress every 5 seconds
            now = time.time()
            if now - last_progress_time >= 5:
                elapsed = now - warmup_start
                print(f"   Warmup progress: {done_count}/{total_count} completed | {fail_count} failed | elapsed {elapsed:.0f}s")
                last_progress_time = now

            if done_count >= total_count:
                warmup_duration = time.time() - warmup_start
                print(f"\nWarmup completed: {done_count} VM | {fail_count} failed | total time {warmup_duration:.1f}s")
                break
            time.sleep(1)

        # Warmup phase complete, exit directly
        print("\nWarmup phase finished, exiting...")
        stop_event.set()
        health_checker.stop()
        for runner in runners:
            runner.join(timeout=2)
        stats_collector.stop()  # Stop stats collector (even though not started in warmup)
        for vm in vm_connections.values():
            vm.close()

        # Small delay to let daemon threads finish their last output
        time.sleep(0.5)

        # Save warmup summary
        warmup_summary = f"Warmup Phase Summary\n{'='*40}\nTotal VMs: {actual_vm_count}\nCompleted: {done_count}\nFailed: {fail_count}\nDuration: {warmup_duration:.1f}s\n"
        os.makedirs("results", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(f"results/warmup_summary_{timestamp}.txt", 'w') as f:
            f.write(warmup_summary)
        print(f"\nWarmup summary saved")

        return {'warmup_summary': warmup_summary}

    # Benchmark phase: run for specified duration
    print(f"\nBenchmark running... ({config.test_duration} seconds)")
    try:
        time.sleep(config.test_duration)
    except KeyboardInterrupt:
        print("\nUser interrupt")

    # Graceful stop
    print("\nStopping all components...")
    stop_event.set()
    health_checker.stop()
    for runner in runners:
        runner.join(timeout=2)
    stats_collector.stop()
    for vm in vm_connections.values():
        vm.close()

    # Small delay to let daemon threads finish their last output
    time.sleep(0.5)

    # Generate report
    report = stats_collector.generate_report()
    print("\n" + report)

    # Save report
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"results/bench_report_{timestamp}.txt", 'w') as f:
        f.write(report)
    print(f"\nReport saved")

    return {'report': report}
