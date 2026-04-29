#!/bin/bash
set -e
# Stop the writers so the named volumes are quiesced during the snapshot.
docker compose stop hermes-agent hermes-dashboard hermes-webui || true
