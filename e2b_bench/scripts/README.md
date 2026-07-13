# E2B Scripts

Utility scripts for E2B sandbox and template management in custom deployment environments.

## Files

| File | Description |
|------|-------------|
| `.env.example` | Example environment file (copy to `.env` and fill in values) |
| `debug_demo.py` | Interactive sandbox debugging tool for command testing |
| `e2b_template_manager.py` | Template management: list, delete by ID/alias, batch delete |
| `delete_sandbox.sh` | Bash script to delete all E2B sandboxes |

## Quick Start

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example ../.env
   ```

2. Edit `.env` with your actual credentials:
   ```bash
   E2B_API_URL=http://your-server-ip:3000
   E2B_API_KEY=your-api-key-here
   ```

3. Run any script:
   ```bash
   python e2b_template_manager.py --env-file ../.env --list-only
   ```

## Prerequisites

All scripts require an `.env` file with the following variables:

```bash
E2B_API_URL=http://192.168.110.10:3000    # Your E2B API endpoint
E2B_API_KEY=your-api-key-here             # Your E2B API key
E2B_ACCESS_TOKEN=your-access-token        # Optional, for Authorization header
```

## Usage

### debug_demo.py

Interactive sandbox debugging tool. Creates a sandbox and allows you to run custom commands in it.

```bash
# Using .env file
python debug_demo.py --env-file ../.env

# Using JSON config (legacy)
python debug_demo.py --config /root/.e2b/config.json --ip 192.168.110.10

# Specify template
python debug_demo.py --env-file ../.env --template openclaw-chromium-v1
```

**Interactive shortcuts:**
- `url` - Test default URL browser open
- `ports` - Check ports 18789/11436
- `ps` - Check openclaw processes
- `config` - Show openclaw config
- `help` - Show available shortcuts

Press `Ctrl+C` to kill sandbox and exit.

### e2b_template_manager.py

Template management with query-then-delete workflow.

```bash
# List all templates (query only)
python e2b_template_manager.py --env-file ../.env --list-only

# Interactive delete mode
python e2b_template_manager.py --env-file ../.env

# Delete all templates (no confirmation)
python e2b_template_manager.py --env-file ../.env --all -y

# Delete by alias
python e2b_template_manager.py --env-file ../.env --alias openclaw-chromium-v1 -y

# Delete by template ID
python e2b_template_manager.py --env-file ../.env --template-id abc123def456 -y
```

**Options:**
- `--env-file` - Path to .env file (same format as delete_sandbox.sh)
- `--config` - Path to JSON config file (legacy, used if --env-file not specified)
- `--ip` - Server IP for custom deployment (only with --config)
- `--template-id` - Delete specific template by ID
- `--alias` - Delete template by alias name
- `--all` - Delete all templates
- `-y, --yes` - Skip confirmation prompts
- `--list-only` - Query only, no deletion

### delete_sandbox.sh

Bash script to delete all sandboxes in one command.

```bash
# Default .env path
./delete_sandbox.sh

# Specify .env file
./delete_sandbox.sh ../.env

# Custom path
./delete_sandbox.sh /path/to/.env
```

**Requirements:**
- `curl` - HTTP client
- `jq` - JSON parser

## Environment File Format

All scripts share the same `.env` format:

```bash
# E2B Custom Deployment Configuration
E2B_API_URL=http://192.168.110.10:3000
E2B_API_KEY=your-team-api-key
E2B_ACCESS_TOKEN=your-access-token  # Optional
```

This format is compatible with both Python scripts and the Bash script, allowing centralized credential management.

## Legacy JSON Config Format

The `debug_demo.py` and `e2b_template_manager.py` also support the legacy JSON config format:

```json
{
  "accessToken": "your-access-token",
  "teamApiKey": "your-team-api-key"
}
```

Path: `/root/.e2b/config.json` (default)

## Notes

- For custom deployments (non-e2b.dev), ensure `E2B_API_URL` starts with `http://` (not `https://`)
- SSL is automatically disabled for `http://` URLs
- API keys are displayed truncated (first 20 chars) for security
- All Python scripts handle Ctrl+C gracefully and clean up resources
