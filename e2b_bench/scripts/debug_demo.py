#!/usr/bin/env python3
"""
E2B Sandbox Debug Demo

An interactive script to debug command execution in E2B sandbox.
Allows continuous custom command testing until Ctrl+C is received.
"""

import argparse
import json
import os
import signal
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from e2b import Sandbox
except ImportError:
    print("Error: e2b package not installed. Run: pip install e2b")
    sys.exit(1)

# Global sandbox reference for cleanup
_sandbox = None


def signal_handler(*_args):
    """Handle Ctrl+C signal to cleanup sandbox"""
    print("\n\n[Signal] Ctrl+C received, cleaning up...")
    if _sandbox:
        try:
            _sandbox.kill()
            print("✓ Sandbox killed successfully")
        except Exception as e:
            print(f"✗ Kill error: {e}")
    print("\n" + "=" * 60)
    print("Debug session ended")
    print("=" * 60)
    sys.exit(0)


def load_credentials(config_path: str) -> tuple:
    """Load E2B credentials from config file"""
    if not os.path.exists(config_path):
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    access_token = data.get("accessToken")
    team_api_key = data.get("teamApiKey")

    if not access_token or not team_api_key:
        print("Error: accessToken or teamApiKey not found in config file!")
        sys.exit(1)

    return access_token, team_api_key


def interactive_command_loop(sbx, default_url: str = "http://192.168.110.10:8080/Weibo.html"):
    """Interactive command loop - user can input custom commands until Ctrl+C"""
    print("\n" + "=" * 60)
    print("Interactive Command Mode")
    print("=" * 60)
    print(f"  Default test URL: {default_url}")
    print("  Type commands to execute in sandbox")
    print("  Press Ctrl+C to kill sandbox and exit")
    print("=" * 60)

    while True:
        try:
            print("\n[Input] Enter command (or press Ctrl+C to exit):")
            user_input = input(">>> ").strip()

            if not user_input:
                print("  (Empty input, showing help)")
                print("  Available shortcuts:")
                print("    'url'   - Test default URL browser open")
                print("    'ports' - Check ports 18789/11436")
                print("    'ps'    - Check openclaw processes")
                print("    'config'- Show openclaw config")
                print("    'help'  - Show this help")
                continue

            # Handle shortcuts
            if user_input == "url":
                user_input = f"openclaw browser --browser-profile openclaw open '{default_url}'"
                print(f"  Executing shortcut: {user_input}")
            elif user_input == "ports":
                user_input = "ss -tlnp | grep -E ':18789|:11436'"
                print(f"  Executing shortcut: {user_input}")
            elif user_input == "ps":
                user_input = "ps aux | grep openclaw | grep -v grep"
                print(f"  Executing shortcut: {user_input}")
            elif user_input == "config":
                user_input = "cat ~/.openclaw/openclaw.json"
                print(f"  Executing shortcut: {user_input}")
            elif user_input == "help":
                print("  Available shortcuts:")
                print("    'url'   - Test default URL browser open")
                print("    'ports' - Check ports 18789/11436")
                print("    'ps'    - Check openclaw processes")
                print("    'config'- Show openclaw config")
                print("    'help'  - Show this help")
                print("  Or type any shell command to execute")
                continue

            # Execute command
            print(f"\n  Command: {user_input}")
            print("  " + "-" * 50)

            try:
                result = sbx.commands.run(user_input, timeout=120, user="root")
                print(f"  Exit code: {result.exit_code}")

                if result.stdout:
                    # Print full stdout, truncate if very long
                    stdout = result.stdout
                    if len(stdout) > 2000:
                        print("  stdout (truncated):")
                        print(stdout[:2000])
                        print(f"  ... ({len(stdout) - 2000} more chars)")
                    else:
                        print("  stdout:")
                        print(stdout)
                else:
                    print("  stdout: (empty)")

                if result.stderr:
                    stderr = result.stderr
                    if len(stderr) > 1000:
                        print("  stderr (truncated):")
                        print(stderr[:1000])
                        print(f"  ... ({len(stderr) - 1000} more chars)")
                    else:
                        print("  stderr:")
                        print(stderr)
                else:
                    print("  stderr: (empty)")

            except Exception as e:
                print(f"  ✗ Error: {str(e)}")

        except KeyboardInterrupt:
            # This will be caught by signal handler
            raise


def debug_sandbox_commands(config_path: str, machine_ip: str = None, template: str = None):
    """Debug various commands in E2B sandbox with interactive mode"""

    # Load credentials
    access_token, team_api_key = load_credentials(config_path)

    # Setup environment
    if machine_ip:
        os.environ["E2B_API_URL"] = f"http://{machine_ip}:3000"
        os.environ["E2B_HTTP_SSL"] = "false"

    os.environ["E2B_ACCESS_TOKEN"] = access_token
    os.environ["E2B_API_KEY"] = team_api_key

    print("=" * 60)
    print("E2B Sandbox Debug Demo")
    print("=" * 60)
    print(f"  Config path: {config_path}")
    print(f"  E2B API URL: {os.environ.get('E2B_API_URL', 'default')}")
    print(f"  Template: {template or 'default'}")

    # 1. Create sandbox
    print("\n[Step 1] Creating sandbox...")
    template_name = template or os.environ.get("E2B_TEMPLATE", "openclaw-chromium-v1")

    try:
        sbx = Sandbox.create(template_name, timeout=86400)
        _sandbox = sbx  # Set global reference for cleanup
        print("✓ Sandbox created successfully")
        print(f"  Sandbox ID: {sbx.sandbox_id if hasattr(sbx, 'sandbox_id') else 'N/A'}")
    except Exception as e:
        print(f"✗ Failed to create sandbox: {e}")
        return

    # 2. Quick port check
    print("\n[Step 2] Checking required ports...")
    port_commands = [
        ("ss -tlnp | grep ':18789' || echo 'Port 18789 NOT listening'", "Port 18789 (openclaw-gateway)"),
        ("ss -tlnp | grep ':11436' || echo 'Port 11436 NOT listening'", "Port 11436 (llama-server)"),
    ]

    for cmd, desc in port_commands:
        print(f"  {desc}:")
        try:
            result = sbx.commands.run(cmd, timeout=10, user="root")
            if "NOT listening" in result.stdout:
                print(f"    ✗ {result.stdout.strip()}")
            else:
                print("    ✓ Listening")
        except Exception as e:
            print(f"    ✗ Error: {str(e)[:50]}")

    # 3. Interactive mode
    print("\n[Step 3] Entering interactive mode...")
    interactive_command_loop(sbx)


def main():
    """Main entry point with CLI argument parsing"""
    parser = argparse.ArgumentParser(description="E2B Sandbox Debug Demo - Interactive command testing tool")

    parser.add_argument(
        "--config",
        type=str,
        default="/root/.e2b/config.json",
        help="Path to E2B config JSON file (default: /root/.e2b/config.json)",
    )

    parser.add_argument("--ip", type=str, default=None, help="Machine IP address for E2B API URL (e.g., 90.91.159.195)")

    parser.add_argument("--template", type=str, default=None, help="E2B template name (default: openclaw-chromium-v1)")

    parser.add_argument(
        "--url", type=str, default="http://192.168.110.10:8080/Weibo.html", help="Default test URL for browser commands"
    )

    args = parser.parse_args()

    # Register signal handler for Ctrl+C
    signal.signal(signal.SIGINT, signal_handler)

    # Run debug
    try:
        debug_sandbox_commands(config_path=args.config, machine_ip=args.ip, template=args.template)
    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        if _sandbox:
            try:
                _sandbox.kill()
            except:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
