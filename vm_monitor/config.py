# vm_monitor/config.py
"""
Configuration Management Module

Manages .env file configuration, NUMA node settings, and getfre YAML config.
All tools' paths are loaded from .env and validated before use.
"""

import glob
import os

# Try to import python-dotenv for .env support
try:
    from dotenv import load_dotenv, set_key

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

# Try to import yaml for getfre config
try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ==================== Constants ====================

ENV_FILE_PATH = ".env"
ENV_REQUIRED_KEYS = [
    "DEVKIT_PATH",
    "KSYS_PATH",
    "KSYS_CONFIG_PATH",
    "UB_WATCH_PATH",
    "SMAP_BW_PATH",
    "GETFRE_PATH",
    "GETFRE_CONFIG_PATH",
]


# ==================== Host topology auto-discovery ====================


def _parse_cpulist(text: str) -> list:
    """Parse a Linux cpulist string (e.g. "0-47,96-127") into a sorted list of ints."""
    cores = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-")
            cores.extend(range(int(start), int(end) + 1))
        else:
            cores.append(int(part))
    return cores


def _read_numa_cpulist(node: int) -> list:
    """Logical CPU IDs belonging to a NUMA node, read from sysfs cpulist."""
    try:
        with open(f"/sys/devices/system/node/node{node}/cpulist") as f:
            return _parse_cpulist(f.read().strip())
    except Exception:
        return []


def _physical_cores_for_numa(node: int) -> list:
    """Physical (non-hyperthread) core IDs for a NUMA node, deduped per socket.

    For each logical CPU on the node, reads its
    ``cpu{N}/topology/thread_siblings_list`` and keeps the lowest sibling ID as
    the physical-core representative (so each physical core appears once).
    Falls back to the full cpulist when topology files are unavailable (e.g.
    some virtualized setups without HT).
    """
    logical = _read_numa_cpulist(node)
    if not logical:
        return []
    physical = set()
    have_topology = False
    for cpu in logical:
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list") as f:
                siblings = _parse_cpulist(f.read().strip())
            if siblings:
                physical.add(min(siblings))
                have_topology = True
                continue
        except Exception:
            pass
        physical.add(cpu)
    if not have_topology:
        # No topology files -> cannot dedup siblings; treat logical as physical.
        return sorted(logical)
    return sorted(physical)


def _count_physical_cores() -> int:
    """Count physical (non-HT) CPU cores on this host from sysfs topology.

    Used as the getfre ``total_cores`` default. Falls back to ``os.cpu_count()``
    when sysfs topology is unavailable (non-Linux / minimal containers).
    """
    physical = set()
    have_topology = False
    for cpu_dir in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
        try:
            with open(f"{cpu_dir}/topology/thread_siblings_list") as f:
                siblings = _parse_cpulist(f.read().strip())
            if siblings:
                physical.add(min(siblings))
                have_topology = True
                continue
        except Exception:
            pass
    if have_topology:
        return len(physical)
    return os.cpu_count() or 1


def _discover_numa_nodes() -> list:
    """Auto-discover NUMA node IDs present on this host from sysfs.

    Returns ``[0]`` when sysfs is unavailable (non-NUMA / non-Linux hosts).
    """
    try:
        nodes = []
        for f in os.listdir("/sys/devices/system/node/"):
            if f.startswith("node") and f[4:].isdigit():
                nodes.append(int(f[4:]))
        return sorted(nodes) if nodes else [0]
    except Exception:
        return [0]


