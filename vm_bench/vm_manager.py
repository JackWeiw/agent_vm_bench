"""
VM Manager Module

Responsible for OpenStack VM creation, SSH connection, health check and lifecycle
Integrates functionality from both create_server.py and vm_bench_lite.py
"""

import os
import re
import subprocess
import time
import threading
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Tuple, Optional, List
from threading import Event, Lock

from .config import Config
from .schemas import VMState, VMStatus


class VMConnection:
    """VM SSH Connection (with Health Detection)"""

    def __init__(self, host: str, port: int, username: str, password: str, vm_id: int):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.vm_id = vm_id
        self.ssh: Optional[paramiko.SSHClient] = None
        self.connected = False
        self.lock = Lock()

    def connect(self, timeout: int = 30) -> bool:
        """Establish SSH connection"""
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=timeout,
                look_for_keys=False
            )
            self.connected = True
            return True
        except Exception as e:
            print(f"[VM{self.vm_id}] Connection failed: {e}")
            self.connected = False
            return False

    def execute(self, command: str, timeout: int = 300, get_exit_code: bool = False) -> Tuple[bool, str, str, float, Optional[int]]:
        """Execute command, optionally return exit code"""
        start = time.perf_counter()
        with self.lock:
            if not self.connected or not self.ssh:
                duration = time.perf_counter() - start
                return False, "", "Not connected", duration, None

            try:
                stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout, get_pty=True)
                out = stdout.read().decode('utf-8', errors='ignore')
                err = stderr.read().decode('utf-8', errors='ignore')
                code = stdout.channel.recv_exit_status() if get_exit_code else 0
                duration = time.perf_counter() - start
                return code == 0, out, err, duration, code if get_exit_code else None
            except Exception as e:
                duration = time.perf_counter() - start
                self.connected = False
                return False, "", str(e), duration, None

    def is_alive(self) -> bool:
        """Check if connection is alive"""
        if not self.connected or not self.ssh:
            return False
        try:
            transport = self.ssh.get_transport()
            if transport and transport.is_active():
                transport.send_ignore()
                return True
            else:
                self.connected = False
                return False
        except:
            self.connected = False
            return False

    def close(self):
        """Close SSH connection"""
        if self.ssh:
            try:
                self.ssh.close()
            except:
                pass
        self.connected = False


