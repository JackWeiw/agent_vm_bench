"""
Health Checker Module

VM Health monitoring via SSH and OpenStack status detection
"""

import json
import subprocess
import threading
import time
from typing import Dict, Optional, Set

from .config import Config
from .schemas import VMState
from .vm_manager import VMConnection


class OpenStackVMChecker:
    """Query VM status via OpenStack CLI"""

    def __init__(self, vm_ips: Dict[int, str], config: Config):
        self.ip_name_map: Dict[str, str] = {}
        self.config = config
        self.os_env = config.get_os_env()
        self._available = bool(self.os_env)
        if self._available:
            self._build_ip_name_map(vm_ips)

    def _build_ip_name_map(self, vm_ips: Dict[int, str]):
        """Build IP -> VM name mapping"""
        if not self._available:
            return
        try:
            result = subprocess.run(
                ["openstack", "server", "list", "-f", "json", "-c", "Name", "-c", "Networks"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.os_env,
                timeout=60,
            )
            if result.returncode != 0:
                print(f"[OpenStack] server list failed: {result.stderr.strip()}")
                return
            servers = json.loads(result.stdout)
            for srv in servers:
                name = srv.get("Name", "")
                networks = srv.get("Networks", "")
                if "=" in networks:
                    ips = networks.split("=", 1)[1]
                    for ip in ips.split(","):
                        ip = ip.strip()
                        if ip:
                            self.ip_name_map[ip] = name
            print(f"[OpenStack] IP->Name mapping: {len(self.ip_name_map)} entries")
        except Exception as e:
            print(f"[OpenStack] Failed to build IP mapping: {e}")

    def get_vm_status(self, vm_name: str) -> Optional[str]:
        """Query VM current status"""
        if not self._available or not vm_name:
            return None
        try:
            result = subprocess.run(
                ["openstack", "server", "show", vm_name, "-f", "value", "-c", "status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.os_env,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def check_vm_offline(self, ip: str) -> tuple:
        """Check if VM is SHUTOFF or ERROR"""
        if not self._available:
            return False, ""
        vm_name = self.ip_name_map.get(ip)
        if not vm_name:
            return False, ""
        status = self.get_vm_status(vm_name)
        if status in ("SHUTOFF", "ERROR"):
            return True, f"OpenStack: {status}"
        return False, ""


class HealthChecker:
    """VM Health Checker - background thread"""

    def __init__(
        self,
        config: Config,
        vm_states: Dict[int, VMState],
        vm_conns: Dict[int, VMConnection],
        os_checker: Optional[OpenStackVMChecker] = None,
    ):
        self.config = config
        self.vm_states = vm_states
        self.vm_conns = vm_conns
        self.os_checker = os_checker
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.offline_vms: Set[int] = set()

    def start(self):
        """Start health check thread"""
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop health check thread"""
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _check_loop(self):
        """Periodic health check"""
        while not self.stop_event.is_set():
            for vm_id, state in self.vm_states.items():
                if vm_id in self.offline_vms:
                    continue

                conn = self.vm_conns.get(vm_id)
                if not conn:
                    continue

                # Check SSH connection
                if not conn.is_alive():
                    state.health.mark_failure("Connection lost")

                    # Check OpenStack status
                    if state.health.consecutive_failures >= 1 and self.os_checker:
                        shutoff, reason = self.os_checker.check_vm_offline(conn.host)
                        if shutoff:
                            self.offline_vms.add(vm_id)
                            state.health.is_connected = False
                            print(f"[VM{vm_id}] OpenStack detected VM offline ({reason})")
                            continue

                    if state.health.check_offline():
                        self.offline_vms.add(vm_id)
                        state.health.is_connected = False
                        print(f"[VM{vm_id}] Marked offline (failures: {state.health.consecutive_failures})")
                else:
                    state.health.mark_success()
                    state.health.last_seen = time.time()

            time.sleep(self.config.health_check_interval)