def load_env_config() -> dict:
    """Load configuration from .env file

    Returns:
        dict with keys: devkit_path, ksys_path, ksys_config_path,
        ub_watch_path, smap_bw_path, devkit_cpu_range,
        getfre_path, getfre_config_path
    """
    config = {
        "devkit_path": "",
        "ksys_path": "",
        "ksys_config_path": "",
        "ub_watch_path": "",
        "smap_bw_path": "",
        "devkit_cpu_range": "",
        "getfre_path": "",
        "getfre_config_path": "",
    }

    if DOTENV_AVAILABLE and os.path.exists(ENV_FILE_PATH):
        load_dotenv(ENV_FILE_PATH)

    # Read from environment variables (set by dotenv or system)
    config["devkit_path"] = os.environ.get("DEVKIT_PATH", "")
    config["ksys_path"] = os.environ.get("KSYS_PATH", "")
    config["ksys_config_path"] = os.environ.get("KSYS_CONFIG_PATH", "")
    config["ub_watch_path"] = os.environ.get("UB_WATCH_PATH", "")
    config["smap_bw_path"] = os.environ.get("SMAP_BW_PATH", "")
    config["devkit_cpu_range"] = os.environ.get("DEVKIT_CPU_RANGE", "")
    config["getfre_path"] = os.environ.get("GETFRE_PATH", "")
    config["getfre_config_path"] = os.environ.get("GETFRE_CONFIG_PATH", "")

    return config


def save_env_config(config: dict):
    """Save configuration back to .env file"""
    env_path = ENV_FILE_PATH

    # Create .env file if not exists
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write("# Log collection tools configuration\n")
            f.write("# Generated by qemu_monitor.py\n\n")

    # Write each key
    if DOTENV_AVAILABLE:
        if config.get("devkit_path"):
            set_key(env_path, "DEVKIT_PATH", config["devkit_path"])
        if config.get("ksys_path"):
            set_key(env_path, "KSYS_PATH", config["ksys_path"])
        if config.get("ksys_config_path"):
            set_key(env_path, "KSYS_CONFIG_PATH", config["ksys_config_path"])
        if config.get("ub_watch_path"):
            set_key(env_path, "UB_WATCH_PATH", config["ub_watch_path"])
        if config.get("smap_bw_path"):
            set_key(env_path, "SMAP_BW_PATH", config["smap_bw_path"])
        if config.get("devkit_cpu_range"):
            set_key(env_path, "DEVKIT_CPU_RANGE", config["devkit_cpu_range"])
        if config.get("getfre_path"):
            set_key(env_path, "GETFRE_PATH", config["getfre_path"])
        if config.get("getfre_config_path"):
            set_key(env_path, "GETFRE_CONFIG_PATH", config["getfre_config_path"])
    else:
        # Fallback: manual write
        with open(env_path, "a") as f:
            for key, value in config.items():
                if value:
                    env_key = key.upper()
                    if env_key in ENV_REQUIRED_KEYS or env_key == "DEVKIT_CPU_RANGE":
                        f.write(f"{env_key}={value}\n")


def validate_and_prompt_missing(config: dict, non_interactive: bool = False) -> dict:
    """Validate paths and prompt user for missing/invalid ones

    Args:
        config: dict with path configurations
        non_interactive: if True, skip prompts and disable missing tools silently

    Returns:
        Updated config dict with valid paths or None for disabled tools
    """
    key_mapping = {
        "DEVKIT_PATH": "devkit_path",
        "KSYS_PATH": "ksys_path",
        "KSYS_CONFIG_PATH": "ksys_config_path",
        "UB_WATCH_PATH": "ub_watch_path",
        "SMAP_BW_PATH": "smap_bw_path",
        "GETFRE_PATH": "getfre_path",
        "GETFRE_CONFIG_PATH": "getfre_config_path",
    }

    prompt_names = {
        "DEVKIT_PATH": "DevKit CLI path (devkit executable)",
        "KSYS_PATH": "ksys executable path",
        "KSYS_CONFIG_PATH": "ksys config.yaml path",
        "UB_WATCH_PATH": "ub_watch executable path",
        "SMAP_BW_PATH": "smap_bw.py script path",
        "GETFRE_PATH": "getfre executable path",
        "GETFRE_CONFIG_PATH": "getfre_config.yaml path",
    }

    for env_key, config_key in key_mapping.items():
        path = config.get(config_key, "")

        # Check if path is valid
        if path and os.path.exists(path):
            continue  # Path is valid, no action needed

        if non_interactive:
            # Non-interactive mode: silently disable missing tools
            if not path or not os.path.exists(path):
                config[config_key] = None  # Mark as disabled
                print(f"  [WARN] {env_key} not configured or invalid, disabled for this session")
        else:
            # Interactive mode: prompt user for input
            while not path or not os.path.exists(path):
                print(f"\n[WARN] {env_key} not configured or path invalid")
                if path:
                    print(f"  Current: {path}")
                user_input = input(f"Enter {prompt_names[env_key]} (or 'skip' to disable): ").strip()

                if user_input.lower() == "skip":
                    config[config_key] = None  # Mark as disabled
                    print(f"  [OK] {env_key} disabled for this session")
                    break

                if os.path.exists(user_input):
                    path = user_input
                    config[config_key] = user_input
                    print(f"  [OK] {env_key} set to: {user_input}")
                else:
                    print(f"  [ERROR] Path does not exist: {user_input}")

    # Save updated config to .env (only if any changes made)
    if non_interactive:
        # In non-interactive mode, don't save disabled tools to .env
        # Only save valid paths
        save_config = {k: v for k, v in config.items() if v is not None and v != ""}
        if save_config:
            save_env_config(save_config)
    else:
        save_env_config(config)
        print("\n[OK] Configuration saved to .env file")

    return config


