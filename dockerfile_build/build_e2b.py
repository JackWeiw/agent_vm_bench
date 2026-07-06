#!/usr/bin/env python3
"""
Build E2B template and create sandbox from the custom image.

This script:
1. Reads E2B configuration from ~/.e2b/config.json
2. Builds an E2B template from the Harbor image
3. Creates a sandbox for testing

Usage:
    python build_e2b.py --server-ip <e2b_api_server_ip> --harbor-ip <harbor_registry_ip>

Example:
    python build_e2b.py --server-ip 141.61.17.196 --harbor-ip 141.61.17.196

Note:
    Harbor registry is accessed via IP:30443 (nginx reverse proxy).
    The Harbor registry and E2B API service are typically deployed on the same server.
"""

import os
import json
import argparse
import sys
from e2b import Template, default_build_logger, wait_for_port
from e2b import Sandbox


# Default configuration
DEFAULT_SERVER_IP = "141.61.17.196"
DEFAULT_HARBOR_IP = "141.61.17.196"


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Build E2B template and create sandbox"
    )
    parser.add_argument(
        "--server-ip",
        default=DEFAULT_SERVER_IP,
        help=f"E2B API server IP address (default: {DEFAULT_SERVER_IP})"
    )
    parser.add_argument(
        "--harbor-ip",
        default=DEFAULT_HARBOR_IP,
        help=f"Harbor registry IP address (default: {DEFAULT_HARBOR_IP})"
    )
    parser.add_argument(
        "--alias",
        default="openclaw-chromium-v1",
        help="Template alias name"
    )
    parser.add_argument(
        "--cpu",
        type=int,
        default=2,
        help="CPU count for sandbox"
    )
    parser.add_argument(
        "--memory",
        type=int,
        default=4096,
        help="Memory in MB for sandbox"
    )
    parser.add_argument(
        "--image",
        default="e2b-orchestration/ubuntu-openclaw-chromium:custom",
        help="Image path in Harbor (default: e2b-orchestration/ubuntu-openclaw-chromium:custom)"
    )
    return parser.parse_args()


def load_e2b_config(config_path: str) -> tuple:
    """
    Load E2B configuration from config file.

    Args:
        config_path: Path to the E2B config JSON file

    Returns:
        Tuple of (access_token, team_api_key)

    Raises:
        FileNotFoundError: If config file doesn't exist
        KeyError: If required fields are missing
    """
    print(f"Reading config file: {config_path}")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    access_token = data.get("accessToken")
    team_api_key = data.get("teamApiKey")

    print("Extracted configuration:")
    print(f"  accessToken: {access_token}")
    print(f"  teamApiKey: {team_api_key}")

    if not access_token or not team_api_key:
        raise KeyError("Missing required fields: accessToken or teamApiKey")

    return access_token, team_api_key


def setup_environment(server_ip: str, access_token: str, team_api_key: str):
    """
    Set up E2B environment variables.

    Args:
        server_ip: E2B API server IP address
        access_token: E2B access token
        team_api_key: E2B team API key
    """
    os.environ["E2B_API_URL"] = f"http://{server_ip}:3000"
    os.environ["E2B_HTTP_SSL"] = "false"
    os.environ["E2B_ACCESS_TOKEN"] = access_token
    os.environ["E2B_API_KEY"] = team_api_key
    os.environ["E2B_DOMAIN"] = "e2b.app"

    print(f"E2B API URL: {os.environ['E2B_API_URL']}")


def build_template(harbor_ip: str, image: str, alias: str, cpu_count: int, memory_mb: int):
    """
    Build E2B template from Harbor image.

    Args:
        harbor_ip: Harbor registry IP address
        image: Image path in Harbor (project/image:tag)
        alias: Template alias name
        cpu_count: CPU count for the template
        memory_mb: Memory in MB for the template
    """
    print("Starting E2B template build...")
    print(f"  Harbor image: {harbor_ip}:30443/{image}")
    print(f"  Template alias: {alias}")
    print(f"  CPU count: {cpu_count}")
    print(f"  Memory: {memory_mb} MB")

    # Build template from Harbor image
    # Harbor uses nginx reverse proxy on port 443
    Template.build(
        Template().from_dockerfile(f'FROM harbor:443/{image}')
        .set_start_cmd(
            "sudo websocat -b --exit-on-eof ws-l:0.0.0.0:8081 tcp:127.0.0.1:22",
            wait_for_port(8081)
        ),
        alias=alias,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        on_build_logs=default_build_logger(),
        skip_cache=True
    )

    print("Template build completed!")


def create_sandbox(alias: str):
    """
    Create and test sandbox from template.

    Args:
        alias: Template alias name
    """
    print(f"Creating sandbox from template: {alias}")

    sbx = Sandbox.create(alias)

    print("Testing sandbox:")
    print(f"  whoami: {sbx.commands.run('whoami')}")  # Expected: guest
    print(f"  SSH ports: {sbx.commands.run('ss -tlnp | grep :22', user='root')}")
    print(f"  Sandbox ID: {sbx.sandbox_id}")

    return sbx


def main():
    """Main entry point."""
    args = parse_arguments()

    print("Configuration:")
    print(f"  SERVER_IP: {args.server_ip}")
    print(f"  HARBOR_IP: {args.harbor_ip}")

    config_path = "/root/.e2b/config.json"

    try:
        # Load configuration
        access_token, team_api_key = load_e2b_config(config_path)

        # Set up environment
        setup_environment(args.server_ip, access_token, team_api_key)

        # Build template
        build_template(args.harbor_ip, args.image, args.alias, args.cpu, args.memory)

        # Create sandbox
        sbx = create_sandbox(args.alias)

        print("\n=== Build completed successfully ===")
        print(f"Sandbox ID: {sbx.sandbox_id}")
        print(f"You can now use the template: {args.alias}")

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Please ensure the E2B config file exists at ~/.e2b/config.json")
        sys.exit(1)
    except KeyError as e:
        print(f"Error: {e}")
        print("Please check the config file contains accessToken and teamApiKey")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()