class VMManager:
    """VM lifecycle management: OpenStack creation + SSH connection"""

    def __init__(self, config: Config, stop_event: Event):
        self.config = config
        self.stop_event = stop_event
        self.vm_states: Dict[int, VMState] = {}
        self.vm_connections: Dict[int, VMConnection] = {}
        self.os_env = config.get_os_env()
        self._openstack_available = bool(self.os_env)

    # === Phase 0: OpenStack VM Creation ===

    def create_all(self) -> Dict[int, VMState]:
        """Batch create VMs via OpenStack API

        Strategy based on create_batch config:
        - With create_batch_size: batched creation to avoid resource spike
        - Without config: full concurrent creation for max performance test

        Returns: {vm_id: VMState}
        """
        if self.config.create_batch_size and self.config.create_batch_size > 0:
            return self._create_batched()
        else:
            return self._create_concurrent()

    def _create_batched(self) -> Dict[int, VMState]:
        """Batched VM creation"""
        total = self.config.total_count
        batch_size = self.config.create_batch_size
        batch_count = self.config.create_batch_count
        ips = self.config.get_ip_range()

        print(f"\n{'='*60}")
        print(f"Batched VM Creation (OpenStack)")
        print(f"  Total: {total} VMs")
        print(f"  Batches: {batch_count} x {batch_size}")
        print(f"  Interval: {self.config.create_batch_interval}s")
        print(f"  IP range: {ips[0]} ~ {ips[-1]}")
        print(f"{'='*60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                print("Stop event detected, aborting creation")
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)

            print(f"\n[Batch {batch_id}/{batch_count-1}] Creating VMs {start_idx+1}-{end_idx}")

            # Concurrent creation of current batch
            batch_states = self._create_batch_concurrent(batch_id, start_idx, end_idx, ips)
            self.vm_states.update(batch_states)

            # Wait between batches (last batch no wait)
            if batch_id < batch_count - 1 and self.config.create_batch_interval:
                print(f"Waiting {self.config.create_batch_interval}s before next batch...")
                time.sleep(self.config.create_batch_interval)

        return self.vm_states

    def _create_concurrent(self) -> Dict[int, VMState]:
        """Full concurrent creation of all VMs"""
        total = self.config.total_count
        ips = self.config.get_ip_range()

        print(f"\n{'='*60}")
        print(f"Concurrent VM Creation (OpenStack)")
        print(f"  Total: {total} VMs (full concurrent)")
        print(f"  IP range: {ips[0]} ~ {ips[-1]}")
        print(f"{'='*60}")

        return self._create_batch_concurrent(batch_id=0, start=0, end=total, ips=ips)

    def _create_batch_concurrent(self, batch_id: int, start: int, end: int, ips: List[str]) -> Dict[int, VMState]:
        """Concurrent creation of one batch"""
        states: Dict[int, VMState] = {}

        with ThreadPoolExecutor(max_workers=end - start) as executor:
            futures = {}

            for i in range(start, end):
                vm_id = i + 1
                vm_name = f"{self.config.vm_prefix}_{vm_id}"
                fixed_ip = ips[i]

                state = VMState(
                    vm_id=vm_id,
                    vm_name=vm_name,
                    fixed_ip=fixed_ip,
                    batch_id=batch_id
                )
                self.vm_states[vm_id] = state

                future = executor.submit(self._create_single, state)
                futures[future] = vm_id

            for future in as_completed(futures):
                vm_id = futures[future]
                state = self.vm_states[vm_id]

                try:
                    result = future.result()
                    if result['success']:
                        state.creation_metrics.status = VMStatus.ACTIVE
                        state.vm_uuid = result['vm_uuid']
                        print(f"[VM{vm_id}] ({state.fixed_ip}) ACTIVE {result['elapsed']:.1f}s")
                    else:
                        state.creation_metrics.status = VMStatus.CREATE_FAILED
                        state.creation_metrics.error_msg = result['error']
                        detail = f" | {result['error'][:50]}" if result['error'] else ""
                        print(f"[VM{vm_id}] ({state.fixed_ip}) FAILED {result['elapsed']:.1f}s{detail}")
                except Exception as e:
                    state.creation_metrics.status = VMStatus.CREATE_FAILED
                    state.creation_metrics.error_msg = str(e)
                    print(f"[VM{vm_id}] ({state.fixed_ip}) EXCEPTION: {str(e)[:80]}")

        return {i + 1: self.vm_states[i + 1] for i in range(start, end)}

    def _create_single(self, state: VMState) -> Dict[str, any]:
        """Create single VM via OpenStack CLI

        Returns: {'success': bool, 'elapsed': float, 'vm_uuid': str, 'error': str}
        """
        state.creation_metrics.status = VMStatus.CREATING
        state.creation_metrics.submit_time = time.time()

        cmd = [
            "openstack", "server", "create",
            "--flavor", self.config.flavor,
            "--image", self.config.image,
            "--nic", f"net-id={self.config.network_id},v4-fixed-ip={state.fixed_ip}",
            "--availability-zone", self.config.availability_zone,
            "--wait",
            "-f", "value",
            "-c", "id",
            state.vm_name,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.os_env,
        )

        try:
            stdout, stderr = proc.communicate(timeout=self.config.create_timeout + 30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            state.creation_metrics.status = VMStatus.TIMEOUT
            state.creation_metrics.active_time = time.time()
            elapsed = state.creation_metrics.active_time - state.creation_metrics.submit_time
            return {'success': False, 'elapsed': elapsed, 'vm_uuid': '', 'error': 'Timeout'}

        state.creation_metrics.active_time = time.time()
        elapsed = state.creation_metrics.active_time - state.creation_metrics.submit_time
        state.creation_metrics.elapsed = elapsed

        if proc.returncode != 0:
            return {'success': False, 'elapsed': elapsed, 'vm_uuid': '', 'error': stderr.strip()[:200]}

        # Capture VM UUID
        vm_uuid = stdout.strip()
        state.vm_uuid = vm_uuid

        # Verify status via VM UUID
        confirm = subprocess.run(
            ["openstack", "server", "show", vm_uuid, "-f", "value", "-c", "status"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.os_env, timeout=30,
        )
        os_status = confirm.stdout.strip()

        if os_status == "ACTIVE":
            return {'success': True, 'elapsed': elapsed, 'vm_uuid': vm_uuid, 'error': ''}
        elif os_status == "ERROR":
            return {'success': False, 'elapsed': elapsed, 'vm_uuid': vm_uuid, 'error': 'OpenStack ERROR'}
        else:
            return {'success': False, 'elapsed': elapsed, 'vm_uuid': vm_uuid, 'error': f'Status: {os_status}'}

    # === Phase 1: SSH Connection ===

    def connect_all(self) -> Dict[int, VMState]:
        """Batch connect to VMs via SSH

        Called after VMs are ACTIVE (Phase 0 complete)
        Strategy based on connect_batch config
        """
        # Filter ACTIVE VMs from Phase 0
        active_states = {vid: s for vid, s in self.vm_states.items()
                         if s.creation_metrics.status == VMStatus.ACTIVE}

        if not active_states:
            print("No ACTIVE VMs to connect")
            return {}

        if self.config.connect_batch_size and self.config.connect_batch_size > 0:
            return self._connect_batched(active_states)
        else:
            return self._connect_concurrent(active_states)

    def _connect_batched(self, active_states: Dict[int, VMState]) -> Dict[int, VMState]:
        """Batched SSH connection"""
        vm_ids = list(active_states.keys())
        total = len(vm_ids)
        batch_size = self.config.connect_batch_size
        batch_count = (total + batch_size - 1) // batch_size

        print(f"\n{'='*60}")
        print(f"Batched SSH Connection")
        print(f"  Total: {total} VMs")
        print(f"  Batches: {batch_count} x {batch_size}")
        print(f"  Interval: {self.config.connect_batch_interval}s")
        print(f"{'='*60}")

        for batch_id in range(batch_count):
            if self.stop_event.is_set():
                break

            start_idx = batch_id * batch_size
            end_idx = min(start_idx + batch_size, total)
            batch_vm_ids = vm_ids[start_idx:end_idx]

            print(f"\n[ConnectBatch {batch_id}/{batch_count-1}] Connecting VMs {start_idx+1}-{end_idx}")

            for vm_id in batch_vm_ids:
                state = active_states[vm_id]
                self._connect_single(state)

            if batch_id < batch_count - 1 and self.config.connect_batch_interval:
                time.sleep(self.config.connect_batch_interval)

        return active_states

    def _connect_concurrent(self, active_states: Dict[int, VMState]) -> Dict[int, VMState]:
        """Full concurrent SSH connection"""
        print(f"\n{'='*60}")
        print(f"Concurrent SSH Connection")
        print(f"  Total: {len(active_states)} VMs")
        print(f"{'='*60}")

        for vm_id, state in active_states.items():
            self._connect_single(state)

        return active_states

    def _connect_single(self, state: VMState) -> bool:
        """Connect single VM via SSH"""
        state.connection_metrics.status = VMStatus.CONNECTING
        state.connection_metrics.connect_time = time.time()

        vm_conn = VMConnection(
            host=state.fixed_ip,
            port=self.config.ssh_port,
            username=self.config.ssh_username,
            password=self.config.ssh_password,
            vm_id=state.vm_id
        )

        success = vm_conn.connect(timeout=self.config.ssh_connect_timeout)
        state.connection_metrics.ready_time = time.time()
        elapsed = state.connection_metrics.ready_time - state.connection_metrics.connect_time
        state.connection_metrics.connect_elapsed = elapsed
        state.connection_metrics.total_elapsed = elapsed

        if success:
            state.connection_metrics.status = VMStatus.CONNECTED
            state.vm_connection = vm_conn
            self.vm_connections[state.vm_id] = vm_conn
            print(f"[VM{state.vm_id}] SSH connected ({elapsed:.1f}s)")
            return True
        else:
            state.connection_metrics.status = VMStatus.OFFLINE
            state.connection_metrics.error_msg = "SSH connection failed"
            print(f"[VM{state.vm_id}] SSH failed ({elapsed:.1f}s)")
            return False

    # === Lifecycle: Delete ===

    def delete_all(self) -> None:
        """Delete all VMs via OpenStack API"""
        if not self._openstack_available:
            print("[OpenStack] Environment not available, skipping deletion")
            return

        print("\nDeleting all VMs...")
        deleted_count = 0

        for state in self.vm_states.values():
            if state.vm_uuid:
                try:
                    subprocess.run(
                        ["openstack", "server", "delete", state.vm_uuid],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, env=self.os_env, timeout=60
                    )
                    state.creation_metrics.status = VMStatus.DELETED
                    deleted_count += 1
                except Exception as e:
                    print(f"[VM{state.vm_id}] Delete error: {str(e)[:50]}")

        print(f"Deleted {deleted_count} VMs")

    # === Close Connections ===

    def close_all(self) -> None:
        """Close all SSH connections"""
        for vm_conn in self.vm_connections.values():
            vm_conn.close()
        self.vm_connections.clear()

    # === Utility: Check OpenStack Status ===

    def check_vm_status(self, vm_uuid: str) -> Optional[str]:
        """Query VM current status via OpenStack CLI"""
        if not self._openstack_available or not vm_uuid:
            return None
        try:
            result = subprocess.run(
                ["openstack", "server", "show", vm_uuid, "-f", "value", "-c", "status"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=self.os_env, timeout=30
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def check_vm_shutoff(self, state: VMState) -> Tuple[bool, str]:
        """Check if VM is SHUTOFF or ERROR via OpenStack"""
        status = self.check_vm_status(state.vm_uuid)
        if status in ("SHUTOFF", "ERROR"):
            return True, f"OpenStack: {status}"
        return False, ""