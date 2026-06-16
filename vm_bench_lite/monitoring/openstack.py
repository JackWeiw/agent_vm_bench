#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenStack Integration Module

Query VM status via OpenStack CLI for detecting shutdowns due to memory overcommit.
"""

import os
import subprocess
import json
import re
from typing import Dict, Optional, Tuple


class OpenStackVMChecker:
    """Query VM status via OpenStack CLI, used to detect SHUTOFF due to memory overcommit"""

    def __init__(self, vm_ips: Dict[int, str]):
        self.ip_name_map: Dict[str, str] = {}  # ip -> name
        self.os_env = self._load_os_env()
        self._available = self.os_env is not None
        self._build_ip_name_map(vm_ips)

    def _load_os_env(self) -> Optional[dict]:
        """Load openrc environment variables"""
        openrc_path = os.path.expanduser("~/.admin-openrc")
        if not os.path.exists(openrc_path):
            print(f"[OpenStack] {openrc_path} not found, skipping OpenStack status detection")
            return None
        try:
            env = os.environ.copy()
            with open(openrc_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        m = re.match(r"export\s+(\w+)=(.*)", line)
                        if m:
                            key, val = m.group(1), m.group(2).strip("\"'")
                            env[key] = val
            env.pop("http_proxy", None)
            env.pop("https_proxy", None)
            env.pop("HTTP_PROXY", None)
            env.pop("HTTPS_PROXY", None)
            print(f"[OpenStack] Loaded openrc environment variables")
            return env
        except Exception as e:
            print(f"[OpenStack] Failed to load openrc: {e}")
            return None

    def _build_ip_name_map(self, vm_ips: Dict[int, str]):
        """Get IP -> name mapping for all VMs at once"""
        if not self._available:
            return
        try:
            result = subprocess.run(
                ["openstack", "server", "list", "-f", "json", "-c", "Name", "-c", "Networks"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.os_env, timeout=60
            )
            if result.returncode != 0:
                print(f"[OpenStack] server list failed: {result.stderr.strip()}")
                return
            servers = json.loads(result.stdout)
            for srv in servers:
                name = srv.get("Name", "")
                networks = srv.get("Networks", "")
                # Parse "netname=ip1,ip2" format
                if "=" in networks:
                    ips = networks.split("=", 1)[1]
                    for ip in ips.split(","):
                        ip = ip.strip()
                        if ip:
                            self.ip_name_map[ip] = name
            print(f"[OpenStack] IP->Name mapping established: {len(self.ip_name_map)} entries")
        except Exception as e:
            print(f"[OpenStack] Failed to build IP mapping: {e}")

    def get_vm_status(self, vm_name: str) -> Optional[str]:
        """Query VM current status (ACTIVE/SHUTOFF/ERROR/...)"""
        if not self._available or not vm_name:
            return None
        try:
            result = subprocess.run(
                ["openstack", "server", "show", vm_name, "-f", "value", "-c", "status"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.os_env, timeout=30
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def check_vm_offline(self, ip: str) -> Tuple[bool, str]:
        """Check if VM has been shut down by hypervisor. Return (is_offline, reason)"""
        if not self._available:
            return False, ""
        vm_name = self.ip_name_map.get(ip)
        if not vm_name:
            return False, ""
        status = self.get_vm_status(vm_name)
        if status in ("SHUTOFF", "ERROR"):
            return True, f"OpenStack status: {status}"
        return False, ""
