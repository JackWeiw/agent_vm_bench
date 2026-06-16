#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Batch Control Module

Batch startup controller for staggered VM execution to avoid resource surge.
"""

import time
import threading
from typing import List, Dict

from ..config import Config


class BatchController:
    """Batch Startup Controller"""

    def __init__(self, config: Config, vm_ids: List[int]):
        self.config = config
        self.vm_ids = sorted(vm_ids)

        # Batch status
        self.batch_ready: Dict[int, bool] = {}
        self.batch_started_count: Dict[int, int] = {}
        self.vm_batch_map: Dict[int, int] = {}

        # Calculate batch allocation
        for i, vm_id in enumerate(self.vm_ids):
            batch_id = i // config.batch_size
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

            print(f"\n{'='*60}")
            print(f"Preparing to start batch {batch_id} / {max_batch}")
            print(f"   VM: {vm_list} (consecutive IP segment)")
            print(f"{'='*60}")

            self.batch_ready[batch_id] = True

            if batch_id < max_batch:
                print(f"\nWaiting {self.config.batch_interval} seconds before starting next batch...")
                time.sleep(self.config.batch_interval)

        print(f"\nAll {max_batch + 1} batches are ready")

    def is_batch_ready(self, batch_id: int) -> bool:
        return self.batch_ready.get(batch_id, False)

    def notify_stress_started(self, vm_id: int):
        batch_id = self.vm_batch_map.get(vm_id)
        if batch_id is not None:
            self.batch_started_count[batch_id] += 1
            expected = sum(1 for vid, b in self.vm_batch_map.items() if b == batch_id)
            if self.batch_started_count[batch_id] >= expected:
                print(f"   Batch {batch_id} startup complete")
