#!/bin/bash

# Delete all E2B sandboxes
# Usage: ./delete_sandbox.sh [--env-file path/to/.env]

# Default env file path
ENV_FILE="${1:-.env}"

# E2B config file path (default: ~/.e2b/config.json)
E2B_CONFIG="${E2B_CONFIG:-$HOME/.e2b/config.json}"

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
    # Parse JSON to get api_key and token
    E2B_API_KEY=$(jq -r '.api_key // empty' "$E2B_CONFIG" 2>/dev/null)
    E2B_ACCESS_TOKEN=$(jq -r '.token // empty' "$E2B_CONFIG" 2>/dev/null)
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
    echo "Please ensure ~/.e2b/config.json contains 'api_key' field"
    exit 1
fi

echo "E2B API URL: $E2B_API_URL"
echo "E2B API Key: ${E2B_API_KEY:0:20}..."  # Show only first 20 chars for security
if [ -n "$E2B_ACCESS_TOKEN" ]; then
    echo "E2B Access Token: ${E2B_ACCESS_TOKEN:0:20}..."
fi

# Get all sandbox IDs (keep quotes, same as original working version)
echo "Fetching sandbox list..."
sandbox_id=$(curl --request GET \
    --url "${E2B_API_URL}/sandboxes" \
    --header "x-api-key: ${E2B_API_KEY}" \
    -s -k | jq '.[].sandboxID')

# Convert to array (same as original)
sandbox_ids=($sandbox_id)

if [ ${#sandbox_ids[@]} -eq 0 ]; then
    echo "No sandboxes found"
    exit 0
fi

echo "Found ${#sandbox_ids[@]} sandboxes"

# Delete each sandbox
for id in "${sandbox_ids[@]}"; do
    echo "Deleting: $id"
    # Remove surrounding quotes
    sd_id=$(echo "${id/#\"/}" | sed 's/"$//')
    curl --request DELETE \
        --url "${E2B_API_URL}/sandboxes/${sd_id}" \
        --header "x-api-key: ${E2B_API_KEY}" \
        -s -k
    echo ""
done

echo "Done! Deleted ${#sandbox_ids[@]} sandboxes"