def calculate_cpu_range_from_numa(numa_nodes: list) -> str:
    """Calculate CPU core range from NUMA node IDs.

    Args:
        numa_nodes: list of NUMA node IDs (e.g., [0, 1])

    Returns:
        CPU range string like "0-95,192-287", or "" if no cpulist could be read
        for any of the requested nodes (do not silently fall back to a wrong range).
    """
    all_cores = []

    for node in numa_nodes:
        cores = _read_numa_cpulist(node)
        if not cores:
            print(f"[WARN] Failed to read CPU list for NUMA node {node}")
            continue
        all_cores.extend(cores)

    if not all_cores:
        return ""  # No usable cpulist — let the caller decide rather than guess.

    # Sort and merge into ranges
    all_cores.sort()
    ranges = []
    start = all_cores[0]
    end = all_cores[0]

    for core in all_cores[1:]:
        if core == end + 1:
            end = core
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = core
            end = core

    # Add last range
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def numa_to_physical_cores(numa_nodes: list, core_interval: int = 1) -> dict:
    """Convert NUMA node IDs to physical core IDs with sampling interval.

    Cores per node are auto-discovered from sysfs topology
    (``/sys/devices/system/node/node{N}/cpulist`` plus per-CPU
    ``thread_siblings_list`` to drop hyperthread siblings), then sampled every
    ``core_interval`` cores. Falls back to the full logical cpulist when HT
    topology is unavailable.

    Args:
        numa_nodes: list of NUMA node IDs (e.g., [0, 1])
        core_interval: sampling interval (1=all physical cores, 2=every other)

    Returns:
        dict: {numa_id: [physical_core_ids]}
    """
    result = {}
    for numa in numa_nodes:
        cores = _physical_cores_for_numa(numa)
        if not cores:
            print(f"[WARN] No physical cores discovered for NUMA node {numa}, skipping")
            continue
        result[numa] = cores[::core_interval]
    return result


def load_getfre_config(config_path: str) -> dict:
    """Load getfre configuration from YAML file

    Args:
        config_path: path to getfre_config.yaml

    Returns:
        dict with keys: getfre_path, total_cores, interval,
        core_interval, numa_nodes
        Returns default config (auto-detected from the host) if file not
        found or invalid
    """
    default_config = {
        "getfre_path": "",
        "total_cores": _count_physical_cores(),
        "interval": 2,
        "core_interval": 1,
        "numa_nodes": _discover_numa_nodes(),
    }

    if not config_path or not os.path.exists(config_path):
        return default_config

    if not YAML_AVAILABLE:
        print("[WARN] yaml module not available, using default getfre config")
        return default_config

    try:
        with open(config_path) as f:
            yaml_config = yaml.safe_load(f)

        if yaml_config:
            # Merge with defaults, yaml values take precedence
            result = dict(default_config)
            for key in result:
                if key in yaml_config:
                    result[key] = yaml_config[key]
            return result
    except Exception as e:
        print(f"[WARN] Failed to load getfre_config.yaml: {e}")
        return default_config
