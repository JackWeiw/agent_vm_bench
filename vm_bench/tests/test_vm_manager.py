"""
Unit tests for vm_bench VMManager module

Tests use mocks to simulate SSH and OpenStack CLI behavior
"""

import os
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm_bench.config import Config
from vm_bench.schemas import VMState, VMStatus
from vm_bench.vm_manager import VMConnection, VMManager


class TestVMConnection(unittest.TestCase):
    """Test VMConnection SSH wrapper"""

    def test_init(self):
        conn = VMConnection(host="192.168.110.11", port=22, username="root", password="test", vm_id=1)
        self.assertEqual(conn.host, "192.168.110.11")
        self.assertEqual(conn.port, 22)
        self.assertEqual(conn.vm_id, 1)
        self.assertFalse(conn.connected)

    @patch("vm_bench.vm_manager.paramiko.SSHClient")
    def test_connect_success(self, mock_ssh_client_class):
        mock_ssh = Mock()
        mock_ssh_client_class.return_value = mock_ssh

        conn = VMConnection(host="192.168.110.11", port=22, username="root", password="test", vm_id=1)

        result = conn.connect(timeout=30)
        self.assertTrue(result)
        self.assertTrue(conn.connected)
        mock_ssh.set_missing_host_key_policy.assert_called_once()
        mock_ssh.connect.assert_called_once()

    @patch("vm_bench.vm_manager.paramiko.SSHClient")
    def test_connect_failure(self, mock_ssh_client_class):
        mock_ssh = Mock()
        mock_ssh.connect.side_effect = Exception("Connection refused")
        mock_ssh_client_class.return_value = mock_ssh

        conn = VMConnection(host="192.168.110.11", port=22, username="root", password="test", vm_id=1)

        result = conn.connect(timeout=30)
        self.assertFalse(result)
        self.assertFalse(conn.connected)

    @patch("vm_bench.vm_manager.paramiko.SSHClient")
    def test_execute_success(self, mock_ssh_client_class):
        mock_ssh = Mock()
        mock_stdin = Mock()
        mock_stdout = Mock()
        mock_stderr = Mock()
        mock_stdout.read.return_value = b"output"
        mock_stderr.read.return_value = b""
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_ssh.exec_command.return_value = (mock_stdin, mock_stdout, mock_stderr)
        mock_ssh_client_class.return_value = mock_ssh

        conn = VMConnection(host="192.168.110.11", port=22, username="root", password="test", vm_id=1)
        conn.connected = True
        conn.ssh = mock_ssh

        success, out, err, duration, code = conn.execute("ls", timeout=30)
        self.assertTrue(success)
        self.assertEqual(out, "output")

    @patch("vm_bench.vm_manager.paramiko.SSHClient")
    def test_execute_not_connected(self, mock_ssh_client_class):
        conn = VMConnection(host="192.168.110.11", port=22, username="root", password="test", vm_id=1)
        conn.connected = False

        success, out, err, duration, code = conn.execute("ls", timeout=30)
        self.assertFalse(success)
        self.assertEqual(err, "Not connected")

    @patch("vm_bench.vm_manager.paramiko.SSHClient")
    def test_close(self, mock_ssh_client_class):
        mock_ssh = Mock()
        mock_ssh_client_class.return_value = mock_ssh

        conn = VMConnection(host="192.168.110.11", port=22, username="root", password="test", vm_id=1)
        conn.ssh = mock_ssh
        conn.connected = True

        conn.close()
        mock_ssh.close.assert_called_once()
        self.assertFalse(conn.connected)


