#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH Connection Module

VM SSH connection management with paramiko.
"""

import time
import threading
import paramiko
from typing import Tuple, Optional


class VMConnection:
    """VM SSH Connection (with Health Detection)"""

    def __init__(self, host: str, port: int, username: str, password: str, vm_id: int):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.vm_id = vm_id
        self.ssh = None
        self.connected = False
        self.lock = threading.Lock()

    def connect(self, timeout: int = 30) -> bool:
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
        if self.ssh:
            try:
                self.ssh.close()
            except:
                pass
        self.connected = False