#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Health Checking Module

VM health monitoring with connection alive verification.
"""

import time
import threading
from typing import Dict, Optional, Set

from ..config import Config
from ..models import VMState
from ..connection import VMConnection
from .openstack import OpenStackVMChecker


class HealthChecker:
    """VM Health Checker"""

    def __init__(self, config: Config, vm_states: Dict[int, VMState], vm_conns: Dict[int, VMConnection],
                 os_checker: Optional[OpenStackVMChecker] = None):
        self.config = config
        self.vm_states = vm_states
        self.vm_conns = vm_conns
        self.os_checker = os_checker
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.offline_vms: Set = set()

    def start(self):
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _check_loop(self):
        while not self.stop_event.is_set():
            for vm_id, state in self.vm_states.items():
                if vm_id in self.offline_vms:
                    continue

                conn = self.vm_conns.get(vm_id)
                if not conn:
                    continue

                # Check if connection is alive
                if not conn.is_alive():
                    state.health.mark_failure("Connection lost")

                    # Check OpenStack status after 1 consecutive failure, detect if shut down due to memory overcommit
                    if state.health.consecutive_failures >= 1 and self.os_checker:
                        shutoff, reason = self.os_checker.check_vm_offline(conn.host)
                        if shutoff:
                            self.offline_vms.add(vm_id)
                            state.health.is_connected = False
                            print(f"[VM{vm_id}] OpenStack detected VM is shut down ({reason})")
                            continue

                    if state.health.check_offline():
                        self.offline_vms.add(vm_id)
                        state.health.is_connected = False
                        print(f"[VM{vm_id}] Marked as offline (consecutive failures: {state.health.consecutive_failures})")
                else:
                    state.health.mark_success()
                    state.health.last_seen = time.time()

            time.sleep(self.config.health_check_interval)
