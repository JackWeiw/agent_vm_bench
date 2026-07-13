#!/usr/bin/env python3
"""
OpenStack Batch VM Creation Performance Statistics Script
Features: Batch concurrent creation of OpenStack VMs, statistics from submission to ACTIVE time and performance percentile values
Dependencies: 1. Install OpenStack CLI  2. Configure ~/.admin-openrc authentication file  3. Access to OpenStack API

Core Parameters:
--start_ip        Starting IP (e.g., 192.168.110.131)
--n               Number of VMs to create (default: 1)
--flavor          VM flavor (default: 2U_4G_40G)
--image           Image name (default: ubuntu-24.04)
--prefix          VM name prefix (default: test_openclaw)
--workers         Concurrency level (default: 50; 0 = full parallel)
--network-id      OpenStack network ID (required)
--az              Availability zone (default: nova_zone:controller)

Usage Example:
python3 create_server.py \
  --start_ip 192.168.110.11 \
  --n 100 \
  --subnet-prefix 192.168.110. \
  --network-id 9ed33763-6bc3-4792-8dc8-697d0d691911 \
  --az nova_zone:controller \
  --flavor 2U_4G_30G_4K \
  --image ubuntu-24.04
"""

import argparse
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


@dataclass
class VMResult:
    name: str
    fixed_ip: str
    vm_id: str = ""  # OpenStack VM UUID, used for unambiguous status check
    submit_time: float = 0.0
    done_time: float = 0.0
    status: str = "pending"  # pending / active / error / timeout / submit_failed
    elapsed: float = 0.0
    detail: str = ""


def load_openrc(path: str = os.path.expanduser("~/.admin-openrc")) -> dict:
    """Parse export statements in openrc file, return environment variable dict."""
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                m = re.match(r"export\s+(\w+)=(.*)", line)
                if m:
                    key, val = m.group(1), m.group(2).strip("'\"")
                    env[key] = val
    return env


def get_os_env() -> dict:
    """Load openrc environment variables, remove http proxy."""
    env = os.environ.copy()
    env.update(load_openrc())
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    return env


