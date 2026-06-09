#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-VM Agent Automation Test Script

Executes a complete single test flow:
1. Delete existing VMs → confirm deletion
2. Create new VMs (n count)
3. Start smap_tool
4. Wait for VMs ready (SSH, openclaw gateway, CPU < 5%)
5. Warmup phase
6. Start monitoring (qemu_monitor.py)
7. Benchmark phase
8. Collect results
9. Cleanup (kill smap_tool, delete VMs)

Usage:
    python auto_vm_test.py --config test_config.yaml
"""

import os
import sys
import time
import signal
import subprocess
import argparse
import yaml
import re
import psutil
import paramiko
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class TestContext:
    """Test execution context"""
    config: Dict
    result_dir: str
    log_file: str
    smap_pid: Optional[int] = None
    smap_stdout: Optional[object] = None
    smap_stderr: Optional[object] = None
    monitor_pid: Optional[int] = None
    monitor_stdout: Optional[object] = None
    monitor_stderr: Optional[object] = None
    vm_ips: List[str] = field(default_factory=list)
    qemu_pids: Dict[str, int] = field(default_factory=dict)
    start_time: float = 0.0


def log(ctx: TestContext, msg: str):
    """Write log message to file and stdout"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(ctx.log_file, "a") as f:
        f.write(line + "\n")


def load_config(config_path: str) -> Dict:
    """Load and parse YAML config file"""
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Normalize types (YAML may parse numbers as strings from template replacement)
    config["vm"]["count"] = int(config["vm"]["count"])
    config["smap_tool"]["swap_size_gb"] = int(config["smap_tool"]["swap_size_gb"])
    config["smap_tool"]["ratio"] = float(config["smap_tool"]["ratio"])
    config["test"]["active_percent"] = float(config["test"]["active_percent"])
    config["test"]["duration"] = int(config["test"]["duration"])
    config["test"]["batch_size"] = int(config["test"]["batch_size"])
    config["test"]["batch_interval"] = int(config["test"]["batch_interval"])
    config["test"]["browser_interval_min"] = int(config["test"]["browser_interval_min"])
    config["test"]["browser_interval_max"] = int(config["test"]["browser_interval_max"])
    config["warmup"]["loops"] = int(config["warmup"]["loops"])
    config["warmup"]["delay"] = int(config["warmup"]["delay"])
    config["warmup"]["batch_size"] = int(config["warmup"]["batch_size"])
    config["warmup"]["batch_interval"] = int(config["warmup"]["batch_interval"])
    config["monitor"]["interval"] = int(config["monitor"]["interval"])
    config["wait"]["ssh_timeout"] = int(config["wait"]["ssh_timeout"])
    config["wait"]["service_timeout"] = int(config["wait"]["service_timeout"])
    config["wait"]["cpu_threshold"] = int(config["wait"]["cpu_threshold"])
    config["wait"]["check_interval"] = int(config["wait"]["check_interval"])

    return config


def save_config_copy(ctx: TestContext, config_path: str):
    """Save config file copy to result directory"""
    import shutil
    dest = os.path.join(ctx.result_dir, "config.yaml")
    shutil.copy(config_path, dest)
    log(ctx, f"Config saved to {dest}")


