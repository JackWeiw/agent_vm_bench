#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM Task Runner Module

Thread runner for executing tasks on individual VMs.
"""

import time
import random
import threading
from typing import Optional

from ..config import Config
from ..models import VMState
from ..connection import VMConnection
from ..tasks.qa import QATaskManager
from ..tasks.stress import StressTaskManager
from ..tasks.browser import BrowserTaskManager
from ..monitoring.health import HealthChecker
from ..monitoring.batch import BatchController


class VMTaskRunner(threading.Thread):
    """VM Task Runner"""

    def __init__(self, vm: VMConnection, state: VMState, config: Config,
                 stop_event: threading.Event, batch_controller: BatchController,
                 qa_manager: QATaskManager, stress_manager: StressTaskManager,
                 health_checker: HealthChecker, browser_manager: Optional[BrowserTaskManager] = None):
        super().__init__(daemon=True)
        self.vm = vm
        self.state = state
        self.config = config
        self.stop_event = stop_event
        self.batch_controller = batch_controller
        self.qa_manager = qa_manager
        self.stress_manager = stress_manager
        self.health_checker = health_checker
        self.browser_manager = browser_manager

    def run(self):
        consecutive_errors = 0

        # Wait for batch ready
        if self.config.browser_mode or self.state.is_stress_vm:
            batch_id = self.state.batch_id
            if batch_id >= 0:
                while not self.stop_event.is_set() and not self.batch_controller.is_batch_ready(batch_id):
                    time.sleep(0.5)

        # Warmup phase: execute warmup tasks then exit
        if self.config.browser_mode and self.config.is_warmup_phase and self.browser_manager:
            self.browser_manager.warmup_phase(self.vm, self.state)
            print(f"[VM{self.vm.vm_id}] Warmup phase completed")
            return

        # Benchmark phase: execute browser benchmark tasks
        while not self.stop_event.is_set():
            try:
                if not self.state.health.is_connected:
                    print(f"[VM{self.vm.vm_id}] VM offline, stopping tasks")
                    break

                # Execute Browser/QA tasks
                if self.config.browser_mode:
                    success, duration, task_type = self.browser_manager.run_browser_task(self.vm, self.state)
                    consecutive_errors = 0 if success else consecutive_errors + 1
                    if not success:
                        self.state.record_browser_failure()
                        self.state.health.mark_failure(f"Browser failed")
                else:
                    success, duration = self.qa_manager.run_qa_query(self.vm, self.state)
                    consecutive_errors = 0 if success else consecutive_errors + 1
                    if not success:
                        self.state.health.mark_failure(f"QA failed")

                # Stress task handling
                if self.state.is_stress_vm:
                    self._handle_stress()

                # Error handling
                if consecutive_errors >= 3:
                    if not self.vm.is_alive():
                        self.state.health.is_connected = False
                        self.health_checker.offline_vms.add(self.vm.vm_id)
                        break
                    consecutive_errors = 0

                # Task interval
                if self.config.browser_mode:
                    # Browser mode: random interval per task round, stagger VM task execution to avoid sudden memory pressure
                    sleep_time = random.uniform(self.config.browser_task_interval_min, self.config.browser_task_interval_max)
                else:
                    # QA/Stress mode: stagger VMs within batch by position
                    batch_start_id = self.state.batch_id * self.config.batch_size + 1
                    vm_offset = (self.vm.vm_id - batch_start_id) * self.config.task_interval
                    sleep_time = max(0.5, self.config.qa_interval + vm_offset)
                time.sleep(sleep_time)

            except Exception as e:
                consecutive_errors += 1
                self.state.health.mark_failure(str(e)[:50])
                if "connection" in str(e).lower():
                    # Try reconnect to avoid misjudging offline due to brief network jitter
                    if self.vm.connect(timeout=10):
                        print(f"[VM{self.vm.vm_id}] SSH reconnect successful")
                        self.state.health.mark_success()
                        self.state.health.is_connected = True
                        if self.vm.vm_id in self.health_checker.offline_vms:
                            self.health_checker.offline_vms.discard(self.vm.vm_id)
                        continue
                    # Reconnect failed, check OpenStack status to confirm if shut down
                    if self.health_checker.os_checker:
                        shutoff, reason = self.health_checker.os_checker.check_vm_offline(self.vm.host)
                        if shutoff:
                            self.state.health.is_connected = False
                            self.health_checker.offline_vms.add(self.vm.vm_id)
                            print(f"[VM{self.vm.vm_id}] OpenStack confirmed VM is shut down ({reason})")
                            break
                    # OpenStack not responding or VM status unknown, mark offline
                    self.state.health.is_connected = False
                    self.health_checker.offline_vms.add(self.vm.vm_id)
                    break
                time.sleep(3)

        print(f"[VM{self.vm.vm_id}] runner ended")

    def _handle_stress(self):
        """Handle Stress task"""
        if not self.state.stress_started:
            batch_id = self.state.batch_id
            while not self.stop_event.is_set():
                if self.batch_controller.is_batch_ready(batch_id):
                    break
                time.sleep(0.5)

            print(f"[VM{self.vm.vm_id}] Batch {batch_id} starting stress_tool")
            success, msg = self.stress_manager.start_stress(self.vm, self.state)
            if success:
                self.state.stress_started = True
                self.batch_controller.notify_stress_started(self.vm.vm_id)
            else:
                print(f"[VM{self.vm.vm_id}] stress startup failed: {msg}")

        elif self.config.stress_keepalive and time.time() - self.state.last_stress_check >= 5:
            self.state.last_stress_check = time.time()
            running, msg = self.stress_manager.check_and_restart(self.vm, self.state)
            if not running:
                print(f"[VM{self.vm.vm_id}] {msg}")
