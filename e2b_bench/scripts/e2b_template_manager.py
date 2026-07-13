#!/usr/bin/env python3
"""
E2B Template Manager - Custom Deployment Version

Supports custom E2B deployment environments with query-then-delete workflow.
Automatically configures necessary environment variables (E2B_API_URL, E2B_HTTP_SSL).
Supports deletion by templateID, alias, or all templates.
Uses the same .env file format as delete_sandbox.sh.
"""

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

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
    print("Session ended")
    print("=" * 60)
    sys.exit(0)


def load_env_file(env_path: str) -> dict:
    """
    Load environment variables from .env file.
    Supports the same format as delete_sandbox.sh.
    Returns dict with loaded variables.
    """
    env_file = Path(env_path)
    if not env_file.exists():
        print(f"Error: Environment file not found: {env_path}")
        print("Usage: python e2b_template_manager.py [path/to/.env] [options]")
        sys.exit(1)

    env_vars = {}
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            # Parse KEY=VALUE format
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Remove surrounding quotes if present
                if value.startswith('"') and value.endswith('"') or value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                env_vars[key] = value
                # Also set in os.environ for compatibility
                os.environ[key] = value

    return env_vars


def load_credentials(config_path: str) -> tuple:
    """
    Load E2B credentials from JSON config file.
    Returns (access_token, team_api_key).
    """
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    import json

    with open(config_file, encoding="utf-8") as f:
        data = json.load(f)

    access_token = data.get("accessToken")
    team_api_key = data.get("teamApiKey")

    if not access_token or not team_api_key:
        print("Error: accessToken or teamApiKey not found in config file!")
        sys.exit(1)

    return access_token, team_api_key


def setup_env_from_env_file(env_path: str) -> dict:
    """
    Setup environment from .env file (same format as delete_sandbox.sh).
    Returns headers and base_url for API calls.
    """
    env_vars = load_env_file(env_path)

    e2b_api_url = env_vars.get("E2B_API_URL")
    e2b_api_key = env_vars.get("E2B_API_KEY")
    e2b_access_token = env_vars.get("E2B_ACCESS_TOKEN", "")

    if not e2b_api_url:
        print("Error: E2B_API_URL must be set in .env file")
        sys.exit(1)

    if not e2b_api_key:
        print("Error: E2B_API_KEY must be set in .env file")
        sys.exit(1)

    # Set environment variables for E2B SDK compatibility
    os.environ["E2B_API_URL"] = e2b_api_url
    os.environ["E2B_API_KEY"] = e2b_api_key
    if e2b_access_token:
        os.environ["E2B_ACCESS_TOKEN"] = e2b_access_token

    # Determine if SSL should be used
    if e2b_api_url.startswith("http://"):
        os.environ["E2B_HTTP_SSL"] = "false"
    else:
        os.environ["E2B_HTTP_SSL"] = "true"

    headers = {"X-API-Key": e2b_api_key, "Content-Type": "application/json"}

    # Add Authorization header if access token is available
    if e2b_access_token:
        headers["Authorization"] = f"Bearer {e2b_access_token}"

    return {
        "base_url": e2b_api_url.rstrip("/"),
        "headers": headers,
        "access_token": e2b_access_token,
        "team_api_key": e2b_api_key,
    }


def setup_env_from_config(config_path: str, machine_ip: str = None) -> dict:
    """
    Setup environment from JSON config file (legacy method).
    Returns headers and base_url for API calls.
    """
    access_token, team_api_key = load_credentials(config_path)

    # For custom deployment, set required environment variables
    if machine_ip:
        os.environ["E2B_API_URL"] = f"http://{machine_ip}:3000"
        os.environ["E2B_HTTP_SSL"] = "false"

    # Authentication
    os.environ["E2B_ACCESS_TOKEN"] = access_token
    os.environ["E2B_API_KEY"] = team_api_key

    base_url = os.environ.get("E2B_API_URL", "https://api.e2b.dev")

    headers = {"X-API-Key": team_api_key, "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    return {
        "base_url": base_url.rstrip("/"),
        "headers": headers,
        "access_token": access_token,
        "team_api_key": team_api_key,
    }