def create_result_dir(config: Dict) -> str:
    """Create result directory with naming: vm{n}_ratio{ratio}_active{percent}_timestamp"""
    base_dir = config["result"]["base_dir"]
    vm_count = config["vm"]["count"]
    ratio = config["smap_tool"]["ratio"]
    active_percent = config["test"]["active_percent"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    dir_name = f"vm{vm_count}_ratio{ratio}_active{active_percent}_{timestamp}"
    result_dir = os.path.join(base_dir, dir_name)
    os.makedirs(result_dir, exist_ok=True)

    # Create subdirectories
    os.makedirs(os.path.join(result_dir, "vm_bench_lite"), exist_ok=True)
    os.makedirs(os.path.join(result_dir, "qemu_monitor"), exist_ok=True)
    os.makedirs(os.path.join(result_dir, "summary"), exist_ok=True)

    return result_dir


def setup_openstack_env(config: Dict) -> Dict:
    """Setup OpenStack environment variables from openrc file"""
    env = os.environ.copy()
    openrc_path = os.path.expanduser(config["openstack"]["openrc_path"])

    if not os.path.exists(openrc_path):
        raise FileNotFoundError(f"OpenRC file not found: {openrc_path}")

    with open(openrc_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                m = re.match(r"export\s+(\w+)=(.*)", line)
                if m:
                    key, val = m.group(1), m.group(2).strip("\"'")
                    env[key] = val

    # Remove proxy settings
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)

    return env


# ==================== Step 2: Delete VMs ====================

def delete_vms(ctx: TestContext, os_env: Dict) -> bool:
    """Delete all existing VMs and confirm deletion"""
    log(ctx, "Step 2: Deleting existing VMs...")

    # Get VM list
    result = subprocess.run(
        ["openstack", "server", "list", "-c", "ID", "-f", "value"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os_env, timeout=60
    )

    if result.returncode != 0:
        log(ctx, f"Failed to list VMs: {result.stderr}")
        return False

    vm_ids = result.stdout.strip().split("\n")
    vm_ids = [id for id in vm_ids if id.strip()]

    if not vm_ids:
        log(ctx, "No VMs to delete")
        return True

    log(ctx, f"Found {len(vm_ids)} VMs to delete")

    # Delete VMs
    result = subprocess.run(
        ["openstack", "server", "delete", "--force"] + vm_ids,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os_env, timeout=300
    )

    if result.returncode != 0:
        log(ctx, f"Delete command failed: {result.stderr}")

    # Wait and confirm deletion
    max_wait = 120
    start = time.time()

    while time.time() - start < max_wait:
        # Check via virsh
        result = subprocess.run(
            ["virsh", "list", "--all"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30
        )

        lines = result.stdout.strip().split("\n")
        running_vms = [l for l in lines if "running" in l]

        if not running_vms:
            log(ctx, "All VMs deleted (virsh confirm)")
            break

        log(ctx, f"Waiting for VM deletion... ({len(running_vms)} still running)")
        time.sleep(10)

    # Final check via openstack
    result = subprocess.run(
        ["openstack", "server", "list", "-c", "ID", "-f", "value"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os_env, timeout=60
    )

    remaining = [id for id in result.stdout.strip().split("\n") if id.strip()]

    if remaining:
        log(ctx, f"WARNING: {len(remaining)} VMs still exist, attempting force destroy...")
        for vm_id in remaining:
            subprocess.run(["virsh", "destroy", vm_id], timeout=30)
        time.sleep(5)

    log(ctx, "VM deletion completed")
    return True


# ==================== Step 3: Create VMs ====================

def create_vms(ctx: TestContext, os_env: Dict) -> bool:
    """Create VMs using create_server.py"""
    log(ctx, "Step 3: Creating VMs...")

    config = ctx.config
    vm_count = config["vm"]["count"]
    start_ip = config["vm"]["start_ip"]
    subnet_prefix = config["openstack"]["subnet_prefix"]
    network_id = config["openstack"]["network_id"]
    az = config["openstack"]["az"]
    flavor = config["openstack"]["flavor"]
    image = config["openstack"]["image"]

    # Calculate start IP number
    if not start_ip.startswith(subnet_prefix):
        raise ValueError(f"start_ip {start_ip} doesn't match subnet_prefix {subnet_prefix}")

    start_ip_num = int(start_ip.split(".")[-1])

    # Build command
    cmd = [
        "python3", "create_server.py",
        "--start_ip", start_ip,
        "--n", str(vm_count),
        "--subnet-prefix", subnet_prefix,
        "--network-id", network_id,
        "--az", az,
        "--flavor", flavor,
        "--image", image
    ]

    log(ctx, f"Executing: {' '.join(cmd)}")

    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800
    )

    # Output result
    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        log(ctx, f"VM creation failed with return code {result.returncode}")
        return False

    # Parse created VM IPs
    ctx.vm_ips = []
    for i in range(vm_count):
        ip = f"{subnet_prefix.rstrip('.')}.{start_ip_num + i}"
        ctx.vm_ips.append(ip)

    log(ctx, f"VM creation completed: {len(ctx.vm_ips)} VMs")
    return True


# ==================== Step 4: Start smap_tool ====================

def get_qemu_pids(ctx: TestContext) -> Dict[str, int]:
    """Get QEMU process PIDs and map to VM IPs"""
    result = subprocess.run(
        ["pidof", "qemu-kvm"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
    )

    if result.returncode != 0 or not result.stdout.strip():
        log(ctx, "No qemu-kvm processes found")
        return {}

    pids = result.stdout.strip().split()
    log(ctx, f"Found {len(pids)} qemu-kvm processes")

    # Map PIDs to IPs (simplified: assume sequential order matches IP order)
    qemu_pids = {}
    for i, pid in enumerate(pids[:len(ctx.vm_ips)]):
        if i < len(ctx.vm_ips):
            qemu_pids[ctx.vm_ips[i]] = int(pid)

    ctx.qemu_pids = qemu_pids
    return qemu_pids


def start_smap_tool(ctx: TestContext) -> bool:
    """Start smap_tool memory migration tool"""
    log(ctx, "Step 4: Starting smap_tool...")

    config = ctx.config
    smap_path = config["smap_tool"]["path"]
    swap_size_gb = config["smap_tool"]["swap_size_gb"]
    ratio = config["smap_tool"]["ratio"]

    # Clean old smap config
    subprocess.run(["rm", "-rf", "/dev/shm/smap_config"], timeout=10)
    log(ctx, "Cleaned /dev/shm/smap_config")

    # Get QEMU PIDs
    qemu_pids = get_qemu_pids(ctx)
    if not qemu_pids:
        log(ctx, "ERROR: No QEMU processes found")
        return False

    # Calculate parameters
    vm_count = len(ctx.vm_ips)
    swap_size_mb = int(swap_size_gb * 1024)
    ratio_percent = int(ratio * 100)  # Convert 0.15 to 15

    # Build command
    cmd = f"./smap_tool {vm_count} `pidof qemu-kvm` --swap-size {swap_size_mb} --ratio {ratio_percent}"

    log(ctx, f"Executing: {cmd}")

    # Redirect stdout/stderr to log files (prevent PIPE buffer overflow)
    smap_log_dir = os.path.join(ctx.result_dir, "smap_tool")
    os.makedirs(smap_log_dir, exist_ok=True)
    smap_stdout_path = os.path.join(smap_log_dir, "smap_stdout.log")
    smap_stderr_path = os.path.join(smap_log_dir, "smap_stderr.log")

    ctx.smap_stdout = open(smap_stdout_path, 'w')
    ctx.smap_stderr = open(smap_stderr_path, 'w')

    # Start smap_tool in background
    proc = subprocess.Popen(
        cmd, shell=True, cwd=smap_path,
        stdout=ctx.smap_stdout, stderr=ctx.smap_stderr
    )

    ctx.smap_pid = proc.pid
    log(ctx, f"smap_tool started with PID {proc.pid}")
    log(ctx, f"smap_tool output redirected to {smap_log_dir}/")

    # Wait a moment and verify
    time.sleep(3)

    if proc.poll() is None:
        log(ctx, "smap_tool running successfully")
        return True
    else:
        # Close files if failed
        ctx.smap_stdout.close()
        ctx.smap_stderr.close()
        log(ctx, f"smap_tool failed to start, return code {proc.returncode}")
        return False


# ==================== Step 5: Wait for VMs Ready ====================

def check_vm_ready(ip: str, username: str, password: str, qemu_pid: int, cpu_threshold: int) -> Tuple[bool, str]:
    """Check if single VM is ready (SSH, openclaw gateway, CPU utilization)"""
    try:
        # SSH connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=ip, port=22, username=username, password=password,
                    timeout=30, look_for_keys=False)

        # Check openclaw process
        stdin, stdout, stderr = ssh.exec_command("pgrep -f openclaw", timeout=10)
        openclaw_pids = stdout.read().decode().strip()

        if not openclaw_pids:
            ssh.close()
            return False, "openclaw process not found"

        # Check port 18789
        stdin, stdout, stderr = ssh.exec_command("ss -tln | grep 18789", timeout=10)
        port_check = stdout.read().decode()

        if '18789' not in port_check:
            ssh.close()
            return False, "port 18789 not listening"

        ssh.close()

        # Check CPU utilization on host
        try:
            proc = psutil.Process(qemu_pid)
            cpu_percent = proc.cpu_percent(interval=1)

            if cpu_percent > cpu_threshold:
                return False, f"CPU utilization {cpu_percent}% > threshold {cpu_threshold}%"
        except psutil.NoSuchProcess:
            return False, "qemu process not found"

        return True, "ready"

    except paramiko.AuthenticationException:
        return False, "SSH authentication failed"
    except paramiko.SSHException as e:
        return False, f"SSH error: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def wait_vms_ready(ctx: TestContext) -> bool:
    """Wait for all VMs to be ready"""
    log(ctx, "Step 5: Waiting for VMs ready...")

    config = ctx.config
    username = config["vm"]["username"]
    password = config["vm"]["password"]
    cpu_threshold = config["wait"]["cpu_threshold"]
    check_interval = config["wait"]["check_interval"]
    timeout = config["wait"]["service_timeout"]

    start_time = time.time()
    ready_vms = set()

    while time.time() - start_time < timeout:
        all_ready = True

        for ip in ctx.vm_ips:
            if ip in ready_vms:
                continue

            qemu_pid = ctx.qemu_pids.get(ip)
            if not qemu_pid:
                continue

            ready, msg = check_vm_ready(ip, username, password, qemu_pid, cpu_threshold)

            if ready:
                ready_vms.add(ip)
                log(ctx, f"VM {ip} ready")
            else:
                all_ready = False
                # Only log if not trivial SSH timeout
                if "SSH" not in msg or time.time() - start_time > 60:
                    log(ctx, f"VM {ip} not ready: {msg}")

        if all_ready and len(ready_vms) >= len(ctx.vm_ips) * 0.9:
            log(ctx, f"All VMs ready: {len(ready_vms)}/{len(ctx.vm_ips)}")
            return True

        log(ctx, f"Waiting... {len(ready_vms)}/{len(ctx.vm_ips)} ready")
        time.sleep(check_interval)

    # Timeout
    log(ctx, f"WARNING: Timeout, {len(ready_vms)}/{len(ctx.vm_ips)} ready")
    return len(ready_vms) >= len(ctx.vm_ips) * 0.7  # Allow 30% failure


# ==================== Step 6: Warmup Phase ====================

def run_warmup(ctx: TestContext) -> bool:
    """Run browser warmup phase"""
    log(ctx, "Step 6: Running warmup phase...")

    config = ctx.config
    vm_count = config["vm"]["count"]
    start_ip = config["vm"]["start_ip"]
    warmup = config["warmup"]

    # Build command
    cmd = [
        "python", "vm_bench_lite.py",
        "-n", str(vm_count),
        "--start-ip", start_ip,
        "--browser-mode",
        "-wp",  # Warmup phase
        "--batch-size", str(warmup["batch_size"]),
        "--batch-interval", str(warmup["batch_interval"]),
        "--warmup-loops", str(warmup["loops"]),
        "--warmup-delay", str(warmup["delay"])
    ]

    # Add warmup URLs
    for url in warmup["urls"]:
        cmd.extend(["--warmup-url", url])

    log(ctx, f"Executing warmup command...")
    log(ctx, f"Command: {' '.join(cmd[:10])}...")

    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800
    )

    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        log(ctx, f"Warmup failed with return code {result.returncode}")
        return False

    # Move warmup_summary to test result directory
    import shutil
    warmup_summaries = Path("results").glob("warmup_summary_*.txt")
    for summary in warmup_summaries:
        dest = os.path.join(ctx.result_dir, "vm_bench_lite", summary.name)
        shutil.move(str(summary), dest)
        log(ctx, f"Warmup summary moved: {summary.name}")

    log(ctx, "Warmup phase completed")
    return True


# ==================== Step 7: Start Monitoring ====================

# Lock file for syncing monitor with benchmark
BENCHMARK_LOCK_FILE = "/tmp/vm_benchmark_running.lock"


def start_monitor(ctx: TestContext) -> bool:
    """Start qemu_monitor.py with stress-file + duration sync

    Monitor waits for lock file to appear before sampling (no idle sampling).
    After duration seconds, monitor stops naturally and generates Excel.
    """
    log(ctx, "Step 7: Starting monitoring...")

    config = ctx.config
    monitor = config["monitor"]
    test = config["test"]

    duration = test["duration"]
    interval = monitor["interval"]
    numa_nodes = ",".join(str(n) for n in monitor["numa_nodes"])
    log_dir = os.path.join(ctx.result_dir, "qemu_monitor")

    # Use --stress-file + -t duration: monitor waits for lock file, then runs for duration
    # This ensures: 1) no idle sampling before benchmark, 2) natural stop after duration, 3) Excel generated
    # --auto-skip: automatically skip missing tools (ksys, devkit, etc.) without prompting
    #              This allows automated runs to continue even when tools are not available
    cmd = [
        "python3", "qemu_monitor.py",
        "-t", str(duration),
        "-i", str(interval),
        "--log-dir", log_dir,
        "--numa", numa_nodes,
        "--stress-file", BENCHMARK_LOCK_FILE,
        "--auto-skip"  # Auto-skip missing tools for automation
    ]

    if monitor["enable_capture"]:
        cmd.append("--enable-capture")

    log(ctx, f"Executing: {' '.join(cmd)}")
    log(ctx, f"Monitor waits for lock file: {BENCHMARK_LOCK_FILE}")
    log(ctx, f"Monitor will run {duration}s after lock file appears, then generate Excel")
    log(ctx, f"Auto-skip enabled: missing tools will be skipped without prompting")

    # Redirect stdout/stderr to log file instead of PIPE
    # PIPE buffer (64KB) can fill up and block the process when qemu_monitor outputs lots of data
    monitor_stdout_log = os.path.join(log_dir, "monitor_stdout.log")
    monitor_stderr_log = os.path.join(log_dir, "monitor_stderr.log")

    # Open files and keep them open while process runs
    stdout_f = open(monitor_stdout_log, 'w')
    stderr_f = open(monitor_stderr_log, 'w')

    proc = subprocess.Popen(cmd, stdout=stdout_f, stderr=stderr_f)

    ctx.monitor_pid = proc.pid
    ctx.monitor_stdout = stdout_f
    ctx.monitor_stderr = stderr_f

    log(ctx, f"Monitor started with PID {ctx.monitor_pid}")
    log(ctx, f"Monitor output redirected to {monitor_stdout_log}")

    time.sleep(2)
    if proc.poll() is None:
        log(ctx, "Monitor ready, waiting for benchmark lock file...")
        return True

    # If failed to start, close files
    stdout_f.close()
    stderr_f.close()
    log(ctx, f"Monitor failed to start")
    return False


# ==================== Step 8: Benchmark Phase ====================

def run_benchmark(ctx: TestContext) -> bool:
    """Run browser benchmark phase"""
    log(ctx, "Step 8: Running benchmark phase...")

    config = ctx.config
    vm_count = config["vm"]["count"]
    start_ip = config["vm"]["start_ip"]
    test = config["test"]

    # Create lock file to signal monitor to start sampling
    Path(BENCHMARK_LOCK_FILE).touch()
    log(ctx, f"Created lock file: {BENCHMARK_LOCK_FILE}")
    log(ctx, "Monitor will now start sampling (aligned with benchmark)")

    # Build command
    cmd = [
        "python", "vm_bench_lite.py",
        "-n", str(vm_count),
        "--start-ip", start_ip,
        "--browser-mode",
        "-bsp", str(test["active_percent"]),  # Browser stress percent
        "--batch-size", str(test["batch_size"]),
        "--batch-interval", str(test["batch_interval"]),
        "--browser-url", test["browser_url"],
        "--browser-interval-min", str(test["browser_interval_min"]),
        "--browser-interval-max", str(test["browser_interval_max"]),
        "-t", str(test["duration"])
    ]

    log(ctx, f"Executing benchmark command...")
    log(ctx, f"Command: {' '.join(cmd)}")

    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=test["duration"] + 300
    )

    # Note: Do NOT remove lock file here. Monitor uses duration to stop naturally.
    # This ensures Excel report is generated properly.
    log(ctx, "Benchmark completed, monitor will stop after duration and generate Excel")

    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        log(ctx, f"Benchmark failed with return code {result.returncode}")
        return False

    # Move benchmark report to result directory
    # vm_bench_lite saves reports to results/ directory
    bench_reports = Path("results").glob("bench_report_*.txt")
    for report in bench_reports:
        import shutil
        dest = os.path.join(ctx.result_dir, "vm_bench_lite", report.name)
        shutil.move(str(report), dest)
        log(ctx, f"Benchmark report moved: {report.name}")

    log(ctx, "Benchmark phase completed")
    return True


# ==================== Step 9: Stop Monitor and Collect Results ====================

def wait_for_ksys_parse_completion(ctx: TestContext, timeout: int = 600) -> bool:
    """Wait for ksys to complete data parsing phase

    ksys workflow:
    1. "Starting to collect data" - collection phase
    2. "Starting to parse data" - parse phase (TIME CONSUMING, can take minutes)
    3. "Starting to process and print data" - parse complete, output starts
    4. Metrics output - final data

    Returns True if ksys parse completed, False if timeout or no ksys log
    """
    ksys_log_path = os.path.join(ctx.result_dir, "qemu_monitor", "ksys.log")

    if not os.path.exists(ksys_log_path):
        log(ctx, "No ksys.log found, skipping ksys wait")
        return True

    log(ctx, f"Waiting for ksys parse completion (timeout={timeout}s)...")
    log(ctx, "Tip: If timeout occurs frequently, increase 'ksys_parse_timeout' in config")

    start_time = time.time()
    parse_started = False
    last_file_size = 0
    warning_thresholds = [0.5, 0.75, 0.9]  # 50%, 75%, 90% of timeout
    warnings_given = [False, False, False]

    while time.time() - start_time < timeout:
        try:
            with open(ksys_log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Monitor file size growth as progress indicator
            current_size = len(content)
            if current_size > last_file_size:
                last_file_size = current_size

            # Check if parse started
            if "Starting to parse data" in content and not parse_started:
                parse_started = True
                log(ctx, "ksys parse phase detected, waiting for completion...")

            # Check if parse completed (process and print data)
            if "Starting to process and print data" in content:
                elapsed = int(time.time() - start_time)
                log(ctx, f"ksys parse completed in {elapsed}s - data output started")
                return True

            # Alternative: check if CPU Metrics section appeared (data output)
            if "CPU Metrics" in content or "Common Microarchitecture Metrics" in content:
                elapsed = int(time.time() - start_time)
                log(ctx, f"ksys data output detected in {elapsed}s - parse completed")
                return True

            # Check for "Data saved successfully" (JSON output mode)
            if "Data saved successfully" in content:
                elapsed = int(time.time() - start_time)
                log(ctx, f"ksys data saved in {elapsed}s - fully completed")
                return True

            elapsed = int(time.time() - start_time)
            elapsed_ratio = elapsed / timeout

            # Progress logging every 30s
            if elapsed % 30 == 0 and elapsed > 0:
                size_kb = current_size / 1024
                log(ctx, f"ksys parse in progress... {elapsed}s/{timeout}s ({elapsed_ratio*100:.0f}%), log size: {size_kb:.1f}KB")

            # Warning at threshold points
            for i, threshold in enumerate(warning_thresholds):
                if elapsed_ratio >= threshold and not warnings_given[i]:
                    warnings_given[i] = True
                    remaining = int(timeout - elapsed)
                    log(ctx, f"WARNING: ksys parse approaching timeout ({threshold*100:.0f}% used), {remaining}s remaining")
                    if threshold >= 0.75:
                        log(ctx, "  Consider increasing 'ksys_parse_timeout' in config for large VM counts")

            time.sleep(5)

        except Exception as e:
            log(ctx, f"Error reading ksys.log: {e}")
            time.sleep(5)

    # Timeout - provide actionable advice
    elapsed = int(time.time() - start_time)
    log(ctx, f"WARNING: ksys parse wait timeout after {elapsed}s")
    log(ctx, "  Possible causes:")
    log(ctx, "    - Large VM count generates more data, parse takes longer")
    log(ctx, "    - System under heavy load, ksys parse slowed")
    log(ctx, "  Solutions:")
    log(ctx, "    - Increase 'ksys_parse_timeout' in config (current: {timeout}s)")
    log(ctx, "    - Reduce monitor sampling interval in config")
    log(ctx, "    - Check ksys.log for parse progress")
    log(ctx, f"    - Current ksys.log size: {os.path.getsize(ksys_log_path)/1024:.1f}KB")
    return False


def stop_monitor(ctx: TestContext):
    """Wait for monitor to complete naturally and generate Excel report"""
    if ctx.monitor_pid:
        log(ctx, f"Waiting for monitor PID {ctx.monitor_pid} to complete...")

        config = ctx.config
        duration = config["test"]["duration"]

        try:
            proc = psutil.Process(ctx.monitor_pid)

            # Phase 1: Wait for sampling to complete (duration + 30s buffer)
            max_wait_sampling = duration + 30
            start = time.time()

            log(ctx, f"Waiting for sampling phase ({duration}s)...")
            while time.time() - start < max_wait_sampling:
                if not proc.is_running():
                    log(ctx, "Monitor process ended during sampling phase")
                    break
                time.sleep(5)

            # Phase 2: Wait for ksys parse completion (CRITICAL - can take minutes)
            # ksys needs to finish parsing before Excel can include its data
            ksys_timeout = ctx.config.get("monitor", {}).get("ksys_parse_timeout", 600)
            log(ctx, f"ksys parse timeout configured: {ksys_timeout}s")
            wait_for_ksys_parse_completion(ctx, ksys_timeout)

            # Phase 3: Wait for Excel generation (up to 120s)
            log(ctx, "Waiting for Excel report generation...")
            excel_start = time.time()
            excel_timeout = 120

            while time.time() - excel_start < excel_timeout:
                if not proc.is_running():
                    log(ctx, "Monitor completed - Excel should be generated")
                    time.sleep(10)  # Wait for final file writes
                    break
                elapsed = int(time.time() - excel_start)
                if elapsed % 20 == 0:
                    log(ctx, f"Excel generation... {elapsed}s")
                time.sleep(5)

            # If still running, force terminate
            if proc.is_running():
                log(ctx, f"Monitor still running after {duration + 150}s total, force terminating...")
                os.kill(ctx.monitor_pid, signal.SIGTERM)
                time.sleep(5)
                try:
                    os.kill(ctx.monitor_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                log(ctx, "Monitor terminated - Excel may not be complete")

        except psutil.NoSuchProcess:
            log(ctx, "Monitor already completed")
            time.sleep(10)

        except Exception as e:
            log(ctx, f"Error waiting for monitor: {e}")

        # Close file handles
        if ctx.monitor_stdout:
            try:
                ctx.monitor_stdout.close()
            except:
                pass
        if ctx.monitor_stderr:
            try:
                ctx.monitor_stderr.close()
            except:
                pass

    # Clean up lock file
    if os.path.exists(BENCHMARK_LOCK_FILE):
        os.remove(BENCHMARK_LOCK_FILE)
        log(ctx, f"Cleaned up lock file: {BENCHMARK_LOCK_FILE}")


def collect_results(ctx: TestContext):
    """Collect and organize results"""
    log(ctx, "Step 9: Collecting results...")

    # Monitor logs are already in qemu_monitor subdirectory

    # Generate summary
    summary = {
        "test_id": os.path.basename(ctx.result_dir),
        "test_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration": ctx.config["test"]["duration"],
        "parameters": {
            "vm_count": ctx.config["vm"]["count"],
            "ratio": ctx.config["smap_tool"]["ratio"],
            "active_percent": ctx.config["test"]["active_percent"],
        }
    }

    # Save summary JSON
    import json
    summary_path = os.path.join(ctx.result_dir, "summary", "metrics_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    log(ctx, f"Results collected in {ctx.result_dir}")


# ==================== Step 10: Cleanup ====================

def cleanup(ctx: TestContext, os_env: Dict):
    """Cleanup: stop smap_tool, delete VMs"""
    log(ctx, "Step 10: Cleanup...")

    # Stop smap_tool
    if ctx.smap_pid:
        log(ctx, f"Stopping smap_tool PID {ctx.smap_pid}...")
        try:
            os.kill(ctx.smap_pid, signal.SIGTERM)
            time.sleep(2)
            try:
                os.kill(ctx.smap_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            log(ctx, "smap_tool stopped")
        except ProcessLookupError:
            log(ctx, "smap_tool already stopped")

    # Close smap_tool file handles
    if ctx.smap_stdout:
        try:
            ctx.smap_stdout.close()
        except:
            pass
    if ctx.smap_stderr:
        try:
            ctx.smap_stderr.close()
        except:
            pass

    # Delete VMs
    delete_vms(ctx, os_env)

    log(ctx, "Cleanup completed")


# ==================== Main ====================

def main():
    parser = argparse.ArgumentParser(description="Multi-VM Agent Automation Test")
    parser.add_argument("--config", required=True, help="Config YAML file path")

    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Create result directory
    result_dir = create_result_dir(config)
    log_file = os.path.join(result_dir, "test_log.txt")

    # Initialize context
    ctx = TestContext(
        config=config,
        result_dir=result_dir,
        log_file=log_file,
        start_time=time.time()
    )

    # Create log file
    Path(log_file).touch()

    # Clean up old lock file before test starts (prevent false detection)
    if os.path.exists(BENCHMARK_LOCK_FILE):
        os.remove(BENCHMARK_LOCK_FILE)
        log(ctx, f"Cleaned up stale lock file before test: {BENCHMARK_LOCK_FILE}")

    log(ctx, "=" * 60)
    log(ctx, "Multi-VM Agent Automation Test Started")
    log(ctx, f"Config: {args.config}")
    log(ctx, f"Result dir: {result_dir}")
    log(ctx, "=" * 60)

    # Save config copy
    save_config_copy(ctx, args.config)

    # Setup OpenStack environment
    os_env = setup_openstack_env(config)

    success = True

    try:
        # Step 2: Delete VMs
        if not delete_vms(ctx, os_env):
            log(ctx, "ERROR: Failed to delete VMs, aborting")
            success = False
            return

        # Step 3: Create VMs
        if not create_vms(ctx, os_env):
            log(ctx, "ERROR: Failed to create VMs, aborting")
            success = False
            return

        # Step 4: Start smap_tool
        if not start_smap_tool(ctx):
            log(ctx, "ERROR: Failed to start smap_tool, aborting")
            success = False
            cleanup(ctx, os_env)
            return

        # Step 5: Wait for VMs ready
        if not wait_vms_ready(ctx):
            log(ctx, "WARNING: Not all VMs ready, continuing with partial VMs")

        # Step 6: Warmup
        if not run_warmup(ctx):
            log(ctx, "WARNING: Warmup failed, continuing...")

        # Step 7: Start monitor
        if not start_monitor(ctx):
            log(ctx, "WARNING: Monitor failed to start, continuing without monitoring")

        # Step 8: Benchmark
        if not run_benchmark(ctx):
            log(ctx, "ERROR: Benchmark failed")
            success = False

        # Step 9: Collect results
        stop_monitor(ctx)
        collect_results(ctx)

        # Step 10: Cleanup
        cleanup(ctx, os_env)

    except KeyboardInterrupt:
        log(ctx, "Test interrupted by user")
        success = False
        cleanup(ctx, os_env)

    except Exception as e:
        log(ctx, f"ERROR: {e}")
        import traceback
        log(ctx, traceback.format_exc())
        success = False
        cleanup(ctx, os_env)

    finally:
        elapsed = time.time() - ctx.start_time
        log(ctx, "=" * 60)
        log(ctx, f"Test completed in {elapsed:.1f} seconds")
        log(ctx, f"Result: {'SUCCESS' if success else 'FAILED'}")
        log(ctx, f"Result directory: {result_dir}")
        log(ctx, "=" * 60)

        # Ensure lock file is cleaned up on exit (prevent stale file for next test)
        if os.path.exists(BENCHMARK_LOCK_FILE):
            os.remove(BENCHMARK_LOCK_FILE)
            log(ctx, f"Cleaned up lock file on exit: {BENCHMARK_LOCK_FILE}")

        print(f"\nTest result saved to: {result_dir}")


if __name__ == "__main__":
    main()