class TestVMManager(unittest.TestCase):
    """Test VMManager lifecycle management"""

    def setUp(self):
        self.config = Config(
            total_count=3,
            start_ip="192.168.110.11",
            network_id="test-network-id",
            flavor="test-flavor",
            image="test-image",
            availability_zone="test-az",
        )
        self.stop_event = threading.Event()

    def test_init(self):
        manager = VMManager(self.config, self.stop_event)
        self.assertEqual(manager.config.total_count, 3)
        self.assertEqual(len(manager.vm_states), 0)

    def test_vm_states_tracking(self):
        manager = VMManager(self.config, self.stop_event)
        # Add a VM state manually
        state = VMState(vm_id=1, fixed_ip="192.168.110.11")
        manager.vm_states[1] = state

        self.assertEqual(len(manager.vm_states), 1)
        self.assertEqual(manager.vm_states[1].vm_id, 1)

    def test_get_ip_range(self):
        ips = self.config.get_ip_range()
        self.assertEqual(len(ips), 3)
        self.assertEqual(ips[0], "192.168.110.11")
        self.assertEqual(ips[1], "192.168.110.12")
        self.assertEqual(ips[2], "192.168.110.13")

    @patch("vm_bench.vm_manager.subprocess.run")
    def test_check_vm_status(self, mock_run):
        manager = VMManager(self.config, self.stop_event)
        manager._openstack_available = True
        manager.os_env = {}

        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "ACTIVE"
        mock_run.return_value = mock_result

        status = manager.check_vm_status("test-uuid")
        self.assertEqual(status, "ACTIVE")

    @patch("vm_bench.vm_manager.subprocess.run")
    def test_check_vm_status_not_available(self, mock_run):
        manager = VMManager(self.config, self.stop_event)
        manager._openstack_available = False

        status = manager.check_vm_status("test-uuid")
        self.assertIsNone(status)

    @patch("vm_bench.vm_manager.VMConnection")
    def test_connect_single_success(self, mock_vm_conn_class):
        mock_conn = Mock()
        mock_conn.connect.return_value = True
        mock_conn.connected = True
        mock_vm_conn_class.return_value = mock_conn

        manager = VMManager(self.config, self.stop_event)
        state = VMState(vm_id=1, fixed_ip="192.168.110.11")
        manager.vm_states[1] = state

        result = manager._connect_single(state)
        self.assertTrue(result)
        self.assertEqual(state.connection_metrics.status, VMStatus.CONNECTED)

    @patch("vm_bench.vm_manager.VMConnection")
    def test_connect_single_failure(self, mock_vm_conn_class):
        mock_conn = Mock()
        mock_conn.connect.return_value = False
        mock_conn.connected = False
        mock_vm_conn_class.return_value = mock_conn

        manager = VMManager(self.config, self.stop_event)
        state = VMState(vm_id=1, fixed_ip="192.168.110.11")
        manager.vm_states[1] = state

        result = manager._connect_single(state)
        self.assertFalse(result)
        self.assertEqual(state.connection_metrics.status, VMStatus.OFFLINE)

    def test_close_all(self):
        manager = VMManager(self.config, self.stop_event)

        # Add mock connections
        mock_conn1 = Mock()
        mock_conn2 = Mock()
        manager.vm_connections[1] = mock_conn1
        manager.vm_connections[2] = mock_conn2

        manager.close_all()
        mock_conn1.close.assert_called_once()
        mock_conn2.close.assert_called_once()
        self.assertEqual(len(manager.vm_connections), 0)


class TestVMManagerBatchCalculations(unittest.TestCase):
    """Test batch calculation logic"""

    def test_batch_count_calculation(self):
        config = Config(total_count=100, create_batch_size=20)
        self.assertEqual(config.create_batch_count, 5)

        config = Config(total_count=95, create_batch_size=20)
        self.assertEqual(config.create_batch_count, 5)  # (95+19)/20 = 5

        config = Config(total_count=3, create_batch_size=20)
        self.assertEqual(config.create_batch_count, 1)


class TestVMStateTransitions(unittest.TestCase):
    """Test VM state transition logic"""

    def test_creation_to_connected(self):
        state = VMState(vm_id=1, fixed_ip="192.168.110.11")

        # Initial: PENDING
        self.assertEqual(state.creation_metrics.status, VMStatus.PENDING)

        # After creation success: ACTIVE
        state.creation_metrics.status = VMStatus.ACTIVE
        self.assertEqual(state.creation_metrics.status, VMStatus.ACTIVE)

        # After SSH connect: CONNECTED
        state.connection_metrics.status = VMStatus.CONNECTED
        self.assertEqual(state.connection_metrics.status, VMStatus.CONNECTED)

    def test_creation_failure(self):
        state = VMState(vm_id=1)
        state.creation_metrics.status = VMStatus.CREATE_FAILED
        self.assertEqual(state.creation_metrics.status, VMStatus.CREATE_FAILED)


if __name__ == "__main__":
    unittest.main()
