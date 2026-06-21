#!/bin/bash

# Delete all E2B sandboxes
# Usage: ./delete_sandbox.sh [--env-file path/to/.env]

# Default env file path
ENV_FILE="${1:-.env}"

# Load environment variables
if [ -f "$ENV_FILE" ]; then
    echo "Loading environment from $ENV_FILE"
    # Export variables from .env file
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "Error: Environment file not found: $ENV_FILE"
    echo "Usage: ./delete_sandbox.sh [path/to/.env]"
    exit 1
fi

# Check required variables
if [ -z "$E2B_API_URL" ] || [ -z "$E2B_API_KEY" ]; then
    echo "Error: E2B_API_URL and E2B_API_KEY must be set in $ENV_FILE"
    exit 1
fi

echo "E2B API URL: $E2B_API_URL"
echo "E2B API Key: ${E2B_API_KEY:0:20}..."  # Show only first 20 chars for security

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