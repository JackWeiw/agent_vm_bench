#!/bin/bash

# Delete E2B sandboxes.
#
# By default this script fetches every sandbox from the E2B API and deletes it.
# Pass --ids-file <path> to delete only the sandbox IDs listed in a text file
# (one ID per line). Blank lines and lines starting with '#' are ignored.
#
# Usage:
#   ./delete_sandbox.sh                                  # delete all sandboxes
#   ./delete_sandbox.sh --ids-file sandboxs.txt           # delete IDs listed in a file
#   ./delete_sandbox.sh --ids-file=sandboxs.txt           # same, = form
#   ./delete_sandbox.sh --env-file .env --ids-file sandboxs.txt
#
# The sandbox IDs file format matches the sandbox_ids_file used by the bench
# config: a plain text file with one sandbox ID per line.

# Defaults
ENV_FILE=".env"
IDS_FILE=""

# E2B config file path (default: ~/.e2b/config.json)
E2B_CONFIG="${E2B_CONFIG:-$HOME/.e2b/config.json}"

# Parse command-line arguments. A bare positional argument is still accepted
# as the env file path for backward compatibility with the original usage.
while [ $# -gt 0 ]; do
    case "$1" in
        --env-file=*)
            ENV_FILE="${1#--env-file=}"
            shift
            ;;
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        --ids-file=*)
            IDS_FILE="${1#--ids-file=}"
            shift
            ;;
        --ids-file)
            IDS_FILE="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '3,12p' "$0"
            exit 0
            ;;
        -*)
            echo "Error: Unknown option: $1"
            echo "Usage: $0 [--env-file path/to/.env] [--ids-file path/to/ids.txt]"
            exit 1
            ;;
        *)
            # Treat the first bare positional argument as the env file
            # to preserve the original `./delete_sandbox.sh .env` form.
            # Flags (--foo) are rejected above so they are never mistaken
            # for the env file.
            if [ -z "$ENV_FILE_SET" ]; then
                ENV_FILE="$1"
                ENV_FILE_SET=1
            fi
            shift
            ;;
    esac
done

# Load E2B_API_URL from .env file
if [ -f "$ENV_FILE" ]; then
    echo "Loading E2B_API_URL from $ENV_FILE"
    # Export variables from .env file
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "Warning: Environment file not found: $ENV_FILE (will use E2B_API_URL from environment if set)"
fi

# Load API key and token from ~/.e2b/config.json
if [ -f "$E2B_CONFIG" ]; then
    echo "Loading API key and token from $E2B_CONFIG"
    # Parse JSON to get teamApiKey and accessToken
    E2B_API_KEY=$(jq -r '.teamApiKey // empty' "$E2B_CONFIG" 2>/dev/null)
    E2B_ACCESS_TOKEN=$(jq -r '.accessToken // empty' "$E2B_CONFIG" 2>/dev/null)
else
    echo "Warning: E2B config file not found: $E2B_CONFIG"
fi

# Check required variables
if [ -z "$E2B_API_URL" ]; then
    echo "Error: E2B_API_URL must be set in $ENV_FILE or environment"
    exit 1
fi

if [ -z "$E2B_API_KEY" ]; then
    echo "Error: E2B_API_KEY not found in $E2B_CONFIG"
    echo "Please ensure ~/.e2b/config.json contains 'teamApiKey' field"
    exit 1
fi

echo "E2B API URL: $E2B_API_URL"
echo "E2B API Key: ${E2B_API_KEY:0:20}..."  # Show only first 20 chars for security
if [ -n "$E2B_ACCESS_TOKEN" ]; then
    echo "E2B Access Token: ${E2B_ACCESS_TOKEN:0:20}..."
fi

# Build the list of sandbox IDs to delete. When --ids-file is provided, read
# IDs from that file; otherwise fetch every sandbox from the API.
if [ -n "$IDS_FILE" ]; then
    if [ ! -f "$IDS_FILE" ]; then
        echo "Error: Sandbox IDs file not found: $IDS_FILE"
        exit 1
    fi

    echo "Reading sandbox IDs from $IDS_FILE"
    # Collect non-empty, non-comment lines into an array.
    sandbox_ids=()
    while IFS= read -r line || [ -n "$line" ]; do
        # Strip a leading hash and surrounding whitespace so commented IDs
        # are skipped, then skip blank lines.
        trimmed="${line#"${line%%[![:space:]]*}"}"   # trim leading whitespace
        trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"  # trim trailing whitespace
        case "$trimmed" in
            ''|\#*) continue ;;
        esac
        sandbox_ids+=("$trimmed")
    done < "$IDS_FILE"
else
    # Get all sandbox IDs (keep quotes, same as original working version)
    echo "Fetching sandbox list..."
    sandbox_id=$(curl --request GET \
        --url "${E2B_API_URL}/sandboxes" \
        --header "x-api-key: ${E2B_API_KEY}" \
        -s -k | jq '.[].sandboxID')

    # Convert to array (same as original)
    sandbox_ids=($sandbox_id)
fi

if [ ${#sandbox_ids[@]} -eq 0 ]; then
    echo "No sandboxes found"
    exit 0
fi

echo "Found ${#sandbox_ids[@]} sandboxes"

# Delete each sandbox
for id in "${sandbox_ids[@]}"; do
    echo "Deleting: $id"
    # Remove surrounding quotes (IDs fetched from the API are JSON-quoted;
    # file-based IDs are not, but this is a safe no-op for them).
    sd_id=$(echo "${id/#\"/}" | sed 's/"$//')
    curl --request DELETE \
        --url "${E2B_API_URL}/sandboxes/${sd_id}" \
        --header "x-api-key: ${E2B_API_KEY}" \
        -s -k
    echo ""
done

echo "Done! Deleted ${#sandbox_ids[@]} sandboxes"