def create_and_wait_vm(
    vm_name: str,
    fixed_ip: str,
    flavor: str,
    image: str,
    network_id: str,
    az: str,
    env: dict,
    timeout: int = 1200,
) -> VMResult:
    result = VMResult(name=vm_name, fixed_ip=fixed_ip)
    result.submit_time = time.time()

    proc = subprocess.Popen(
        [
            "openstack",
            "server",
            "create",
            "--flavor",
            flavor,
            "--image",
            image,
            "--nic",
            f"net-id={network_id},v4-fixed-ip={fixed_ip}",
            "--availability-zone",
            az,
            "--wait",
            "-f",
            "value",
            "-c",
            "id",
            vm_name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        result.status = "timeout"
        result.done_time = time.time()
        result.elapsed = result.done_time - result.submit_time
        return result

    result.done_time = time.time()
    result.elapsed = result.done_time - result.submit_time

    if proc.returncode != 0:
        result.status = "submit_failed"
        result.detail = stderr.strip()[:200]
        return result

    # Capture VM ID from create output (use ID for unambiguous status check)
    result.vm_id = stdout.strip()

    # Use VM ID (not name) to avoid ambiguity when duplicate names exist
    confirm = subprocess.run(
        ["openstack", "server", "show", result.vm_id, "-f", "value", "-c", "status"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=30,
    )
    state = confirm.stdout.strip()
    if state == "ACTIVE":
        result.status = "active"
    elif state == "ERROR":
        result.status = "error"
    else:
        result.status = state.lower() if state else "unknown"

    return result


def calc_stats(elapsed_list: list[float]) -> dict:
    if not elapsed_list:
        return {}
    sorted_list = sorted(elapsed_list)
    n = len(sorted_list)

    def percentile(p: float) -> float:
        idx = min(int(math.ceil(p / 100.0 * n)) - 1, n - 1)
        return sorted_list[max(idx, 0)]

    return {
        "min": sorted_list[0],
        "max": sorted_list[-1],
        "avg": sum(sorted_list) / n,
        "p50": percentile(50),
        "p95": percentile(95),
        "p99": percentile(99),
        "count": n,
    }


def main():
    import warnings
    warnings.warn(
        "create_server.py is deprecated. "
        "Use 'python -m vm_bench --create-only' instead. See docs/vm_bench-usage-guide.md",
        DeprecationWarning,
        stacklevel=2
    )
    parser = argparse.ArgumentParser(description="Batch create OpenStack VMs and statistics creation performance")
    parser.add_argument("--start_ip", default=None, help="Starting IP, e.g., 192.168.110.131")
    parser.add_argument("--n", type=int, default=1, help="Number of VMs to create")
    parser.add_argument("--flavor", default="2U_4G_40G", help="VM flavor")
    parser.add_argument("--image", default="ubuntu-24.04", help="Image name")
    parser.add_argument("--prefix", default="test_openclaw", help="VM name prefix")
    parser.add_argument("--workers", type=int, default=50, help="Concurrency, default 50, 0=full parallel")
    parser.add_argument("--subnet-prefix", default="192.168.110.", help="Subnet IP prefix, e.g., 192.168.110.")
    parser.add_argument("--network-id", default="2661422b-37c4-4d84-90ce-521167c676c0", help="OpenStack Network ID")
    parser.add_argument("--az", default="nova_zone:controller", help="Availability Zone")

    args = parser.parse_args()

    # Dynamic IP parsing
    subnet_prefix = args.subnet_prefix
    prefix_stripped = subnet_prefix.rstrip(".")
    start_ip_num = 131  # Default value

    if args.start_ip:
        if not args.start_ip.startswith(prefix_stripped + "."):
            print(f"Error: Invalid IP format, must start with {prefix_stripped}.")
            sys.exit(1)
        try:
            start_ip_num = int(args.start_ip.split(".")[-1])
        except ValueError:
            print("Error: Last IP segment must be a number")
            sys.exit(1)

    network_id = args.network_id
    az = args.az

    # Check IP range
    end_ip = start_ip_num + args.n - 1
    if end_ip > 254 or start_ip_num < 1:
        print("Error: IP exceeds valid range (1-254)")
        sys.exit(1)

    # Load openrc environment variables
    env = get_os_env()

    # Prepare VM list
    vms = []
    for i in range(args.n):
        ip = start_ip_num + i
        name = f"{args.prefix}_{i + 1}"
        fixed_ip = f"{subnet_prefix}{ip}"
        vms.append((name, fixed_ip))

    print(f"Planning to create {args.n} VMs, IP range: {subnet_prefix}{start_ip_num} ~ {subnet_prefix}{end_ip}")
    print()

    # Concurrent creation
    workers = args.workers if args.workers > 0 else args.n
    results = []
    global_start = time.time()

    def _task(name, ip):
        return create_and_wait_vm(
            vm_name=name,
            fixed_ip=ip,
            flavor=args.flavor,
            image=args.image,
            network_id=network_id,
            az=az,
            env=env,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_task, name, ip): name for name, ip in vms}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            if r.status == "active":
                print(f"[{r.name:>20s}] ({r.fixed_ip})  ACTIVE   {r.elapsed:.1f}s")
            else:
                detail_info = f" | ❌ {r.detail.strip()}" if r.detail else ""
                print(f"[{r.name:>20s}] ({r.fixed_ip})  {r.status.upper():>8s}   {r.elapsed:.1f}s{detail_info}")

    global_end = time.time()
    total_elapsed = global_end - global_start

    # Statistics
    active_list = [r.elapsed for r in results if r.status == "active"]
    stats = calc_stats(active_list)

    success_count = len(active_list)
    fail_count = args.n - success_count

    print()
    print("=" * 55)
    print("  VM Creation Performance Statistics (Submit -> ACTIVE)")
    print("=" * 55)
    print(f"  Total: {args.n}  |  Success: {success_count}  |  Failed: {fail_count}")
    print(f"  Total time: {total_elapsed:.1f}s")
    print()

    if stats:
        print(f"  {'Min:':<10s} {stats['min']:<10.1f}s")
        print(f"  {'Max:':<10s} {stats['max']:<10.1f}s")
        print(f"  {'Avg:':<10s} {stats['avg']:<10.1f}s")
        print(f"  {'P50:':<10s} {stats['p50']:<10.1f}s")
        print(f"  {'P95:':<10s} {stats['p95']:<10.1f}s")
        print(f"  {'P99:':<10s} {stats['p99']:<10.1f}s")

    print("=" * 55)


if __name__ == "__main__":
    main()
