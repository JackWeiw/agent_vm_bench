#!/bin/bash
# Batch delete test containers created by docker_bench
# Usage: ./delete_containers.sh [prefix]

PREFIX="${1:-oc-bench}"

echo "Finding containers with prefix: $PREFIX"
CONTAINERS=$(docker ps -a --filter "name=$PREFIX" --format "{{.Names}}")

if [ -z "$CONTAINERS" ]; then
    echo "No containers found with prefix '$PREFIX'"
    exit 0
fi

COUNT=$(echo "$CONTAINERS" | wc -l)
echo "Found $COUNT containers:"
echo "$CONTAINERS"

echo ""
echo "Deleting all containers..."
for name in $CONTAINERS; do
    echo "Removing $name..."
    docker rm -f "$name"
done

echo ""
echo "Done. Deleted $COUNT containers."
