#!/usr/bin/env bash
set -o allexport; source .env; set +o allexport

# Wait for hermes-webui to become healthy
for i in $(seq 1 60); do
    status=$(docker-compose ps hermes-webui --format '{{.Status}}' 2>/dev/null || true)
    if echo "$status" | grep -q healthy; then
        break
    fi
    sleep 5
done