def _extract_templates(data: Any) -> List[Dict]:
    """
    Extract template list from API response.
    Compatible with multiple response formats:
    {"templates": [...]}, {"data": [...]}, direct [...]
    """
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("templates", "data", "items", "results", "template_list"):
            if key in data and isinstance(data[key], list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v

    return []


def _get_template_id(t: Any) -> str:
    """Extract ID from template object, compatible with various field names"""
    if isinstance(t, dict):
        for key in ("templateID", "id", "template_id", "templateId", "_id"):
            if key in t and t[key]:
                return str(t[key])
    return str(t) if t else "N/A"


def _get_template_aliases(t: Any) -> List[str]:
    """Extract all aliases from template"""
    if isinstance(t, dict):
        aliases = t.get("aliases")
        if isinstance(aliases, list) and aliases:
            return [str(a) for a in aliases]
    return []


def _get_template_alias(t: Any) -> str:
    """Extract primary alias from template (prefer first in aliases array)"""
    aliases = _get_template_aliases(t)
    if aliases:
        return aliases[0]

    if isinstance(t, dict):
        names = t.get("names")
        if isinstance(names, list) and names:
            return str(names[0])
        for key in ("alias", "name", "templateName", "template_name", "displayName"):
            if key in t and t[key]:
                return str(t[key])
    return "unnamed"


def _get_template_status(t: Any) -> str:
    """Extract status from template object"""
    if isinstance(t, dict):
        for key in ("buildStatus", "status", "state", "phase"):
            if key in t and t[key]:
                return str(t[key])
    return "unknown"


def _find_template_by_alias(templates: List[Any], alias: str) -> Optional[Dict]:
    """Find template by alias, returns first matching template"""
    alias_lower = alias.lower()
    for t in templates:
        aliases = _get_template_aliases(t)
        if any(a.lower() == alias_lower for a in aliases):
            return t
        # Also try to match names field
        if isinstance(t, dict):
            names = t.get("names")
            if isinstance(names, list):
                if any(n.lower() == alias_lower or n.lower().endswith(f"/{alias_lower}") for n in names):
                    return t
    return None


def list_templates(env: dict) -> List[Dict]:
    """
    Query all created templates (query first).
    """
    url = f"{env['base_url']}/templates"

    print(f"\n[Query] GET {url}")

    try:
        resp = requests.get(url, headers=env["headers"], timeout=30)
        resp.raise_for_status()
        data = resp.json()

        import json

        raw_preview = json.dumps(data, ensure_ascii=False)[:500]
        print(f"  [Debug] Raw response: {raw_preview}...")

        templates = _extract_templates(data)
        return templates

    except requests.exceptions.ConnectionError as e:
        print(f"✗ Connection failed: {e}")
        print(f"  Please check if E2B_API_URL is correct. Current: {env['base_url']}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"✗ HTTP error: {e}")
        try:
            print(f"  Response: {resp.text[:500]}")
        except:
            pass
        sys.exit(1)
    except Exception as e:
        print(f"✗ Failed to query templates: {e}")
        sys.exit(1)


def delete_template(env: dict, template_id: str, force: bool = False) -> bool:
    """
    Delete specified template (then delete).
    """
    url = f"{env['base_url']}/templates/{template_id}"

    if not force:
        confirm = input(f"  Confirm delete template '{template_id}'? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("  Delete cancelled")
            return False

    print(f"\n[Delete] DELETE {url}")

    try:
        resp = requests.delete(url, headers=env["headers"], timeout=30)
        resp.raise_for_status()
        print(f"✓ Template '{template_id}' deleted successfully")
        return True
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 404:
            print(f"✗ Template '{template_id}' not found (404)")
        elif resp.status_code == 403:
            print(f"✗ Permission denied, cannot delete template '{template_id}' (403)")
        else:
            print(f"✗ Delete failed: {e}")
            try:
                print(f"  Response: {resp.text[:500]}")
            except:
                pass
        return False
    except Exception as e:
        print(f"✗ Delete exception: {e}")
        return False


def print_templates(templates: List[Any]):
    """Format and print template list, showing aliases and names"""
    if not templates:
        print("  (No templates)")
        return

    print(f"\n  {'No.':<4} {'Template ID':<26} {'Alias':<22} {'Names':<26} {'Status':<10}")
    print("  " + "-" * 94)

    for idx, t in enumerate(templates, 1):
        tid = _get_template_id(t)
        alias = _get_template_alias(t)

        # Display names field
        names_str = ""
        if isinstance(t, dict):
            names = t.get("names")
            if isinstance(names, list) and names:
                names_str = ", ".join(names)

        status = _get_template_status(t)
        print(f"  {idx:<4} {tid:<26} {alias:<22} {names_str:<26} {status:<10}")


def delete_all_templates(env: dict, templates: List[Any], force: bool = False):
    """Delete all templates"""
    if not templates:
        print("\n[!] No templates to delete")
        return 0

    if not force:
        confirm = input(f"\n  Confirm delete all {len(templates)} templates? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("  Cancelled")
            return 0

    print(f"\n[Delete All] Starting to delete {len(templates)} templates...")
    deleted = 0
    failed = 0

    for t in templates:
        tid = _get_template_id(t)
        alias = _get_template_alias(t)
        if tid and tid != "N/A":
            print(f"\n  [{deleted + failed + 1}/{len(templates)}] Deleting {alias} ({tid})...")
            if delete_template(env, tid, force=True):
                deleted += 1
            else:
                failed += 1

    print(f"\n✓ Deletion complete: {deleted} succeeded, {failed} failed")
    return deleted


def delete_by_alias(env: dict, templates: List[Any], alias: str, force: bool = False):
    """Delete template by alias"""
    target = _find_template_by_alias(templates, alias)

    if not target:
        print(f"\n✗ Template with alias '{alias}' not found")
        print("  Available aliases:")
        for t in templates:
            aliases = _get_template_aliases(t)
            if aliases:
                print(f"    - {', '.join(aliases)} (ID: {_get_template_id(t)})")
        return False

    tid = _get_template_id(target)
    actual_alias = _get_template_alias(target)
    print(f"\n[Found] alias '{alias}' -> Template ID: {tid}, Primary alias: {actual_alias}")

    return delete_template(env, tid, force=force)


def interactive_delete(env: dict, templates: List[Any]):
    """Interactive mode to select and delete templates"""
    if not templates:
        print("\n[!] No templates to delete")
        return

    print("\n" + "=" * 60)
    print("Interactive Delete Mode")
    print("=" * 60)
    print("  Enter template number to delete")
    print("  Enter template ID to delete")
    print("  Enter alias to delete (e.g., openclaw, openclaw-chromium-v1)")
    print("  Enter 'all' to delete all templates")
    print("  Enter 'q' or Ctrl+C to exit")
    print("=" * 60)

    while True:
        try:
            print("\n[Input] Select template to delete:")
            user_input = input(">>> ").strip()

            if not user_input or user_input.lower() in ("q", "quit", "exit"):
                print("  Exiting delete mode")
                break

            # Delete all
            if user_input.lower() == "all":
                delete_all_templates(env, templates, force=False)
                break

            # Delete by number
            if user_input.isdigit():
                idx = int(user_input) - 1
                if 0 <= idx < len(templates):
                    tid = _get_template_id(templates[idx])
                    delete_template(env, tid)
                else:
                    print(f"  ✗ Number {user_input} out of range (1-{len(templates)})")
                continue

            # Delete by templateID (usually length > 10 and alphanumeric)
            if len(user_input) > 10 and user_input.isalnum():
                delete_template(env, user_input)
                continue

            # Delete by alias
            delete_by_alias(env, templates, user_input)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  ✗ Error: {e}")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="E2B Template Manager - Custom Deployment Version (Query then Delete)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use .env file (same format as delete_sandbox.sh)
  python e2b_template_manager.py --env-file .env
  python e2b_template_manager.py --env-file /path/to/.env --list-only

  # Use JSON config file (legacy method)
  python e2b_template_manager.py --config /root/.e2b/config.json --ip 192.168.110.10

  # Non-interactive modes
  python e2b_template_manager.py --env-file .env --all -y
  python e2b_template_manager.py --env-file .env --alias openclaw-chromium-v1
  python e2b_template_manager.py --env-file .env --template-id abc123def456
        """,
    )

    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Path to .env file (same format as delete_sandbox.sh). If not specified, uses --config instead.",
    )

    parser.add_argument(
        "--config",
        type=str,
        default="/root/.e2b/config.json",
        help="E2B config file path (default: /root/.e2b/config.json). Used when --env-file is not specified.",
    )

    parser.add_argument(
        "--ip",
        type=str,
        default=None,
        help="E2B custom deployment server IP (e.g., 192.168.110.10). Only used with --config.",
    )

    parser.add_argument(
        "--template-id", type=str, default=None, help="Specify template ID to delete (non-interactive mode)"
    )

    parser.add_argument("--alias", type=str, default=None, help="Delete template by alias (e.g., openclaw-chromium-v1)")

    parser.add_argument("--all", action="store_true", dest="delete_all", help="Delete all templates")

    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation, delete directly (use with --template-id / --alias / --all)",
    )

    parser.add_argument("--list-only", action="store_true", help="Only query list, do not enter delete mode")

    args = parser.parse_args()

    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)

    # Setup environment
    print("=" * 60)
    print("E2B Template Manager (Custom Deploy)")
    print("=" * 60)

    # Prefer --env-file over --config
    if args.env_file:
        env = setup_env_from_env_file(args.env_file)
        print(f"  Env file: {args.env_file}")
    else:
        env = setup_env_from_config(args.config, args.ip)
        print(f"  Config path: {args.config}")

    print(f"  E2B API URL: {env['base_url']}")
    print(f"  HTTP SSL: {os.environ.get('E2B_HTTP_SSL', 'default')}")
    # Only show first 20 chars of API key for security
    api_key = env["team_api_key"]
    print(f"  E2B API Key: {api_key[:20]}...")

    # Step 1: Query all templates
    print("\n[Step 1] Querying template list...")
    templates = list_templates(env)
    print_templates(templates)

    if args.list_only:
        print("\n[List-only] Query-only mode, exiting")
        sys.exit(0)

    # Step 2: Delete mode
    print("\n[Step 2] Entering delete mode...")

    # Non-interactive mode priority: --all > --alias > --template-id
    if args.delete_all:
        delete_all_templates(env, templates, force=args.yes)
    elif args.alias:
        delete_by_alias(env, templates, args.alias, force=args.yes)
    elif args.template_id:
        delete_template(env, args.template_id, force=args.yes)
    else:
        # Interactive mode
        interactive_delete(env, templates)

    print("\n" + "=" * 60)
    print("Done")
    print("=" * 60)


if __name__ == "__main__":
    main()
